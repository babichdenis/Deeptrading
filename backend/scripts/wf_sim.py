#!/usr/bin/env python3
"""wf_sim.py - Stage 14: WALK-FORWARD (rolling out-of-sample) validation.

Master ledger: optuna_regime_sim.py (16 stages). This script = stage 14,
the final stability gate for the champion before any live-promotion decision.

Facts driving the design:
  * stage 03: JOINT params fitted on TRAIN 12-16.09, picked on VAL 17-19.09;
  * stage 16/A blind 01-11.09 (untouched days): REF +3961.84 (n=487, wr 68.6%,
    PF 2.03) vs JOINT +3539.75 (n=488, wr 67.6%, PF 1.85) -> REF STANDS;
  * stage 15 monte-carlo: champion risk acceptable (results_mc_champion.json);
  * stage 14 question: does the optimize->trade loop add OOS value when rolled
    forward day by day (the deployment reality), vs keeping REF fixed?

Protocol (pre-registered; no OOS peeking, no parameter changes after OOS):
  * data: blind_data.pkl (25.08-12.09 MSK) + q2optuna_data.pkl (12-19.09 MSK,
    12.09 deduped) + wf_fresh.pkl (>= 20.09, read-only dump from the live DB).
    Days < 01.09 are warmup only (stage 16/A protocol).
  * timeline day = >= MIN_BARS_PER_DAY 1m bars AND last bar >= 18:50 MSK
    (an in-progress current day is excluded automatically).
  * folds: expanding train (all timeline days < D, at least MIN_TRAIN_DAYS)
    -> test on D (1 day).
  * fold fit: Optuna TPE(seed=1000+fold), WF_TRIALS trials over the stage-03
    JOINT space (per-setup params of the 6 REF setups + thr 1.2..2.6).
    Objective = train net. Exit env EXACTLY the champion one via
    blind_test.simulate (cost 0.045%/side, ~6400 RUR/pos, session, day_end
    close, RSI(14) 1m 70/30, cd15).
  * OOS arms on D: REF (fixed champion, thr=2.0), JOINT-fixed (stage-03
    winner, JOINT_THR), WF-refit (fold-best params+thr).
  * output: results_wf.json rewritten after every fold (crash-safe).

Run (worker .8):  python wf_sim.py    (~15-25 min: 40 trials x ~12 folds)
Env: WF_TRIALS=40, WF_TIME_LIMIT=2700 (sec).
"""
import json
import os
import pickle
import sys
import time
from collections import Counter
from datetime import date, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import optuna

from blind_test import (JOINT_THR, LOOKBACK_MIN, SIDS, build, counts_for,
                        events_for, simulate)
from app.engine.models import Candle
from app.services.ensemble import resample

N_TRIALS = int(os.environ.get("WF_TRIALS", "40"))
TIME_LIMIT = int(os.environ.get("WF_TIME_LIMIT", "2700"))
MIN_TRAIN_DAYS = 6
MIN_BARS_PER_DAY = 200
DAY_COMPLETE_HM = 18 * 60 + 50
TIMELINE_FIRST = date(2026, 9, 1)
REF_THR = 2.0
OUT = os.path.join(HERE, "results_wf.json")


def _utc(t):
    return t if getattr(t, "tzinfo", None) else t.replace(tzinfo=timezone.utc)


def load_raw():
    raw = {}

    def add(path):
        if not os.path.exists(path):
            return 0
        with open(path, "rb") as fh:
            d = pickle.load(fh)
        n = 0
        for f, v in d.items():
            rows = [(_utc(r[0]), r[1], r[2], r[3], r[4], r[5])
                    for r in v["candles"]]
            if f not in raw:
                raw[f] = {"ticker": str(v.get("ticker") or f), "candles": rows}
                n += len(rows)
            else:
                seen = set(r[0] for r in raw[f]["candles"])
                extra = [r for r in rows if r[0] not in seen]
                raw[f]["candles"].extend(extra)
                n += len(extra)
        return n

    src = []
    for name in ("blind_data.pkl", "q2optuna_data.pkl", "wf_fresh.pkl"):
        n = add(os.path.join(HERE, "data", name))
        src.append("%s=%d" % (name, n))
    for f in raw:
        raw[f]["candles"].sort(key=lambda r: r[0])
    return raw, src


