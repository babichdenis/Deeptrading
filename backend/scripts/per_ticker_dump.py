#!/usr/bin/env python3
"""Per-ticker compute_ensemble dump: прогнать каждый тикер на месяц с optuna-параметрами
из БД, сохранить сделки (entry/exit ts, цены, side, lot) в jsonl для портфельной сборки.

Usage: python per_ticker_dump.py --out /tmp/trades_A.jsonl --tickers SMLT,GAZP
"""
import asyncio, os, sys, json, argparse
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

JUL_FROM = datetime(2026, 7, 20, tzinfo=timezone.utc)
JUL_TO = datetime(2026, 7, 27, tzinfo=timezone.utc)
WARMUP = datetime(2026, 5, 15, tzinfo=timezone.utc)  # warmup 60+ дней

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


def optuna_params_dict(p):
    if not p:
        return {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0,
                "active_sids": ALL_SIDS, "strategy_params": dict(V2P)}
    return {
        "sl_mult": float(p.get("sl_mult", 4.0)),
        "rr": float(p.get("rr", 4.0)),
        "quorum": int(p.get("quorum", 2)),
        "vol_thr": float(p.get("vol_thr", 0.0) or 0.0),
        "active_sids": list(p.get("active_sids", ALL_SIDS)),
        "strategy_params": {k: dict(v) for k, v in (p.get("strategy_params") or {}).items()},
    }


def make_req(figi, lot, p, flip_mode, entry_session, from_ts, to_ts):
    # flip_mode: full => confirm_flip=True, neutral=None (flip во все режимы)
    #            semi => confirm_flip=True, neutral="semi_flip" (в NEUTRAL закрыть без входа)
    #            none => confirm_flip=False (встречный сигнал = просто exit, без разворота)
    if flip_mode == "none":
        confirm_flip = 0
        neutral = None
    elif flip_mode == "semi":
        confirm_flip = 2
        neutral = "semi_flip"
    else:  # full
        confirm_flip = 2
        neutral = None
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


async def load_candles(figi):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=WARMUP, date_to=JUL_TO)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--tickers", required=True)
    ap.add_argument("--flip-mode", default="semi", choices=["full", "semi", "none"],
                    help="full=флип везде | semi=semi_flip в NEUTRAL | none=без флипа")
    ap.add_argument("--entry-session", default="main", choices=["main", "all"])
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    out_rows = []
    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: NOT FOUND"); continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)
        candles = await load_candles(figi)
        if not candles:
            print(f"{tkr}: no candles"); continue
        req = make_req(figi, lot, p, args.flip_mode, args.entry_session, JUL_FROM, JUL_TO)
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"{tkr}: ERROR {res['error']}"); continue
        st = res.get("static", {})
        trades = st.get("trades", [])
        # regime attribution
        from app.services.regime import RegimeDetector, regime_at
        from app.services.ensemble import resample as _rs
        try:
            c5 = _rs(candles, 300)
            rr = RegimeDetector().compute(c5)
        except Exception:
            rr = None
        for t in trades:
            ets = datetime.fromisoformat(t["entry_ts"])
            regime = None
            if rr is not None:
                r = regime_at(rr, ets)
                regime = r["state"] if r else "UNKNOWN"
            out_rows.append({
                "ticker": tkr, "figi": figi, "lot": lot,
                "side": t["side"], "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
                "entry_px": t["entry_px"], "exit_px": t["exit_px"],
                "exit_reason": t["exit_reason"], "regime": regime or "UNKNOWN",
                "qty_at_10k": None,  # qty пересчитаем на портфельной сборке
                "net_at_10k": t["net"],
            })
        print(f"{tkr}: {len(trades)} trades")

    with open(args.out, "w") as f:
        for r in out_rows:
            f.write(json.dumps(r) + "\n")
    print(f"Saved {len(out_rows)} trades -> {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
