#!/usr/bin/env python3
"""Top-10 main+flip: разбивка сделок по exit_reason."""
import asyncio, os, sys
from datetime import datetime, timezone
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble

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

TOP10 = ["SMLT", "NLMK", "ASTR", "MAGN", "GMKN", "NVTK", "SNGSP", "GAZP", "LENT", "CHMF"]
COMMISSION = 0.0005


def base_req(figi, capital, lot):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "main", "quorum": 2,
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": COMMISSION, "slippage_bps": 2.0,
        "capital": capital, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
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


async def main():
    eligible = await get_eligible()
    by_ticker = {t: (f, l) for f, t, l in eligible}
    candles_map = {}
    for ticker in TOP10:
        if ticker not in by_ticker:
            continue
        figi, lot = by_ticker[ticker]
        candles = await load_august(figi)
        candles_map[ticker] = (figi, candles, lot)
        print("loaded %s (%d)" % (ticker, len(candles)))

    grand = Counter()
    grand_net = {}
    per_ticker = {}
    for ticker, (figi, candles, lot) in candles_map.items():
        res = compute_ensemble(candles, base_req(figi, 10000, lot))
        trades = res.get("static", {}).get("trades", []) if "error" not in res else []
        c = Counter()
        net_by = {}
        for t in trades:
            r = t.get("exit_reason", "?")
            c[r] += 1
            net_by[r] = net_by.get(r, 0.0) + t.get("net", 0)
        per_ticker[ticker] = {"total": len(trades), "by_reason": dict(c), "net_by": net_by}
        grand.update(c)
        for r, n in net_by.items():
            grand_net[r] = grand_net.get(r, 0.0) + n

    print()
    print("=" * 100)
    print("  Разбивка по exit_reason — Top-10, main, flip=2, commission 0.05%")
    print("=" * 100)
    print("  %-8s %6s | %10s %10s %10s %10s %10s" % (
        "Ticker", "Всего", "SL", "TP", "Flip(sig)", "Session", "EndData"))
    print("  " + "-" * 78)
    for ticker in TOP10:
        d = per_ticker.get(ticker)
        if not d:
            continue
        b = d["by_reason"]
        print("  %-8s %6d | %10d %10d %10d %10d %10d" % (
            ticker, d["total"],
            b.get("stop_loss", 0), b.get("target", 0),
            b.get("signal_exit", 0), b.get("session_close", 0), b.get("end_of_data", 0)))

    total = sum(grand.values())
    print("  " + "-" * 78)
    print("  %-8s %6d | %10d %10d %10d %10d %10d" % (
        "TOTAL", total,
        grand.get("stop_loss", 0), grand.get("target", 0),
        grand.get("signal_exit", 0), grand.get("session_close", 0), grand.get("end_of_data", 0)))
    print()
    print("  Net по причинам:")
    print("    stop_loss    : %+12.2f (%d)" % (grand_net.get("stop_loss", 0), grand.get("stop_loss", 0)))
    print("    target       : %+12.2f (%d)" % (grand_net.get("target", 0), grand.get("target", 0)))
    print("    signal_exit  : %+12.2f (%d)   <- flip-перевороты" % (grand_net.get("signal_exit", 0), grand.get("signal_exit", 0)))
    print("    session_close: %+12.2f (%d)" % (grand_net.get("session_close", 0), grand.get("session_close", 0)))
    print("    end_of_data  : %+12.2f (%d)" % (grand_net.get("end_of_data", 0), grand.get("end_of_data", 0)))
    print()
    sl_tp = grand.get("stop_loss", 0) + grand.get("target", 0)
    print("  Сделок по SL/TP: %d (%.1f%%)  |  Flip-переворотов: %d (%.1f%%)" % (
        sl_tp, sl_tp / total * 100, grand.get("signal_exit", 0), grand.get("signal_exit", 0) / total * 100))


if __name__ == "__main__":
    asyncio.run(main())
