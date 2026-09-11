"""Бэктест вариантов кворума: baseline 5 / +stoch-фильтр / -rsi+stoch / volume.

Прогон compute_ensemble по eligible-тикерам за период, агрегация Trades/WR/Net/PF.
Запуск: cd backend && .venv/bin/python3 scripts/test_kvorum_variants.py [DAYS]
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
STOCH = {"k_period": 14, "d_period": 3, "oversold": 20, "overbought": 80}


def setups(ids):
    return [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in ids]


BASE5 = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout"]
NO_RSI5 = ["stochastic", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout"]
NO_RSI4 = ["bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout"]

VARIANTS = [
    ("1. baseline 5, q2", setups(BASE5), None, 2),
    ("1b. baseline 5, q3", setups(BASE5), None, 3),
    ("5. 5+volume_drop, q2", setups(BASE5 + ["volume_drop"]), None, 2),
    ("5b. 5+volume_drop, q3", setups(BASE5 + ["volume_drop"]), None, 3),
    ("6. 5+volume_climax, q3", setups(BASE5 + ["volume_climax"]), None, 3),
    ("7. 5+vol_drop+climax, q3", setups(BASE5 + ["volume_drop", "volume_climax"]), None, 3),
    ("8. -rsi+stoch+vol_drop, q3", setups(NO_RSI5 + ["volume_drop"]), None, 3),
    ("2. 5+stoch-фильтр, q3", setups(BASE5), STOCH, 3),
]


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i "
            "JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible' ORDER BY i.ticker"
        ))).fetchall()
    return [(r[0], r[1], int(r[2]) if r[2] else 1) for r in rows]


def base_req(figi, lot, setups_cfg, stoch, quorum=2):
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": quorum,
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000,
        "lot": lot, "setups": setups_cfg, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip",
    }
    if stoch:
        req["stoch_filter"] = stoch
    return req


async def main():
    elig = await get_eligible()
    print(f"eligible: {len(elig)} тикеров, период {DAYS} дней")
    dfrom = datetime.now(timezone.utc) - timedelta(days=DAYS)
    candles_map = {}
    async with SessionLocal() as db:
        for figi, ticker, lot in elig:
            c = await _lc(db, figi, 1, date_from=dfrom)
            if len(c) >= 200:
                candles_map[ticker] = (figi, c, lot)
    print(f"с данными: {len(candles_map)} тикеров")

    print("\n" + "=" * 108)
    print("  %-30s %7s %6s %8s %10s %12s %12s %7s" % ("Variant", "Trades", "Wins", "WR%", "GrossWin", "GrossLoss", "Net", "PF"))
    print("=" * 108)
    for label, setups_cfg, stoch, q in VARIANTS:
        all_tr = []
        for ticker, (figi, c, lot) in candles_map.items():
            try:
                res = compute_ensemble(c, base_req(figi, lot, setups_cfg, stoch, q))
                if "error" not in res:
                    all_tr.extend(res.get("static", {}).get("trades", []))
            except Exception as e:
                print("  err", ticker, type(e).__name__, str(e)[:60])
        wins = [t for t in all_tr if t.get("net", 0) > 0]
        losses = [t for t in all_tr if t.get("net", 0) < 0]
        gw = sum(t["net"] for t in wins)
        gl = sum(t["net"] for t in losses)
        net = sum(t.get("net", 0) for t in all_tr)
        wr = len(wins) / len(all_tr) * 100 if all_tr else 0
        pf = gw / abs(gl) if gl else 0
        print("  %-30s %7d %6d %7.1f%% %+11.0f %+11.0f %+11.0f %7.2f" % (
            label, len(all_tr), len(wins), wr, gw, gl, net, pf))


asyncio.run(main())
