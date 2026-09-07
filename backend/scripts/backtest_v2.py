#!/usr/bin/env python3
"""Backtest v2: REAL PaperBotRuntime._process_candle() with BacktestBroker.
No code duplication — same bot, just different I/O.
"""
import asyncio
import os
import sys
import time
import bisect
import json as _json
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

MSK = ZoneInfo("Europe/Moscow")
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

from sqlalchemy import text, select
from app.database import SessionLocal
from app.engine.costs import CostModel
from app.engine.models import Side
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
from app.bot.runtime import BotConfig, PaperBotRuntime, _sessions_allowed
from app.services.signals import _load_candles as _lc

import datetime as _dt_mod

class _FakeDatetime(_dt_mod.datetime):
    _bt_now = None
    @classmethod
    def now(cls, tz=None):
        if cls._bt_now is None:
            return _dt_mod.datetime.now(tz)
        if tz is not None:
            return cls._bt_now.astimezone(tz)
        return cls._bt_now.replace(tzinfo=None)

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO   = datetime(2026, 9, 1, tzinfo=timezone.utc)
WARMUP_DAYS = 4
INITIAL_CASH = 10_000.0
SIGNAL_WINDOW = 2000

CFG = BotConfig(
    strategy_id="ensemble_v4", interval_name="1min", top_n=20,
    qty_per_trade=1, sl_mode="atr", atr_period=14,
    atr_multiplier=4.0, atr_risk_reward=4.0,
    initial_cash=INITIAL_CASH, daily_loss_limit=5000.0,
    use_ensemble=True, ensemble_capital=INITIAL_CASH / 20,
    ensemble_quorum=2, ensemble_session="main",
    sessions=["morning", "day", "evening"],
    leverage=1.0, commission_rate=0.003, slippage_bps=2.0,
    confirm_flip=2, reentry_cooldown_bars=15, overnight=False,
    use_margin=True, max_margin_pct=80.0,
    long_allowed=True, short_allowed=True,
)

bt_log = open(os.path.join(LOG_DIR, "backtest_v2.log"), "w", encoding="utf-8")


