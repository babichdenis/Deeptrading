#!/usr/bin/env python3
"""Backtest: full bot engine on historical data (August 2026).
Uses the same logic as runtime._process_candle() with BacktestBroker.
Optimized: cursor-based buffer management to avoid O(N) per-bar filtering.
"""
import asyncio
import os
import sys
import time
import logging
import bisect
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MSK = ZoneInfo("Europe/Moscow")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

bt_logger = logging.getLogger("backtest")
bt_logger.setLevel(logging.DEBUG)
fh = logging.FileHandler(os.path.join(LOG_DIR, "backtest_august.log"), mode="w", encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
bt_logger.addHandler(fh)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(message)s"))
bt_logger.addHandler(ch)

print("Starting backtest...", flush=True)

from sqlalchemy import text
from app.database import SessionLocal
from app.engine.costs import CostModel
from app.engine.exits import AtrStopPolicy, intrabar_exit
from app.engine.models import Candle as EngineCandle, PositionState, Side, DecisionAction
from app.engine.policies import SignalPolicy
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
from app.bot.runtime import BotConfig, BotOrder, _sessions_allowed
from app.services.signals import _load_candles as _lc

print("Imports OK", flush=True)

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO   = datetime(2026, 9, 1, tzinfo=timezone.utc)
WARMUP_DAYS = 60
INITIAL_CASH = 10_000.0
SIGNAL_WINDOW = 2000

CFG = BotConfig(
    strategy_id="ensemble_v4", interval_name="1min", top_n=20,
    qty_per_trade=1, sl_mode="atr", atr_period=14,
    atr_multiplier=4.0, atr_risk_reward=4.0,
    initial_cash=INITIAL_CASH, daily_loss_limit=5000.0,
    use_ensemble=True, ensemble_capital=INITIAL_CASH,
    ensemble_quorum=2, ensemble_session="main",
    sessions=["morning", "day", "evening"],
    leverage=1.0, commission_rate=0.0005, slippage_bps=2.0,
    confirm_flip=2, reentry_cooldown_bars=15, overnight=False,
    use_margin=False, max_margin_pct=80.0,
    long_allowed=True, short_allowed=True,
)
POSITION_PCT = 0.20  # бюджет на позицию = 20% текущего портфеля (equity)
ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]
V2P = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}
V2_BASE_P = {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0,
             "active_sids": ALL_SIDS, "strategy_params": V2P}


@dataclass
class BTPosition:
    figi: str; ticker: str; side: str; qty: int; entry_price: float
    entry_time: datetime; stop_loss: float | None = None; take_profit: float | None = None
    strategy_id: str = "ensemble_v4"; margin_frozen: float = 0.0


@dataclass
class BTTrade:
    figi: str; ticker: str; side: str; qty: int; entry_price: float
    entry_time: datetime; exit_price: float; exit_time: datetime
    exit_reason: str; gross_pnl: float; commission: float
    slippage_cost: float; net_pnl: float; strategy_id: str = "ensemble_v4"


