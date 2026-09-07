"""Compare OLD engine (compute_ensemble direct) vs NEW engine (runtime._process_candle).
Same tickers, same params, same commission — different execution paths.
Tests: session=main vs session=all on both engines.
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

FIGIS = {
    "BBG004730N88": ("SBER", 1),
    "BBG004730RP0": ("GAZP", 10),
    "BBG004731032": ("LKOH", 1),
    "BBG004731354": ("ROSN", 1),
    "BBG008F2T3T2": ("RUAL", 10),
}

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO   = datetime(2026, 9, 1, tzinfo=timezone.utc)
CAPITAL = 10000.0

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]

V2_PARAMS = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}


def make_req(figi, lot, session, commission_rate=0.003, confirm_flip=2, cooldown=15):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    return {
        "figi": figi,
        "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1},
        "entry_session": session,
        "quorum": 2,
        "same_side_reentry_cooldown_bars": cooldown,
        "carry_overnight": session == "all",
        "opposite_hold": confirm_flip > 0,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": commission_rate,
        "slippage_bps": 2.0,
        "capital": CAPITAL,
        "lot": lot,
        "setups": setups,
        "use_all_setups": False,
        "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(),
        "to_ts": DATE_TO.isoformat(),
    }


def analyze(trades, label):
    total = len(trades)
    if total == 0:
        return {"label": label, "trades": 0, "wins": 0, "losses": 0, "net": 0,
                "pf": 0, "wr": 0, "gross_win": 0, "gross_loss": 0}
    wins = [t for t in trades if t["net"] > 0]
    losses = [t for t in trades if t["net"] <= 0]
    gw = sum(t["net"] for t in wins)
    gl = abs(sum(t["net"] for t in losses))
    return {"label": label, "trades": total, "wins": len(wins), "losses": len(losses),
            "net": gw - gl, "pf": gw / gl if gl else 0, "wr": len(wins) / total * 100,
            "gross_win": gw, "gross_loss": gl}


async def run_old(req, candles):
    t0 = time.time()
    res = compute_ensemble(candles, req)
    elapsed = time.time() - t0
    trades = res.get("static", {}).get("trades", [])
    return trades, elapsed


async def run_new(figi, ticker, candles, session, commission_rate, confirm_flip, cooldown):
    from app.bot.runtime import BotConfig, PaperBotRuntime, _sessions_allowed
    from app.engine.costs import CostModel

    sessions = ["main"] if session == "main" else ["morning", "day", "evening"]

    cfg = BotConfig(
        strategy_id="ensemble_v4", interval_name="1min", top_n=20,
        qty_per_trade=1, sl_mode="atr", atr_period=14,
        atr_multiplier=4.0, atr_risk_reward=4.0,
        initial_cash=CAPITAL, daily_loss_limit=5000.0,
        use_ensemble=True, ensemble_capital=CAPITAL,
        ensemble_quorum=2, ensemble_session=session,
        sessions=sessions,
        leverage=1.0, commission_rate=commission_rate, slippage_bps=2.0,
        confirm_flip=confirm_flip, reentry_cooldown_bars=cooldown,
        overnight=False,
        use_margin=True, max_margin_pct=80.0,
        long_allowed=True, short_allowed=True,
    )

    cost_model = CostModel(commission_rate=commission_rate, slippage_bps=2.0)
    from app.bot.paper_broker import PaperBroker
    from app.database import SessionLocal as SL
    broker = PaperBroker(SL, cost_model)
    runtime = PaperBotRuntime()
    runtime.config = cfg
    runtime.broker = broker
    await runtime.init_strategies()

    t0 = time.time()
    import app.bot.runtime as _rt_mod
    _orig_dt = _rt_mod.datetime

    for c in candles:
        class _FakeDt:
            _bt_now = c.ts
            @classmethod
            def now(cls, tz=None):
                if tz is not None:
                    return cls._bt_now.astimezone(tz)
                return cls._bt_now
        _rt_mod.datetime = _FakeDt
        try:
            await runtime._process_candle(c)
        except Exception:
            pass
    _rt_mod.datetime = _orig_dt
    elapsed = time.time() - t0
    return broker.trades, elapsed


def fmt(r):
    return "%4d trades  W=%3d  L=%3d  Net=%+10.2f  PF=%5.2f  WR=%5.1f%%  (%4.1fs)" % (
        r["trades"], r["wins"], r["losses"], r["net"], r["pf"], r["wr"], r.get("elapsed", 0))


async def main():
    print("=" * 95)
    print("ENGINE COMPARISON: OLD (compute_ensemble) vs NEW (runtime._process_candle)")
    print("ATR 14/4.0/4.0 | Commission=0.3%% | confirm_flip=2 | cooldown=15")
    print("Period: %s .. %s" % (DATE_FROM.date(), DATE_TO.date()))
    print("=" * 95)

    # Load data
    all_data = {}
    for figi, (ticker, lot) in FIGIS.items():
        print("Loading %s..." % ticker, end="", flush=True)
        candles = await _load(figi)
        print(" %d candles" % len(candles))
        all_data[figi] = (ticker, lot, candles)

    COMMISSION = 0.003  # 0.3% — как в боте
    CONFIRM_FLIP = 2
    COOLDOWN = 15

    for session in ["main", "all"]:
        print("\n" + "=" * 95)
        print("SESSION: %s" % session.upper())
        print("=" * 95)

        # OLD engine
        print("\n--- OLD ENGINE (compute_ensemble direct) ---")
        old_total = 0
        old_all = []
        for figi, (ticker, lot, candles) in all_data.items():
            req = make_req(figi, lot, session, COMMISSION, CONFIRM_FLIP, COOLDOWN)
            trades, elapsed = await run_old(req, candles)
            m = analyze(trades, "OLD")
            m["elapsed"] = elapsed
            old_total += m["net"]
            old_all.extend(trades)
            print("  %-6s %s" % (ticker, fmt(m)))
        old_all_m = analyze(old_all, "OLD TOTAL")
        old_all_m["elapsed"] = 0
        print("  %-6s %s" % ("TOTAL", fmt(old_all_m)))

        # NEW engine
        print("\n--- NEW ENGINE (runtime._process_candle) ---")
        new_total = 0
        new_all = []
        for figi, (ticker, lot, candles) in all_data.items():
            trades, elapsed = await run_new(figi, ticker, candles, session, COMMISSION, CONFIRM_FLIP, COOLDOWN)
            m = analyze(trades, "NEW")
            m["elapsed"] = elapsed
            new_total += m["net"]
            new_all.extend(trades)
            print("  %-6s %s" % (ticker, fmt(m)))
        new_all_m = analyze(new_all, "NEW TOTAL")
        new_all_m["elapsed"] = 0
        print("  %-6s %s" % ("TOTAL", fmt(new_all_m)))

        # Delta
        delta = new_all_m["net"] - old_all_m["net"]
        print("\n  DELTA: %+.2f₽ (new vs old)  |  trades delta: %d" % (delta, new_all_m["trades"] - old_all_m["trades"]))

    print("\n" + "=" * 95)
    print("DONE")
    print("=" * 95)


async def _load(figi):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


if __name__ == "__main__":
    asyncio.run(main())
