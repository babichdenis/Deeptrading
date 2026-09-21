#!/usr/bin/env python3
"""optuna_votes_sim.py - staged Optuna pipeline for ensemble VOTE STRENGTHS (worker .2).

Strategy for this session (user): fast offline scripts on machine .2; when a
stage yields a result, cross-check the winner through the live bot (.4).
Order (agreed plan): baseline REF -> individual setups -> ablation ->
vote WEIGHTS (Optuna) -> EXIT (Optuna) -> per-ticker enable (Optuna,
diagnostic) -> stability; conclusions recorded to results_optuna_votes.json
for future sessions. Per-setup PARAMETER tuning (RSI period etc.) is the NEXT
script - it needs signal recompute per trial and is kept separate on purpose.

Fixed environment (parity with every previous sim):
  day session 9:50-19:00 MSK entries, bar turnover >= 50k RUR,
  ~6400 RUR/position, cost 0.045%/side, close at date change (midnight),
  RSI(14) 1m exit (base 70/30), cooldown 15 bars, votes = 6 setups on 10m
  bars accumulated over a 15m window (identical to q2_rsi_sim / mtf_sim REF).

Data: q2optuna_data.pkl - 26 figis, 1m bars 12-19.09.
Split: TRAIN 12-16.09 (5 days) / VAL 17-19.09 (3 days, last day partial).
  Optuna objective = TRAIN net - 0.3*pstdev(daily nets) - 50*max(0, 40-n);
  winner = best VAL net among top-12 trials (search on train, select on val).
  NOTE: no untouched TEST period remains in this dataset -> blind test on
  fresh data is a required follow-up (recorded in conclusions).

Run on .2:  cd q2opt & python -u optuna_votes_sim.py
Run on .4:  Q2DATA=backend/data/q2optuna_data.pkl python -u scripts/optuna_votes_sim.py
"""
from __future__ import annotations

import json
import os
import pickle
import statistics
import sys
import time
from bisect import bisect_left
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("Q2ROOT") or HERE)

from app.engine.models import Candle  # noqa: E402
from app.services.ensemble import generate_signals, resample  # noqa: E402

import optuna  # noqa: E402

optuna.logging.set_verbosity(optuna.logging.WARNING)

MSK = timezone(timedelta(hours=3))
COST = 0.00045
NOTIONAL = 6400.0
TURNOVER = 50000.0
LOOKBACK_MIN = 15
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
TRAIN_LAST = date(2026, 9, 16)
VAL_FIRST = date(2026, 9, 17)
M10_SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
]
SIDS = [s for s, _ in M10_SETUPS]
SHORT = {"rsi_reversal": "rsi", "bollinger_reclaim": "boll", "vwap_reclaim": "vwap",
         "macd_cross": "macd", "donchian_breakout": "donch", "volume_drop": "vold"}
REF = {"w": [1.0] * 6, "thr": 2.0, "opp": 0.0, "ob": 70.0, "os": 30.0, "cd": 15}

PRIOR_FINDINGS = [
    "champion: q2c entries (quorum-2 of 6 setups, 10m votes, 15m window) + RSI(14)1m 70/30 exit + cooldown 15 -> full-period net +4046..+4294 (12-19.09)",
    "M10->M5->M1 cascade REJECTED: best cascade +3818 < REF +4294 (mtf_sim run 2)",
    "cooldown 0 REJECTED: +437 vs +4294 - churn right after exit eats the edge",
    "MACD-1m-cross entries negative across all tests; ensemble quorum is the only positive entry base",
    "live Q2rsi2 (all muzzles off): remaining gap vs sim is driven mainly by stop_loss exits (absent in the sim)",
]


def rsi_wilder(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, 50.0)
    if n < period + 1:
        return out
    d = np.diff(closes)
    g = np.maximum(d, 0.0)
    l = np.maximum(-d, 0.0)
    ag = float(g[:period].mean())
    al = float(l[:period].mean())

    def v(a, b):
        if b <= 0:
            return 100.0 if a > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + a / b)

    out[period] = v(ag, al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + float(g[i - 1])) / period
        al = (al * (period - 1) + float(l[i - 1])) / period
        out[i] = v(ag, al)
    return out


def states_for(ts, ev_list, sids, lookback_sec):
    ev_t = [e[0] for e in ev_list]
    out = []
    j = 0
    lb = timedelta(seconds=lookback_sec)
    for t in ts:
        while j < len(ev_list) and ev_list[j][0] <= t:
            j += 1
        lo_i = bisect_left(ev_t, t - lb)
        buy = set()
        sell = set()
        for k in range(lo_i, j):
            buy |= ev_list[k][1]
            sell |= ev_list[k][2]
        out.append(tuple(1 if s in buy else (-1 if s in sell else 0) for s in sids))
    return out


