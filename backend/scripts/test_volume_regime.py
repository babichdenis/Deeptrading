"""Анализ volume_drop по режимам: какие любит, какие нет (вариант 7).

Запуск: cd backend && PYTHONPATH=. .venv/bin/python3 scripts/test_volume_regime.py [DAYS]
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import compute_ensemble, regime_at
from app.bot.ensemble_strategy import V2_SETUPS

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
BASE5 = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross", "donchian_breakout"]
SETUPS = [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in BASE5 + ["volume_drop"]]


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i "
            "JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible' ORDER BY i.ticker"
        ))).fetchall()
    return [(r[0], r[1], int(r[2]) if r[2] else 1) for r in rows]


def base_req(figi, lot):
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all", "quorum": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "capital": 10000, "lot": lot, "setups": SETUPS,
        "neutral_mode": "semi_flip",
    }


def agg(d, net):
    d["n"] += 1
    d["net"] += net
    if net > 0:
        d["w"] += 1
        d["gw"] += net
    else:
        d["gl"] += net


def show(title, g):
    print(f"\n  {title}")
    print("  %-14s %6s %6s %7s %11s %7s" % ("Regime", "Trades", "Wins", "WR%", "Net", "PF"))
    for reg in sorted(g, key=lambda x: -g[x]["net"]):
        d = g[reg]
        pf = d["gw"] / abs(d["gl"]) if d["gl"] else 0
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        print("  %-14s %6d %6d %6.1f%% %+11.0f %7.2f" % (reg, d["n"], d["w"], wr, d["net"], pf))


async def main():
    elig = await get_eligible()
    dfrom = datetime.now(timezone.utc) - timedelta(days=DAYS)
    cmap = {}
    async with SessionLocal() as db:
        for figi, ticker, lot in elig:
            c = await _lc(db, figi, 1, date_from=dfrom)
            if len(c) >= 200:
                cmap[ticker] = (figi, c, lot)
    print(f"тикеров {len(cmap)}, период {DAYS}д")

    all_by_reg = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0, "gw": 0.0, "gl": 0.0})
    vd_by_reg = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0, "gw": 0.0, "gl": 0.0})
    novd_by_reg = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0, "gw": 0.0, "gl": 0.0})
    vd_sigs_total = 0
    vd_in_quorum = 0
    total_trades = 0

    for ticker, (figi, c, lot) in cmap.items():
        try:
            res = compute_ensemble(c, base_req(figi, lot))
        except Exception:
            continue
        if "error" in res:
            continue
        st = res.get("static", {})
        _so = st.get("setups") or {}
        if "volume_drop" in _so:
            vd_sigs_total += int(_so["volume_drop"].get("signals", 0))
        timeline = (res.get("regime") or {}).get("timeline") or []
        # event_id -> members_for
        members = {q.get("event_id"): (q.get("members_for") or []) for q in st.get("quorum_list", [])}
        # entry_ts (5m граница) -> members_for; trade исполняется на след. 1m баре,
        # поэтому берём ближайший предшествующий entry.
        ent_list = sorted([e for e in st.get("entries", []) if e.get("ts")], key=lambda e: e["ts"])

        def members_for_trade(tts):
            best = None
            for e in ent_list:
                if e["ts"] <= tts:
                    best = e
                else:
                    break
            return members.get(best.get("quorum_event_id"), []) if best else []

        for t in st.get("trades", []):
            ets = t.get("entry_ts")
            reg = "NO_REGIME"
            try:
                if ets:
                    _d = datetime.fromisoformat(ets)
                    for iv in timeline:
                        if iv.get("from") and iv.get("to") and iv["from"] <= _d < iv["to"]:
                            reg = iv.get("state") or "NO_REGIME"
                            break
            except Exception:
                pass
            net = float(t.get("net", 0))
            total_trades += 1
            agg(all_by_reg[reg], net)
            mem = members_for_trade(ets) if ets else []
            if "volume_drop" in mem:
                vd_in_quorum += 1
                agg(vd_by_reg[reg], net)
            else:
                agg(novd_by_reg[reg], net)

    print(f"volume_drop: сигналов {vd_sigs_total}, сделок с ним в кворуме {vd_in_quorum} / {total_trades}")
    show("ВСЕ сделки по режимам", all_by_reg)
    show("Сделки С volume_drop", vd_by_reg)
    show("Сделки БЕЗ volume_drop", novd_by_reg)


asyncio.run(main())
