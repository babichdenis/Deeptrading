#!/usr/bin/env python3
"""Серия 5.0 — Диагностика flip-сделок по режимам (без изменения движка).

Для каждого тикера top-10:
  - RegimeDetector (5m) на свечах августа;
  - для каждой сделки (flip/SL/TP) режим в момент ВЫХОДА;
  - breakdown по exit_reason × regime;
  - net, net/сделку, bars_held по режимам и сессиям.

Usage: python s5_diag.py [main|all]
"""
import asyncio, os, sys
from datetime import datetime, timezone
from collections import defaultdict
from statistics import mean

SESSION_MODE = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("main", "all") else "main"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble, cached_resample
from app.services.regime import RegimeDetector, regime_at

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

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
TF_SECONDS = {"1min": 60, "5min": 300, "15min": 900}

TOP10 = ["SMLT", "NLMK", "ASTR", "MAGN", "GMKN", "NVTK", "SNGSP", "GAZP", "LENT", "CHMF"]
COMMISSION = 0.0005


def base_req(figi, capital, lot, regime_tf_sec=300):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": SESSION_MODE, "quorum": 2,
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": COMMISSION, "slippage_bps": 2.0,
        "capital": capital, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "regime": {"tf": "5min"},
        "from_ts": DATE_FROM.isoformat(), "to_ts": DATE_TO.isoformat(),
    }