class BacktestBroker:
    """Drop-in replacement for PaperBroker. Same interface, in-memory."""
    def __init__(self, initial_cash, cost_model, margin_data, config):
        self.initial_cash = initial_cash
        self._cash = initial_cash
        self.costs = cost_model
        self.config = config
        self.positions_data = {}
        self.trades = []
        self.margin_data = margin_data

    async def ensure_account(self, initial_cash=100_000.0): pass

    async def cash(self):
        return self._cash

    async def positions(self):
        result = []
        for figi, p in self.positions_data.items():
            result.append(type('Pos', (), {
                'figi': figi, 'ticker': p['ticker'], 'side': p['side'],
                'qty': p['qty'], 'entry_price': p['entry_price'],
                'entry_time': p['entry_time'],
                'stop_loss': p['stop_loss'], 'take_profit': p['take_profit'],
                'strategy_id': p.get('strategy_id', 'ensemble_v4'),
            })())
        return result

    async def get_position(self, figi):
        p = self.positions_data.get(figi)
        if p is None:
            return None
        return type('Pos', (), {
            'figi': figi, 'ticker': p['ticker'], 'side': p['side'],
            'qty': p['qty'], 'entry_price': p['entry_price'],
            'entry_time': p['entry_time'],
            'stop_loss': p['stop_loss'], 'take_profit': p['take_profit'],
            'strategy_id': p.get('strategy_id', 'ensemble_v4'),
        })()

    async def open_position(self, figi, ticker, side, qty, price, stop_loss, take_profit, strategy_id):
        if figi in self.positions_data:
            return None
        slip_side = Side.BUY if side == "BUY" else Side.SELL
        fill = self.costs.fill_price(price, slip_side)
        entry_commission = self.costs.commission(fill * qty)
        lev = max(1.0, float(self.config.leverage or 1.0))
        own_funds = fill * qty / lev

        md = self.margin_data.get(figi, {})
        max_lots = md.get("buy_max_lots", 9999) if side == "BUY" else md.get("sell_max_lots", 9999)
        if max_lots and max_lots > 0:
            pct = self.config.max_margin_pct / 100.0
            max_capped = int(max_lots * pct)
            if qty > max_capped:
                qty = max_capped
                own_funds = fill * qty / lev

        total_cost = own_funds + entry_commission
        if total_cost > self._cash:
            available = self._cash - entry_commission
            if available <= 0: return None
            qty = max(1, int(available * lev / fill))
            own_funds = fill * qty / lev
            total_cost = own_funds + entry_commission
            if total_cost > self._cash: return None

        self._cash -= total_cost
        norm_side = "LONG" if side == "BUY" else "SHORT"
        self.positions_data[figi] = {
            'ticker': ticker, 'side': norm_side, 'qty': qty,
            'entry_price': fill, 'entry_time': datetime.now(timezone.utc),
            'stop_loss': stop_loss, 'take_profit': take_profit,
            'strategy_id': strategy_id, 'margin_frozen': own_funds,
        }
        return fill

    async def close_position(self, figi, price, reason):
        p = self.positions_data.pop(figi, None)
        if p is None: return None
        exit_side = Side.BUY if p['side'] == "SHORT" else Side.SELL
        actual_exit = self.costs.fill_price(price, exit_side)
        direction_mult = 1 if p['side'] == "LONG" else -1
        gross = (actual_exit - p['entry_price']) * p['qty'] * direction_mult
        exit_commission = self.costs.commission(actual_exit * p['qty'])
        entry_commission = self.costs.commission(p['entry_price'] * p['qty'])
        total_comm = entry_commission + exit_commission
        slippage_cost = abs(actual_exit - price) * p['qty']
        net = gross - total_comm - slippage_cost
        self._cash += p['margin_frozen'] + net
        trade = type('Trade', (), {
            'figi': figi, 'ticker': p['ticker'], 'side': p['side'],
            'qty': p['qty'], 'entry_price': p['entry_price'],
            'entry_time': p['entry_time'], 'exit_price': actual_exit,
            'exit_time': datetime.now(timezone.utc), 'exit_reason': reason,
            'gross_pnl': gross, 'commission': total_comm,
            'slippage_cost': slippage_cost, 'net_pnl': net,
        })()
        self.trades.append(trade)
        return trade

    async def update_protective_levels(self, figi, stop, target):
        p = self.positions_data.get(figi)
        if p is not None:
            p['stop_loss'] = stop
            p['take_profit'] = target


class BacktestBotRuntime(PaperBotRuntime):
    """Bot runtime that uses BacktestBroker and logs trades to file."""

    def __init__(self, broker, cfg):
        super().__init__()
        self.broker = broker
        self.config = cfg
        self.broker_mode = "paper"
        self._bt_trades = []
        self.tcs_to_bbg = {}
        self.bbg_to_tcs = {}

    async def init_tcs_mapping(self):
        from app.models.instrument import Instrument
        async with SessionLocal() as db:
            rows = await db.execute(select(Instrument.ticker, Instrument.figi))
            figi_by_ticker = dict(rows.all())
            ii_rows = await db.execute(text("SELECT figi, ticker FROM instrument_info"))
            for ii_figi, ii_ticker in ii_rows.all():
                bb = figi_by_ticker.get(ii_ticker)
                if bb and ii_figi:
                    self.bbg_to_tcs[bb] = ii_figi
                    self.tcs_to_bbg[ii_figi] = bb

    async def _st_open(self, figi, ticker, side, qty, price, sl, tp, meta=None, leverage=1.0):
        line = "OPEN %s %s %s qty=%d @ %.2f sl=%.2f tp=%.2f" % (
            figi[-6:], ticker, side, qty, price,
            sl if sl else 0, tp if tp else 0)
        bt_log.write(line + "\n")
        if meta:
            qe = meta.get("quorum_event", {})
            members = qe.get("members_for", [])
            oppose = qe.get("opposition", [])
            bt_log.write("  QUORUM votes=%d/%d members_for=[%s] opposition=[%s]\n" % (
                qe.get("votes", 0), qe.get("quorum_k", 0),
                "+".join(members), "+".join(oppose)))
            setups = meta.get("setups", {})
            for sid, sv in setups.items():
                if isinstance(sv, dict):
                    bt_log.write("  SETUP %s BUY=%d SELL=%d total=%d\n" % (
                        sid, sv.get("BUY", 0), sv.get("SELL", 0), sv.get("signals", 0)))
        bt_log.flush()
        print("  " + line, flush=True)

    async def _st_close(self, figi, exit_price, reason="", net=None, meta=None):
        line = "CLOSE %s @ %.2f reason=%s net=%+.2f" % (
            figi[-6:], exit_price, reason, net if net else 0)
        bt_log.write(line + "\n")
        if meta:
            bh = meta.get("bars_held", "?")
            sl = meta.get("sl")
            tp = meta.get("tp")
            note = meta.get("signal_note", "")
            bt_log.write("  EXIT_META bars_held=%s sl=%s tp=%s signal_note=%s\n" % (
                bh, sl if sl else "—", tp if tp else "—", note or "—"))
        bt_log.flush()
        print("  " + line, flush=True)


