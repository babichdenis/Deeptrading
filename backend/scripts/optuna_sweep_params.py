#!/usr/bin/env python3
"""Optuna v2: global + per-strategy params per ticker. Train w1, validate w2 & w3.

Base params:  sl_mult, rr, quorum, vol_thr, inc_<sid>
Strategy params (only tuned when strategy active):
  rsi_reversal:        period, oversold, overbought
  bollinger_reclaim:   period, k
  pullback_ema:        trend_ema, pull_ema
  vwap_reclaim:        k
  range_compression:   lookback, atr_period, pct
  macd_cross:          fast, slow, signal_period
  donchian_breakout:   period

Validation: best params applied to w2 AND w3 vs V2 baseline.
"""
import argparse, asyncio, os, sys, time, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc
import optuna

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout",
            "trend_up", "trend_down", "range_reversion",
            "long_ensemble", "short_ensemble", "range_ensemble", "hv_ensemble", "neutral_ensemble"]
V2_PARAMS = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
    "trend_up": {}, "trend_down": {}, "range_reversion": {},
    "long_ensemble": {}, "short_ensemble": {}, "range_ensemble": {},
    "hv_ensemble": {}, "neutral_ensemble": {},
}
# Какие параметры у каждой стратегии варьировать + диапазоны вокруг V2
STRAT_PARAM_RANGES = {
    "rsi_reversal": [("period", 8, 24, 2), ("oversold", 20, 40, 2), ("overbought", 65, 85, 2)],
    "bollinger_reclaim": [("period", 10, 20, 1), ("k", 0.5, 2.0, 0.1)],
    "pullback_ema": [("trend_ema", 15, 25, 1), ("pull_ema", 8, 15, 1)],
    "vwap_reclaim": [("k", 1.0, 3.0, 0.1)],
    "range_compression_breakout": [("lookback", 10, 20, 1), ("atr_period", 12, 20, 1), ("pct", 25, 50, 2)],
    "macd_cross": [("fast", 6, 18, 1), ("slow", 20, 32, 1), ("signal_period", 6, 12, 1)],
    "donchian_breakout": [("period", 30, 60, 1)],
}

W1_FROM = datetime(2026, 8, 29, tzinfo=timezone.utc)
W1_TO = datetime(2026, 9, 5, tzinfo=timezone.utc)
W2_FROM = datetime(2026, 9, 5, tzinfo=timezone.utc)
W2_TO = datetime(2026, 9, 12, tzinfo=timezone.utc)
W3_FROM = datetime(2026, 9, 12, tzinfo=timezone.utc)
W3_TO = datetime(2026, 9, 13, tzinfo=timezone.utc)


def suggest_strategy_params(trial, sid, active):
    """Suggest params for one strategy, or V2 defaults if inactive."""
    if not active:
        return dict(V2_PARAMS.get(sid, {}))
    out = dict(V2_PARAMS.get(sid, {}))
    for pname, lo, hi, step in STRAT_PARAM_RANGES.get(sid, []):
        cur = out.get(pname)
        is_float = isinstance(cur, float) or "k" in pname or pname == "pct"
        if is_float:
            out[pname] = trial.suggest_float(f"{sid}.{pname}", lo, hi, step=step)
        else:
            out[pname] = trial.suggest_int(f"{sid}.{pname}", int(lo), int(hi), step=int(step))
    return out


def make_req(figi, lot, from_ts, to_ts, sl_mult, rr, quorum, vol_thr, strat_params, confirm_flip=2):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(strat_params.get(s, V2_PARAMS.get(s, {})))}
              for s in ALL_SIDS]
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


def run_arm(candles, figi, lot, from_ts, to_ts, sl_mult, rr, quorum, vol_thr, strat_params):
    req = make_req(figi, lot, from_ts, to_ts, sl_mult, rr, quorum, vol_thr, strat_params)
    res = compute_ensemble(candles, req)
    if "error" in res:
        return 0.0, 0, 0.0
    st = res.get("static", {})
    ec = st.get("economic", {}) or {}
    net = ec.get("net", 0.0) or 0.0
    trades = ec.get("trades", 0) or len(st.get("trades", []))
    pf = ec.get("profit_factor", 0.0) or 0.0
    return net, trades, pf


def objective_factory(candles_w1, figi, lot):
    def objective(trial):
        sl_mult = trial.suggest_float("sl_mult", 3.0, 6.0, step=0.5)
        rr = trial.suggest_float("rr", 2.0, 6.0, step=0.5)
        quorum = trial.suggest_int("quorum", 2, 3)
        vol_thr = trial.suggest_float("vol_thr", 0.0, 1.5, step=0.2)

        # Include strategy only if it has historically positive edge; but let Optuna decide
        active = set()
        strat_params = {}
        for sid in ALL_SIDS:
            inc = trial.suggest_categorical(f"inc_{sid}", [True, False])
            if inc:
                active.add(sid)
            strat_params[sid] = suggest_strategy_params(trial, sid, inc)
        if not active:
            strat_params["pullback_ema"] = dict(V2_PARAMS["pullback_ema"])
            strat_params["bollinger_reclaim"] = dict(V2_PARAMS["bollinger_reclaim"])

        net, trades, pf = run_arm(candles_w1, figi, lot, W1_FROM, W1_TO,
                                  sl_mult, rr, quorum, vol_thr, strat_params)
        if trades < 8:
            net = net - 5000
        return net
    return objective


