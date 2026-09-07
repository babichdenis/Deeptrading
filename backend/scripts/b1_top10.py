#!/usr/bin/env python3
"""Top-10 WR% tickers: confirm_flip=2, session=all, commission=0.03%."""
import asyncio, os, sys
from datetime import datetime, timezone

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

# Top-10 by WR% from previous 0.05% test
TOP10 = ["SMLT", "NLMK", "ASTR", "MAGN", "GMKN", "NVTK", "SNGSP", "GAZP", "LENT", "CHMF"]


def base_req(figi, capital, lot, commission):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    return {
        "figi": figi,
        "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "all",
        "quorum": 2,
        "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True,
        "opposite_hold": False,
        "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": commission,
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


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot
            FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            ORDER BY i.ticker
        """))).fetchall()
    return [(r.figi, r.ticker, r.lot) for r in rows]


def run_variant(candles_map, eligible, label, commission):
    results = []
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_gross_win = 0.0
    total_gross_loss = 0.0
    total_net = 0.0
    total_commission = 0.0

    for figi, ticker, lot in eligible:
        if ticker not in TOP10:
            continue
        if figi not in candles_map:
            continue
        candles = candles_map[figi]
        if not candles:
            continue
        res = compute_ensemble(candles, base_req(figi, 10000, lot, commission))
        trades = res.get("static", {}).get("trades", []) if "error" not in res else []
        wins = [t for t in trades if t.get("net", 0) > 0]
        losses = [t for t in trades if t.get("net", 0) < 0]
        gross_win = sum(t["net"] for t in wins)
        gross_loss = sum(t["net"] for t in losses)
        comm = sum(t.get("commission", 0) for t in trades)
        net = sum(t["net"] for t in trades)
        wr = len(wins) / len(trades) * 100 if trades else 0

        results.append({
            "ticker": ticker, "figi": figi, "lot": lot,
            "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "gross_win": gross_win, "gross_loss": gross_loss, "commission": comm,
            "net": net, "wr": wr,
        })

        total_trades += len(trades)
        total_wins += len(wins)
        total_losses += len(losses)
        total_gross_win += gross_win
        total_gross_loss += gross_loss
        total_commission += comm
        total_net += net

    total_wr = total_wins / total_trades * 100 if total_trades else 0
    pf = total_gross_win / abs(total_gross_loss) if total_gross_loss else 0
    avg_win = total_gross_win / total_wins if total_wins else 0
    avg_loss = total_gross_loss / total_losses if total_losses else 0

    print()
    print("=" * 120)
    print("  " + label)
    print("  Commission: %.2f%%  |  ATR: 4.0/4.0  |  Session: ALL  |  Confirm_flip: 2  |  Capital: 10K per ticker  |  Aug 2026" % (commission * 100))
    print("=" * 120)
    print("  %-8s %7s %6s %6s %6s %12s %12s %12s %12s %8s" % (
        "Ticker", "Trades", "Wins", "Loss", "WR%", "Gross Win", "Gross Loss", "Commission", "Net", "ROI%"))
    print("  " + "-" * 115)

    for r in results:
        roi = r["net"] / 10000 * 100
        print("  %-8s %7d %6d %6d %5.1f%% %+12.2f %+12.2f %+12.2f %+12.2f %+6.1f%%" % (
            r["ticker"], r["trades"], r["wins"], r["losses"], r["wr"],
            r["gross_win"], r["gross_loss"], r["commission"], r["net"], roi))

    print("  " + "-" * 115)
    total_roi = total_net / (10000 * len(results)) * 100 if results else 0
    print("  %-8s %7d %6d %6d %5.1f%% %+12.2f %+12.2f %+12.2f %+12.2f %+6.1f%%" % (
        "TOTAL", total_trades, total_wins, total_losses, total_wr,
        total_gross_win, total_gross_loss, total_commission, total_net, total_roi))
    print()
    print("  PF: %.2f  |  Avg Win: %+.2f  |  Avg Loss: %+.2f  |  Commission: %.2f RUB" % (
        pf, avg_win, avg_loss, total_commission))
    print("  Commission eats: %.1f%% of gross profit" % (total_commission / total_gross_win * 100 if total_gross_win else 0))

    return {
        "trades": total_trades, "wins": total_wins, "losses": total_losses,
        "gross_win": total_gross_win, "gross_loss": total_gross_loss,
        "commission": total_commission, "net": total_net, "pf": pf, "wr": total_wr,
    }


async def main():
    eligible = await get_eligible()
    print("Eligible: %d, Top-10 by WR%%: %s" % (len(eligible), ", ".join(TOP10)))

    candles_map = {}
    for figi, ticker, lot in eligible:
        if ticker not in TOP10:
            continue
        try:
            candles = await load_august(figi)
            candles_map[figi] = candles
            print("  %s: %d candles" % (ticker, len(candles)))
        except Exception as e:
            print("  %s: ERROR %s" % (ticker, e))

    r1 = run_variant(candles_map, eligible, "Top-10, confirm_flip=2, session=ALL, commission 0.03%", 0.0003)

    # Also show 0.05% for comparison
    r2 = run_variant(candles_map, eligible, "Top-10, confirm_flip=2, session=ALL, commission 0.05% (reference)", 0.0005)

    print()
    print("=" * 120)
    print("  COMPARISON: 0.03% vs 0.05%")
    print("=" * 120)
    print("  %-40s %7s %6s %12s %12s %6s" % ("Variant", "Trades", "WR%", "Commission", "Net", "PF"))
    print("  " + "-" * 80)
    print("  %-40s %7d %5.1f%% %+12.2f %+12.2f %6.2f" % (
        "0.03% (target)", r1["trades"], r1["wr"], r1["commission"], r1["net"], r1["pf"]))
    print("  %-40s %7d %5.1f%% %+12.2f %+12.2f %6.2f" % (
        "0.05% (reference)", r2["trades"], r2["wr"], r2["commission"], r2["net"], r2["pf"]))
    print("  %-40s %+7d %+5.1f%% %+12.2f %+12.2f %+6.2f" % (
        "SAVINGS (0.03 vs 0.05)", r2["trades"] - r1["trades"], r2["wr"] - r1["wr"],
        r2["commission"] - r1["commission"], r2["net"] - r1["net"],
        r2["pf"] - r1["pf"]))


if __name__ == "__main__":
    asyncio.run(main())
