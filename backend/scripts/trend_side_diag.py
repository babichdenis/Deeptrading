#!/usr/bin/env python3
"""Диагностика: режим (RegimeDetector) x сторона (BUY/SELL) x причина выхода.

Гипотеза пользователя: шорт в TREND_UP — направленно неверная сделка; даже при
идеальной портфельной аллокации она тянет результат вниз.

Скрипт на периоде считает compute_ensemble (как per_ticker_dump), обогащает сделки
режимом входа и выдаёт кросс-таб regime x side: N, WR%, Net, Net/tr, exit reasons.

Usage: python trend_side_diag.py --from 2026-06-01 --to 2026-06-30 \
       --warmup 2026-05-15 --tickers SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL
"""
import asyncio, os, sys, argparse
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble, resample as _rs
from app.services.signals import _load_candles as _lc
from app.services.regime import RegimeDetector, regime_at

from per_ticker_dump import V2P, ALL_SIDS, optuna_params_dict


def make_req(figi, lot, p, flip_mode, entry_session, from_ts, to_ts):
    if flip_mode == "none":
        confirm_flip, neutral = 0, None
    elif flip_mode == "semi":
        confirm_flip, neutral = 2, "semi_flip"
    else:  # full
        confirm_flip, neutral = 2, None
    setups = [{"strategy_id": s, "tf": "5min",
               "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
              for s in ALL_SIDS]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": entry_session,
        "quorum": p["quorum"], "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": confirm_flip,
        "exit_policy": {"id": "atr_stop",
                        "params": {"period": 14, "multiplier": p["sl_mult"], "risk_reward": p["rr"]}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": neutral,
        "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
    }
    if p["vol_thr"] and p["vol_thr"] > 0:
        req["volume_filter_threshold"] = p["vol_thr"]
    return req


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True)
    ap.add_argument("--to", dest="to", required=True)
    ap.add_argument("--warmup", default=None, help="старт загрузки свечей (по умолчанию from-45д)")
    ap.add_argument("--tickers", required=True)
    ap.add_argument("--flip-mode", default="semi", choices=["full", "semi", "none"])
    ap.add_argument("--entry-session", default="main", choices=["main", "all"])
    args = ap.parse_args()

    frm = datetime.fromisoformat(args.frm).replace(tzinfo=timezone.utc)
    to = datetime.fromisoformat(args.to).replace(tzinfo=timezone.utc)
    warm = datetime.fromisoformat(args.warmup).replace(tzinfo=timezone.utc) if args.warmup else frm - timedelta(days=45)
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    # regime x side
    agg = defaultdict(lambda: {"n": 0, "w": 0, "net": 0.0, "exit": defaultdict(lambda: [0, 0.0])})
    per_ticker = defaultdict(lambda: defaultdict(lambda: {"n": 0, "net": 0.0, "w": 0}))

    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: NOT FOUND"); continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)

        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=warm, date_to=to)
        if not candles:
            print(f"{tkr}: no candles"); continue
        req = make_req(figi, lot, p, args.flip_mode, args.entry_session, frm, to)
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"{tkr}: ERROR {res['error']}"); continue
        trades = res.get("static", {}).get("trades", [])

        try:
            c5 = _rs(candles, 300)
            rr = RegimeDetector().compute(c5)
        except Exception:
            rr = None

        for t in trades:
            ets = datetime.fromisoformat(t["entry_ts"])
            regime = "UNKNOWN"
            if rr is not None:
                r = regime_at(rr, ets)
                regime = r["state"] if r else "UNKNOWN"
            side = t["side"]
            net = t["net"]
            win = 1 if net > 0 else 0
            key = f"{regime}|{side}"
            agg[key]["n"] += 1
            agg[key]["w"] += win
            agg[key]["net"] += net
            agg[key]["exit"][t["exit_reason"]][0] += 1
            agg[key]["exit"][t["exit_reason"]][1] += net
            per_ticker[tkr][key]["n"] += 1
            per_ticker[tkr][key]["net"] += net
            per_ticker[tkr][key]["w"] += win
        print(f"{tkr}: {len(trades)} trades")

    print(f"\n=== Regime x Side ({args.frm} .. {args.to}, flip={args.flip_mode}, sess={args.entry_session}) ===")
    print(f"{'Regime|Side':<22} {'N':>6} {'WR%':>7} {'Net':>10} {'Net/tr':>8}  exits (N/Net)")
    for key in sorted(agg, key=lambda k: k):
        a = agg[key]
        wr = 100.0 * a["w"] / a["n"] if a["n"] else 0
        er = "; ".join(f"{k}x/{v[1]:+.0f}" for k, v in a["exit"].items())
        print(f"{key:<22} {a['n']:>6} {wr:>6.1f}% {a['net']:>+10.1f} {a['net']/a['n']:>+8.2f}  {er}")

    print(f"\n=== По тикерам: shorts@TREND_UP ===")
    print(f"{'Ticker':<8} {'N':>5} {'WR%':>7} {'Net':>10} {'Net/tr':>8}")
    for tkr in sorted(per_ticker):
        k = "TREND_UP|SELL"
        a = per_ticker[tkr].get(k)
        if not a or a["n"] == 0:
            print(f"{tkr:<8} {'0':>5}") ; continue
        wr = 100.0 * a["w"] / a["n"]
        print(f"{tkr:<8} {a['n']:>5} {wr:>6.1f}% {a['net']:>+10.1f} {a['net']/a['n']:>+8.2f}")


if __name__ == "__main__":
    asyncio.run(main())