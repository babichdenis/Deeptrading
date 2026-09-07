#!/usr/bin/env python3
"""Test ATR 2.0 vs ATR 4.0 at commission=0.05%."""
import asyncio, os, sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import SessionLocal
from app.services.ensemble import compute_ensemble

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


def run_variant(candles_map, label, atr_mult, atr_rr, commission):
    total_trades = 0
    total_wins = 0
    total_losses = 0
    total_gross_win = 0.0
    total_gross_loss = 0.0
    total_net = 0.0

    print()
    print("=" * 90)
    print("  " + label)
    print("  Commission: %.2f%%  |  ATR mult: %s  |  Risk/Reward: %s" % (commission * 100, atr_mult, atr_rr))
    print("=" * 90)
    print("  %-6s %7s %6s %6s %6s %10s %10s %10s" % ("Ticker", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net"))
    print("  " + "-" * 75)

    for figi, (ticker, lot) in FIGIS.items():
        candles = candles_map[figi]
        res = compute_ensemble(candles, base_req(figi, 10000, lot, atr_mult, atr_rr, commission))
        trades = res.get("static", {}).get("trades", []) if "error" not in res else []
        wins = [t for t in trades if t.get("net", 0) > 0]
        losses = [t for t in trades if t.get("net", 0) < 0]
        gross_win = sum(t["net"] for t in wins)
        gross_loss = sum(t["net"] for t in losses)
        net = sum(t["net"] for t in trades)
        wr = len(wins) / len(trades) * 100 if trades else 0

        print("  %-6s %7d %6d %6d %5.1f%% %+10.2f %+10.2f %+10.2f" % (
            ticker, len(trades), len(wins), len(losses), wr, gross_win, gross_loss, net))

        total_trades += len(trades)
        total_wins += len(wins)
        total_losses += len(losses)
        total_gross_win += gross_win
        total_gross_loss += gross_loss
        total_net += net

    total_wr = total_wins / total_trades * 100 if total_trades else 0
    pf = total_gross_win / abs(total_gross_loss) if total_gross_loss else 0
    avg_win = total_gross_win / total_wins if total_wins else 0
    avg_loss = total_gross_loss / total_losses if total_losses else 0

    print("  " + "-" * 75)
    print("  %-6s %7d %6d %6d %5.1f%% %+10.2f %+10.2f %+10.2f" % (
        "TOTAL", total_trades, total_wins, total_losses, total_wr, total_gross_win, total_gross_loss, total_net))
    print("  PF: %.2f  |  Avg Win: %+.2f  |  Avg Loss: %+.2f" % (pf, avg_win, avg_loss))

    return (total_trades, total_wins, total_losses, total_gross_win, total_gross_loss, total_net, pf, total_wr)


async def main():
    print("Loading candles...")
    candles_map = {}
    for figi, (ticker, lot) in FIGIS.items():
        candles = await load_august(figi)
        candles_map[figi] = candles
        print("  %s: %d candles" % (ticker, len(candles)))

    r1 = run_variant(candles_map, "ATR 2.0/2.0, commission 0.05%", 2.0, 2.0, 0.0005)
    r2 = run_variant(candles_map, "ATR 4.0/4.0, commission 0.05%", 4.0, 4.0, 0.0005)

    print()
    print("=" * 90)
    print("  COMPARISON SUMMARY")
    print("=" * 90)
    print("  %-30s %7s %6s %10s %6s" % ("Variant", "Trades", "WR%", "Net", "PF"))
    print("  " + "-" * 60)
    print("  %-30s %7d %5.1f%% %+10.2f %6.2f" % ("ATR 2.0/2.0", r1[0], r1[7], r1[5], r1[6]))
    print("  %-30s %7d %5.1f%% %+10.2f %6.2f" % ("ATR 4.0/4.0", r2[0], r2[7], r2[5], r2[6]))
    print("  %-30s %+7d %+5.1f%% %+10.2f %+6.2f" % ("Delta", r2[0] - r1[0], r2[7] - r1[7], r2[5] - r1[5], r2[6] - r1[6]))


if __name__ == "__main__":
    asyncio.run(main())
