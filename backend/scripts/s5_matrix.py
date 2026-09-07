#!/usr/bin/env python3
"""Series 5 Matrix: 6 tests with volume filter + semi/full flip + per-regime quorum.

Tests:
  1. Semi-flip + Volume (baseline quorum)
  2. Full flip + Volume (baseline quorum)
  3. Semi-flip + Volume + Gate2 quorum
  4. Full flip + Volume + Gate2 quorum
  5. Semi-flip + Volume + My quorum
  6. Full flip + Volume + My quorum
"""
import asyncio, os, sys, json
from datetime import datetime, timezone
from collections import defaultdict

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

TICKERS_5 = ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]

# Per-regime quorum variants
GATE2_QUORUM = {
    "NEUTRAL": ["bollinger_reclaim", "rsi_reversal", "vwap_reclaim"],
    "HIGH_VOLATILITY": ALL_SIDS,
    "TREND_UP": ["pullback_ema", "macd_cross", "donchian_breakout", "rsi_reversal"],
    "TREND_DOWN": ["pullback_ema", "macd_cross", "donchian_breakout", "rsi_reversal"],
    "RANGE": ALL_SIDS,
}

MY_QUORUM = {
    "NEUTRAL": ["bollinger_reclaim", "rsi_reversal", "vwap_reclaim"],
    "HIGH_VOLATILITY": ALL_SIDS,
    "TREND_UP": ALL_SIDS,
    "TREND_DOWN": ALL_SIDS,
    "RANGE": ALL_SIDS,
}


def base_req(figi, lot, neutral_mode, volume_thr=1.0, per_regime_quorum=None):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    req = {
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
        "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005,
        "slippage_bps": 2.0,
        "capital": 10000,
        "lot": lot,
        "setups": setups,
        "use_all_setups": False,
        "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(),
        "to_ts": DATE_TO.isoformat(),
    }
    if neutral_mode:
        req["neutral_mode"] = neutral_mode
    if volume_thr is not None:
        req["volume_filter_threshold"] = volume_thr
    if per_regime_quorum is not None:
        req["per_regime_quorum"] = per_regime_quorum
    return req


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


def run_test(candles_map, label, neutral_mode, volume_thr=1.0, per_regime_quorum=None):
    commission = 0.0005
    capital = 10000.0
    all_trades = []
    vol_rejected = 0

    for ticker, (figi, candles, lot) in candles_map.items():
        res = compute_ensemble(candles, base_req(figi, lot, neutral_mode, volume_thr, per_regime_quorum))
        static = res.get("static", {})
        trades = static.get("trades", [])
        all_trades.extend([(ticker, t) for t in trades])
        capital += sum(t.get("net", 0) for t in trades)
        # Count volume rejections
        for r in static.get("rejected", []):
            if "VOL_FILTER" in r.get("reason", ""):
                vol_rejected += 1

    if not all_trades:
        return {"label": label, "trades": 0, "net": 0, "wr": 0, "pf": 0, "capital": 10000}

    wins = [t for _, t in all_trades if t.get("net", 0) > 0]
    losses = [t for _, t in all_trades if t.get("net", 0) <= 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = sum(t["net"] for t in losses)
    net = sum(t["net"] for _, t in all_trades)
    wr = len(wins) / len(all_trades) * 100 if all_trades else 0
    pf = gross_win / abs(gross_loss) if gross_loss else 0

    # Regime breakdown
    regime_stats = defaultdict(lambda: {"n": 0, "net": 0.0, "wins": 0})
    for _, t in all_trades:
        r = t.get("regime", "UNKNOWN")
        regime_stats[r]["n"] += 1
        regime_stats[r]["net"] += t.get("net", 0)
        if t.get("net", 0) > 0:
            regime_stats[r]["wins"] += 1

    # Per-ticker
    by_ticker = defaultdict(lambda: {"n": 0, "net": 0.0, "wins": 0})
    for tk, t in all_trades:
        by_ticker[tk]["n"] += 1
        by_ticker[tk]["net"] += t.get("net", 0)
        if t.get("net", 0) > 0:
            by_ticker[tk]["wins"] += 1

    print()
    print("=" * 120)
    print("  " + label)
    print("=" * 120)
    print("  Initial: 10000 | Final: %.0f | Net: %+.0f | Trades: %d | WR: %.1f%% | PF: %.2f | Vol_rejected: %d" % (
        capital, net, len(all_trades), wr, pf, vol_rejected))
    print("  Gross Win: %+.0f | Gross Loss: %+.0f" % (gross_win, gross_loss))

    # Per-ticker
    print()
    print("  %-8s %5s %5s %5s %6s %10s" % ("Ticker", "N", "Win", "Loss", "WR%", "Net"))
    print("  " + "-" * 50)
    for tk in sorted(by_ticker, key=lambda x: -by_ticker[x]["net"]):
        d = by_ticker[tk]
        tk_wr = d["wins"] / d["n"] * 100 if d["n"] else 0
        print("  %-8s %5d %5d %5d %5.1f%% %+10.0f" % (
            tk, d["n"], d["wins"], d["n"] - d["wins"], tk_wr, d["net"]))

    # Regime
    print()
    print("  %-18s %5s %6s %10s %7s" % ("Regime", "N", "WR%", "Net", "Net/t"))
    print("  " + "-" * 55)
    for r_name in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]:
        if r_name in regime_stats:
            rs = regime_stats[r_name]
            rs_wr = rs["wins"] / rs["n"] * 100 if rs["n"] else 0
            rs_nt = rs["net"] / rs["n"] if rs["n"] else 0
            print("  %-18s %5d %5.1f%% %+10.0f %+7.0f" % (
                r_name, rs["n"], rs_wr, rs["net"], rs_nt))

    return {"label": label, "trades": len(all_trades), "net": net, "wr": wr, "pf": pf,
            "gross_win": gross_win, "gross_loss": gross_loss, "capital": capital,
            "vol_rejected": vol_rejected}


