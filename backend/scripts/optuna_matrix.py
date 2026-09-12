#!/usr/bin/env python3
"""Матрица тестов с ОПТИМИЗИРОВАННЫМИ параметрами из БД (instruments.optuna_params).

Каждый тикер берёт СВОИ параметры (SL/RR/quorum/vol/активные стратегии/их параметры),
сохранённые из optuna_params_results.json.

Конфиги (10K на акцию, per-ticker изолированный прогон как в s5 A-E):
  1. optuna + semi_flip + main
  2. optuna + full flip + main
  3. optuna + semi_flip + all
  4. optuna + full flip + all
  5. baseline V2 + semi_flip + main  (референс)

Период: накопительный август (2026-08-01 → 2026-09-01) или OOS w2+w3.
"""
import asyncio, os, sys, json, argparse
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

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
V2_BASE = {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0,
           "active_sids": ALL_SIDS, "strategy_params": dict(V2_PARAMS)}

AUG_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
AUG_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)
OOS_FROM = datetime(2026, 8, 17, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)
SEP_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)
SEP_TO = datetime(2026, 9, 13, tzinfo=timezone.utc)


def make_req(figi, lot, p, neutral_mode, entry_session, from_ts, to_ts, bias_mode="info"):
    setups = [{"strategy_id": s, "tf": "5min",
               "params": dict(p["strategy_params"].get(s, V2_PARAMS.get(s, {})))}
              for s in p["active_sids"]]
    req = {
        "figi": figi, "bias_mode": bias_mode,
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": entry_session,
        "quorum": int(p["quorum"]), "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop",
                        "params": {"period": 14, "multiplier": p["sl_mult"], "risk_reward": p["rr"]}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
    }
    if neutral_mode:
        req["neutral_mode"] = neutral_mode
    if p["vol_thr"] and p["vol_thr"] > 0:
        req["volume_filter_threshold"] = p["vol_thr"]
    return req


def run_ticker(candles, figi, lot, p, neutral_mode, entry_session, from_ts, to_ts, bias_mode="info"):
    req = make_req(figi, lot, p, neutral_mode, entry_session, from_ts, to_ts, bias_mode=bias_mode)
    res = compute_ensemble(candles, req)
    if "error" in res:
        return None
    st = res.get("static", {})
    ec = st.get("economic", {}) or {}
    trades = st.get("trades", [])
    wins = [t for t in trades if t.get("net", 0) > 0]
    losses = [t for t in trades if t.get("net", 0) <= 0]
    gw = sum(t["net"] for t in wins)
    gl = sum(t["net"] for t in losses)
    net = gw + gl
    wr = len(wins) / len(trades) * 100 if trades else 0
    pf = gw / abs(gl) if gl else 0
    return {"trades": len(trades), "wins": len(wins), "losses": len(losses),
            "wr": wr, "gw": gw, "gl": gl, "net": net, "pf": pf}


async def load_candles(figi, from_ts, to_ts):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=from_ts, date_to=to_ts)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="aug", choices=["aug", "oos", "sep"])
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()
    if args.period == "aug":
        from_ts, to_ts = AUG_FROM, AUG_TO
        period_label = "AUG (01-31.08)"
    elif args.period == "oos":
        from_ts, to_ts = OOS_FROM, OOS_TO
        period_label = "OOS (17-31.08)"
    else:
        from_ts, to_ts = SEP_FROM, SEP_TO
        period_label = "SEP (01-13.09)"

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT i.figi, i.ticker, i.lot, i.optuna_params
            FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            AND NOT (i.ticker='T' AND i.figi='BBG000BSJK37')
            ORDER BY i.ticker
        """))).fetchall()
    meta = {}
    for r in rows:
        if r.ticker in meta:
            continue
        meta[r.ticker] = (r.figi, r.lot, r.optuna_params)
    if args.tickers:
        tks = [t.strip() for t in args.tickers.split(",") if t.strip()]
    else:
        tks = sorted(meta.keys())

    print("=" * 130)
    print(f"MATRIX: period={period_label}, capital=10K/ticker, tickers={len(tks)}")
    print("=" * 130)

    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: not found"); continue
        figi, lot, opt = meta[tkr]
        if opt:
            p_opt = {"sl_mult": float(opt.get("sl_mult", 4.0)),
                     "rr": float(opt.get("rr", 4.0)),
                     "quorum": int(opt.get("quorum", 2)),
                     "vol_thr": float(opt.get("vol_thr", 0.0) or 0.0),
                     "active_sids": list(opt.get("active_sids", ALL_SIDS)),
                     "strategy_params": {k: dict(v) for k, v in (opt.get("strategy_params") or {}).items()}}
        else:
            p_opt = dict(V2_BASE)
        p_v2 = dict(V2_BASE)

        candles = await load_candles(figi, from_ts, to_ts)
        if not candles:
            print(f"{tkr}: no candles"); continue

        cfgs = [
            ("1 opt semi main", p_opt, "semi_flip", "main", "info"),
            ("2 opt full main", p_opt, None, "main", "info"),
            ("3 opt semi all", p_opt, "semi_flip", "all", "info"),
            ("4 opt full all", p_opt, None, "all", "info"),
            ("5 v2  semi main", p_v2, "semi_flip", "main", "info"),
            ("6 v2 veto main", p_v2, "semi_flip", "main", "veto"),
            ("7 v2 strict main", p_v2, "semi_flip", "main", "strict_ct"),
            ("8 v2 veto all", p_v2, "semi_flip", "all", "veto"),
        ]
        print(f"\n--- {tkr} (lot={lot}, {len(candles)}c) ---")
        res_line = {}
        for label, p, nm, sess, bm in cfgs:
            r = run_ticker(candles, figi, lot, p, nm, sess, from_ts, to_ts, bias_mode=bm)
            if r is None:
                print(f"  {label:16s} ERROR")
                continue
            res_line[label] = r
            print(f"  {label:16s} net={r['net']:+10.0f}  tr={r['trades']:4d}  "
                  f"wr={r['wr']:5.1f}%  pf={r['pf']:5.2f}  gw={r['gw']:+10.0f} gl={r['gl']:+10.0f}")
        # save partial per-ticker to jsonl
        with open(f"/tmp/matrix_{period_label}.jsonl", "a") as f:
            f.write(json.dumps({"ticker": tkr, "results": res_line}) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