async def load_margin_data():
    margin = {}
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT ii.ticker, ii.buy_max_lots, ii.sell_max_lots, "
            "ii.margin_leverage, i.long_lev, i.short_lev, u.figi as u_figi, u.lot_size "
            "FROM instrument_info ii "
            "LEFT JOIN instruments i ON ii.figi = i.figi "
            "LEFT JOIN universe u ON ii.ticker = u.ticker AND u.eligible_tier = 'eligible' "
            "WHERE u.figi IS NOT NULL"
        ))).all()
        for r in rows:
            margin[r.u_figi] = {
                "buy_max_lots": r.buy_max_lots or 0,
                "sell_max_lots": r.sell_max_lots or 0,
                "margin_leverage": float(r.margin_leverage or 1.0),
                "long_lev": float(r.long_lev or 1.0),
                "short_lev": float(r.short_lev or 1.0),
                "lot": int(r.lot_size or 1),
            }
    return margin


def fmt_ts(dt):
    return dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M")


async def run_backtest():
    t0 = time.time()
    print("=" * 80)
    print("BACKTEST v2 — REAL PaperBotRuntime._process_candle()")
    print("Period: %s .. %s" % (DATE_FROM.date(), DATE_TO.date()))
    print("Capital: %s RUB" % INITIAL_CASH)
    print("=" * 80)

    margin_data = await load_margin_data()
    print("Margin: %d tickers" % len(margin_data), flush=True)

    broker = BacktestBroker(
        initial_cash=INITIAL_CASH,
        cost_model=CostModel(commission_rate=CFG.commission_rate, slippage_bps=CFG.slippage_bps),
        margin_data=margin_data, config=CFG,
    )

    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT figi, ticker, lot_size FROM universe "
            "WHERE eligible_tier = 'eligible' ORDER BY ticker"
        ))).all()
    eligible = [(r[0], r[1], r[2]) for r in rows]
    print("Eligible: %d tickers" % len(eligible), flush=True)

    tickers_map = {}
    all_candles = {}
    warmup_from = DATE_FROM - timedelta(days=WARMUP_DAYS)

    for i, (figi, ticker, lot) in enumerate(eligible):
        tickers_map[figi] = ticker
        print("  [%d/%d] %s..." % (i + 1, len(eligible), ticker), end="", flush=True)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warmup_from, date_to=DATE_TO)
        if not candles:
            print(" NO DATA", flush=True)
            continue
        print(" %d candles" % len(candles), flush=True)
        all_candles[figi] = candles

    print("\nInit REAL BotRuntime...", flush=True)
    runtime = BacktestBotRuntime(broker=broker, cfg=CFG)
    await runtime.init_tcs_mapping()
    print("tcs_to_bbg: %d mappings" % len(runtime.tcs_to_bbg), flush=True)

    for figi, ticker, lot in eligible:
        if figi not in all_candles:
            continue
        lot_int = int(lot) if lot else 10
        runtime.strategies[figi] = EnsembleV4Strategy(EnsembleParams(
            figi=figi, lot=lot_int, capital=CFG.ensemble_capital,
            quorum=CFG.ensemble_quorum, session="all", sessions=CFG.sessions,
        ))
        runtime.tickers[figi] = ticker
        runtime.buffers[figi] = deque(maxlen=SIGNAL_WINDOW)

    print("Strategies: %d" % len(runtime.strategies), flush=True)

    class CandleWithFigi:
        __slots__ = ('ts', 'open', 'high', 'low', 'close', 'volume', 'figi')
        def __init__(self, c, figi):
            self.ts = c.ts
            self.open = c.open
            self.high = c.high
            self.low = c.low
            self.close = c.close
            self.volume = c.volume
            self.figi = figi

    merged = []
    for figi, clist in all_candles.items():
        for c in clist:
            if c.ts >= DATE_FROM and c.ts < DATE_TO:
                merged.append((c.ts, figi, CandleWithFigi(c, figi)))
    merged.sort(key=lambda x: x[0])
    print("Merged candles: %d" % len(merged), flush=True)
    print("=" * 80, flush=True)

    bar_counter = 0
    errors = 0
    import app.bot.runtime as _rt_mod
    _orig_dt = _rt_mod.datetime
    _rt_mod.datetime = _FakeDatetime
    _debug_count = 0
    for ts, figi, candle in merged:
        bar_counter += 1
        if bar_counter % 50000 == 0:
            elapsed = time.time() - t0
            speed = bar_counter / elapsed if elapsed > 0 else 0
            print("[%d/%d] ts=%s cash=%.2f pos=%d trades=%d errs=%d %.0fs (%.0f bars/s)" % (
                bar_counter, len(merged), fmt_ts(ts), broker._cash,
                len(broker.positions_data), len(broker.trades), errors, elapsed, speed), flush=True)

        try:
            _FakeDatetime._bt_now = ts
            await runtime._process_candle(candle)
        except Exception as e:
            errors += 1
            if errors <= 10:
                print("ERROR at %s: %s" % (fmt_ts(ts), str(e)[:200]), flush=True)
            if errors == 11:
                print("... suppressing further errors ...", flush=True)
    _rt_mod.datetime = _orig_dt

    print("\nForce closing...", flush=True)
    for figi in list(broker.positions_data.keys()):
        p = broker.positions_data.get(figi)
        if p:
            clist = all_candles.get(figi, [])
            last_price = float(clist[-1].close) if clist else p['entry_price']
            await broker.close_position(figi, last_price, "backtest_end")

    elapsed = time.time() - t0
    trades = broker.trades
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    total_net = sum(t.net_pnl for t in trades)
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / len(trades) * 100 if trades else 0

    report = []
    report.append("=" * 80)
    report.append("RESULTS — BACKTEST v2 (REAL BotRuntime)")
    report.append("=" * 80)
    report.append("  Trades:       %d (wins=%d losses=%d)" % (len(trades), len(wins), len(losses)))
    report.append("  Net PnL:      %+.2f RUB" % total_net)
    report.append("  Final cash:   %s RUB" % format(broker._cash, ",.2f"))
    report.append("  Return:       %+.1f%%" % (total_net / INITIAL_CASH * 100))
    report.append("  Win rate:     %.1f%%" % wr)
    report.append("  Profit factor:%.2f" % pf)
    report.append("  Gross win:    %+.2f RUB" % gross_win)
    report.append("  Gross loss:   %+.2f RUB" % (-gross_loss))
    report.append("  Total comm:   %.2f RUB" % sum(t.commission for t in trades))
    report.append("  Total slip:   %.2f RUB" % sum(t.slippage_cost for t in trades))
    report.append("  Errors:       %d" % errors)
    report.append("  Elapsed:      %.1fs" % elapsed)
    report.append("")
    report.append("  Per-ticker:")
    by_ticker = {}
    for t in trades:
        by_ticker.setdefault(t.ticker, []).append(t)
    for ticker in sorted(by_ticker.keys()):
        ts = by_ticker[ticker]
        tw = [t for t in ts if t.net_pnl > 0]
        twr = len(tw) / len(ts) * 100 if ts else 0
        tn = sum(t.net_pnl for t in ts)
        report.append("    %6s: %4d trades WR=%.0f%% net=%+8.2f RUB avg=%+.2f" % (
            ticker, len(ts), twr, tn, tn / len(ts)))
    report.append("=" * 80)

    for line in report:
        bt_log.write(line + "\n")
        print(line, flush=True)

    bt_log.close()


if __name__ == "__main__":
    asyncio.run(run_backtest())