# ------- stage-03 JOINT search space (prefix-unique optuna param names) -------

def joint_space(trial):
    """(params_by_sid, thr) of one candidate; mirrors optuna_params_sim SPACE."""
    p = {}
    for sid in SIDS:
        if "rsi" in sid:
            p[sid] = {"period": trial.suggest_int("rsi_period", 8, 24),
                      "oversold": round(trial.suggest_float("rsi_os", 20.0, 40.0), 1),
                      "overbought": round(trial.suggest_float("rsi_ob", 60.0, 90.0), 1)}
        elif "boll" in sid:
            p[sid] = {"period": trial.suggest_int("boll_period", 8, 30),
                      "k": round(trial.suggest_float("boll_k", 0.6, 2.4), 2)}
        elif "vwap" in sid:
            p[sid] = {"k": round(trial.suggest_float("vwap_k", 0.5, 3.0), 2)}
        elif "macd" in sid:
            fast = trial.suggest_int("m_fast", 6, 16)
            slow = trial.suggest_int("m_slow", 20, 40)
            p[sid] = {"fast": fast, "slow": max(slow, fast + 4),
                      "signal_period": trial.suggest_int("m_sig", 5, 13)}
        elif "donch" in sid:
            p[sid] = {"period": trial.suggest_int("donch_period", 20, 90)}
        elif "volume" in sid:
            p[sid] = {"ma_len": trial.suggest_int("vol_ma", 10, 40),
                      "drop_ratio": round(trial.suggest_float("vol_drop", 1.1, 2.5), 2)}
    thr = trial.suggest_float("thr", 1.2, 2.6)
    return p, thr


class _Shim(object):
    """Rebuild a config from stored best_params through the same joint_space."""

    def __init__(self, bp):
        self.bp = bp

    def suggest_int(self, name, lo, hi):
        return int(self.bp[name])

    def suggest_float(self, name, lo, hi):
        return float(self.bp[name])


def _metrics(trades):
    """trades must be sorted by entry ts; pnl at index 4."""
    pn = [float(t[4]) for t in trades]
    net = sum(pn)
    n = len(pn)
    wr = 100.0 * sum(1 for x in pn if x > 0) / n if n else 0.0
    gp = sum(x for x in pn if x > 0)
    gl = -sum(x for x in pn if x < 0)
    eq = peak = mdd = 0.0
    for x in pn:
        eq += x
        if eq > peak:
            peak = eq
        if peak - eq > mdd:
            mdd = peak - eq
    return {"net": round(net, 2), "n": n, "wr": round(wr, 1),
            "pf": round(gp / gl, 2) if gl > 0 else None,
            "maxdd": round(mdd, 2)}


