#!/usr/bin/env python3
"""Series 5.1: NEUTRAL-gate + semi-flip test.

5 тикеров, 10K портфель, без маржи, с автонакоплением.
3 варианта:
  A: baseline (flip=2, все режимы)
  B: NEUTRAL gate (flip=0 в NEUTRAL, flip=2 в HV/TREND)
  C: NEUTRAL semi-flip (закрытие без входа в NEUTRAL)
"""
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

# 5 тикеров с примерно равной ценой (100-200₽), лот=1
TICKERS_5 = ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]


def base_req(figi, lot, commission, neutral_mode=None):
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
        "commission_rate": commission,
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


def run_portfolio(candles_map, label, neutral_mode=None, initial_capital=10000):
    """Portfolio backtest: 10K total, distribute by price, accumulate."""
    commission = 0.0005
    capital = initial_capital
    all_trades = []

    # Распределяем капитал поровну
    per_ticker_capital = capital / len(candles_map)

    for ticker, (figi, candles, lot) in candles_map.items():
        res = compute_ensemble(candles, base_req(figi, lot, commission, neutral_mode))
        trades = res.get("static", {}).get("trades", []) if "error" not in res else []
        all_trades.extend([(ticker, t) for t in trades])

        # Автонакопление: реинвестируем прибыль
        ticker_net = sum(t.get("net", 0) for t in trades)
        capital += ticker_net

    # Агрегируем
    wins = [t for _, t in all_trades if t.get("net", 0) > 0]
    losses = [t for _, t in all_trades if t.get("net", 0) < 0]
    gross_win = sum(t["net"] for t in wins)
    gross_loss = sum(t["net"] for t in losses)
    net = sum(t["net"] for _, t in all_trades)
    comm = sum(t.get("commission", 0) for _, t in all_trades)
    wr = len(wins) / len(all_trades) * 100 if all_trades else 0
    pf = gross_win / abs(gross_loss) if gross_loss else 0

    print()
    print("=" * 100)
    print("  " + label)
    print("=" * 100)
    print("  Initial: %d | Final: %.0f | Net: %+.0f | Trades: %d | WR: %.1f%% | PF: %.2f" % (
        initial_capital, capital, net, len(all_trades), wr, pf))
    print("  Gross Win: %+.0f | Gross Loss: %+.0f | Commission: %.0f" % (
        gross_win, gross_loss, comm))

    # По тикерам
    print()
    print("  %-8s %7s %6s %6s %8s %12s %12s" % ("Ticker", "Trades", "Wins", "Loss", "WR%", "Gross", "Net"))
    print("  " + "-" * 70)
    by_ticker = {}
    for ticker, t in all_trades:
        by_ticker.setdefault(ticker, {"trades": 0, "wins": 0, "losses": 0, "gross": 0.0, "net": 0.0})
        by_ticker[ticker]["trades"] += 1
        by_ticker[ticker]["net"] += t.get("net", 0)
        by_ticker[ticker]["gross"] += t.get("net", 0)
        if t.get("net", 0) > 0:
            by_ticker[ticker]["wins"] += 1
        else:
            by_ticker[ticker]["losses"] += 1
    for ticker in sorted(by_ticker, key=lambda x: -by_ticker[x]["net"]):
        r = by_ticker[ticker]
        wr_t = r["wins"] / r["trades"] * 100 if r["trades"] else 0
        print("  %-8s %7d %6d %6d %5.1f%% %+12.0f %+12.0f" % (
            ticker, r["trades"], r["wins"], r["losses"], wr_t, r["gross"], r["net"]))
    print()

    # Regime breakdown
    regime_stats = {}
    for ticker, t in all_trades:
        r = t.get("regime", "UNKNOWN")
        regime_stats.setdefault(r, {"n": 0, "net": 0.0, "wins": 0})
        regime_stats[r]["n"] += 1
        regime_stats[r]["net"] += t.get("net", 0)
        if t.get("net", 0) > 0:
            regime_stats[r]["wins"] += 1
    print("  Regime breakdown:")
    for r_name in ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]:
        if r_name in regime_stats:
            rs = regime_stats[r_name]
            wr_r = rs["wins"] / rs["n"] * 100 if rs["n"] else 0
            net_t = rs["net"] / rs["n"] if rs["n"] else 0
            print("    %-18s %5d trades  net=%+10.0f  net/t=%+6.0f  WR=%5.1f%%" % (
                r_name, rs["n"], rs["net"], net_t, wr_r))

    return {"trades": len(all_trades), "wr": wr, "net": net, "pf": pf,
            "gross_win": gross_win, "gross_loss": gross_loss, "capital": capital}


async def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    eligible = await get_eligible()
    by_ticker = {t: (f, l) for f, t, l in eligible}

    print("Loading candles for 5 tickers...")
    candles_map = {}
    for ticker in TICKERS_5:
        if ticker not in by_ticker:
            print("  %s: NOT in eligible, skip" % ticker); continue
        figi, lot = by_ticker[ticker]
        try:
            candles = await load_august(figi)
            candles_map[ticker] = (figi, candles, lot)
            # Показываем цену и лот
            px = candles[-1].close if candles else 0
            print("  %s: %d candles, price=%.0f, lot=%d, position_cost=%.0f" % (
                ticker, len(candles), px, lot, px * lot))
        except Exception as e:
            print("  %s: ERROR %s" % (ticker, e))

    if not candles_map:
        print("No tickers loaded!"); return

    results = []

    if mode in ("all", "baseline"):
        results.append(run_portfolio(candles_map,
            "A: Baseline (flip=2, all regimes)", neutral_mode=None))

    if mode in ("all", "gate"):
        results.append(run_portfolio(candles_map,
            "B: NEUTRAL gate (flip=0 in NEUTRAL)", neutral_mode="gate"))

    if mode in ("all", "semi"):
        results.append(run_portfolio(candles_map,
            "C: NEUTRAL semi-flip (close without re-entry)", neutral_mode="semi_flip"))

    if len(results) >= 2:
        print()
        print("=" * 100)
        print("  COMPARISON")
        print("=" * 100)
        labels = ["Baseline", "Gate", "Semi-flip"]
        print("  %-15s %7s %6s %10s %10s %6s" % ("Variant", "Trades", "WR%", "Net", "PF", "Capital"))
        print("  " + "-" * 60)
        for i, r in enumerate(results):
            print("  %-15s %7d %5.1f%% %+10.0f %6.2f %10.0f" % (
                labels[i], r["trades"], r["wr"], r["net"], r["pf"], r["capital"]))


if __name__ == "__main__":
    asyncio.run(main())
