"""Матрица режимов для volume_drop: в каких режимах его пускать.

Запуск: cd backend && PYTHONPATH=. .venv/bin/python3 scripts/test_regime_matrix.py [DAYS]
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import compute_ensemble
from app.bot.ensemble_strategy import V2_SETUPS

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
BASE5 = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout"]


def setups(ids):
    return [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in ids]


HV = ["HIGH_VOLATILITY"]
TR = ["TREND_DOWN", "TREND_UP"]
NE = ["NEUTRAL"]
VARIANTS = [
    ("1. baseline (5)", BASE5, None),
    ("2. +vd все режимы", BASE5 + ["volume_drop"], None),
    ("3. +vd HV", BASE5 + ["volume_drop"], HV),
    ("4. +vd HV+TREND_DOWN", BASE5 + ["volume_drop"], ["HIGH_VOLATILITY", "TREND_DOWN"]),
    ("5. +vd NEUTRAL", BASE5 + ["volume_drop"], NE),
    ("6. +vd HV+NEUTRAL", BASE5 + ["volume_drop"], ["HIGH_VOLATILITY", "NEUTRAL"]),
    ("7. +vd HV+TREND(up+down)", BASE5 + ["volume_drop"], ["HIGH_VOLATILITY", "TREND_DOWN", "TREND_UP"]),
    ("8. +vd все кроме NEUTRAL", BASE5 + ["volume_drop"], ["HIGH_VOLATILITY", "TREND_DOWN", "TREND_UP", "RANGE"]),
]


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i "
            "JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible' ORDER BY i.ticker"
        ))).fetchall()
    return [(r[0], r[1], int(r[2]) if r[2] else 1) for r in rows]


def base_req(figi, lot, ids, allowed):
    req = {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "capital": 10000, "lot": lot, "setups": setups(ids),
        "neutral_mode": "semi_flip",
    }
    if allowed is not None:
        req["regime_setups_filter"] = {"volume_drop": allowed}
    return req


async def main():
    elig = await get_eligible()
    dfrom = datetime.now(timezone.utc) - timedelta(days=DAYS)
    cmap = {}
    async with SessionLocal() as db:
        for figi, ticker, lot in elig:
            c = await _lc(db, figi, 1, date_from=dfrom)
            if len(c) >= 200:
                cmap[ticker] = (figi, c, lot)
    print(f"тикеров {len(cmap)}, период {DAYS}д\n")
    print("=" * 108)
    print("  %-30s %7s %6s %8s %10s %12s %12s %7s" % ("Variant", "Trades", "Wins", "WR%", "GrossWin", "GrossLoss", "Net", "PF"))
    print("=" * 108)
    for label, ids, allowed in VARIANTS:
        tr = []
        for ticker, (figi, c, lot) in cmap.items():
            try:
                res = compute_ensemble(c, base_req(figi, lot, ids, allowed))
                if "error" not in res:
                    tr.extend(res.get("static", {}).get("trades", []))
            except Exception as e:
                print("  err", ticker, type(e).__name__, str(e)[:50])
        w = [t for t in tr if t.get("net", 0) > 0]
        l = [t for t in tr if t.get("net", 0) < 0]
        gw = sum(t["net"] for t in w)
        gl = sum(t["net"] for t in l)
        net = sum(t.get("net", 0) for t in tr)
        wr = len(w) / len(tr) * 100 if tr else 0
        pf = gw / abs(gl) if gl else 0
        print("  %-30s %7d %6d %7.1f%% %+11.0f %+11.0f %+11.0f %7.2f" % (
            label, len(tr), len(w), wr, gw, gl, net, pf))


asyncio.run(main())
