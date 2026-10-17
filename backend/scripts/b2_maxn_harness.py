"""FORK of the canonical engine runner for max-N concurrent positions per FIGI.

Ответвление (не переосмысление): переиспользует примитивы движка байт-в-байт
(Candle/Signal/Position/Trade/Side, CostModel.fill_price/commission, TradeLedger,
CandleWindow), меняет ровно ОДНО: одна позиция на FIGI -> список позиций (кап N).

Стратегия emits ТОЛЬКО entry-сигналы (dip/pump); горизонтный выход (H баров) и
cutoff — механически в раннере по bars_held/minute, закрытие на open[i]. Это
эквивалент канонику (exit-сигнал стратегии на баре j -> fill на open[j+1]).
trailing/BE/abort/partial/confirm выключены (для mean-reversion не используются).

N=1 обязан совпасть с каноническим engine_harness (341 trade, +4.02%) — валидация.
Движок app/engine НЕ тронут.
"""
import warnings; warnings.filterwarnings("ignore")
import sys, time
import pandas as pd, numpy as np
from collections import defaultdict, Counter

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from app.engine.models import Candle, Signal, Side, Position, PositionState, ExitReason
from app.engine.policies import SignalPolicyConfig
from app.engine.exits import FixedSlTpPolicy
from app.engine.costs import CostModel
from app.engine.ledger import TradeLedger
from app.engine.runner import EngineConfig
from app.engine.views import CandleWindow

THR = 0.03
H = 60
CUTOFF_UTC = 15 * 60 + 44      # 18:44 MSK in UTC minutes
ENTRY_ROOM = CUTOFF_UTC - H    # enter only if <= this so +H stays same-day

P = "/private/var/folders/xd/mbm9tg015tz3cnm8szvdw8000000gp/T/opencode/y2026_hl.pkl"
df = pd.read_pickle(P)


class MeanReversionStrategyN:
    """dip (ret30<-3% + down3) -> BUY; pump (ret30>+3% + up3) -> SELL.

    Fork of engine_harness.MeanReversionStrategy: emits ENTRY-only signals,
    capped at N concurrent (reads runner's live positions). Horizon/cutoff exit
    is handled mechanically by the runner (not via exit-signal)."""
    strategy_id = "mean_reversion_dip_maxn"
    version = "1.0.0"

    def __init__(self, thr=THR, horizon=H, allowed_clean=None, max_n=1):
        self.thr = thr; self.horizon = horizon
        self._closes = []
        self._allowed = allowed_clean
        self._max_n = max_n
        self._positions = []

    def warmup_bars(self):
        return 31

    def set_positions(self, positions):
        self._positions = positions

    def on_bar(self, bars):
        n = len(bars)
        if n < 32:
            return None
        cur = bars[n - 1]
        cl = self._closes
        if len(cl) < n:
            for k in range(len(cl), n):
                cl.append(bars[k].close)
        elif len(cl) > n:
            cl[:] = [bars[k].close for k in range(n)]
        i = n - 1
        minute = cur.ts.hour * 60 + cur.ts.minute
        if len(self._positions) >= self._max_n:
            return None
        if minute > ENTRY_ROOM:
            return None
        if self._allowed is not None and i not in self._allowed:
            return None
        ret30 = cl[-1] / cl[-31] - 1.0
        down3 = cl[-1] < cl[-2] < cl[-3] < cl[-4]
        up3 = cl[-1] > cl[-2] > cl[-3] > cl[-4]
        if ret30 < -self.thr and down3:
            return Signal(self.strategy_id, Side.BUY, cur.ts, "dip_down3")
        if ret30 > self.thr and up3:
            return Signal(self.strategy_id, Side.SELL, cur.ts, "pump_up3")
        return None