def main():
    t0 = time.perf_counter()
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    raw, src = load_raw()
    per, _ev = build(raw)
    if not per:
        print("no data")
        sys.exit(1)
    print("=== STAGE 14: WALK-FORWARD (expanding train -> 1-day OOS) ===")
    print("data %s | figis %d | 1m bars %d | prep %.1fs" % (
        " ".join(src), len(per), sum(len(d["ts"]) for d in per.values()),
        time.perf_counter() - t0))
    sys.stdout.flush()

    bars10 = {}
    for f in sorted(per):
        rows = raw[f]["candles"]
        candles = [Candle(ts=r[0], open=float(r[1]), high=float(r[2]),
                          low=float(r[3]), close=float(r[4]),
                          volume=int(r[5] or 0)) for r in rows]
        bars10[f] = resample(candles, 600)

    cnt = Counter()
    last_hm = {}
    for d in per.values():
        for o, h in zip(d["day"], d["hm"]):
            cnt[o] += 1
            if h > last_hm.get(o, -1):
                last_hm[o] = h
    days_ok = sorted(o for o, n in cnt.items()
                     if n >= MIN_BARS_PER_DAY and last_hm.get(o, -1) >= DAY_COMPLETE_HM)
    timeline = [o for o in days_ok if date.fromordinal(o) >= TIMELINE_FIRST]
    if not timeline:
        print("no timeline days")
        sys.exit(1)
    print("days: %d with data, %d complete; timeline %s..%s (%d days)" % (
        len(cnt), len(days_ok), date.fromordinal(timeline[0]).isoformat(),
        date.fromordinal(timeline[-1]).isoformat(), len(timeline)))
    sys.stdout.flush()

    res_folds = []
    oos_ref, oos_joint, oos_wf = [], [], []
    idxs = [i for i in range(len(timeline)) if i >= MIN_TRAIN_DAYS]

    def dump(done):
        results = {
            "stage": "14 walk-forward",
            "env": {"n_trials": N_TRIALS, "min_train_days": MIN_TRAIN_DAYS,
                    "min_bars_per_day": MIN_BARS_PER_DAY,
                    "timeline_first": TIMELINE_FIRST.isoformat(),
                    "ref_thr": REF_THR, "joint_thr": float(JOINT_THR),
                    "sources": src,
                    "elapsed_sec": round(time.perf_counter() - t0, 1)},
            "folds": res_folds,
            "oos_ref": _metrics(sorted(oos_ref, key=lambda t: t[0])),
            "oos_joint": _metrics(sorted(oos_joint, key=lambda t: t[0])),
            "oos_wf": _metrics(sorted(oos_wf, key=lambda t: t[0])),
            "done": done,
        }
        with open(OUT, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1)

    if not idxs:
        dump(True)
        print("no eligible folds (timeline too short)")
        sys.exit(0)
    print("folds: %d (test %s..%s) x %d trials" % (
        len(idxs), date.fromordinal(timeline[idxs[0]]).isoformat(),
        date.fromordinal(timeline[idxs[-1]]).isoformat(), N_TRIALS))
    sys.stdout.flush()

    for k, i in enumerate(idxs):
        if time.perf_counter() - t0 > TIME_LIMIT:
            print("time limit hit after %d folds" % k)
            break
        tf = time.perf_counter()
        train_set = set(timeline[:i])
        test_o = timeline[i]
        dstr = date.fromordinal(test_o).isoformat()
        train_view = {f: dict(d, blind=[o in train_set for o in d["day"]])
                      for f, d in per.items()}
        test_view = {f: dict(d, blind=[o == test_o for o in d["day"]])
                     for f, d in per.items()}

        def objective(trial):
            try:
                p, thr = joint_space(trial)
                setups = [(sid, p[sid]) for sid in SIDS if sid in p]
                net = 0.0
                for f, d in per.items():
                    ev = events_for(setups, bars10[f])
                    nb, ns = counts_for(d["ts"], ev, LOOKBACK_MIN * 60)
                    net += sum(t[4] for t in simulate(train_view[f], nb, ns, thr))
                return net
            except Exception:
                return -1e9

        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=1000 + k))
        study.optimize(objective, n_trials=N_TRIALS)
        p_best, thr_best = joint_space(_Shim(study.best_params))
        setups_best = [(sid, p_best[sid]) for sid in SIDS if sid in p_best]

        ref_tr, jnt_tr, wf_tr = [], [], []
        for f, d in per.items():
            nb, ns = d["votes"]["REF"]
            ref_tr += simulate(test_view[f], nb, ns, REF_THR)
            nb, ns = d["votes"]["JOINT"]
            jnt_tr += simulate(test_view[f], nb, ns, JOINT_THR)
            ev = events_for(setups_best, bars10[f])
            nb, ns = counts_for(d["ts"], ev, LOOKBACK_MIN * 60)
            wf_tr += simulate(test_view[f], nb, ns, thr_best)
        oos_ref += ref_tr
        oos_joint += jnt_tr
        oos_wf += wf_tr

        row = {"test_day": dstr, "train_days": len(train_set),
               "train_net": round(study.best_value, 2),
               "thr": round(float(thr_best), 3),
               "oos_ref_net": round(sum(t[4] for t in ref_tr), 2),
               "oos_joint_net": round(sum(t[4] for t in jnt_tr), 2),
               "oos_wf_net": round(sum(t[4] for t in wf_tr), 2),
               "oos_ref_n": len(ref_tr), "oos_joint_n": len(jnt_tr),
               "oos_wf_n": len(wf_tr),
               "params": p_best,
               "sec": round(time.perf_counter() - tf, 1)}
        res_folds.append(row)
        dump(False)
        print("fold %2d/%d %s | train %2dd net %+10.2f | OOS REF %+9.2f (n=%3d) | "
              "JOINT %+9.2f (n=%3d) | WF %+9.2f (n=%3d) thr %.2f | %.0fs" % (
                  k + 1, len(idxs), dstr, len(train_set), study.best_value,
                  row["oos_ref_net"], row["oos_ref_n"],
                  row["oos_joint_net"], row["oos_joint_n"],
                  row["oos_wf_net"], row["oos_wf_n"], row["thr"], row["sec"]))
        sys.stdout.flush()

    mr = _metrics(sorted(oos_ref, key=lambda t: t[0]))
    mj = _metrics(sorted(oos_joint, key=lambda t: t[0]))
    mw = _metrics(sorted(oos_wf, key=lambda t: t[0]))
    print()
    print("=== OOS TOTALS (walk-forward timeline) ===")
    print("  REF   (fixed champion): net %+0.2f n=%d wr %.1f%% pf %s maxdd %s" % (
        mr["net"], mr["n"], mr["wr"], mr["pf"], mr["maxdd"]))
    print("  JOINT (stage-03 fixed): net %+0.2f n=%d wr %.1f%% pf %s maxdd %s" % (
        mj["net"], mj["n"], mj["wr"], mj["pf"], mj["maxdd"]))
    print("  WF    (daily refit)   : net %+0.2f n=%d wr %.1f%% pf %s maxdd %s" % (
        mw["net"], mw["n"], mw["wr"], mw["pf"], mw["maxdd"]))

    concl = []
    if res_folds:
        thrs = [r["thr"] for r in res_folds]
        wf_beats = sum(1 for r in res_folds if r["oos_wf_net"] > r["oos_ref_net"])
        concl.append("timeline %s..%s: %d folds x %d trials (expanding train >= %dd)"
                     % (res_folds[0]["test_day"], res_folds[-1]["test_day"],
                        len(res_folds), N_TRIALS, MIN_TRAIN_DAYS))
        concl.append("OOS totals: REF %+0.2f (n=%d) | JOINT-fixed %+0.2f (n=%d) | "
                     "WF-refit %+0.2f (n=%d)"
                     % (mr["net"], mr["n"], mj["net"], mj["n"],
                        mw["net"], mw["n"]))
        concl.append("per-day: WF-refit beats REF on %d/%d days; thr %.2f..%.2f"
                     % (wf_beats, len(res_folds), min(thrs), max(thrs)))
        pr = {}
        for r in res_folds:
            for sid, pp in (r.get("params") or {}).items():
                for k2, v2 in (pp or {}).items():
                    pr.setdefault("%s.%s" % (sid, k2), []).append(v2)
        ranges = "; ".join("%s %s..%s" % (k2, min(v), max(v))
                           for k2, v in sorted(pr.items()))
        concl.append("refit stability (min..max over folds): " + ranges)
        verdict = ("WF-refit ADDS OOS value -> consider scheduled refit in live ops"
                   if mw["net"] > mr["net"] else
                   "REF STANDS: rolling refit does not add OOS value; keep fixed champion")
        concl.append("VERDICT: " + verdict)
    else:
        concl.append("no folds produced")
    print()
    print("=== CONCLUSIONS (results_wf.json) ===")
    for c in concl:
        print("  - %s" % c)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"stage": "14 walk-forward",
                   "env": {"n_trials": N_TRIALS,
                           "min_train_days": MIN_TRAIN_DAYS,
                           "timeline_first": TIMELINE_FIRST.isoformat(),
                           "ref_thr": REF_THR, "joint_thr": float(JOINT_THR),
                           "sources": src},
                   "folds": res_folds,
                   "oos_ref": mr, "oos_joint": mj, "oos_wf": mw,
                   "conclusions": concl, "done": True},
                  fh, ensure_ascii=False, indent=1)
    print("done in %.1f min -> %s" % ((time.perf_counter() - t0) / 60.0, OUT))


if __name__ == "__main__":
    main()
