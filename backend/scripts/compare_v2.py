#!/usr/bin/env python3
"""Сравнение 10000 vs 2000 на V2 параметрах (август 2026)."""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal

FIGIS = {
    "BBG004730N88": ("SBER", 1),
    "BBG004730RP0": ("GAZP", 10),
    "BBG004731032": ("LKOH", 1),
    "BBG004731354": ("ROSN", 1),
    "BBG008F2T3T2": ("RUAL", 10),
}

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


def base_req(figi, capital, lot):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
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


def qty_formula(capital, price, lot):
    return max(int(capital / (price * lot)) * lot, lot)


async def main():
    from app.services.ensemble import compute_ensemble

    print("=" * 90)
    print("СРАВНЕНИЕ 10000 vs 2000 — V2 PARAMS — август 2026")
    print("=" * 90)

    candles_map = {}
    for figi, (ticker, lot) in FIGIS.items():
        candles = await load_august(figi)
        candles_map[figi] = candles
        print("  %s: lot=%d, candles=%d, first_open=%.2f" % (ticker, lot, len(candles), candles[0].open if candles else 0))

    results = {}
    for figi, (ticker, lot) in FIGIS.items():
        candles = candles_map[figi]
        first_price = candles[0].open if candles else 0

        res_10k = compute_ensemble(candles, base_req(figi, 10000, lot))
        res_2k = compute_ensemble(candles, base_req(figi, 2000, lot))

        trades_10k = res_10k["static"].get("trades", []) if "error" not in res_10k else []
        trades_2k = res_2k["static"].get("trades", []) if "error" not in res_2k else []

        net_10k = sum(t["net"] for t in trades_10k)
        net_2k = sum(t["net"] for t in trades_2k)

        qty_10k = qty_formula(10000, first_price, lot)
        qty_2k = qty_formula(2000, first_price, lot)

        results[ticker] = {
            "lot": lot, "price": first_price,
            "qty_10k": qty_10k, "qty_2k": qty_2k,
            "trades_10k": len(trades_10k), "trades_2k": len(trades_2k),
            "net_10k": net_10k, "net_2k": net_2k,
        }

    print("\n%s %8s %8s | %8s %8s | %8s %8s" % (
        "Ticker", "Price", "Lot", "10K qty", "10K net", "2K qty", "2K net"))
    print("-" * 80)

    for ticker, d in results.items():
        print("%-6s %8.2f %8d | %8d %+10.2f | %8d %+10.2f" % (
            ticker, d["price"], d["lot"],
            d["qty_10k"], d["net_10k"],
            d["qty_2k"], d["net_2k"]))

    total_10k = sum(d["net_10k"] for d in results.values())
    total_2k = sum(d["net_2k"] for d in results.values())
    trades_10k = sum(d["trades_10k"] for d in results.values())
    trades_2k = sum(d["trades_2k"] for d in results.values())

    print("-" * 80)
    print("TOTAL %8s %8s | %8s %+10.2f | %8s %+10.2f" % ("", "", "", total_10k, "", total_2k))
    print("\n10000: trades=%d net=%+.2f (%.1f%%)" % (trades_10k, total_10k, total_10k/10000*100))
    print("2000:  trades=%d net=%+.2f (%.1f%%)" % (trades_2k, total_2k, total_2k/2000*100))
    print("ratio: %.2fx (vs 5x capital)" % (total_10k/total_2k if total_2k else 0))


if __name__ == "__main__":
    asyncio.run(main())
