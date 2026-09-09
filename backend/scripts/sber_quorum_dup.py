#!/usr/bin/env python3
"""Проверка: вызывает ли compute_ensemble (как бот - на 1m свечах) кворум
с задвоенным голосом одной стратегии. Воспроизводит точный вызов
EnsembleV4Strategy.on_bar: списком 1m свечей -> compute_ensemble.
"""
import asyncio, json
from datetime import datetime, timezone, timedelta
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


async def main():
    async with SessionLocal() as db:
        r = (await db.execute(text(
            "SELECT figi, lot, optuna_params FROM instruments WHERE ticker='SBER'"))).first()
        figi, lot, opt = r[0], r[1], r[2] or {}
        candles = await _lc(db, figi, 1,
                            date_from=datetime.now(timezone.utc) - timedelta(days=3),
                            date_to=datetime.now(timezone.utc))

    active = list(opt.get("active_sids", ALL))
    sp = {k: dict(v) for k, v in (opt.get("strategy_params") or {}).items()}
    setups = [{"strategy_id": s, "tf": "5min",
               "params": dict(sp.get(s, V2P.get(s, {})))} for s in active]
    print("active:", active)

    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": int(opt.get("quorum", 2)),
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop",
                        "params": {"period": 14,
                                   "multiplier": float(opt.get("sl_mult", 4)),
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
    ql = st.get("quorum_list", [])
    q_dup = [q for q in ql
             if len(set(q.get("members_for") or [])) < len(q.get("members_for") or [])]
    print("quorum events:", len(ql), " с дублями одной стратегии:", len(q_dup))
    for q in q_dup[:10]:
        print("  %s %s votes=%s members=%s total=%s k=%s" % (
            q.get("ts"), q.get("side"), q.get("votes"),
            q.get("members_for"), q.get("total_members"), q.get("quorum_k")))
    print("--- пары votes>1 но total<votes (=дубль) ---")
    for q in ql:
        total = q.get("total_members") or 0
        v = q.get("votes") or 0
        memb = q.get("members_for") or []
        if v > total or len(set(memb)) < len(memb):
            print("  %s %s votes=%s total=%s members=%s" % (
                q.get("ts"), q.get("side"), v, total, memb))


asyncio.run(main())