class BacktestBroker:
    def __init__(self, initial_cash, cost_model, margin_data, config):
        self.initial_cash = initial_cash
        self.cash_val = initial_cash
        self.costs = cost_model
        self.config = config
        self.positions = {}
        self.trades = []
        self.margin_data = margin_data
        self._total_frozen = 0.0

    async def ensure_account(self, initial_cash=100_000.0): pass
    async def cash(self): return self.cash_val
    async def get_position(self, figi): return self.positions.get(figi)

    async def open_position(self, figi, ticker, side, qty, price, stop_loss, take_profit, strategy_id, ts=None):
        if figi in self.positions: return None
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
        if total_cost > self.cash_val:
            available = self.cash_val - entry_commission
            if available <= 0: return None
            qty = max(1, int(available * lev / fill))
            own_funds = fill * qty / lev
            total_cost = own_funds + entry_commission
            if total_cost > self.cash_val: return None

        self.cash_val -= total_cost
        self._total_frozen += own_funds
        self.positions[figi] = BTPosition(
            figi=figi, ticker=ticker, side=side, qty=qty,
            entry_price=fill, entry_time=ts or datetime.now(timezone.utc),
            stop_loss=stop_loss, take_profit=take_profit,
            strategy_id=strategy_id, margin_frozen=own_funds,
        )
        bt_logger.debug("OPEN %s %s qty=%d @ %.2f cash=%.2f" % (side, ticker, qty, fill, self.cash_val))
        return fill

    async def close_position(self, figi, price, reason, ts=None):
        pos = self.positions.pop(figi, None)
        if pos is None: return None
        exit_side = Side.BUY if pos.side == "SELL" else Side.SELL
        actual_exit = self.costs.fill_price(price, exit_side)
        direction_mult = 1 if pos.side == "BUY" else -1
        gross = (actual_exit - pos.entry_price) * pos.qty * direction_mult
        exit_commission = self.costs.commission(actual_exit * pos.qty)
        entry_commission = self.costs.commission(pos.entry_price * pos.qty)
        total_comm = entry_commission + exit_commission
        slippage_cost = abs(actual_exit - price) * pos.qty
        net = gross - total_comm - slippage_cost
        self.cash_val += pos.margin_frozen + net
        self._total_frozen -= pos.margin_frozen
        trade = BTTrade(
            figi=figi, ticker=pos.ticker, side=pos.side, qty=pos.qty,
            entry_price=pos.entry_price, entry_time=pos.entry_time,
            exit_price=actual_exit, exit_time=ts or datetime.now(timezone.utc),
            exit_reason=reason,
            gross_pnl=gross, commission=total_comm, slippage_cost=slippage_cost,
            net_pnl=net, strategy_id=pos.strategy_id,
        )
        self.trades.append(trade)
        bt_logger.debug("CLOSE %s %s @ %.2f reason=%s net=%+.2f" % (pos.side, pos.ticker, actual_exit, reason, net))
        return trade

    async def update_protective_levels(self, figi, stop, target):
        pos = self.positions.get(figi)
        if pos is not None:
            pos.stop_loss = stop
            pos.take_profit = target


async def load_margin_data():
    margin = {}
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT ii.ticker, ii.figi as ii_figi, ii.buy_max_lots, ii.sell_max_lots, "
            "ii.margin_leverage, ii.buy_money_amount, "
            "i.long_lev, i.short_lev, u.figi as u_figi, u.lot_size "
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
                "buy_money": float(r.buy_money_amount or 0),
                "long_lev": float(r.long_lev or 1.0),
                "short_lev": float(r.short_lev or 1.0),
                "lot": int(r.lot_size or 1),
            }
    return margin


def fmt_ts(dt):
    return dt.astimezone(MSK).strftime("%Y-%m-%d %H:%M")


