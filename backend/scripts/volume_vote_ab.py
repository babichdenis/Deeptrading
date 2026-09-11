#!/usr/bin/env python3
"""Volume Exhaustion Шаг 3: volume-стратегии как ГОЛОСА в кворуме.

Стратегии зарегистрированы в каталоге (volume_drop, volume_climax,
volume_divergence) и участвуют в merge_quorum наравне с 7 базовыми.
Ядро quorum НЕ тронуто — добавляются только участники голосования.

Usage: python scripts/volume_vote_ab.py [--month 7|8] [--tickers ...]
"""
import argparse, asyncio, os, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

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

VARIANTS = {
    "baseline": [],
    "v_drop": ["volume_drop"],
    "v_climax": ["volume_climax"],
    "v_div": ["volume_divergence"],
    "drop+climax": ["volume_drop", "volume_climax"],
    "all3": ["volume_drop", "volume_climax", "volume_divergence"],
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
    print(f"\n=== A/B volume-vote (Шаг 3): {len(names)} configs ===")
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


def as_dict(r):
    if isinstance(r, dict):
        return r
    return r._mapping


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", type=int, choices=[7, 8], default=7)
    ap.add_argument("--from-date", default=None, help="ISO UTC, напр. 2026-06-01")
    ap.add_argument("--to-date", default=None, help="ISO UTC, напр. 2026-06-30")
    ap.add_argument("--warmup-days", type=int, default=60,
                    help="сколько дней истории до from-date подавать на прогрев")
    ap.add_argument("--label", default=None, help="метка прогона (для вывода)")
    ap.add_argument("--keep-all", action="store_true",
                    help="drop_useless=False (без отсева стратегий)")
    ap.add_argument("--variants", default=None,
                    help="список вариантов через запятую (по умолчанию все 6)")
    ap.add_argument("--tickers", default="SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL")
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]
    drop_useless = not args.keep_all
    variants = VARIANTS
    if args.variants:
        names = [v.strip() for v in args.variants.split(",") if v.strip()]
        variants = {k: VARIANTS[k] for k in names if k in VARIANTS}

    if args.from_date and args.to_date:
        from_ts = datetime.fromisoformat(args.from_date).replace(tzinfo=timezone.utc)
        to_ts = datetime.fromisoformat(args.to_date).replace(tzinfo=timezone.utc)
    elif args.month == 7:
        from_ts, to_ts = datetime(2026, 7, 20, tzinfo=timezone.utc), datetime(2026, 7, 27, tzinfo=timezone.utc)
    else:
        from_ts, to_ts = datetime(2026, 8, 20, tzinfo=timezone.utc), datetime(2026, 8, 27, tzinfo=timezone.utc)
    warmup = from_ts - timedelta(days=args.warmup_days)
    if args.label:
        print(f"### Прогон: {args.label} ({from_ts.date()} .. {to_ts.date()}, drop_useless={drop_useless})")

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    results = {nm: {"trades": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0, "net": 0.0}
               for nm in variants}
    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: NOT FOUND"); continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warmup, date_to=to_ts)
        if not candles:
            print(f"{tkr}: no candles"); continue
        base_setups = [{"strategy_id": s, "tf": "5min",
                        "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
                       for s in BASE_SIDS]
        for nm, extra in variants.items():
            setups = base_setups + [{"strategy_id": s, "tf": "5min", "params": {}}
                                    for s in extra]
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
                "use_all_setups": False, "drop_useless": drop_useless,
                "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
                "volume_features": True,
            }
            if p["vol_thr"] and p["vol_thr"] > 0:
                req["volume_filter_threshold"] = p["vol_thr"]
            res = compute_ensemble(candles, req)
            if "error" in res:
                print(f"{tkr} {nm}: ERROR {res['error']}"); continue
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
        print(f"{tkr}: done")
    summarize(list(variants), results)


if __name__ == "__main__":
    asyncio.run(main())