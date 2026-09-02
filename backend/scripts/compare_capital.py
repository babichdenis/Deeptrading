#!/usr/bin/env python3
"""Сравнение 10000 vs 2000 на 5 акциях (август 2026). Правильные FIGIs."""
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal

FIGIS = {
    "BBG004730N88": "SBER",  # lot=1
    "BBG004730RP0": "GAZP",  # lot=10
    "BBG004731032": "LKOH",  # lot=1
    "BBG004731354": "ROSN",  # lot=1
    "BBG008F2T3T2": "RUAL",  # lot=10
}

LOTS = {
    "BBG004730N88": 1,  # SBER
    "BBG004730RP0": 10, # GAZP
    "BBG004731032": 1,  # LKOH
    "BBG004731354": 1,  # ROSN
    "BBG008F2T3T2": 10, # RUAL
}

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]


def base_req(figi, capital):
    lot = LOTS.get(figi, 10)
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
    print("СРАВНЕНИЕ: 10000 vs 2000 (правильные FIGIs, август 2026)")
    print("=" * 70)

    # Load candles once
    candles_map = {}
    for figi, ticker in FIGIS.items():
        candles = await load_august(figi)
        candles_map[figi] = candles
        print("  %s: %d candles, lot=%d" % (ticker, len(candles), LOTS[figi]))

    for capital in [10000, 2000]:
        print("\n" + "=" * 70)
        print("КАПИТАЛ: %d" % capital)
        print("=" * 70)

        all_trades = []
        for figi, ticker in FIGIS.items():
            candles = candles_map[figi]
            req = base_req(figi, capital)
            res = compute_ensemble(candles, req)

            if "error" in res:
                print("  %s: ERROR %s" % (ticker, res["error"]))
                continue

            trades = res["static"].get("trades", [])
            econ = res["static"].get("economic", {})
            net = econ.get("net", 0)
            lot = LOTS[figi]
            all_trades.extend(trades)

            wins = sum(1 for t in trades if t["net"] > 0)
            wr = wins / max(1, len(trades)) * 100

            print("  %s (lot=%d): %d trades net=%+.2f wr=%.0f%%" % (
                ticker, lot, len(trades), net, wr))

            if trades:
                t0 = trades[0]
                qty = t0.get("qty", 0)
                print("    first: entry=%.2f qty=%d cost=%.0f net=%+.2f" % (
                    t0.get("entry_price", 0), qty, t0.get("entry_price", 0) * qty, t0["net"]))

        total = sum(t["net"] for t in all_trades)
        wins = sum(1 for t in all_trades if t["net"] > 0)
        gp = abs(sum(t["net"] for t in all_trades if t["net"] > 0))
        gn = abs(sum(t["net"] for t in all_trades if t["net"] < 0))
        pf = gp / gn if gn else float("inf")

        print("\n  ИТОГО: trades=%d net=%+.2f wr=%.1f%% pf=%.2f" % (
            len(all_trades), total, wins/max(1,len(all_trades))*100, pf))
        print("  Equity: %d + %.2f = %.2f" % (capital, total, capital + total))


if __name__ == "__main__":
    asyncio.run(main())