async def run_backtest():
    t0 = time.time()
    bt_logger.info("=" * 80)
    bt_logger.info("BACKTEST FULL BOT - August 2026")
    bt_logger.info("Period: %s .. %s" % (DATE_FROM.date(), DATE_TO.date()))
    bt_logger.info("Capital: %s RUB" % INITIAL_CASH)
    bt_logger.info("Sessions: %s" % CFG.sessions)
    bt_logger.info("Leverage: %sx | Margin: %s" % (CFG.leverage, CFG.use_margin))
    bt_logger.info("Commission: %.1f%% | Slippage: %s bps" % (CFG.commission_rate * 100, CFG.slippage_bps))
    bt_logger.info("SL mode: %s ATR(%d) x%.1f RR=%.1f" % (CFG.sl_mode, CFG.atr_period, CFG.atr_multiplier, CFG.atr_risk_reward))
    bt_logger.info("Quorum: %d | Confirm flip: %d" % (CFG.ensemble_quorum, CFG.confirm_flip))
    bt_logger.info("Cooldown: %d bars" % CFG.reentry_cooldown_bars)
    bt_logger.info("=" * 80)

    print("Loading margin data...", flush=True)
    margin_data = await load_margin_data()
    print("Margin data: %d tickers" % len(margin_data), flush=True)

    broker = BacktestBroker(
        initial_cash=INITIAL_CASH,
        cost_model=CostModel(commission_rate=CFG.commission_rate, slippage_bps=CFG.slippage_bps),
        margin_data=margin_data, config=CFG,
    )

    print("Loading eligible tickers (with optuna params)...", flush=True)
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT i.figi, i.ticker, i.lot, i.optuna_params
            FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            AND NOT (i.ticker='T' AND i.figi='BBG000BSJK37')
            ORDER BY i.ticker
        """))).all()
    eligible = [(r[0], r[1], int(r[2]) if r[2] else 1, r[3]) for r in rows]
    # дедуп (T задвоен)
    seen = set()
    eligible = [e for e in eligible if not (e[1] in seen or seen.add(e[1]))]
    print("Eligible: %d tickers" % len(eligible), flush=True)

    strategies = {}
    all_candles = {}  # figi -> list[EngineCandle] (all candles including warmup)
    tickers_map = {}
    per_ticker_sl_rr = {}  # figi -> (sl_mult, rr) для exit_policy при исполнении
    warmup_from = DATE_FROM - timedelta(days=WARMUP_DAYS)

    def optuna_params_dict(p):
        if not p:
            return dict(V2_BASE_P)
        return {
            "sl_mult": float(p.get("sl_mult", 4.0)),
            "rr": float(p.get("rr", 4.0)),
            "quorum": int(p.get("quorum", 2)),
            "vol_thr": float(p.get("vol_thr", 0.0) or 0.0),
            "active_sids": list(p.get("active_sids", ALL_SIDS)),
            "strategy_params": {k: dict(v) for k, v in (p.get("strategy_params") or {}).items()},
        }

    for i, (figi, ticker, lot, optuna_p) in enumerate(eligible):
        tickers_map[figi] = ticker
        print("  [%d/%d] %s..." % (i + 1, len(eligible), ticker), end="", flush=True)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warmup_from, date_to=DATE_TO)
        if not candles:
            print(" NO DATA", flush=True)
            continue
        print(" %d candles" % len(candles), flush=True)

        op = optuna_params_dict(optuna_p)
        active = op["active_sids"]
        sp = op["strategy_params"]
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(sp.get(s, V2P.get(s, {})))} for s in active]
        strategies[figi] = EnsembleV4Strategy(EnsembleParams(
            figi=figi, lot=int(lot) if lot else 10,
            capital=CFG.ensemble_capital, quorum=op["quorum"],
            session="all", sessions=CFG.sessions,
            setups=setups, sl_mult=op["sl_mult"], rr=op["rr"],
            vol_thr=op["vol_thr"], neutral_mode="semi_flip",
        ))
        per_ticker_sl_rr[figi] = (op["sl_mult"], op["rr"])
        ec = [EngineCandle(ts=c.ts, open=c.open, high=c.high,
                           low=c.low, close=c.close, volume=c.volume) for c in candles]
        all_candles[figi] = ec

    print("\nStrategies: %d" % len(strategies), flush=True)

    # Build merged candle list for backtest period only
    merged = []
    for figi, clist in all_candles.items():
        for c in clist:
            if c.ts >= DATE_FROM and c.ts < DATE_TO:
                merged.append((c.ts, figi, c))
    merged.sort(key=lambda x: x[0])
    print("Merged candles: %d (from %d tickers)" % (len(merged), len(all_candles)), flush=True)

    # Build time-indexed cursor per ticker for fast window extraction
    # cursor[figi] = index into all_candles[figi] up to current time
    figi_times = {}
    figi_time_idx = {}
    for figi, clist in all_candles.items():
        figi_times[figi] = [c.ts for c in clist]
        figi_time_idx[figi] = 0

    # Pre-compute 5min-aligned candle indices per ticker for on_bar
    figi_5min_idx = {}
    for figi, clist in all_candles.items():
        figi_5min_idx[figi] = [i for i, c in enumerate(clist) if c.ts.minute % 5 == 0]

    bar_counter = 0
    held = set()
    pending_orders = {}
    opposite_count = {}
    last_exit_bar = {}
    exit_plans = {}
    signals_seen = 0
    orders_submitted = 0
    orders_filled = 0
    skipped_no_cash = 0
    skipped_no_session = 0
    skipped_cooldown = 0
    skipped_opposite = 0
    skipped_held = 0

    for idx, (ts, figi, candle) in enumerate(merged):
        bar_counter += 1
        ticker = tickers_map.get(figi, figi[-6:])

        if bar_counter % 50000 == 0:
            elapsed = time.time() - t0
            speed = bar_counter / elapsed if elapsed > 0 else 0
            line = "  [%d/%d] ts=%s cash=%.2f pos=%d trades=%d elapsed=%.0fs (%.0f bars/s)" % (
                bar_counter, len(merged), fmt_ts(ts), broker.cash_val,
                len(broker.positions), len(broker.trades), elapsed, speed)
            bt_logger.info(line)
            print(line, flush=True)

        # 1. Execute pending order at candle open
        order = pending_orders.pop(figi, None)
        if order is not None and order.status != "CANCELLED":
            if order.action == "close":
                trade = await broker.close_position(figi, candle.open, "signal_exit", ts=candle.ts)
                order.status = "FILLED"
                held.discard(figi)
                last_exit_bar[figi] = bar_counter
                if trade:
                    bt_logger.info("  FILL CLOSE %s @ %.2f net=%+.2f" % (ticker, candle.open, trade.net_pnl))
            else:
                _sl, _rr = per_ticker_sl_rr.get(figi, (CFG.atr_multiplier, CFG.atr_risk_reward))
                exit_policy = AtrStopPolicy(period=CFG.atr_period, multiplier=_sl,
                                            risk_reward=_rr)
                # Get buffer window up to current ts
                idx_end = bisect.bisect_right(figi_times.get(figi, []), ts)
                buf_raw = all_candles.get(figi, [])[:idx_end]
                from app.services.ensemble import resample as _resample5
                buf_5m = _resample5(buf_raw[-SIGNAL_WINDOW:], 300) if buf_raw else []
                side_enum = Side.BUY if order.side == "BUY" else Side.SELL
                plan = exit_policy.plan_entry(side_enum, candle.open, buf_5m)
                fill_price = await broker.open_position(
                    figi=figi, ticker=ticker, side=order.side, qty=order.qty,
                    price=candle.open,
                    stop_loss=round(plan.stop_loss, 6) if plan.stop_loss else None,
                    take_profit=round(plan.take_profit, 6) if plan.take_profit else None,
                    strategy_id=CFG.strategy_id, ts=candle.ts,
                )
                if fill_price and fill_price > 0:
                    order.status = "FILLED"
                    order.price = fill_price
                    held.add(figi)
                    exit_plans[figi] = exit_policy
                    orders_filled += 1
                    bt_logger.info("  FILL OPEN %s %s qty=%d @ %.2f sl=%.2f tp=%.2f" % (
                        order.side, ticker, order.qty, fill_price,
                        plan.stop_loss if plan.stop_loss else 0,
                        plan.take_profit if plan.take_profit else 0))
                else:
                    order.status = "CANCELLED"
                    skipped_no_cash += 1

        # 2. Trailing stop
        if figi in held:
            pos = await broker.get_position(figi)
            if pos and pos.stop_loss is not None:
                ep = exit_plans.get(figi)
                if ep is not None and hasattr(ep, "update_stop"):
                    side_enum = Side.BUY if pos.side == "BUY" else Side.SELL
                    idx_end = bisect.bisect_right(figi_times.get(figi, []), ts)
                    buf_list = all_candles.get(figi, [])[:idx_end]
                    from app.services.ensemble import resample as _resample5u
                    buf_5m_u = _resample5u(buf_list[-SIGNAL_WINDOW:], 300) if buf_list else []
                    new_stop = ep.update_stop(side_enum, pos.entry_price, pos.stop_loss, buf_5m_u)
                    if new_stop is not None and new_stop != pos.stop_loss:
                        await broker.update_protective_levels(figi, new_stop, pos.take_profit)
                        pos.stop_loss = new_stop

        # 3. Intrabar exit (SL/TP)
        if figi in held:
            pos = await broker.get_position(figi)
            if pos:
                state = PositionState.LONG if pos.side == "BUY" else PositionState.SHORT
                _dbg_candle = EngineCandle(ts=candle.ts, open=candle.open, high=candle.high,
                                 low=candle.low, close=candle.close, volume=candle.volume)
                if ticker == "MTSS" and len(broker.trades) < 3:
                    print("  DBG %s %s figi=%s side=%s entry=%.2f sl=%.2f tp=%.2f" % (
                        ticker, fmt_ts(ts), figi, pos.side, pos.entry_price,
                        pos.stop_loss, pos.take_profit), flush=True)
                    print("    candle: O=%.2f H=%.2f L=%.2f C=%.2f" % (
                        candle.open, candle.high, candle.low, candle.close), flush=True)
                price_hit, reason = intrabar_exit(
                    _dbg_candle,
                    state, pos.stop_loss, pos.take_profit
                )
                if price_hit is not None:
                    if ticker == "MTSS" and len(broker.trades) < 3:
                        print("    EXIT: price_hit=%.2f reason=%s" % (price_hit, reason), flush=True)
                    trade = await broker.close_position(figi, price_hit, reason, ts=candle.ts)
                    held.discard(figi)
                    opposite_count.pop(figi, None)
                    exit_plans.pop(figi, None)
                    last_exit_bar[figi] = bar_counter
                    if trade:
                        bt_logger.info("  EXIT %s %s @ %.2f net=%+.2f" % (reason, ticker, price_hit, trade.net_pnl))
                    continue

        # 4. Overnight force close
        if figi in held and CFG.overnight:
            if not _sessions_allowed(ts, CFG.sessions):
                trade = await broker.close_position(figi, candle.open, "overnight_force_close", ts=candle.ts)
                held.discard(figi)
                opposite_count.pop(figi, None)
                last_exit_bar[figi] = bar_counter
                if trade:
                    bt_logger.info("  OVERNIGHT CLOSE %s net=%+.2f" % (ticker, trade.net_pnl))
                continue

        # 5. Signal processing (5min boundaries only)
        if candle.ts.minute % 5 != 0:
            continue

        strategy = strategies.get(figi)
        if strategy is None:
            continue

        # Get window of candles up to current ts, last SIGNAL_WINDOW only
        idx_end = bisect.bisect_right(figi_times.get(figi, []), ts)
        win = all_candles.get(figi, [])[:idx_end][-SIGNAL_WINDOW:]

        if len(win) < 50:
            continue

        try:
            sig = await asyncio.to_thread(strategy.on_bar, win)
        except Exception as e:
            bt_logger.debug("  SIGNAL ERROR %s: %s" % (ticker, e))
            sig = None

        if sig is None:
            continue

        signals_seen += 1

        pos_now = await broker.get_position(figi)
        state_now = (PositionState.LONG if pos_now and pos_now.side == "LONG" else
                     PositionState.SHORT if pos_now and pos_now.side == "SHORT" else
                     PositionState.FLAT)

        pol = SignalPolicy()
        action, note = pol.decide(sig, state_now, 0)

        if action is DecisionAction.ACCEPT_ENTRY:
            if figi in held:
                skipped_held += 1
                continue
            if not _sessions_allowed(ts, CFG.sessions):
                skipped_no_session += 1
                continue
            cooldown = CFG.reentry_cooldown_bars
            if cooldown > 0 and figi in last_exit_bar:
                bars_since = bar_counter - last_exit_bar[figi]
                if bars_since < cooldown:
                    skipped_cooldown += 1
                    continue
            cf = CFG.confirm_flip
            if cf > 0 and pos_now is not None:
                cur_side = "BUY" if pos_now.side == "LONG" else "SELL"
                if sig.side.value != cur_side:
                    opposite_count[figi] = opposite_count.get(figi, 0) + 1
                    if opposite_count[figi] < cf:
                        skipped_opposite += 1
                        continue
            opposite_count[figi] = 0

            md = margin_data.get(figi, {})
            lot = md.get("lot", 10)
            lev = max(1.0, float(CFG.leverage or 1.0))
            # Equity = cash + текущая стоимость открытых позиций
            equity = broker.cash_val
            for _f, _p in broker.positions.items():
                _tick = tickers_map.get(_f, _f[-6:])
                _cl = all_candles.get(_f, [])
                if _cl:
                    _px = _cl[min(len(_cl) - 1, bisect.bisect_right(figi_times.get(_f, []), ts) - 1)].close if ts else 0
                    _px = _px if _px else _p.entry_price
                else:
                    _px = _p.entry_price
                equity += _p.qty * _px if _p.side == "BUY" else -_p.qty * _px
            budget = max(0.0, equity * POSITION_PCT)
            lot_cost = candle.open * lot
            if lot_cost <= 0: continue
            own_per_lot = lot_cost / lev
            lots = max(1, int(budget / own_per_lot))

            if CFG.use_margin:
                max_lots = md.get("buy_max_lots", 9999) if sig.side.value == "BUY" else md.get("sell_max_lots", 9999)
                if max_lots and max_lots > 0:
                    pct = CFG.max_margin_pct / 100.0
                    max_capped = int(max_lots * pct)
                    if lots > max_capped:
                        lots = max_capped

            if lots <= 0: continue

            pending_orders[figi] = BotOrder(
                id="bt_%d_%s" % (bar_counter, figi[-6:]), figi=figi, ticker=ticker,
                action="open", side=sig.side.value, qty=lots * lot,  # в ШТУКАХ
            )
            orders_submitted += 1
            bt_logger.debug("  ORDER %s %s lots=%d qty_shares=%d budget=%.0f" % (
                sig.side.value, ticker, lots, lots * lot, budget))

        elif action is DecisionAction.ACCEPT_EXIT:
            cf = CFG.confirm_flip
            if cf > 0 and pos_now is not None:
                cur_side = "BUY" if pos_now.side == "LONG" else "SELL"
                if sig.side.value != cur_side:
                    opposite_count[figi] = opposite_count.get(figi, 0) + 1
                    if opposite_count[figi] < cf:
                        skipped_opposite += 1
                        continue
            opposite_count[figi] = 0
            pending_orders[figi] = BotOrder(
                id="bt_%d_%s" % (bar_counter, figi[-6:]), figi=figi, ticker=ticker,
                action="close", side=sig.side.value, qty=pos_now.qty if pos_now else 1,
            )
            orders_submitted += 1

    # Force close remaining
    bt_logger.info("\nForce closing remaining positions...")
    print("Force closing remaining positions...", flush=True)
    for figi in list(broker.positions.keys()):
        pos = broker.positions.get(figi)
        if pos:
            ticker = tickers_map.get(figi, figi[-6:])
            clist = all_candles.get(figi, [])
            last_price = float(clist[-1].close) if clist else pos.entry_price
            last_ts = clist[-1].ts if clist else pos.entry_time
            trade = await broker.close_position(figi, last_price, "backtest_end", ts=last_ts)
            if trade:
                bt_logger.info("  FORCE CLOSE %s @ %.2f net=%+.2f" % (ticker, last_price, trade.net_pnl))

    # Report
    elapsed = time.time() - t0
    trades = broker.trades
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    total_net = sum(t.net_pnl for t in trades)
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = abs(sum(t.net_pnl for t in losses))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / len(trades) * 100 if trades else 0

    # --- Regime attribution (по времени входа, через RegimeDetector per ticker) ---
    from app.services.regime import RegimeDetector, regime_at
    from app.services.ensemble import resample as _resample_regime
    regime_rows = {}
    for figi, clist in all_candles.items():
        try:
            c5 = _resample_regime(clist, 300)
            regime_rows[figi] = RegimeDetector().compute(c5)
        except Exception:
            regime_rows[figi] = None

    def trade_regime(t):
        rr = regime_rows.get(t.figi)
        if rr is None:
            return "UNKNOWN"
        r = regime_at(rr, t.entry_time)
        return r["state"] if r else "UNKNOWN"
    for t in trades:
        t.regime = trade_regime(t)

    report = []
    report.append("=" * 100)
    report.append("RESULTS - PORTFOLIO OPTUNA BACKTEST - August 2026 (10K pool, 20% pos)")
    report.append("=" * 100)
    report.append("  Trades:       %d (wins=%d losses=%d)" % (len(trades), len(wins), len(losses)))
    report.append("  Net PnL:      %+.2f RUB" % total_net)
    report.append("  Final cash:   %s RUB (from %s)" % (format(broker.cash_val, ",.2f"), format(INITIAL_CASH, ",.0f")))
    report.append("  Return:       %+.1f%%" % (total_net / INITIAL_CASH * 100))
    report.append("  Win rate:     %.1f%%" % wr)
    report.append("  Profit factor:%.2f" % pf)
    report.append("  Gross win:    %+.2f RUB (avg %+.2f)" % (gross_win, gross_win / len(wins) if wins else 0))
    report.append("  Gross loss:   %+.2f RUB (avg %+.2f)" % (-gross_loss, -gross_loss / len(losses) if losses else 0))
    report.append("  Avg net/trade:%+.2f RUB" % (total_net / len(trades) if trades else 0))
    report.append("  Total comm:   %.2f RUB" % sum(t.commission for t in trades))
    report.append("  Total slip:   %.2f RUB" % sum(t.slippage_cost for t in trades))
    report.append("  Elapsed:      %.1fs" % elapsed)
    report.append("")

    # Exit reasons
    report.append("  Exit reasons:")
    by_exit = {}
    for t in trades:
        by_exit.setdefault(t.exit_reason, []).append(t)
    for ex in sorted(by_exit, key=lambda x: -len(by_exit[x])):
        ts = by_exit[ex]
        tw = [t for t in ts if t.net_pnl > 0]
        tn = sum(t.net_pnl for t in ts)
        report.append("    %-18s: %4d trades WR=%.0f%% net=%+9.2f RUB" % (ex, len(ts), len(tw) / len(ts) * 100 if ts else 0, tn))
    report.append("")

    # Per-regime stats (по режиму входа)
    report.append("  Per-regime (по режиму входа):")
    report.append("    %-18s %6s %5s %5s %6s %10s %10s %10s %6s %8s" % (
        "Regime", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "AvgNet"))
    by_regime = {}
    for t in trades:
        r = getattr(t, "regime", "UNKNOWN")
        by_regime.setdefault(r, []).append(t)
    for rname in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE", "UNKNOWN"]:
        if rname not in by_regime:
            continue
        ts = by_regime[rname]
        tw = [t for t in ts if t.net_pnl > 0]
        tl = [t for t in ts if t.net_pnl <= 0]
        tgw = sum(t.net_pnl for t in tw)
        tgl = sum(t.net_pnl for t in tl)
        tn = tgw + tgl
        tp = tgw / abs(tgl) if tgl else 0
        report.append("    %-18s %6d %5d %5d %5.1f%% %+10.2f %+10.2f %+10.2f %6.2f %+8.2f" % (
            rname, len(ts), len(tw), len(tl), len(tw) / len(ts) * 100 if ts else 0,
            tgw, tgl, tn, tp, tn / len(ts) if ts else 0))
    report.append("")

    # Per-regime x ticker
    report.append("  Per-regime × ticker (Net):")
    rt_map = {}
    for t in trades:
        r = getattr(t, "regime", "UNKNOWN")
        rt_map.setdefault(r, {}).setdefault(t.ticker, 0.0)
        rt_map[r][t.ticker] += t.net_pnl
    all_tickers_r = sorted({t.ticker for t in trades})
    header = "    %-18s" % "Regime" + "".join("%+11s" % tk for tk in all_tickers_r)
    report.append(header)
    for rname in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]:
        if rname not in rt_map:
            continue
        line = "    %-18s" % rname
        for tk in all_tickers_r:
            line += "%+11.0f" % rt_map[rname].get(tk, 0)
        report.append(line)
    report.append("")

    # Все сделки (вход → выход)
    report.append("  All trades (entry → exit):")
    report.append("    %-7s %-4s %-12s %-8s %-12s %-8s %-9s %-10s %-10s %-9s" % (
        "Ticker", "Side", "EntryTime", "EntryPx", "ExitTime", "ExitPx",
        "ExitReason", "Qty", "NetPnl", "Regime"))
    for t in sorted(trades, key=lambda x: x.entry_time):
        report.append("    %-7s %-4s %-12s %8.2f %-12s %8.2f %-9s %-10d %+10.2f %-9s" % (
            t.ticker, t.side, fmt_ts(t.entry_time), t.entry_price,
            fmt_ts(t.exit_time), t.exit_price, t.exit_reason, t.qty,
            t.net_pnl, getattr(t, "regime", "?")))
    report.append("")

    # Per-ticker full stats
    report.append("  Per-ticker:")
    report.append("    %-7s %6s %5s %5s %6s %10s %10s %10s %6s %8s" % (
        "Ticker", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "AvgNet"))
    by_ticker = {}
    for t in trades:
        by_ticker.setdefault(t.ticker, []).append(t)
    for ticker in sorted(by_ticker.keys()):
        ts = by_ticker[ticker]
        tw = [t for t in ts if t.net_pnl > 0]
        tl = [t for t in ts if t.net_pnl <= 0]
        tgw = sum(t.net_pnl for t in tw)
        tgl = sum(t.net_pnl for t in tl)
        tn = tgw + tgl
        tp = tgw / abs(tgl) if tgl else 0
        twr = len(tw) / len(ts) * 100 if ts else 0
        report.append("    %-7s %6d %5d %5d %5.1f%% %+10.2f %+10.2f %+10.2f %6.2f %+8.2f" % (
            ticker, len(ts), len(tw), len(tl), twr, tgw, tgl, tn, tp, tn / len(ts) if ts else 0))
    report.append("")

    # Simultaneous positions + capital distribution tracking
    report.append("  Capital/positions tracking:")
    # Rebuild equity path from trades + track max concurrent
    # We approximate concurrent via trades sorted by entry/exit
    events = []
    for t in trades:
        events.append((t.entry_time, +1, t.ticker, t.net_pnl))
        events.append((t.exit_time, -1, t.ticker, t.net_pnl))
    events.sort(key=lambda x: x[0])
    concurrent = 0
    max_concurrent = 0
    max_concurrent_at = None
    for ev in events:
        concurrent += ev[1]
        if concurrent > max_concurrent:
            max_concurrent = concurrent
            max_concurrent_at = ev[0]
    report.append("    Max concurrent positions: %d (at %s)" % (max_concurrent, fmt_ts(max_concurrent_at) if max_concurrent_at else "-"))
    report.append("")

    # Per-day net + equity curve
    report.append("  Per-day:")
    day_stats = {}
    for t in trades:
        d = t.exit_time.astimezone(MSK).date().isoformat()
        day_stats.setdefault(d, {"net": 0.0, "n": 0, "wins": 0})
        day_stats[d]["net"] += t.net_pnl
        day_stats[d]["n"] += 1
        if t.net_pnl > 0:
            day_stats[d]["wins"] += 1
    cum_equity = INITIAL_CASH
    report.append("    %-12s %6s %5s %10s %10s" % ("Date", "Trades", "WR%", "DayNet", "Equity"))
    for d in sorted(day_stats.keys()):
        ds = day_stats[d]
        cum_equity += ds["net"]
        dwr = ds["wins"] / ds["n"] * 100 if ds["n"] else 0
        report.append("    %-12s %6d %4.0f%% %+10.2f %10.2f" % (d, ds["n"], dwr, ds["net"], cum_equity))
    report.append("")

    report.append("  Signals seen:      %d" % signals_seen)
    report.append("  Orders submitted:  %d" % orders_submitted)
    report.append("  Orders filled:     %d" % orders_filled)
    report.append("  Skipped (cash):    %d" % skipped_no_cash)
    report.append("  Skipped (session): %d" % skipped_no_session)
    report.append("  Skipped (cooldown):%d" % skipped_cooldown)
    report.append("  Skipped (opposite):%d" % skipped_opposite)
    report.append("  Skipped (held):    %d" % skipped_held)
    report.append("  Log: %s" % os.path.join(LOG_DIR, "backtest_august.log"))
    report.append("=" * 100)

    for line in report:
        bt_logger.info(line)
        print(line, flush=True)


if __name__ == "__main__":
    asyncio.run(run_backtest())
