#!/usr/bin/env python3
"""Детальный разбор SBER: почему эталон даёт 0 входов сегодня.
Выводим funnel compute_ensemble: сколько сырых сигналов, quorum-голосов,
входов, и по каким причинам отклонено (rejected_by_reason).
Гипотеза: ансамбль/кворум генерит лишние голоса -> бот входит, эталон - нет.
НЕ меняет движок.
"""
import asyncio, json
from datetime import datetime, timezone, timedelta
from collections import Counter
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
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
DAYS = 8


async def main():
    async with SessionLocal() as db:
        r = (await db.execute(text(
            "SELECT figi, ticker, lot, optuna_params FROM instruments WHERE ticker='SBER'"))).first()
        figi, lot, opt = r[0], r[2], r[3] or {}

        candles = await _lc(db, figi, 1,
                            date_from=datetime.now(timezone.utc) - timedelta(days=DAYS),
                            date_to=datetime.now(timezone.utc))

    active = list(opt.get("active_sids", ALL))
    sp = {k: dict(v) for k, v in (opt.get("strategy_params") or {}).items()}
    setups = [{"strategy_id": s, "tf": "5min",
               "params": dict(sp.get(s, V2P.get(s, {})))} for s in active]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": int(opt.get("quorum", 2)),
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop",
                        "params": {"period": 14, "multiplier": float(opt.get("sl_mult", 4)),
                                   "risk_reward": float(opt.get("rr", 4))}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": opt.get("neutral_mode", "semi_flip"),
        "from_ts": candles[0].ts.isoformat(), "to_ts": candles[-1].ts.isoformat(),
    }
    res = compute_ensemble(candles, req)
    if "error" in res:
        print("ERROR", res); return

    st = res.get("static", {})
    funnel = st.get("funnel", {})
    print("=== SBER funnel (эталон) ===")
    for k, v in funnel.items():
        if k != "reentry_rejected_list":
            print(f"  {k}: {v}")
    print("\n=== rejected_by_reason ===")
    for k, v in st.get("funnel_tf", {}).get("rejected_by_reason", {}).items():
        print(f"  {k}: {v}")

    # Куда смотрят: entries accepted за сегодня
    print("\n=== accepted decisions (всего) ===")
    acc = st.get("entries", [])
    print("count:", len(acc))
    for a in acc:
        print("  ", a.get("ts"), a.get("side"), "qe=", a.get("quorum_event_id"))

    # quorum-события
    ql = st.get("quorum_list", [])
    print("\n=== quorum events (всего %d) ===" % len(ql))
    for q in ql:
        print("  %s %s votes=%s buy=%s sell=%s members=%s" % (
            q.get("ts"), q.get("side"), q.get("votes"), q.get("buy_votes"),
            q.get("sell_votes"), q.get("members_for")))

    # setup-сигналы каждого активного сетапа (bollinger)
    setups_out = st.get("setups", {})
    print("\n=== setup-сигналы по стратегиям ===")
    for sid, sd in setups_out.items():
        print("  %s: BUY=%s SELL=%s (signals=%s)" % (sid, sd.get("BUY"), sd.get("SELL"), sd.get("signals")))

    # rejected за сегодня с деталями
    print("\n=== rejected (последние, за сегодня) ===")
    rej = [r for r in st.get("rejected", []) if str(r.get("ts", "")).startswith("2026-09-07")]
    print("today rejected count:", len(rej))
    for r in rej[-40:]:
        print("  %s %s %s" % (r.get("ts"), r.get("side"), r.get("reason")))


asyncio.run(main())
