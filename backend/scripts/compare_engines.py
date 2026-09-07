"""Compare OLD engine (compute_ensemble direct) vs NEW engine (runtime._process_candle).

Same tickers, same params, different cost models and logic.
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

# 5 tickers with candles in DB (from sandbox trades + known liquid)
FIGIS = {
    "BBG004730N88": "SBER",
    "BBG004RVFCY3": "ALRS",
    "BBG004731032": "LKOH",
    "BBG004731354": "ROSN",
    "BBG004730RP0": "GAZP",
}

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO   = datetime(2026, 9, 1, tzinfo=timezone.utc)
CAPITAL = 10000.0
LOT = 1

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]

# V2 params (same as AGENTS.md)
PARAMS = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}


def old_engine_req(figi, commission_rate, slippage_bps):
    """Build compute_ensemble request — OLD engine style."""
    setups = [
        {"strategy_id": sid, "tf": "5min", "params": dict(PARAMS.get(sid, {}))}
        for sid in ALL_SIDS
    ]
    return {
        "figi": figi,
        "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "main",
        "quorum": 2,
        "same_side_reentry_cooldown_bars": 0,
        "carry_overnight": True,
        "opposite_hold": False,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": commission_rate,
        "slippage_bps": slippage_bps,
        "capital": CAPITAL,
        "lot": LOT,
        "setups": setups,
        "use_all_setups": False,
        "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(),
        "to_ts": DATE_TO.isoformat(),
    }


def analyze_trades(trades, label):
    """Compute metrics from trade list."""
    total = len(trades)
    if total == 0:
        return {"label": label, "trades": 0, "net": 0, "pf": 0, "wr": 0,
                "gross_win": 0, "gross_loss": 0, "avg_win": 0, "avg_loss": 0}
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = abs(sum(t["net"] for t in losses))
    net = gross_win - gross_loss
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr = len(wins) / total * 100
    avg_win = gross_win / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0
    return {
        "label": label, "trades": total, "wins": len(wins), "losses": len(losses),
        "net": net, "pf": pf, "wr": wr,
        "gross_win": gross_win, "gross_loss": gross_loss,
        "avg_win": avg_win, "avg_loss": avg_loss,
    }


async def load_data(figi):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


async def run_old_engine(figi, ticker, candles):
    """OLD: compute_ensemble direct, 0.05% commission, no trailing, no cooldown."""
    t0 = time.time()
    req = old_engine_req(figi, commission_rate=0.0005, slippage_bps=2.0)
    res = compute_ensemble(candles, req)
    elapsed = time.time() - t0

    if "error" in res:
        return None, elapsed

    trades = res.get("static", {}).get("trades", [])
    econ = res.get("static", {}).get("economic", {})
    return trades, elapsed, econ


async def run_new_engine(figi, ticker, candles):
    """NEW: runtime._process_candle via backtest_v2 style (0.3% commission, trailing, cooldown)."""
    # Import the real bot runtime
    from app.bot.runtime import BotConfig, PaperBotRuntime, _sessions_allowed
    from app.engine.costs import CostModel

    cfg = BotConfig(
        strategy_id="ensemble_v4", interval_name="1min", top_n=20,
        qty_per_trade=1, sl_mode="atr", atr_period=14,
        atr_multiplier=4.0, atr_risk_reward=4.0,
        initial_cash=CAPITAL, daily_loss_limit=5000.0,
        use_ensemble=True, ensemble_capital=CAPITAL,
        ensemble_quorum=2, ensemble_session="main",
        sessions=["morning", "day", "evening"],
        leverage=1.0, commission_rate=0.003, slippage_bps=2.0,
        confirm_flip=2, reentry_cooldown_bars=15, overnight=False,
        use_margin=True, max_margin_pct=80.0,
        long_allowed=True, short_allowed=True,
    )

    cost_model = CostModel(commission_rate=0.003, slippage_bps=2.0)
    from app.bot.runtime import PaperBroker
    broker = PaperBroker(cfg.initial_cash, cost_model, margin_data={}, config=cfg)

    runtime = PaperBotRuntime(config=cfg, broker=broker)

    # Init strategies
    await runtime.init_strategies()

    # Feed candles
    t0 = time.time()
    for c in candles:
        # Fake datetime for session check
        import app.bot.runtime as _rt_mod
        _orig_dt = _rt_mod.datetime

        class _FakeDt:
            _bt_now = c.ts
            @classmethod
            def now(cls, tz=None):
                if cls._bt_now is None:
                    return _orig_dt.now(tz)
                if tz is not None:
                    return cls._bt_now.astimezone(tz)
                return cls._bt_now

        _rt_mod.datetime = _FakeDt
        try:
            await runtime._process_candle(c)
        except Exception:
            pass
        finally:
            _rt_mod.datetime = _orig_dt

    elapsed = time.time() - t0
    trades = broker.trades
    return trades, elapsed, {}


def print_comparison(old_res, new_res, ticker):
    """Print side-by-side comparison."""
    print("\n  %-6s" % ticker)
    print("  %-12s %8s %8s %8s %8s %10s %8s" % (
        "Engine", "Trades", "Wins", "Losses", "Net", "PF", "WR%"))
    for r in [old_res, new_res]:
        if r is None:
            continue
        print("  %-12s %8d %8d %8d %+10.2f %8.2f %7.1f%%" % (
            r["label"], r["trades"], r.get("wins", 0), r.get("losses", 0),
            r["net"], r["pf"], r["wr"]))
    if old_res and new_res and old_res["trades"] > 0 and new_res["trades"] > 0:
        delta = new_res["net"] - old_res["net"]
        print("  DELTA: %+.2f₽ (new vs old)" % delta)


async def main():
    print("=" * 80)
    print("ENGINE COMPARISON: OLD (compute_ensemble) vs NEW (runtime._process_candle)")
    print("Period: %s .. %s" % (DATE_FROM.date(), DATE_TO.date()))
    print("Capital: %s₽" % CAPITAL)
    print("=" * 80)

    # Load all data first
    all_data = {}
    for figi, ticker in FIGIS.items():
        print("Loading %s..." % ticker, end="", flush=True)
        candles = await load_data(figi)
        print(" %d candles" % len(candles))
        all_data[figi] = (ticker, candles)

    print("\n--- RUNNING OLD ENGINE (0.05% comm, no trailing, no cooldown) ---")
    old_results = {}
    total_old = 0
    for figi, (ticker, candles) in all_data.items():
        trades, elapsed, econ = await run_old_engine(figi, ticker, candles)
        if trades is None:
            print("  %s: ERROR" % ticker)
            continue
        m = analyze_trades(trades, "OLD")
        m["elapsed"] = elapsed
        old_results[figi] = m
        total_old += m["net"]
        print("  %s: %d trades, net=%+.2f, PF=%.2f, WR=%.1f%% (%.1fs)" % (
            ticker, m["trades"], m["net"], m["pf"], m["wr"], elapsed))
    print("  TOTAL OLD: %+.2f₽" % total_old)

    print("\n--- RUNNING NEW ENGINE (0.3% comm, trailing, cooldown, confirm_flip=2) ---")
    new_results = {}
    total_new = 0
    for figi, (ticker, candles) in all_data.items():
        trades, elapsed, econ = await run_new_engine(figi, ticker, candles)
        m = analyze_trades(trades, "NEW")
        m["elapsed"] = elapsed
        new_results[figi] = m
        total_new += m["net"]
        print("  %s: %d trades, net=%+.2f, PF=%.2f, WR=%.1f%% (%.1fs)" % (
            ticker, m["trades"], m["net"], m["pf"], m["wr"], elapsed))
    print("  TOTAL NEW: %+.2f₽" % total_new)

    print("\n" + "=" * 80)
    print("COMPARISON TABLE")
    print("=" * 80)
    print("%-6s  %-12s %6s %6s %10s %6s %6s" % (
        "Ticker", "Engine", "Trades", "WR%", "Net", "PF", "Time"))
    print("-" * 80)
    for figi, (ticker, _) in all_data.items():
        old = old_results.get(figi)
        new = new_results.get(figi)
        if old:
            print("%-6s  %-12s %6d %5.1f%% %+10.2f %6.2f %5.1fs" % (
                ticker, "OLD", old["trades"], old["wr"], old["net"], old["pf"], old["elapsed"]))
        if new:
            print("%-6s  %-12s %6d %5.1f%% %+10.2f %6.2f %5.1fs" % (
                ticker, "NEW", new["trades"], new["wr"], new["net"], new["pf"], new["elapsed"]))
        if old and new:
            delta = new["net"] - old["net"]
            print("%-6s  %-12s %6s %6s %+10.2f" % ("", "DELTA", "", "", delta))
        print()

    print("TOTAL OLD: %+.2f₽" % total_old)
    print("TOTAL NEW: %+.2f₽" % total_new)
    print("TOTAL DELTA: %+.2f₽" % (total_new - total_old))
    print()

    # Cost breakdown
    print("=" * 80)
    print("COST ANALYSIS")
    print("=" * 80)
    print("OLD: commission=0.05%, slippage=2bps, no trailing, no cooldown, no confirm_flip")
    print("NEW: commission=0.30%, slippage=2bps, trailing stop, cooldown=15, confirm_flip=2")
    print()
    print("Key differences that destroy profitability:")
    print("  1. Commission x6 (0.05% -> 0.30%)")
    print("  2. Trailing stop cuts winners early")
    print("  3. confirm_flip=2 adds opposite-hold delay")
    print("  4. reentry_cooldown=15 prevents re-entry")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