async def main():
    eligible = await get_eligible()
    by_ticker = {t: (f, l) for f, t, l in eligible}

    print("Loading candles...")
    candles_map = {}
    for ticker in TICKERS_5:
        if ticker not in by_ticker:
            print("  %s: NOT in eligible" % ticker); continue
        figi, lot = by_ticker[ticker]
        candles = await load_august(figi)
        candles_map[ticker] = (figi, candles, lot)
        px = candles[-1].close if candles else 0
        print("  %s: %d candles, price=%.0f, lot=%d" % (ticker, len(candles), px, lot))

    if not candles_map:
        print("No tickers!"); return

    results = []

    # Test 1: Semi-flip + Volume + baseline quorum
    results.append(run_test(candles_map,
        "1: Semi-flip + Volume (baseline quorum)",
        neutral_mode="semi_flip", volume_thr=1.0))

    # Test 2: Full flip + Volume + baseline quorum
    results.append(run_test(candles_map,
        "2: Full flip + Volume (baseline quorum)",
        neutral_mode=None, volume_thr=1.0))

    # Test 3: Semi-flip + Volume + Gate2 quorum
    results.append(run_test(candles_map,
        "3: Semi-flip + Volume + Gate2 quorum",
        neutral_mode="semi_flip", volume_thr=1.0, per_regime_quorum=GATE2_QUORUM))

    # Test 4: Full flip + Volume + Gate2 quorum
    results.append(run_test(candles_map,
        "4: Full flip + Volume + Gate2 quorum",
        neutral_mode=None, volume_thr=1.0, per_regime_quorum=GATE2_QUORUM))

    # Test 5: Semi-flip + Volume + My quorum
    results.append(run_test(candles_map,
        "5: Semi-flip + Volume + My quorum",
        neutral_mode="semi_flip", volume_thr=1.0, per_regime_quorum=MY_QUORUM))

    # Test 6: Full flip + Volume + My quorum
    results.append(run_test(candles_map,
        "6: Full flip + Volume + My quorum",
        neutral_mode=None, volume_thr=1.0, per_regime_quorum=MY_QUORUM))

    # Summary
    print()
    print("=" * 120)
    print("  SUMMARY")
    print("=" * 120)
    print("  %-50s %6s %10s %6s %5s %6s" % ("Test", "Trades", "Net", "WR%", "PF", "VolRej"))
    print("  " + "-" * 95)
    for r in results:
        print("  %-50s %6d %+10.0f %5.1f%% %5.2f %6d" % (
            r["label"], r["trades"], r["net"], r["wr"], r["pf"], r["vol_rejected"]))

    # Save
    out = os.path.join(os.path.dirname(__file__), "..", "s5_matrix_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print()
    print("  Saved to %s" % out)


if __name__ == "__main__":
    asyncio.run(main())
