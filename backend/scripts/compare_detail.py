#!/usr/bin/env python3
"""Детальное сравнение 10000 vs 2000 по каждой акции."""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal

FIGIS = {
    "BBG004730N88": ("SBER", 1, 250),   # ticker, lot, approx_price
    "BBG004730RP0": ("GAZP", 10, 150),
    "BBG004731032": ("LKOH", 1, 7000),
    "BBG004731354": ("ROSN", 1, 500),
    "BBG008F2T3T2": ("RUAL", 10, 40),
}

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]


def base_req(figi, capital, lot):
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


def qty_shares_formula(capital, price, lot):
    """Та же формула что в ensemble.py"""
    return max(int(capital / (price * lot)) * lot, lot)


async def main():
    from app.services.ensemble import compute_ensemble

    print("=" * 90)
    print("ДЕТАЛЬНОЕ СРАВНЕНИЕ: 10000 vs 2000")
    print("=" * 90)

    # Load candles
    candles_map = {}
    for figi, (ticker, lot, approx) in FIGIS.items():
        candles = await load_august(figi)
        candles_map[figi] = candles
        # Get actual first candle price
        if candles:
            first_price = candles[0].open
        else:
            first_price = approx
        print("\n%s: lot=%d, candles=%d, first_open=%.2f" % (ticker, lot, len(candles), first_price))

    # Run both capital scenarios
    results = {}
    for figi, (ticker, lot, approx) in FIGIS.items():
        candles = candles_map[figi]
        first_price = candles[0].open if candles else approx

        res_10k = compute_ensemble(candles, base_req(figi, 10000, lot))
        res_2k = compute_ensemble(candles, base_req(figi, 2000, lot))

        trades_10k = res_10k["static"].get("trades", []) if "error" not in res_10k else []
        trades_2k = res_2k["static"].get("trades", []) if "error" not in res_2k else []

        net_10k = sum(t["net"] for t in trades_10k)
        net_2k = sum(t["net"] for t in trades_2k)

        # Qty sizing
        qty_10k = qty_shares_formula(10000, first_price, lot)
        qty_2k = qty_shares_formula(2000, first_price, lot)
        cost_10k = qty_10k * first_price
        cost_2k = qty_2k * first_price

        results[ticker] = {
            "lot": lot,
            "price": first_price,
            "qty_10k": qty_10k,
            "qty_2k": qty_2k,
            "cost_10k": cost_10k,
            "cost_2k": cost_2k,
            "trades_10k": len(trades_10k),
            "trades_2k": len(trades_2k),
            "net_10k": net_10k,
            "net_2k": net_2k,
        }

    # Detailed table
    print("\n" + "=" * 90)
    print("ТАБЛИЦА СРАВНЕНИЯ")
    print("=" * 90)
    print("%-6s %8s %8s | %8s %8s %8s | %8s %8s %8s" % (
        "Ticker", "Price", "Lot",
        "10K qty", "10K cost", "10K net",
        "2K qty", "2K cost", "2K net"))
    print("-" * 90)

    for ticker, d in results.items():
        print("%-6s %8.2f %8d | %8d %8.0f %+.2f | %8d %8.0f %+.2f" % (
            ticker, d["price"], d["lot"],
            d["qty_10k"], d["cost_10k"], d["net_10k"],
            d["qty_2k"], d["cost_2k"], d["net_2k"]))

    # Summary
    print("-" * 90)
    total_net_10k = sum(d["net_10k"] for d in results.values())
    total_net_2k = sum(d["net_2k"] for d in results.values())
    total_trades_10k = sum(d["trades_10k"] for d in results.values())
    total_trades_2k = sum(d["trades_2k"] for d in results.values())

    print("%-6s %8s %8s | %8s %8s %+.2f | %8s %8s %+.2f" % (
        "TOTAL", "", "",
        "", "", total_net_10k,
        "", "", total_net_2k))

    print("\nИТОГО:")
    print("  10000: trades=%d net=%+.2f (%.1f%%)" % (
        total_trades_10k, total_net_10k, total_net_10k/10000*100))
    print("  2000:  trades=%d net=%+.2f (%.1f%%)" % (
        total_trades_2k, total_net_2k, total_net_2k/2000*100))
    print("  ratio: net %.2fx (vs 5x capital)" % (total_net_10k/total_net_2k if total_net_2k else 0))


if __name__ == "__main__":
    asyncio.run(main())
