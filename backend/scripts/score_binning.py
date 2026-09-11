#!/usr/bin/env python3
"""Series 3, шаг 1: бининг сделок по score уверенности (calibration check).

Score считается на входе (ensemble.py:_signal_score) и прицеплен к сделке при
req["score_features"]=True. Группируем сделки по score-бинам и компонентам,
считаем Trades/WR/Net/Net-per-trade — есть ли монотонная зависимость.

Usage: python scripts/score_binning.py [--tickers ...]
"""
import argparse, asyncio, os, sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

BASE_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
             "range_compression_breakout", "macd_cross", "donchian_breakout"]
V2P = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}


def optuna_params_dict(p):
    if not p:
        return {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0}
    return {
        "sl_mult": float(p.get("sl_mult", 4.0)),
        "rr": float(p.get("rr", 4.0)),
        "quorum": int(p.get("quorum", 2)),
        "vol_thr": float(p.get("vol_thr", 0.0) or 0.0),
        "strategy_params": {k: dict(v) for k, v in (p.get("strategy_params") or {}).items()},
    }


def stat(net, wins, n):
    return f"{n:>5} | {wins/max(n,1)*100:>5.1f} | {net:>10.2f} | {net/max(n,1):>8.2f}"


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL")
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    periods = [("Jul", datetime(2026, 7, 20, tzinfo=timezone.utc), datetime(2026, 7, 27, tzinfo=timezone.utc)),
               ("Aug", datetime(2026, 8, 20, tzinfo=timezone.utc), datetime(2026, 8, 27, tzinfo=timezone.utc))]
    warmup = datetime(2026, 5, 15, tzinfo=timezone.utc)

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    all_trades = []
    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: NOT FOUND"); continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warmup, date_to=periods[-1][2])
        if not candles:
            print(f"{tkr}: no candles"); continue
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
                  for s in BASE_SIDS]
        for pname, fr, to in periods:
            req = {
                "figi": figi, "bias_mode": "info",
                "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
                "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
                "quorum": p["quorum"], "same_side_reentry_cooldown_bars": 15,
                "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
                "neutral_mode": "semi_flip",
                "exit_policy": {"id": "atr_stop",
                                "params": {"period": 14, "multiplier": p["sl_mult"],
                                           "risk_reward": p["rr"]}},
                "commission_rate": 0.0005, "slippage_bps": 2.0,
                "capital": 10000, "lot": lot, "setups": setups,
                "use_all_setups": False, "drop_useless": False,
                "from_ts": fr.isoformat(), "to_ts": to.isoformat(),
                "volume_features": True, "score_features": True,
            }
            if p["vol_thr"] and p["vol_thr"] > 0:
                req["volume_filter_threshold"] = p["vol_thr"]
            res = compute_ensemble(candles, req)
            if "error" in res:
                print(f"{tkr} {pname}: ERROR {res['error']}"); continue
            for t in res.get("static", {}).get("trades", []):
                t["_ticker"] = tkr
                t["_period"] = pname
                all_trades.append(t)
        print(f"{tkr}: done")

    print(f"\nВсего сделок: {len(all_trades)}")
    print(f"\n=== Бины по score ===")
    print(f"{'Bin':<12} | {'N':>5} | {'WR%':>5} | {'Net':>10} | {'Net/trade':>8}")
    bins = [(-1.0, 0.0), (0.0, 0.3), (0.3, 0.5), (0.5, 0.7), (0.7, 0.85), (0.85, 1.01)]
    for lo, hi in bins:
        sel = [t for t in all_trades if lo <= t.get("score", -2) < hi]
        net = sum(t["net"] for t in sel)
        wins = sum(1 for t in sel if t["net"] > 0)
        print(f"[{lo:.2f},{hi:.2f})   | {stat(net, wins, len(sel))}")

    print(f"\n=== Компоненты (Net/trade) ===")
    for label, pred in [
        ("vol_drop=1", lambda t: t.get("score_components", {}).get("vol_drop")),
        ("vol_drop=0", lambda t: not t.get("score_components", {}).get("vol_drop")),
        ("against_bias=1", lambda t: t.get("score_components", {}).get("against_bias")),
        ("macd_against=1", lambda t: t.get("score_components", {}).get("macd_against")),
        ("regime=NEUTRAL", lambda t: t.get("score_components", {}).get("regime") == "NEUTRAL"),
        ("regime=TREND_UP", lambda t: t.get("score_components", {}).get("regime") == "TREND_UP"),
        ("regime=TREND_DOWN", lambda t: t.get("score_components", {}).get("regime") == "TREND_DOWN"),
        ("regime=HIGH_VOL", lambda t: t.get("score_components", {}).get("regime") == "HIGH_VOLATILITY"),
        ("votes>=3", lambda t: float(t.get("score_components", {}).get("votes", 0)) >= 3),
        ("votes=2", lambda t: float(t.get("score_components", {}).get("votes", 0)) == 2),
        ("LONG", lambda t: t["side"] == "LONG"),
        ("SHORT", lambda t: t["side"] == "SHORT"),
    ]:
        sel = [t for t in all_trades if pred(t)]
        net = sum(t["net"] for t in sel)
        wins = sum(1 for t in sel if t["net"] > 0)
        print(f"{label:<16} | {stat(net, wins, len(sel))}")


if __name__ == "__main__":
    asyncio.run(main())