#!/usr/bin/env python3
"""Optuna pilot: tune global params per-ticker on week1, validate on week2.

Tune on week1 (Aug 10-17) per ticker:
  - sl_mult   (2.5 - 6.0)   exit ATR multiplier
  - rr        (2.5 - 6.0)   risk/reward = TP distance
  - quorum    (2 - 4)
  - vol_thr   (0.0 - 2.0)   volume filter threshold (0 = off)
  - inc_*     (drop weak strategies per ticker)
Validate best params on week2 (Aug 17-24) and compare vs V2 baseline (mult=4, rr=4, q=2, no vol).

Usage: PYTHONPATH=. .venv/bin/python3 -u scripts/optuna_sweep_week.py [--trials 40] [--tickers SMLT,NLMK,GAZP]
"""
import argparse, asyncio, os, sys, time, json
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc
import optuna

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

W1_FROM = datetime(2026, 8, 10, tzinfo=timezone.utc)
W1_TO = datetime(2026, 8, 17, tzinfo=timezone.utc)
W2_FROM = datetime(2026, 8, 17, tzinfo=timezone.utc)
W2_TO = datetime(2026, 8, 24, tzinfo=timezone.utc)


def make_req(figi, lot, from_ts, to_ts, sl_mult, rr, quorum, vol_thr, active_sids, confirm_flip=2):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2_PARAMS.get(s, {}))}
              for s in active_sids]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
        "quorum": quorum, "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": confirm_flip,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl_mult, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip",
        "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
    }
    if vol_thr and vol_thr > 0:
        req["volume_filter_threshold"] = vol_thr
    return req


def run_req(candles, req):
    res = compute_ensemble(candles, req)
    if "error" in res:
        return 0.0, 0, 0.0
    st = res.get("static", {})
    ec = st.get("economic", {}) or {}
    net = ec.get("net", 0.0) or 0.0
    trades = ec.get("trades", 0) or len(st.get("trades", []))
    pf = ec.get("profit_factor", 0.0) or 0.0
    return net, trades, pf


def run_arm(candles, figi, lot, from_ts, to_ts, active_sids, sl_mult=4.0, rr=4.0, quorum=2, vol_thr=0.0):
    req = make_req(figi, lot, from_ts, to_ts, sl_mult, rr, quorum, vol_thr, active_sids)
    return run_req(candles, req)


async def load_week(figi, w_from, w_to):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=w_from, date_to=w_to)