class MaxNEngineRunner:
    """FORK of EngineRunner: single st.position -> st.positions (<=N per FIGI).

    Execution byte-for-byte with canonical:
      entry fill = CostModel.fill_price(open[i+1], side)  (next-open)
      exit  fill = CostModel.fill_price(open[i], side)    (open of maturity bar)
      net = gross - (entry_comm + exit_comm); gross by slipped prices.
    Exit is mechanical: bars_held >= H or minute >= CUTOFF -> SIGNAL_EXIT at open[i]."""

    def __init__(self, strategy, exit_policy, config, max_n=1):
        self.strategy = strategy
        self.exit_policy = exit_policy
        self.cfg = config
        self.cost = config.cost_model
        self._max_n = max_n
        self._chain_seq = 0

    def _validate_candles(self, candles):
        import math
        prev_ts = None
        for idx, c in enumerate(candles):
            for value in (c.open, c.high, c.low, c.close):
                if not math.isfinite(float(value)):
                    raise ValueError(f"candle[{idx}] not finite")
            if float(c.high) < float(c.low):
                raise ValueError(f"candle[{idx}]: high < low")
            if float(c.high) < max(float(c.open), float(c.close)) - 1e-12:
                raise ValueError(f"candle[{idx}]: high < max(open,close)")
            if float(c.low) > min(float(c.open), float(c.close)) + 1e-12:
                raise ValueError(f"candle[{idx}]: low > min(open,close)")
            if prev_ts is not None and c.ts <= prev_ts:
                raise ValueError(f"candle[{idx}]: ts not increasing")
            prev_ts = c.ts

    def run(self, candles):
        reset = getattr(self.strategy, "reset", None)
        if callable(reset):
            reset()
        total = len(candles)
        if total == 0:
            return TradeLedger()
        self._validate_candles(candles)
        ledger = TradeLedger()
        positions = []
        pending_entry = None
        warmup = self.strategy.warmup_bars()
        for i in range(total):
            bar = candles[i]
            minute = bar.ts.hour * 60 + bar.ts.minute
            # 1. close matured positions (horizon bars_held>=H) at open[i].
            #    ENTRY_ROOM guarantees horizon fires before cutoff (entry<=17:44,
            #    +60bars<=18:44), so no separate cutoff clause — exact N=1 match.
            if positions:
                for p in list(positions):
                    if p.bars_held >= self.strategy.horizon:
                        self._close(i, bar.ts, p, bar.open,
                                    ExitReason.SIGNAL_EXIT.value, ledger)
                        positions.remove(p)
            # 2. fill pending entry at open[i] (cap N)
            if pending_entry is not None:
                if len(positions) < self._max_n:
                    positions.append(self._open(i, bar, candles, pending_entry, ledger))
                pending_entry = None
            # 3. age open positions
            for p in positions:
                p.bars_held += 1
            # 4. poll strategy (entry-only); signal -> fill next bar open
            if i + 1 < total and i >= warmup - 1:
                self.strategy.set_positions(positions)
                signal = self.strategy.on_bar(CandleWindow(candles, 0, i + 1))
                if signal is not None and signal.kind == "entry" and len(positions) < self._max_n:
                    pending_entry = signal
        # end-of-data: close any remaining at last close
        last = candles[-1]
        for p in positions:
            self._close(total - 1, last.ts, p, last.close,
                        ExitReason.END_OF_DATA.value, ledger)
        return ledger

    def _open(self, index, bar, candles, signal, ledger):
        side = signal.side
        base = bar.open
        fill = self.cost.fill_price(base, side)
        notional = fill * self.cfg.qty
        commission = self.cost.commission(notional)
        slippage = abs(fill - base) * self.cfg.qty
        plan = self.exit_policy.plan_entry(side, fill, CandleWindow(candles, 0, index))
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        self._chain_seq += 1
        chain_id = f"{self.cfg.figi}-P{self._chain_seq:04d}"
        ledger.log(index, bar.ts, "FILL_ENTRY",
                   f"{side.value} qty={self.cfg.qty} price={fill} chain={chain_id}")
        return Position(
            figi=self.cfg.figi, state=state, qty=self.cfg.qty,
            entry_time=bar.ts, entry_index=index, entry_price=fill,
            initial_stop=plan.stop_loss, target=plan.take_profit,
            bars_held=0, entry_commission=commission, entry_slippage=slippage,
            chain_id=chain_id,
        )

    def _close(self, index, ts, position, base_price, reason, ledger):
        action_side = Side.SELL if position.state is PositionState.LONG else Side.BUY
        fill = self.cost.fill_price(base_price, action_side)
        exit_commission = self.cost.commission(fill * position.qty)
        exit_slippage = abs(fill - base_price) * position.qty
        if position.state is PositionState.LONG:
            gross = (fill - position.entry_price) * position.qty
        else:
            gross = (position.entry_price - fill) * position.qty
        commission = position.entry_commission + exit_commission
        slippage = position.entry_slippage + exit_slippage
        net = gross - commission
        trade = ledger.add_trade(
            figi=position.figi, side=position.state.value, qty=position.qty,
            entry_index=position.entry_index, entry_time=position.entry_time,
            entry_price=position.entry_price, exit_index=index, exit_time=ts,
            exit_price=fill, bars_held=position.bars_held, gross_pnl=gross,
            commission=commission, slippage=slippage, net_pnl=net,
            exit_reason=reason, initial_stop=position.initial_stop,
            take_profit=position.target, chain_id=position.chain_id,
        )
        ledger.log(index, ts, "FILL_EXIT",
                   f"trade={trade.trade_id} reason={reason} net={net:.4f}")


