#!/usr/bin/env python3
"""B1 backtest: 20 eligible tickers, Aug 2026, commission 0.3% vs 0.05%."""
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


def base_req(figi, capital, lot, atr_mult, atr_rr, commission):
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
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": atr_mult, "risk_reward": atr_rr}},
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


def run_variant(candles_map, eligible, label, atr_mult, atr_rr, commission):
    results = []
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_gross_win = 0.0
    total_gross_loss = 0.0
    total_net = 0.0
    total_commission = 0.0

    for figi, ticker, lot in eligible:
        if figi not in candles_map:
            continue
        candles = candles_map[figi]
        if not candles:
            continue
        res = compute_ensemble(candles, base_req(figi, 10000, lot, atr_mult, atr_rr, commission))
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
    print("=" * 110)
    print("  " + label)
    print("  Commission: %.2f%%  |  ATR: %s/%s  |  Capital: 10K per ticker  |  Period: Aug 2026" % (commission * 100, atr_mult, atr_rr))
    print("=" * 110)
    print("  %-8s %7s %6s %6s %6s %10s %10s %10s %10s %10s" % (
        "Ticker", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Commission", "Net", "ROI%"))
    print("  " + "-" * 105)

    for r in results:
        roi = r["net"] / 10000 * 100
        print("  %-8s %7d %6d %6d %5.1f%% %+10.2f %+10.2f %+10.2f %+10.2f %+6.1f%%" % (
            r["ticker"], r["trades"], r["wins"], r["losses"], r["wr"],
            r["gross_win"], r["gross_loss"], r["commission"], r["net"], roi))

    print("  " + "-" * 105)
    total_roi = total_net / (10000 * len(results)) * 100 if results else 0
    print("  %-8s %7d %6d %6d %5.1f%% %+10.2f %+10.2f %+10.2f %+10.2f %+6.1f%%" % (
        "TOTAL", total_trades, total_wins, total_losses, total_wr,
        total_gross_win, total_gross_loss, total_commission, total_net, total_roi))
    print()
    print("  PF: %.2f  |  Avg Win: %+.2f  |  Avg Loss: %+.2f  |  Total commission: %.2f RUB" % (
        pf, avg_win, avg_loss, total_commission))
    print("  Commission eats: %.1f%% of gross profit" % (total_commission / total_gross_win * 100 if total_gross_win else 0))
    print("  Trades/ticker: %d avg  |  Tickers: %d" % (total_trades / len(results) if results else 0, len(results)))

    return {
        "trades": total_trades, "wins": total_wins, "losses": total_losses,
        "gross_win": total_gross_win, "gross_loss": total_gross_loss,
        "commission": total_commission, "net": total_net, "pf": pf, "wr": total_wr,
    }


async def main():
    eligible = await get_eligible()
    print("Eligible tickers: %d" % len(eligible))

    print("Loading candles for %d tickers..." % len(eligible))
    candles_map = {}
    for figi, ticker, lot in eligible:
        try:
            candles = await load_august(figi)
            candles_map[figi] = candles
            print("  %s: %d candles" % (ticker, len(candles)))
        except Exception as e:
            print("  %s: ERROR %s" % (ticker, e))

    r1 = run_variant(candles_map, eligible, "ATR 4.0/4.0, commission 0.3% (CURRENT TARIFF)", 4.0, 4.0, 0.003)
    r2 = run_variant(candles_map, eligible, "ATR 4.0/4.0, commission 0.05% (TARGET TARIFF)", 4.0, 4.0, 0.0005)

    print()
    print("=" * 110)
    print("  COMPARISON: 0.3% vs 0.05% commission")
    print("=" * 110)
    print("  %-30s %7s %6s %10s %10s %6s %10s" % (
        "Variant", "Trades", "WR%", "Commission", "Net", "PF", "Delta"))
    print("  " + "-" * 80)
    print("  %-30s %7d %5.1f%% %+10.2f %+10.2f %6.2f" % (
        "0.3% (current)", r1["trades"], r1["wr"], r1["commission"], r1["net"], r1["pf"]))
    print("  %-30s %7d %5.1f%% %+10.2f %+10.2f %6.2f" % (
        "0.05% (target)", r2["trades"], r2["wr"], r2["commission"], r2["net"], r2["pf"]))
    print("  %-30s %+7d %+5.1f%% %+10.2f %+10.2f %+6.2f %+10.2f" % (
        "SAVINGS", r2["trades"] - r1["trades"], r2["wr"] - r1["wr"],
        r2["commission"] - r1["commission"], r2["net"] - r1["net"],
        r2["pf"] - r1["pf"], r2["net"] - r1["net"]))
    print()
    print("  Commission at 0.3%%: %.2f RUB/month" % r1["commission"])
    print("  Commission at 0.05%%: %.2f RUB/month" % r2["commission"])
    print("  Monthly savings: %.2f RUB" % (r1["commission"] - r2["commission"]))
    print("  Annual savings: %.2f RUB" % ((r1["commission"] - r2["commission"]) * 12))
    print("  Tariff payback: %.1f months (at %.0f RUB/month tariff)" % (
        3000 / (r1["commission"] - r2["commission"]), r1["commission"] - r2["commission"]))


if __name__ == "__main__":
    asyncio.run(main())