def objective_factory(candles_w1, figi, lot, from_ts, to_ts):
    def objective(trial):
        sl_mult = trial.suggest_float("sl_mult", 2.5, 6.0, step=0.5)
        rr = trial.suggest_float("rr", 2.5, 6.0, step=0.5)
        quorum = trial.suggest_int("quorum", 2, 4)
        vol_thr = trial.suggest_float("vol_thr", 0.0, 2.0, step=0.2)
        active = []
        for sid in ALL_SIDS:
            if trial.suggest_categorical(f"inc_{sid}", [True, False]):
                active.append(sid)
        if not active:
            active = ["pullback_ema"]  # guard
        net, trades, pf = run_arm(candles_w1, figi, lot, from_ts, to_ts, active,
                                  sl_mult=sl_mult, rr=rr, quorum=quorum, vol_thr=vol_thr)
        # penalize tiny trade counts to avoid overfit on few trades
        if trades < 10:
            net = net - 5000
        return net
    return objective


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--tickers", default="SMLT,NLMK,GAZP,LENT,CHMF")
    args = ap.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT ticker, figi, lot FROM instruments WHERE ticker = ANY(:tk)"),
            {"tk": tickers})).fetchall()
    meta = {r.ticker: (r.figi, r.lot) for r in rows}

    results = {}
    for ticker in tickers:
        if ticker not in meta:
            print(f"{ticker}: NOT FOUND"); continue
        figi, lot = meta[ticker]
        print(f"\n===== {ticker} (figi={figi}, lot={lot}) =====")
        candles_w1 = await load_week(figi, W1_FROM, W1_TO)
        candles_w2 = await load_week(figi, W2_FROM, W2_TO)
        print(f"  w1 candles={len(candles_w1)}, w2 candles={len(candles_w2)}")

        # V2 baseline on w1 and w2 (mult=4, rr=4, quorum=2, no vol, all sids)
        base_net_w1, base_tr_w1, base_pf_w1 = run_arm(candles_w1, figi, lot, W1_FROM, W1_TO, ALL_SIDS)
        base_net_w2, base_tr_w2, base_pf_w2 = run_arm(candles_w2, figi, lot, W2_FROM, W2_TO, ALL_SIDS)
        print(f"  V2 baseline  w1: net={base_net_w1:+.0f} tr={base_tr_w1} pf={base_pf_w1} | "
              f"w2: net={base_net_w2:+.0f} tr={base_tr_w2} pf={base_pf_w2}")

        # Optuna on w1
        study = optuna.create_study(direction="maximize",
                                    sampler=optuna.samplers.TPESampler(seed=42))
        t0 = time.time()
        study.optimize(objective_factory(candles_w1, figi, lot, W1_FROM, W1_TO),
                       n_trials=args.trials, show_progress_bar=False)
        dt = time.time() - t0
        bp = study.best_params
        best_sids = [s for s in ALL_SIDS if bp.get(f"inc_{s}", True)]
        if not best_sids: best_sids = ["pullback_ema"]
        best_vol = bp["vol_thr"] if bp["vol_thr"] > 0 else 0.0

        # Apply best params on w2 (validation)
        val_net_w1, val_tr_w1, val_pf_w1 = run_arm(candles_w1, figi, lot, W1_FROM, W1_TO, best_sids,
                                                   sl_mult=bp["sl_mult"], rr=bp["rr"],
                                                   quorum=bp["quorum"], vol_thr=best_vol)
        val_net_w2, val_tr_w2, val_pf_w2 = run_arm(candles_w2, figi, lot, W2_FROM, W2_TO, best_sids,
                                                   sl_mult=bp["sl_mult"], rr=bp["rr"],
                                                   quorum=bp["quorum"], vol_thr=best_vol)
        print(f"  Optuna {args.trials} trials in {dt:.0f}s, best obj={study.best_value:+.0f}")
        print(f"  best_params: sl={bp['sl_mult']} rr={bp['rr']} quorum={bp['quorum']} "
              f"vol={best_vol} sids={len(best_sids)}/7")
        print(f"  OPT   w1: net={val_net_w1:+.0f} tr={val_tr_w1} pf={val_pf_w1} | "
              f"w2: net={val_net_w2:+.0f} tr={val_tr_w2} pf={val_pf_w2}")

        results[ticker] = {
            "figi": figi, "lot": lot,
            "trials": args.trials, "elapsed_s": round(dt, 1),
            "best_params": {**bp, "active_sids": best_sids},
            "baseline_w1": {"net": base_net_w1, "trades": base_tr_w1, "pf": base_pf_w1},
            "baseline_w2": {"net": base_net_w2, "trades": base_tr_w2, "pf": base_pf_w2},
            "opt_w1": {"net": val_net_w1, "trades": val_tr_w1, "pf": val_pf_w1},
            "opt_w2": {"net": val_net_w2, "trades": val_tr_w2, "pf": val_pf_w2},
        }

    print("\n===== SUMMARY =====")
    for tk, r in results.items():
        print(f"{tk:6s} base(w1={r['baseline_w1']['net']:+.0f},w2={r['baseline_w2']['net']:+.0f}) "
              f"opt(w1={r['opt_w1']['net']:+.0f},w2={r['opt_w2']['net']:+.0f}) "
              f"sl={r['best_params']['sl_mult']} rr={r['best_params']['rr']} "
              f"q={r['best_params']['quorum']} vol={r['best_params']['vol_thr']} "
              f"sids={r['best_params']['active_sids']}")

    out = os.path.join(os.path.dirname(__file__), "optuna_week_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nSaved to", out)


if __name__ == "__main__":
    asyncio.run(main())
