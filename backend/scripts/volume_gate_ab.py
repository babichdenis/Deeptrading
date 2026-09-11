#!/usr/bin/env python3
"""Volume Exhaustion Шаг 2: A/B gate на compute_ensemble (пост-фильтр входов).

Конфиги:
  baseline      — без volume_gate (SEMI-FLIP)
  v4_confirm    — SHORT(SELL) требует volume_on_drop
  dryup_block   — LONG(BUY) блокируется при dryup
  full          — v4_confirm + dryup_block + climax_long block для BUY

Usage: python scripts/volume_gate_ab.py [--month 7|8] [--tickers ...]
"""
import argparse, asyncio, os, sys, statistics
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
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

GATES = {
    "baseline": None,
    "v4_confirm": {"require": {"volume_on_drop": ["SELL"]}},
    "dryup_block": {"block": {"dryup": ["BUY"]}},
    "full": {"require": {"volume_on_drop": ["SELL"]},
             "block": {"dryup": ["BUY"], "climax_long": ["BUY"]}},
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


def summarize(names, results):
    print(f"\n=== A/B volume_gate: {len(names)} configs ===")
    print(f"{'Config':<14} | {'Trades':>6} | {'Wins':>5} | {'Losses':>6} | {'Net':>10} | {'WR%':>5} | {'PF':>6} | {'GrossW':>9} | {'GrossL':>9}")
    print("-" * 100)
    for nm in names:
        r = results[nm]
        neg = r["gross_loss"]
        pf = r["gross_win"] / neg if neg > 1e-9 else float("inf")
        print(f"{nm:<14} | {r['trades']:>6} | {r['wins']:>5} | {r['trades']-r['wins']:>6} | "
              f"{r['net']:>10.2f} | {r['wins']/max(r['trades'],1)*100:>5.1f} | {pf:>6.2f} | "
              f"{r['gross_win']:>9.2f} | {r['gross_loss']:>9.2f}")
    print()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", type=int, choices=[7, 8], default=7)
    ap.add_argument("--tickers", default="SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL")
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    if args.month == 7:
        from_ts, to_ts = datetime(2026, 7, 20, tzinfo=timezone.utc), datetime(2026, 7, 27, tzinfo=timezone.utc)
    else:
        from_ts, to_ts = datetime(2026, 8, 20, tzinfo=timezone.utc), datetime(2026, 8, 27, tzinfo=timezone.utc)
    warmup = datetime(2026, 5, 15, tzinfo=timezone.utc)

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    results = {nm: {"trades": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0, "net": 0.0}
               for nm in GATES}
    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: NOT FOUND"); continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warmup, date_to=to_ts)
        if not candles:
            print(f"{tkr}: no candles"); continue
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
                  for s in ALL_SIDS]
        for nm, gate in GATES.items():
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
                "use_all_setups": False, "drop_useless": True,
                "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
                "volume_features": True,
            }
            if p["vol_thr"] and p["vol_thr"] > 0:
                req["volume_filter_threshold"] = p["vol_thr"]
            if gate is not None:
                req["volume_gate"] = gate
            res = compute_ensemble(candles, req)
            if "error" in res:
                print(f"{tkr}: ERROR {res['error']}"); continue
            trades = res.get("static", {}).get("trades", [])
            r = results[nm]
            r["trades"] += len(trades)
            for t in trades:
                r["net"] += t["net"]
                if t["net"] > 0:
                    r["wins"] += 1
                    r["gross_win"] += t["net"]
                else:
                    r["gross_loss"] += -t["net"]
            rej = res.get("static", {}).get("rejected", [])
            gated = [x for x in rej if x.get("reason", "").startswith("VOL_GATE")]
            if gated:
                print(f"  {tkr} {nm}: +{len(gated)} gated ({len(trades)} trades)")
        print(f"{tkr}: done")
    summarize(list(GATES), results)


if __name__ == "__main__":
    asyncio.run(main())