def prep_ticker(figi, g):
    """Build sanitized candles + clean-window mask ONCE per ticker (cached across N)."""
    g = g.sort_values("ts").reset_index(drop=True)
    o = g["open"].to_numpy(np.float64)
    h = g["high"].to_numpy(np.float64)
    lo = g["low"].to_numpy(np.float64)
    c = g["close"].to_numpy(np.float64)
    lo = np.minimum(lo, np.minimum(o, c))
    h = np.maximum(h, np.maximum(o, c))
    n = len(g)
    prev_c = np.empty(n); prev_c[0] = np.nan; prev_c[1:] = c[:-1]
    jump = np.abs(c / prev_c - 1.0) > 0.30
    junk = np.zeros(n, bool)
    for d in range(-2, 3):
        junk |= np.roll(jump, d)
    junk[:2] = True; junk[-2:] = True
    unclean = np.zeros(n, bool)
    for j in np.where(junk)[0]:
        unclean[max(0, j - H): j + 1] = True
    allowed = set(np.where(~unclean)[0].tolist())
    candles = [Candle(ts=g["ts"].iloc[k], open=float(o[k]), high=float(h[k]),
                      low=float(lo[k]), close=float(c[k]), volume=0.0) for k in range(n)]
    return candles if n >= 80 else None, allowed


def run_ticker(figi, candles, allowed, max_n):
    strat = MeanReversionStrategyN(allowed_clean=allowed, max_n=max_n)
    exit_pol = FixedSlTpPolicy(stop_pct=0.99, target_pct=99.0)
    cfg = EngineConfig(figi=figi, qty=1, mode="both", allow_short=True,
                       cost_model=CostModel(), signal_policy=SignalPolicyConfig())
    runner = MaxNEngineRunner(strat, exit_pol, cfg, max_n=max_n)
    ledger = runner.run(candles)
    return ledger.trades, ledger.fingerprint()


def portfolio(trades, capital=100000, K=10):
    evs = []
    for t in trades:
        ret = t.net_pnl / t.entry_price if t.entry_price else 0.0
        evs.append((t.entry_time, t.exit_time, ret, t.exit_reason, t.bars_held))
    evs.sort(key=lambda x: x[0])
    slot = capital / K
    eq = capital; open_pos = []
    bym = defaultdict(lambda: [0.0, 0])
    for et, xt, ret, er, bh in evs:
        while open_pos and open_pos[0][0] <= et:
            x2, n2, m2 = open_pos.pop(0)
            eq += slot * n2; bym[m2][0] += slot * n2; bym[m2][1] += 1
        if len(open_pos) < K:
            open_pos.append((xt, ret, str(np.datetime64(et, 'M'))))
        open_pos.sort(key=lambda x: x[0])
    for x2, n2, m2 in open_pos:
        eq += slot * n2; bym[m2][0] += slot * n2; bym[m2][1] += 1
    return eq, bym, evs


if __name__ == "__main__":
    t0 = time.time()
    prepared = {}
    for figi, g in df.groupby("figi"):
        candles, allowed = prep_ticker(figi, g)
        if candles is not None:
            prepared[figi] = (candles, allowed)
    print(f"prepared {len(prepared)} tickers ({time.time()-t0:.1f}s)", flush=True)
    results = {}
    for N in (1, 2, 3, 5):
        tN = time.time()
        all_trades = []
        fps = []
        for figi, (candles, allowed) in prepared.items():
            tr, fp = run_ticker(figi, candles, allowed, N)
            all_trades.extend(tr)
            if fp:
                fps.append(fp)
        eq, bym, evs = portfolio(all_trades, K=10)
        tot = eq - 100000
        results[N] = (len(all_trades), tot, bym, all_trades)
        print(f"\n=== MAXN N={N} | trades {len(all_trades)} | total {tot:+8.0f} RUB ({tot/1000:+5.2f}%) [{time.time()-tN:.0f}s] ===", flush=True)
        for m in sorted(bym):
            pnl, nt = bym[m]
            print(f"  {m}: {pnl:+8.0f} ({pnl/1000:+5.2f}%) n{nt}", flush=True)
        import hashlib
        comb = hashlib.sha256("".join(sorted(fps)).encode()).hexdigest()[:12]
        print(f"  fingerprint: {comb}", flush=True)
    t1 = results[1][3]
    er = Counter(t.exit_reason for t in t1)
    bh = [t.bars_held for t in t1]
    print(f"\nN=1 exit_reasons: {dict(er)}", flush=True)
    if bh:
        print(f"N=1 bars_held: mean {np.mean(bh):.1f} median {np.median(bh):.0f} min {min(bh)} max {max(bh)}", flush=True)
    print(f"\nDONE {time.time()-t0:.1f}s", flush=True)
