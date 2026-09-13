#!/usr/bin/env python3
"""Быстрый прогон compute_ensemble по периодам с live-подобной конфигурацией.

Сравнение с replay-тестом (runtime). Отличия от runtime (ожидаемы):
  - нет runtime-гейтов trend_alignment / trade_regimes (в compute_ensemble их нет);
  - изолированные 10K/тикер, без общего портфеля/маржи.
Поэтому цифры не идентичны, но структура (знак, режимы, голоса) сопоставима.

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/fast_period_compare.py
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
from app.bot.ensemble_strategy import V2_SETUPS

V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
V2P["volume_drop"] = {"ma_len": 20, "drop_ratio": 1.5}
LIVE_SIDS = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross",
             "donchian_breakout", "volume_drop"]
BOL_FILTER = {"bollinger_reclaim": ["NEUTRAL", "TREND_UP", "TREND_DOWN"]}

PERIODS = {
    "jul": (datetime(2026, 7, 14, tzinfo=timezone.utc), datetime(2026, 7, 18, 23, 59, tzinfo=timezone.utc)),
    "aug": (datetime(2026, 8, 11, tzinfo=timezone.utc), datetime(2026, 8, 15, 23, 59, tzinfo=timezone.utc)),
    "sep": (datetime(2026, 9, 8, tzinfo=timezone.utc), datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc)),
}
REGIMES = ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "NEUTRAL", "RANGE", "NO_REGIME"]


def build_req(figi, lot, sl, rr, f, t):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in LIVE_SIDS]
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "all",
        "quorum": 2, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": setups, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip", "regime_setups_filter": BOL_FILTER,
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
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()
    only = [t.strip() for t in args.tickers.split(",") if t.strip()] or None

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot, i.optuna_params FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            AND NOT (i.ticker = 'T' AND i.figi = 'BBG000BSJK37')
            ORDER BY i.ticker
        """))).fetchall()
        meta = {}
        for r in rows:
            if r[1] in meta:
                continue
            meta[r[1]] = (r[0], int(r[2]) if r[2] else 1, r[3] or {})
        if only:
            meta = {k: v for k, v in meta.items() if k in only}
        print("тикеров:", len(meta))
        for pname, (f, t) in PERIODS.items():
            tot = _blank()
            by_reg = defaultdict(_blank)
            by_strat = defaultdict(_blank)
            for tkr, (figi, lot, opt) in meta.items():
                c = await _lc(db, figi, 1, date_from=f, date_to=t)
                if not c or len(c) < 200:
                    continue
                sl = float(opt.get("sl_mult", 4.0))
                rr = float(opt.get("rr", 4.0))
                try:
                    res = compute_ensemble(c, build_req(figi, lot, sl, rr, f, t))
                except Exception:
                    continue
                if "error" in res:
                    continue
                st = res.get("static", {})
                tl = RegimeDetector().compute(resample(c, 300) or []) or []
                eps = (st.get("episodes") or {}).get("list", [])
                qmap = {q["event_id"]: (q.get("members_for") or []) for q in (st.get("quorum_list") or [])}
                for ep in eps:
                    if ep.get("status") != "TRADED":
                        continue
                    net = ep.get("net_sum", ep.get("net") or 0.0) or 0.0
                    et = ep.get("entry_time")
                    r = regime_at(tl, datetime.fromisoformat(et)) if (et and tl) else None
                    reg = r["state"] if r else "NO_REGIME"
                    _add(tot, net)
                    _add(by_reg[reg], net)
                    for sid in (qmap.get(ep.get("quorum_event_id"), []) or ["—"]):
                        _add(by_strat[str(sid)], net)
            wr, pf = _fmt(tot)
            print("\n===== %s  trades=%d  net=%+.0f  WR=%.1f%%  PF=%.2f =====" % (
                pname, tot["trades"], tot["net"], wr, pf))
            print("  режимы:  " + "  ".join("%s:%+.0f(%d)" % (r, by_reg[r]["net"], by_reg[r]["trades"]) for r in REGIMES if r in by_reg))
            print("  голоса:  " + "  ".join("%s:%+.0f(%d)" % (s, b["net"], b["trades"]) for s, b in sorted(by_strat.items(), key=lambda kv: -kv[1]["net"])))


if __name__ == "__main__":
    asyncio.run(main())