def build_per(raw):
    per = {}
    for f in sorted(raw):
        rows = raw[f]["candles"]
        if len(rows) < 300:
            continue
        candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]), low=float(x[3]),
                          close=float(x[4]), volume=int(x[5] or 0)) for x in rows]
        ts = [c.ts for c in candles]
        ev = defaultdict(lambda: {"B": set(), "S": set()})
        for sid, params in M10_SETUPS:
            bars = resample(candles, 600)
            try:
                sigs = generate_signals(sid, params, bars)
            except Exception:
                sigs = []
            for sg in sigs:
                sds = str(sg.get("side", "")).upper()
                if "BUY" in sds:
                    ev[sg["ts"]]["B"].add(sid)
                elif "SELL" in sds:
                    ev[sg["ts"]]["S"].add(sid)
        ev_list = [(t, ev[t]["B"], ev[t]["S"]) for t in sorted(ev)]
        states = states_for(ts, ev_list, SIDS, LOOKBACK_MIN * 60)
        votes = np.array(states, dtype=np.int8)
        cl = [float(c.close) for c in candles]
        vl = [float(c.volume or 0) for c in candles]
        r = rsi_wilder(cl, 14)
        day_l = []
        hm_l = []
        m_tr = []
        m_va = []
        for t in ts:
            tm = t.astimezone(MSK)
            d0 = tm.date()
            day_l.append(d0.toordinal())
            hm_l.append(tm.hour * 60 + tm.minute)
            m_tr.append(d0 <= TRAIN_LAST)
            m_va.append(d0 >= VAL_FIRST)
        per[f] = dict(
            ticker=str(raw[f].get("ticker") or f),
            ts=ts, cl_l=cl, vl_l=vl, r_l=[float(x) for x in r],
            day_l=day_l, hm_l=hm_l, m_tr=m_tr, m_va=m_va,
            Vp=(votes == 1).astype(np.float32),
            Vm=(votes == -1).astype(np.float32),
        )
    return per