async def load_week(figi, w_from, w_to):
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=w_from, date_to=w_to)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=60)
    ap.add_argument("--tickers", default="SMLT")
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
        print(f"\n===== {ticker} =====")
        cw1 = await load_week(figi, W1_FROM, W1_TO)
        cw2 = await load_week(figi, W2_FROM, W2_TO)
        cw3 = await load_week(figi, W3_FROM, W3_TO)

        # V2 baseline
        def _base(cw, f_, t_):
            return run_arm(cw, figi, lot, f_, t_, 4.0, 4.0, 2, 0.0, dict(V2_PARAMS))
        b1 = _base(cw1, W1_FROM, W1_TO)
        b2 = _base(cw2, W2_FROM, W2_TO)
        b3 = _base(cw3, W3_FROM, W3_TO)
        print(f"  V2 base: w1={b1[0]:+.0f}/{b1[1]}  w2={b2[0]:+.0f}/{b2[1]}  w3={b3[0]:+.0f}/{b3[1]}")

        study = optuna.create_study(direction="maximize",
                                    sampler=optuna.samplers.TPESampler(seed=42))
        t0 = time.time()
        study.optimize(objective_factory(cw1, figi, lot), n_trials=args.trials)
        dt = time.time() - t0
        bp = study.best_params
        best_params = {}
        for sid in ALL_SIDS:
            best_params[sid] = suggest_strategy_params_from_dict(bp, sid)
        best_vol = bp["vol_thr"] if bp["vol_thr"] > 0 else 0.0

        v1 = run_arm(cw1, figi, lot, W1_FROM, W1_TO, bp["sl_mult"], bp["rr"], bp["quorum"], best_vol, best_params)
        v2 = run_arm(cw2, figi, lot, W2_FROM, W2_TO, bp["sl_mult"], bp["rr"], bp["quorum"], best_vol, best_params)
        v3 = run_arm(cw3, figi, lot, W3_FROM, W3_TO, bp["sl_mult"], bp["rr"], bp["quorum"], best_vol, best_params)
        active = [s for s in ALL_SIDS if bp.get(f"inc_{s}", False)]
        print(f"  Optuna {args.trials}t in {dt:.0f}s best={study.best_value:+.0f}")
        print(f"  params sl={bp['sl_mult']} rr={bp['rr']} q={bp['quorum']} vol={best_vol} active={active}")
        for sid in ALL_SIDS:
            if bp.get(f"inc_{sid}"):
                print(f"    {sid}: {best_params[sid]}")
        print(f"  OPT  w1={v1[0]:+.0f}/{v1[1]}  w2={v2[0]:+.0f}/{v2[1]}  w3={v3[0]:+.0f}/{v3[1]}")

        results[ticker] = {
            "figi": figi, "lot": lot, "trials": args.trials, "elapsed_s": round(dt, 1),
            "best_params": bp, "best_strat_params": best_params,
            "baseline": {"w1": b1, "w2": b2, "w3": b3},
            "opt": {"w1": v1, "w2": v2, "w3": v3},
        }

    print("\n===== SUMMARY =====")
    for tk, r in results.items():
        b, o = r["baseline"], r["opt"]
        d2 = o["w2"][0] - b["w2"][0]
        d3 = o["w3"][0] - b["w3"][0]
        print(f"{tk:6s} w1 {b['w1'][0]:+.0f}->{o['w1'][0]:+.0f} | "
              f"w2 {b['w2'][0]:+.0f}->{o['w2'][0]:+.0f} ({d2:+.0f}) | "
              f"w3 {b['w3'][0]:+.0f}->{o['w3'][0]:+.0f} ({d3:+.0f})")

    out = os.path.join(os.path.dirname(__file__), "optuna_params_results.json")
    with open(out, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("\nSaved to", out)


def suggest_strategy_params_from_dict(bp, sid):
    """Rebuild strat_params from flat trial params dict."""
    out = dict(V2_PARAMS.get(sid, {}))
    if not bp.get(f"inc_{sid}", False):
        return out
    for pname, lo, hi, step in STRAT_PARAM_RANGES.get(sid, []):
        key = f"{sid}.{pname}"
        if key in bp:
            out[pname] = bp[key]
    return out


if __name__ == "__main__":
    asyncio.run(main())