async def load_august(figi):
    from app.services.signals import _load_candles as _lc
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot
            FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            ORDER BY i.ticker
        """))).fetchall()
    return [(r.figi, r.ticker, r.lot) for r in rows]


def session_of(dt: datetime) -> str:
    from zoneinfo import ZoneInfo
    msk = dt.astimezone(ZoneInfo("Europe/Moscow"))
    hm = msk.hour * 60 + msk.minute
    if hm < 9 * 60 + 50:
        return "morning"
    if hm < 19 * 60:
        return "day"
    return "evening"


async def main():
    eligible = await get_eligible()
    by_ticker = {t: (f, l) for f, t, l in eligible}

    # агрегаты: regime -> stats
    def _empty_regime():
        return {"n": 0, "wins": 0, "net": 0.0, "bars": [],
                "by_exit": defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0}),
                "by_session": defaultdict(lambda: {"n": 0, "wins": 0, "net": 0.0})}
    order = ["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL"]
    agg = {st: _empty_regime() for st in order}
    per_ticker = {}

    for ticker in TOP10:
        if ticker not in by_ticker:
            print("%s: нет в eligible" % ticker); continue
        figi, lot = by_ticker[ticker]
        candles = await load_august(figi)
        print("=== %s: %d свечей ===" % (ticker, len(candles)))

        # режимный детектор на 5m
        c5 = cached_resample(candles, TF_SECONDS["5min"])
        detector = RegimeDetector()
        regime_row = detector.compute(c5)
        print("  regime_row: %d баров (5m)" % len(regime_row))

        res = compute_ensemble(candles, base_req(figi, 10000, lot))
        trades = res.get("static", {}).get("trades", []) if "error" not in res else []
        flips = [t for t in trades if t.get("exit_reason") == "signal_exit"]
        print("  trades=%d flips=%d" % (len(trades), len(flips)))

        local = {st: _empty_regime() for st in order}

        for t in trades:
            ts = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
            reg = regime_at(regime_row, ts)
            state = reg["state"] if reg else "NEUTRAL"
            net = t.get("net", 0.0)
            bars = t.get("bars_held", 0)
            sess = session_of(ts)
            exit_reason = t.get("exit_reason", "unknown")

            for target in (local[state], agg[state]):
                target["n"] += 1
                target["wins"] += 1 if net > 0 else 0
                target["net"] += net
                target["bars"].append(bars)
                target["by_exit"][exit_reason]["n"] += 1
                target["by_exit"][exit_reason]["wins"] += 1 if net > 0 else 0
                target["by_exit"][exit_reason]["net"] += net
                target["by_session"][sess]["n"] += 1
                target["by_session"][sess]["wins"] += 1 if net > 0 else 0
                target["by_session"][sess]["net"] += net

        per_ticker[ticker] = {"total": len(trades), "flips": len(flips), "by_regime": {st: dict(local[st]) for st in order}}

        # краткая строка по тикеру (flip сделки)
        parts = []
        for st in ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL"):
            d = local[st]
            if d["n"]:
                parts.append("%s:%d(%+.0f)" % (st.split("_")[-1] if st.startswith("TREND") else st,
                                               d["n"], d["net"]))
        print("  trades by regime: " + " | ".join(parts))

    # Итоговая сводка
    print()
    print("=" * 100)
    print("  СВОДКА: все сделки по режиму выхода (top-10, %s, flip=2, comm 0.05%%)" % SESSION_MODE)
    print("=" * 100)
    order = ["TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL"]
    print("  %-15s %7s %6s %12s %12s %10s %8s" % ("Regime", "N", "Wins", "Net", "Net/сделк", "bars_avg", "WR%"))
    print("  " + "-" * 75)
    total_n = 0
    total_net = 0.0
    for st in order:
        d = agg[st]
        if d["n"] == 0:
            continue
        print("  %-15s %7d %6d %+12.2f %+12.2f %10.1f %7.1f%%" % (
            st, d["n"], d["wins"], d["net"], d["net"] / d["n"],
            mean(d["bars"]) if d["bars"] else 0, d["wins"] / d["n"] * 100))
        total_n += d["n"]
        total_net += d["net"]
    print("  " + "-" * 75)
    print("  %-15s %7d %12s %+12.2f %+12.2f" % (
        "TOTAL", total_n, "", total_net, total_net / total_n if total_n else 0))

    # Breakdown по exit_reason × regime
    print()
    print("=" * 100)
    print("  EXIT REASON × REGIME BREAKDOWN")
    print("=" * 100)
    all_reasons = set()
    for st in order:
        for r in agg[st]["by_exit"]:
            all_reasons.add(r)
    all_reasons = sorted(all_reasons)

    for st in order:
        d = agg[st]
        if d["n"] == 0:
            continue
        print("  --- %s (N=%d, net=%+.0f) ---" % (st, d["n"], d["net"]))
        for r in all_reasons:
            rd = d["by_exit"][r]
            if rd["n"] == 0:
                continue
            wr = rd["wins"] / rd["n"] * 100 if rd["n"] else 0
            print("    %-20s %5d trades  WR=%5.1f%%  net=%+10.0f₽  net/t=%+8.1f₽" % (
                r, rd["n"], wr, rd["net"], rd["net"] / rd["n"]))
        print()

    # По сессиям (RANGE vs TREND)
    print("  По сессиям (RANGE vs TREND vs HV vs NEUTRAL):")
    print("  %-10s | %20s | %20s | %20s | %20s" % ("Session", "RANGE", "TREND", "HIGH_VOL", "NEUTRAL"))
    trend_states = ("TREND_UP", "TREND_DOWN")
    for sess in ("morning", "day", "evening"):
        r_d = agg["RANGE"]["by_session"][sess]
        t_n = sum(agg[s]["by_session"][sess]["n"] for s in trend_states)
        t_w = sum(agg[s]["by_session"][sess]["wins"] for s in trend_states)
        t_net = sum(agg[s]["by_session"][sess]["net"] for s in trend_states)
        hv = agg["HIGH_VOLATILITY"]["by_session"][sess]
        ne = agg["NEUTRAL"]["by_session"][sess]
        print("  %-10s | %4d/%+8.0f | %4d/%+8.0f | %4d/%+8.0f | %4d/%+8.0f" % (
            sess, r_d["n"], r_d["net"], t_n, t_net, hv["n"], hv["net"], ne["n"], ne["net"]))


if __name__ == "__main__":
    asyncio.run(main())