def simulate(d, bs, ss, thr, opp, ob, os_, cd, mask):
    cl = d["cl_l"]
    vl = d["vl_l"]
    r = d["r_l"]
    day = d["day_l"]
    hm = d["hm_l"]
    ts = d["ts"]
    n = len(ts)
    out = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    for i in range(1, n):
        if pos and day[i] != day[i - 1]:
            px = cl[i - 1]
            out.append((ts[entry_i], ts[i - 1], sgn,
                        sgn * (px - ep) * eq - COST * (ep + px) * eq))
            pos = 0
            last_exit = i - 1
        if i < 60:
            continue
        if pos == 0:
            if i - last_exit <= cd:
                continue
            if cl[i] * vl[i] < TURNOVER:
                continue
            h = hm[i]
            if not (DAY_START <= h < DAY_END):
                continue
            if mask is not None and not mask[i]:
                continue
            b = bs[i]
            s = ss[i]
            if b >= thr and s <= opp:
                want = 1
            elif s >= thr and b <= opp:
                want = -1
            else:
                continue
            sgn = want
            ep = cl[i]
            eq = max(1, round(NOTIONAL / ep))
            pos = 1
            entry_i = i
        else:
            if (sgn == 1 and r[i] > ob) or (sgn == -1 and r[i] < os_):
                px = cl[i]
                out.append((ts[entry_i], ts[i], sgn,
                            sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
    if pos:
        px = cl[-1]
        out.append((ts[entry_i], ts[-1], sgn,
                    sgn * (px - ep) * eq - COST * (ep + px) * eq))
    return out


def run_all(per, cfg, split, figis=None):
    trades = []
    w = np.array(cfg["w"], dtype=np.float32)
    for f, d in per.items():
        if figis is not None and f not in figis:
            continue
        bs = (d["Vp"] @ w).tolist()
        ss = (d["Vm"] @ w).tolist()
        if split == "train":
            mask = d["m_tr"]
        elif split == "val":
            mask = d["m_va"]
        else:
            mask = None
        for t in simulate(d, bs, ss, cfg["thr"], cfg["opp"], cfg["ob"], cfg["os"], cfg["cd"], mask):
            trades.append(t + (d["ticker"],))
    return trades


def agg(trades, days=None):
    n = len(trades)
    wins = sum(1 for t in trades if t[3] > 0)
    net = sum(t[3] for t in trades)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t[0].astimezone(MSK).date().isoformat()] += t[3]
    vals = [by_day.get(d, 0.0) for d in days] if days else list(by_day.values())
    dstd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    ls = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = "L" if t[2] == 1 else "S"
        ls[k][0] += 1
        ls[k][1] += t[3]
    pts = []
    for t in trades:
        pts.append((t[0], 1))
        pts.append((t[1], -1))
    pts.sort()
    cur = mx = 0
    for _t, dd in pts:
        cur += dd
        if cur > mx:
            mx = cur
    tk = defaultdict(lambda: [0, 0.0])
    for t in trades:
        tk[t[4]][0] += 1
        tk[t[4]][1] += t[3]
    return dict(n=n, wins=wins, net=net, day_std=dstd,
                by_day={k: round(v, 2) for k, v in by_day.items()},
                L=[ls["L"][0], round(ls["L"][1], 2)],
                S=[ls["S"][0], round(ls["S"][1], 2)], mx=mx,
                by_tk={k: [v[0], round(v[1], 2)] for k, v in tk.items()})


def fmt_cfg(cfg):
    return ("w=[%s] thr=%.2f opp=%.2f ob=%.0f os=%.0f cd=%d" % (
        ",".join("%.2f" % x for x in cfg["w"]),
        cfg["thr"], cfg["opp"], cfg["ob"], cfg["os"], cfg["cd"]))


def params_to_cfg(params, exit_cfg):
    w = [float(params["w_" + sid]) for sid in SIDS]
    return {"w": w,
            "thr": float(params["thr"]),
            "opp": float(params["opp"]),
            "ob": exit_cfg["ob"], "os": exit_cfg["os"], "cd": exit_cfg["cd"]}


def pick_best(study, topk=12, min_train_n=40):
    cands = [t for t in study.trials
             if t.state == optuna.trial.TrialState.COMPLETE
             and t.user_attrs.get("train_n", 0) >= min_train_n]
    cands.sort(key=lambda t: -(t.value or -1e9))
    cands = cands[:topk]
    if not cands:
        cands = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    if not cands:
        return None, []
    best = max(cands, key=lambda t: t.user_attrs.get("val_net", -1e9))
    return best, cands


def main():
    t_start = time.perf_counter()
    data_path = os.environ.get("Q2DATA") or os.path.join(HERE, "data", "q2optuna_data.pkl")
    if not os.path.exists(data_path):
        print("DATA NOT FOUND: %s" % data_path)
        sys.exit(1)
    with open(data_path, "rb") as fh:
        raw = pickle.load(fh)
    per = build_per(raw)
    if not per:
        print("no data")
        sys.exit(1)
    all_days = sorted({t.astimezone(MSK).date() for d in per.values() for t in d["ts"]})
    train_days = [d for d in all_days if d <= TRAIN_LAST]
    val_days = [d for d in all_days if d >= VAL_FIRST]
    tr_iso = [d.isoformat() for d in train_days]
    va_iso = [d.isoformat() for d in val_days]
    n_bars = sum(len(d["ts"]) for d in per.values())
    print("=== OPTUNA VOTES: staged pipeline (search on TRAIN, select on VAL) ===")
    print("data: figis=%d | 1m bars=%d | %s -> %s MSK" % (
        len(per), n_bars,
        min(d["ts"][0] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(d["ts"][-1] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M")))
    print("TRAIN days: %s | VAL days: %s (last partial)" % (tr_iso, va_iso))
    print("env: cost %.3f%%/side, ~%.0f RUR/pos, session 9:50-19:00, cd=15, exit RSI 70/30" % (
        100 * COST, NOTIONAL))
    results = {"when": datetime.now().isoformat(),
               "data": {"figis": len(per), "bars": n_bars},
               "train_days": tr_iso, "val_days": va_iso}

    # ---------- 0. REF (equal weights = q2c champion) ----------
    ref_tr = agg(run_all(per, REF, "train"), train_days)
    ref_va = agg(run_all(per, REF, "val"), val_days)
    print()
    print("=== 0. REF (equal weights, thr=2, opp=0, rsi 70/30, cd=15) ===")
    print("  TRAIN: n=%d wr=%.1f%% net=%+.2f day_std=%.0f | VAL: n=%d wr=%.1f%% net=%+.2f" % (
        ref_tr["n"], 100.0 * ref_tr["wins"] / max(ref_tr["n"], 1), ref_tr["net"], ref_tr["day_std"],
        ref_va["n"], 100.0 * ref_va["wins"] / max(ref_va["n"], 1), ref_va["net"]))
    print("  REF by day (train): %s" % ref_tr["by_day"])
    print("  REF by day (val)  : %s" % ref_va["by_day"])
    results["ref"] = {"train": ref_tr, "val": ref_va, "cfg": REF}

    # ---------- 1. INDIVIDUAL setups (each alone, thr=1.0) ----------
    print()
    print("=== 1. INDIVIDUAL setups (w=e_k, thr=1.0) - train/val net ===")
    indiv = {}
    for k, (sid, _p) in enumerate(M10_SETUPS):
        w = [0.0] * 6
        w[k] = 1.0
        cfg = dict(REF)
        cfg["w"] = w
        cfg["thr"] = 1.0
        m_tr = agg(run_all(per, cfg, "train"), train_days)
        m_va = agg(run_all(per, cfg, "val"), val_days)
        indiv[sid] = {"train": {k2: v for k2, v in m_tr.items() if k2 != "by_tk"},
                      "val": {k2: v for k2, v in m_va.items() if k2 != "by_tk"}}
        print("  %-20s TRAIN n=%4d wr=%5.1f%% net=%+9.2f | VAL n=%4d wr=%5.1f%% net=%+9.2f" % (
            sid, m_tr["n"], 100.0 * m_tr["wins"] / max(m_tr["n"], 1), m_tr["net"],
            m_va["n"], 100.0 * m_va["wins"] / max(m_va["n"], 1), m_va["net"]))
    results["individual"] = indiv

    # ---------- 2. ABLATION (drop one setup) ----------
    print()
    print("=== 2. ABLATION (w_k=0, others 1, thr=2.0) - train/val net vs REF ===")
    abl = {}
    for k, (sid, _p) in enumerate(M10_SETUPS):
        w = [1.0] * 6
        w[k] = 0.0
        cfg = dict(REF)
        cfg["w"] = w
        m_tr = agg(run_all(per, cfg, "train"), train_days)
        m_va = agg(run_all(per, cfg, "val"), val_days)
        abl["-" + sid] = {"train": {k2: v for k2, v in m_tr.items() if k2 != "by_tk"},
                          "val": {k2: v for k2, v in m_va.items() if k2 != "by_tk"}}
        print("  -%-19s TRAIN n=%4d net=%+9.2f (d %+8.2f) | VAL n=%4d net=%+9.2f (d %+8.2f)" % (
            sid, m_tr["n"], m_tr["net"], m_tr["net"] - ref_tr["net"],
            m_va["n"], m_va["net"], m_va["net"] - ref_va["net"]))
    results["ablation"] = abl

    # ---------- 3. STAGE A: Optuna vote weights + thr + opp ----------
    def space_A(trial):
        w = [trial.suggest_float("w_" + sid, 0.0, 1.0) for sid in SIDS]
        return {"w": w,
                "thr": trial.suggest_float("thr", 0.75, 2.5),
                "opp": trial.suggest_float("opp", 0.0, 0.2),
                "ob": REF["ob"], "os": REF["os"], "cd": REF["cd"]}

    def make_objective(space):
        def objective(trial):
            cfg = space(trial)
            m_tr = agg(run_all(per, cfg, "train"), train_days)
            m_va = agg(run_all(per, cfg, "val"), val_days)
            trial.set_user_attr("train_net", m_tr["net"])
            trial.set_user_attr("train_n", m_tr["n"])
            trial.set_user_attr("train_dstd", m_tr["day_std"])
            trial.set_user_attr("val_net", m_va["net"])
            trial.set_user_attr("val_n", m_va["n"])
            o = m_tr["net"] - 0.3 * m_tr["day_std"]
            if m_tr["n"] < 40:
                o -= 50.0 * (40 - m_tr["n"])
            return o
        return objective

    t0 = time.perf_counter()
    studyA = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=7))
    studyA.optimize(make_objective(space_A), n_trials=220)
    bestA, candsA = pick_best(studyA)
    print()
    print("=== 3. STAGE A: Optuna weights+thr+opp (220 trials, %.0fs) ===" % (time.perf_counter() - t0))
    print("  leaderboard (top by VAL among top-12 by train-objective):")
    for t in candsA[:10]:
        print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | %s" % (
            t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
            t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
            fmt_cfg(params_to_cfg(t.params, REF))))
    cfgA = params_to_cfg(bestA.params, REF) if bestA else dict(REF)
    a_tr = agg(run_all(per, cfgA, "train"), train_days)
    a_va = agg(run_all(per, cfgA, "val"), val_days)
    print("  WINNER A: %s" % fmt_cfg(cfgA))
    print("  TRAIN n=%d wr=%.1f%% net=%+.2f | VAL n=%d wr=%.1f%% net=%+.2f  (REF val %+.2f)" % (
        a_tr["n"], 100.0 * a_tr["wins"] / max(a_tr["n"], 1), a_tr["net"],
        a_va["n"], 100.0 * a_va["wins"] / max(a_va["n"], 1), a_va["net"], ref_va["net"]))
    results["stageA"] = {"cfg": cfgA, "train": a_tr, "val": a_va,
                         "n_trials": len(studyA.trials)}

    # ---------- 4. STAGE B: Optuna exit (ob/os/cd) on A-winner ----------
    def space_B(trial):
        return {"w": cfgA["w"], "thr": cfgA["thr"], "opp": cfgA["opp"],
                "ob": trial.suggest_float("ob", 55.0, 85.0),
                "os": trial.suggest_float("os", 15.0, 45.0),
                "cd": trial.suggest_int("cd", 5, 45)}

    t0 = time.perf_counter()
    studyB = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=11))
    studyB.optimize(make_objective(space_B), n_trials=160)
    bestB, candsB = pick_best(studyB)
    print()
    print("=== 4. STAGE B: Optuna exit ob/os/cd on A-winner (160 trials, %.0fs) ===" % (time.perf_counter() - t0))
    for t in candsB[:10]:
        p = t.params
        print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | ob=%.0f os=%.0f cd=%d" % (
            t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
            t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
            p["ob"], p["os"], p["cd"]))
    if bestB:
        cfgB = dict(cfgA)
        cfgB["ob"] = float(bestB.params["ob"])
        cfgB["os"] = float(bestB.params["os"])
        cfgB["cd"] = int(bestB.params["cd"])
    else:
        cfgB = dict(cfgA)
    b_tr = agg(run_all(per, cfgB, "train"), train_days)
    b_va = agg(run_all(per, cfgB, "val"), val_days)
    print("  WINNER B: %s" % fmt_cfg(cfgB))
    print("  TRAIN n=%d wr=%.1f%% net=%+.2f | VAL n=%d wr=%.1f%% net=%+.2f  (A-val %+.2f, REF-val %+.2f)" % (
        b_tr["n"], 100.0 * b_tr["wins"] / max(b_tr["n"], 1), b_tr["net"],
        b_va["n"], 100.0 * b_va["wins"] / max(b_va["n"], 1), b_va["net"],
        a_va["net"], ref_va["net"]))
    results["stageB"] = {"cfg": cfgB, "train": b_tr, "val": b_va,
                         "n_trials": len(studyB.trials)}

    # ---------- 5. STAGE C: per-ticker enable (diagnostic) ----------
    figi_list = sorted(per)

    def objective_C(trial):
        enabled = set()
        for idx, f in enumerate(figi_list):
            if trial.suggest_categorical("m_%02d" % idx, [1, 0]) == 1:
                enabled.add(f)
        m_tr = agg(run_all(per, cfgB, "train", figis=enabled), train_days)
        m_va = agg(run_all(per, cfgB, "val", figis=enabled), val_days)
        trial.set_user_attr("train_net", m_tr["net"])
        trial.set_user_attr("train_n", m_tr["n"])
        trial.set_user_attr("val_net", m_va["net"])
        trial.set_user_attr("val_n", m_va["n"])
        o = m_tr["net"] - 0.3 * m_tr["day_std"]
        if m_tr["n"] < 40:
            o -= 50.0 * (40 - m_tr["n"])
        return o

    t0 = time.perf_counter()
    studyC = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=13))
    studyC.optimize(objective_C, n_trials=120)
    bestC, candsC = pick_best(studyC)
    print()
    print("=== 5. STAGE C: per-ticker enable on B-winner (120 trials, %.0fs) - DIAGNOSTIC ===" % (time.perf_counter() - t0))
    enabledC = set()
    if bestC:
        for idx, f in enumerate(figi_list):
            if bestC.params.get("m_%02d" % idx) == 1:
                enabledC.add(f)
    disabled = [per[f]["ticker"] for f in figi_list if f not in enabledC]
    c_tr = agg(run_all(per, cfgB, "train", figis=enabledC), train_days) if enabledC else b_tr
    c_va = agg(run_all(per, cfgB, "val", figis=enabledC), val_days) if enabledC else b_va
    print("  disabled tickers (%d): %s" % (len(disabled), ",".join(disabled) if disabled else "(none)"))
    print("  TRAIN n=%d net=%+.2f | VAL n=%d net=%+.2f  (B-val %+.2f)" % (
        c_tr["n"], c_tr["net"], c_va["n"], c_va["net"], b_va["net"]))
    print("  WARNING: per-ticker binary selection on 5 train days is the most overfit-prone"
          " stage - treat as diagnostic unless val gain is large and consistent.")
    results["stageC"] = {"disabled": disabled,
                         "train": c_tr, "val": c_va,
                         "n_trials": len(studyC.trials)}

    # ---------- 6. STABILITY: champion vs REF by day (all data) ----------
    champ_trades = run_all(per, cfgB, "all")
    ref_trades = run_all(per, REF, "all")
    ch = agg(champ_trades, all_days_iso := [d.isoformat() for d in all_days])
    rf = agg(ref_trades, all_days_iso)
    print()
    print("=== 6. STABILITY: champion(B) vs REF, full period by day ===")
    print("  day      REF net   champion net   delta")
    for d in all_days_iso:
        r0 = rf["by_day"].get(d, 0.0)
        c0 = ch["by_day"].get(d, 0.0)
        print("  %s %+9.2f %+13.2f %+9.2f" % (d, r0, c0, c0 - r0))
    print("  TOTAL: REF %+.2f (n=%d) | champion %+.2f (n=%d)" % (
        rf["net"], rf["n"], ch["net"], ch["n"]))
    tk_top = sorted(ch["by_tk"].items(), key=lambda kv: -kv[1][1])[:6]
    tk_bot = sorted(ch["by_tk"].items(), key=lambda kv: kv[1][1])[:6]
    print("  champion best tickers : %s" % [(t, v[0], v[1]) for t, v in tk_top])
    print("  champion worst tickers: %s" % [(t, v[0], v[1]) for t, v in tk_bot])
    results["stability"] = {"by_day_ref": rf["by_day"], "by_day_champ": ch["by_day"],
                            "total_ref": rf["net"], "total_champ": ch["net"],
                            "champ_by_tk": ch["by_tk"]}

    # ---------- conclusions ----------
    concl = list(PRIOR_FINDINGS)
    concl.append("REF split: train %+.2f / val %+.2f (12-16 vs 17-19.09)" % (
        ref_tr["net"], ref_va["net"]))
    concl.append("stage A (weights): val %+.2f vs REF %+.2f -> %s" % (
        a_va["net"], ref_va["net"],
        "weights HELP" if a_va["net"] > ref_va["net"] + 50 else "weights ~neutral (equal weights stand)"))
    concl.append("stage B (exit): val %+.2f vs A %+.2f -> %s" % (
        b_va["net"], a_va["net"],
        "exit tuning HELPS" if b_va["net"] > a_va["net"] + 50 else "exit 70/30 cd=15 already near-optimal"))
    concl.append("stage C (per-ticker): val %+.2f vs B %+.2f -> %s" % (
        c_va["net"], b_va["net"],
        "ticker selection helps (verify! overfit-prone)" if c_va["net"] > b_va["net"] + 100
        else "per-ticker selection NOT justified on this data"))
    concl.append("overfit check: champion train %+.2f vs val %+.2f (gap %.0f); REF gap %.0f" % (
        b_tr["net"], b_va["net"], b_tr["net"] - b_va["net"], ref_tr["net"] - ref_va["net"]))
    concl.append("NO untouched TEST period left in 12-19.09 -> next data load must keep a blind test window")
    concl.append("NEXT STEPS: (1) per-setup PARAM optuna (needs signal recompute per trial);"
                 " (2) port champion to live bot variant q2opt (bot-section like q2rsi2)"
                 " and cross-check on .4 replay; (3) walk-forward when more days are loaded")
    print()
    print("=== CONCLUSIONS (recorded to results_optuna_votes.json) ===")
    for c in concl:
        print("  - %s" % c)
    results["conclusions"] = concl

    out_path = os.path.join(HERE, "results_optuna_votes.json")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        print("results saved: %s" % out_path)
    except Exception as e:
        print("results save FAILED: %s %s" % (type(e).__name__, str(e)[:80]))
    print()
    print("wall %.0fs | OPTUNA_VOTES_DONE" % (time.perf_counter() - t_start))


if __name__ == "__main__":
    main()
