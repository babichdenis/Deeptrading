#!/usr/bin/env python3
"""Сравнение: 5x10k independent vs 50k pool (20% each)."""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal

FIGIS = {
    "BBG008F2T3T2": "SBER",
    "BBG004S681M2": "GAZP",
    "BBG004S683W7": "LKOH",
    "BBG004S68CP5": "ROSN",
    "BBG004S681B4": "RUAL",
}

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]


def base_req(figi, capital, lot=10):
    setups = [{"strategy_id": sid, "tf": "5min", "params": {}} for sid in ALL_SIDS]
    return {
        "figi": figi,
        "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "main",
        "quorum": 2,
        "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True,
        "opposite_hold": False,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
        "commission_rate": 0.0005,
        "slippage_bps": 2.0,
        "capital": capital,
        "lot": lot,
        "setups": setups,
        "use_all_setups": False,
        "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(),
        "to_ts": DATE_TO.isoformat(),
    }


async def load_august(figi):
    from app.services.signals import _load_candles as _lc
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


async def main():
    from app.services.ensemble import compute_ensemble

    print("=" * 70)
    print("TEST: 50k pool = 5x10k independent?")
    print("=" * 70)

    # Load candles once
    candles_map = {}
    for figi, ticker in FIGIS.items():
        candles = await load_august(figi)
        candles_map[figi] = candles
        print("  %s: %d candles" % (ticker, len(candles)))

    # Test 1: Each stock gets 10k independently (5 x 10k = 50k total)
    print("\n--- INDEPENDENT: 10k per stock ---")
    ind_results = {}
    for figi, ticker in FIGIS.items():
        req = base_req(figi, 10000)
        res = compute_ensemble(candles_map[figi], req)
        if "error" in res:
            print("  %s: ERROR %s" % (ticker, res["error"]))
            continue
        trades = res["static"].get("trades", [])
        net = sum(t["net"] for t in trades)
        ind_results[ticker] = net
        print("  %s: %d trades net=%+.2f" % (ticker, len(trades), net))

    ind_total = sum(ind_results.values())
    print("  TOTAL: %+.2f (equity: %.2f)" % (ind_total, 50000 + ind_total))

    # Test 2: Pool 50k, 20% per stock = 10k each
    print("\n--- POOL: 50k total, 20% per stock ---")
    pool_results = {}
    for figi, ticker in FIGIS.items():
        req = base_req(figi, 50000 * 0.2)  # 20% of 50k = 10k
        res = compute_ensemble(candles_map[figi], req)
        if "error" in res:
            print("  %s: ERROR %s" % (ticker, res["error"]))
            continue
        trades = res["static"].get("trades", [])
        net = sum(t["net"] for t in trades)
        pool_results[ticker] = net
        print("  %s: %d trades net=%+.2f" % (ticker, len(trades), net))

    pool_total = sum(pool_results.values())
    print("  TOTAL: %+.2f (equity: %.2f)" % (pool_total, 50000 + pool_total))

    # Compare
    print("\n--- СРАВНЕНИЕ ---")
    for ticker in FIGIS.values():
        i = ind_results.get(ticker, 0)
        p = pool_results.get(ticker, 0)
        diff = abs(i - p)
        print("  %s: independent=%+.2f pool=%+.2f diff=%.2f" % (ticker, i, p, diff))

    print("\n  TOTAL: independent=%+.2f pool=%+.2f diff=%.2f" % (ind_total, pool_total, abs(ind_total - pool_total)))


if __name__ == "__main__":
    asyncio.run(main())
