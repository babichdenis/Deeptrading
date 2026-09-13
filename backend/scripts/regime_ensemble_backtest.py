#!/usr/bin/env python3
"""Бэктест ансамблей по режимам (long/short/range/hv/neutral) через compute_ensemble.

Роутер: long_ensemble→TREND_UP, short_ensemble→TREND_DOWN, range_ensemble→RANGE,
hv_ensemble→HIGH_VOLATILITY, neutral_ensemble→NEUTRAL. quorum=1 (каждый ансамбль самостоятелен).

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/regime_ensemble_backtest.py --from 2026-08-15 --to 2026-09-12
"""
import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import compute_ensemble, resample
from app.services.regime import RegimeDetector, regime_at

SIDS = ["long_ensemble", "short_ensemble", "range_ensemble", "hv_ensemble", "neutral_ensemble"]
ROUTER = {
    "long_ensemble": ["TREND_UP"],
    "short_ensemble": ["TREND_DOWN"],
    "range_ensemble": ["RANGE"],
    "hv_ensemble": ["HIGH_VOLATILITY"],
    "neutral_ensemble": ["NEUTRAL"],
}
REGIMES = ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "NEUTRAL", "RANGE", "NO_REGIME"]


def build_req(figi, lot, f, t, sl=4.0, rr=6.0):
    setups = [{"strategy_id": s, "tf": "5min", "params": {}} for s in SIDS]
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "all",
        "quorum": 1, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": setups, "use_all_setups": False, "drop_useless": False,
        "neutral_mode": None, "regime_setups_filter": ROUTER,
        "from_ts": f.isoformat(), "to_ts": t.isoformat(),
    }


def _blank():
    return {"trades": 0, "wins": 0, "net": 0.0, "gw": 0.0, "gl": 0.0}


def _add(b, net):
    b["trades"] += 1
    b["wins"] += 1 if net > 0 else 0
    b["net"] += net
    if net > 0:
        b["gw"] += net
    else:
        b["gl"] += net


def _fmt(b):
    wr = b["wins"] / b["trades"] * 100 if b["trades"] else 0.0
    pf = b["gw"] / abs(b["gl"]) if b["gl"] else 0.0
    return wr, pf


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", default="2026-08-15")
    ap.add_argument("--to", dest="dto", default="2026-09-12")
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()
    f = datetime.fromisoformat(args.dfrom + "T00:00:00+00:00")
    t = datetime.fromisoformat(args.dto + "T23:59:00+00:00")
    only = [x.strip() for x in args.tickers.split(",") if x.strip()] or None

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier='eligible'
            AND NOT (i.ticker='T' AND i.figi='BBG000BSJK37') ORDER BY i.ticker
        """))).fetchall()
        meta = {}
        for r in rows:
            if r[1] in meta:
                continue
            if only and r[1] not in only:
                continue
            meta[r[1]] = (r[0], int(r[2]) if r[2] else 1)

        tot = _blank()
        by_reg = defaultdict(_blank)
        by_strat = defaultdict(_blank)
        by_tkr = defaultdict(_blank)
        for tkr, (figi, lot) in meta.items():
            c = await _lc(db, figi, 1, date_from=f, date_to=t)
            if not c or len(c) < 200:
                continue
            try:
                res = compute_ensemble(c, build_req(figi, lot, f, t))
            except Exception as e:
                print("err", tkr, type(e).__name__, str(e)[:50]); continue
            if "error" in res:
                continue
            st = res.get("static", {})
            tl = RegimeDetector().compute(resample(c, 300) or []) or []
            qmap = {q["event_id"]: (q.get("members_for") or []) for q in (st.get("quorum_list") or [])}
            for ep in (st.get("episodes") or {}).get("list", []):
                if ep.get("status") != "TRADED":
                    continue
                net = ep.get("net_sum", ep.get("net") or 0.0) or 0.0
                et = ep.get("entry_time")
                r = regime_at(tl, datetime.fromisoformat(et)) if (et and tl) else None
                reg = r["state"] if r else "NO_REGIME"
                _add(tot, net); _add(by_reg[reg], net); _add(by_tkr[tkr], net)
                for sid in (qmap.get(ep.get("quorum_event_id"), []) or ["—"]):
                    _add(by_strat[str(sid)], net)

    wr, pf = _fmt(tot)
    print("=" * 100)
    print("REGIME ENSEMBLE BACKTEST %s .. %s  (quorum=1, sl4/rr6, 10K/тикер)" % (args.dfrom, args.dto))
    print("=" * 100)
    print("OVERALL: trades=%d net=%+.0f WR=%.1f%% PF=%.2f" % (tot["trades"], tot["net"], wr, pf))
    print("\nПо режимам:")
    for reg in REGIMES:
        if reg in by_reg:
            w, p = _fmt(by_reg[reg])
            print("  %-18s N=%4d net=%+9.0f WR=%5.1f%% PF=%5.2f" % (reg, by_reg[reg]["trades"], by_reg[reg]["net"], w, p))
    print("\nПо ансамблям:")
    for sid, b in sorted(by_strat.items(), key=lambda kv: -kv[1]["net"]):
        w, p = _fmt(b)
        print("  %-18s N=%4d net=%+9.0f WR=%5.1f%% PF=%5.2f" % (sid, b["trades"], b["net"], w, p))
    print("\nТоп тикеров:")
    for tkr, b in sorted(by_tkr.items(), key=lambda kv: -kv[1]["net"])[:8]:
        print("  %-7s N=%4d net=%+9.0f" % (tkr, b["trades"], b["net"]))


if __name__ == "__main__":
    asyncio.run(main())
