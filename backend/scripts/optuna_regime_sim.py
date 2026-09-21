#!/usr/bin/env python3
"""optuna_regime_sim.py - MERGED 16-STAGE PLAN: REGIME layer (stages 02/10/11/12).

Master ledger lives HERE from now on (supersedes the 14-stage ledger in
optuna_params_sim.py). History: results_optuna_votes.json (stages 01,04-09),
results_optuna_params.json (stage 03), mtf_sim.py (stages 06,08).

  [x] 01 baseline REF: q2c entries (quorum-2 of 6 setups, 10m votes, 15m
        window, champion params) + RSI(14)1m 70/30 exit + cd=15
        -> TRAIN(12-16.09) +2744.97 / VAL(17-19.09) +1583.57 / ALL +4328.54
  [~] 02 regime detector check   <- THIS SCRIPT (diagnostics, detector FIXED)
  [ ] 03 setup params WITHOUT regime (optuna_params_sim.py - launched in
        parallel on .2; regime stages here run on REF so variables stay
        clean; if 03 adopts a new champion -> RE-RUN this script on it)
  [x] 04 ablation: -volume_drop -2117 train; -donchian -1228 train
  [x] 05 function weights (Optuna A, 220tr): REJECTED (val +612 vs +1584)
  [x] 06 TF weights / M10->M5->M1 cascade: REJECTED (best +3818 vs +4294)
  [x] 07 entry threshold/edge: covered by 05 (thr/opp searched)
  [x] 08 M1 trigger: REJECTED (every cascade variant < base, mtf_sim)
  [x] 09 exit ob/os/cd (Optuna B): REJECTED (val -225); 70/30 cd15 stands
  [~] 10 regime MULTIPLIERS (soft priors w*mult[regime][group]) <- THIS
  [~] 11 regime THRESHOLDS (thr per regime bucket)              <- THIS
  [~] 12 regime EXITS (ob/os per regime bucket) + chaos filter  <- THIS
  [ ] 13 limited joint optimization (after 03 + 10-12 settle)
  [ ] 14 walk-forward (needs more days loaded)
  [ ] 15 monte-carlo (after a champion survives VAL)
  [ ] 16 BLIND TEST - MANDATORY: no fresh window left in 12-19.09!

REGIME DESIGN (user spec):
  * regime = UPPER MANAGEMENT LAYER over the ensemble, never a vote;
  * direction x volatility diagnosed as SEPARATE axes (3x3 grid);
  * detector FIXED = live RegimeDetector (champion params, no co-optimization
    with the ensemble: first verify, then fix, then optimize the USE);
  * detector TF: live bot uses H1, but H1 warmup (69 bars ~ 5.4 trading days)
    eats most of the 7.5-day window -> PRIMARY = 15m bars (warmup ~1.6 day);
    10/30/60m coverage reported for reference. Regime becomes visible only
    AFTER the 15m bucket completes (no look-ahead); vote mapping keeps the
    old bucket-start convention for exact REF parity;
  * multipliers as soft PRIORS: w_eff = 1.0 * mult[regime][group]
    (0.0 = group off in that regime = soft allow/deny); groups:
    trend={macd_cross, donchian_breakout}, meanrev={rsi, boll, vwap, vold};
  * exits per regime bucket (TREND can hold longer, HIGH_VOL flees faster);
  * chaos filter (HIGH_VOL + low directional efficiency |move|/path -> skip).

Guards (same protocol as every stage):
  * TRAIN 12-16.09 / VAL 17-19.09; objective = TRAIN net
    - 0.3*pstdev(daily nets) - 50*max(0, 40-n);
  * search on TRAIN, SELECT on VAL (winner = best VAL among top-K train);
  * adoption margin: a stage wins only if VAL > base VAL + 50 RUR;
  * fixed env: cost 0.045%/side, ~6400 RUR/pos, session 9:50-19:00 MSK,
    bar turnover >= 50k, EOD close, cd=15, opp=0.

Run on .2:  cd q2opt & python -u optuna_regime_sim.py
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
from app.services.regime import RegimeDetector  # noqa: E402

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
OPP = 0.0
CD = 15
REF_TRAIN_NET = 2744.97
REF_VAL_NET = 1583.57
REF_ALL_NET = 4328.54
REF_ALL_N = 475
ADOPT_MARGIN = 50.0

SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
]
SIDS = [s for s, _ in SETUPS]
TREND_G = {"macd_cross", "donchian_breakout"}
GRP = np.array([0 if s in TREND_G else 1 for s in SIDS], dtype=int)
BUCKETS = ["TREND_UP", "TREND_DOWN", "FLAT", "HIGH_VOL"]
STATE_MAP = {"TREND_UP": 0, "TREND_DOWN": 1, "RANGE": 2, "NEUTRAL": 2, "HIGH_VOLATILITY": 3}
DET_TF = 900                 # primary detector TF: 15m bars
DET_TFS = (600, 900, 1800, 3600)

PLAN = [
    "01 [x] baseline REF (q2c + RSI70/30 + cd15): train +2744.97 / val +1583.57",
    "02 [~] regime detector check (THIS SCRIPT, diagnostics, detector fixed)",
    "03 [ ] setup params WITHOUT regime (optuna_params_sim.py, parallel on .2)",
    "04 [x] ablation: -volume_drop -2117 train; -donchian -1228 train",
    "05 [x] function weights: REJECTED (val +612 vs +1584)",
    "06 [x] TF weights / cascade M10-M5-M1: REJECTED (+3818 vs +4294)",
    "07 [x] entry threshold/edge: covered by 05",
    "08 [x] M1 trigger: REJECTED (mtf_sim)",
    "09 [x] exit ob/os/cd: REJECTED (val -225); 70/30 cd15 stands",
    "10 [~] regime MULTIPLIERS (THIS SCRIPT)",
    "11 [~] regime THRESHOLDS (THIS SCRIPT)",
    "12 [~] regime EXITS + chaos filter (THIS SCRIPT)",
    "13 [ ] limited joint optimization (after 03 + 10-12)",
    "14 [ ] walk-forward (needs more days)",
    "15 [ ] monte-carlo (after champion survives VAL)",
    "16 [ ] BLIND TEST - mandatory (no fresh window left in 12-19.09)",
]

PRIOR_FINDINGS = [
    "REF: q2c entries + RSI(14)1m 70/30 + cd15 -> train +2744.97 / val +1583.57 / all +4328.54 (n=475)",
    "REJECTED so far: vote weights (A), exit ob/os/cd (B), per-ticker (C), cascade M10-M5-M1, cd=0, M1 triggers",
    "stage 03 (per-setup params) launched in parallel on .2 (optuna_params_sim.py)",
]

TRAIN_DAYS: list = []
VAL_DAYS: list = []
ALL_DAYS: list = []


# ---------------- indicators / events ----------------

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


def efficiency(cl, n_min):
    """Directional efficiency |net move| / path over last n_min bars (1m closes)."""
    cl = np.asarray(cl, dtype=float)
    n = len(cl)
    out = np.ones(n)
    if n < 2:
        return out
    d = np.abs(np.diff(cl, prepend=cl[0]))
    cs = np.concatenate(([0.0], np.cumsum(d)))
    idx = np.arange(n)
    lo = np.maximum(0, idx - n_min)
    path = cs[idx] - cs[lo]
    move = np.abs(cl[idx] - cl[lo])
    ok = path > 1e-12
    out[ok] = move[ok] / path[ok]
    return out


def detector_series(candles, ts, tf_sec):
    """Per-1m-bar (regime_code, atr_percentile) from the LAST COMPLETED tf-bar.

    Detector = live RegimeDetector, default (champion) params, FIXED.
    A tf-bar labeled T covers [T, T+tf) -> it becomes visible at T+tf
    (completion time): no look-ahead.
    """
    n = len(ts)
    codes = np.full(n, 2, dtype=np.int8)
    ap = np.full(n, np.nan)
    try:
        bars = resample(candles, tf_sec)
        st = RegimeDetector().compute(bars)
    except Exception:
        st = []
    if not st:
        return codes, ap
    j = 0
    cur = 2
    cur_ap = float("nan")
    done = timedelta(seconds=tf_sec)
    for i in range(n):
        t = ts[i]
        while j < len(st) and st[j]["ts"] + done <= t:
            cur = STATE_MAP.get(st[j].get("state"), 2)
            fts = st[j].get("features") or {}
            vv = fts.get("atr_percentile")
            cur_ap = float(vv) if vv is not None else float("nan")
            j += 1
        codes[i] = cur
        ap[i] = cur_ap
    return codes, ap


def build_per(raw):
    per = {}
    for f in sorted(raw):
        rows = raw[f]["candles"]
        if len(rows) < 300:
            continue
        candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]), low=float(x[3]),
                          close=float(x[4]), volume=int(x[5] or 0)) for x in rows]
        ts = [c.ts for c in candles]
        cl = [float(c.close) for c in candles]
        vl = [float(c.volume or 0) for c in candles]
        cln = np.asarray(cl)

        ev = defaultdict(lambda: {"B": set(), "S": set()})
        bars10 = resample(candles, 600)
        for sid, params in SETUPS:
            try:
                sigs = generate_signals(sid, params, bars10)
            except Exception:
                sigs = []
            for sg in sigs:
                sds = str(sg.get("side", "")).upper()
                if "BUY" in sds:
                    ev[sg["ts"]]["B"].add(sid)
                elif "SELL" in sds:
                    ev[sg["ts"]]["S"].add(sid)
        ev_list = [(t, ev[t]["B"], ev[t]["S"]) for t in sorted(ev)]
        votes = np.array(states_for(ts, ev_list, SIDS, LOOKBACK_MIN * 60), dtype=np.int8)

        rc = None
        ap = None
        cov = {}
        for tf in DET_TFS:
            codes, aps = detector_series(candles, ts, tf)
            cov[tf] = codes
            if tf == DET_TF:
                rc = codes
                ap = aps

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
            ts=ts, cl=cl, vl=vl,
            r=[float(x) for x in rsi_wilder(cln, 14)],
            Vp=(votes == 1).astype(np.float32),
            Vm=(votes == -1).astype(np.float32),
            rc=rc, ap=ap, cov=cov,
            eff30=efficiency(cln, 30), eff60=efficiency(cln, 60),
            day=day_l, hm=hm_l,
            m_tr=np.asarray(m_tr, dtype=bool),
            m_va=np.asarray(m_va, dtype=bool),
        )
    return per


# ---------------- simulation (REF-parity loop, regime-aware) ----------------

def simulate(d, sb, ss, thr_a, ob_a, os_a, entry_ok):
    """Trades of one figi. Tuple: (ts_in, ts_out, sgn, reason, pnl, ticker, i_entry)."""
    ts = d["ts"]
    cl = d["cl"]
    vl = d["vl"]
    r = d["r"]
    day = d["day"]
    hm = d["hm"]
    tk = d["ticker"]
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
            out.append((ts[entry_i], ts[i - 1], sgn, "day_end",
                        sgn * (px - ep) * eq - COST * (ep + px) * eq, tk, entry_i))
            pos = 0
            last_exit = i - 1
        if i < 60:
            continue
        if pos == 0:
            if i - last_exit <= CD:
                continue
            if cl[i] * vl[i] < TURNOVER:
                continue
            if not (DAY_START <= hm[i] < DAY_END):
                continue
            if not entry_ok[i]:
                continue
            b = sb[i]
            s = ss[i]
            th = thr_a[i]
            if b >= th and s <= OPP:
                want = 1
            elif s >= th and b <= OPP:
                want = -1
            else:
                continue
            sgn = want
            ep = cl[i]
            eq = max(1, round(NOTIONAL / ep))
            pos = 1
            entry_i = i
        else:
            if (sgn == 1 and r[i] > ob_a[i]) or (sgn == -1 and r[i] < os_a[i]):
                px = cl[i]
                out.append((ts[entry_i], ts[i], sgn, "exit",
                            sgn * (px - ep) * eq - COST * (ep + px) * eq, tk, entry_i))
                pos = 0
                last_exit = i
    if pos:
        px = cl[-1]
        out.append((ts[entry_i], ts[-1], sgn, "day_end",
                    sgn * (px - ep) * eq - COST * (ep + px) * eq, tk, entry_i))
    return out


def agg(trades, days=None):
    """days = list of iso-date strings (day_std really penalized)."""
    n = len(trades)
    wins = sum(1 for t in trades if t[4] > 0)
    net = sum(t[4] for t in trades)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t[0].astimezone(MSK).date().isoformat()] += t[4]
    vals = [by_day.get(dd, 0.0) for dd in days] if days else list(by_day.values())
    dstd = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    ls = defaultdict(lambda: [0, 0.0])
    tk = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = "L" if t[2] == 1 else "S"
        ls[k][0] += 1
        ls[k][1] += t[4]
        tk[t[5]][0] += 1
        tk[t[5]][1] += t[4]
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
    return dict(n=n, wins=wins, net=net, day_std=dstd,
                by_day={k: round(v, 2) for k, v in by_day.items()},
                L=[ls["L"][0], round(ls["L"][1], 2)],
                S=[ls["S"][0], round(ls["S"][1], 2)], mx=mx,
                by_tk={k: [v[0], round(v[1], 2)] for k, v in tk.items()})


def run_eval(per, mult=None, thr=None, ob=None, os_=None, eff=None,
             splits=("train", "val")):
    """Run the sim with a regime-layer config.

    mult: 4x2 [[trend_mult, meanrev_mult] per bucket] or None (=all 1.0);
    thr/ob/os_: per-bucket lists or None (=2.0 / 70.0 / 30.0 everywhere);
    eff: (which, min_eff, hv_only) chaos filter or None.
    """
    wmat = np.ones((4, 6), dtype=float)
    if mult is not None:
        for b in range(4):
            wmat[b][GRP == 0] = float(mult[b][0])
            wmat[b][GRP == 1] = float(mult[b][1])
    thr_v = np.full(4, 2.0) if thr is None else np.asarray(thr, dtype=float)
    ob_v = np.full(4, 70.0) if ob is None else np.asarray(ob, dtype=float)
    os_v = np.full(4, 30.0) if os_ is None else np.asarray(os_, dtype=float)
    trades = {sp: [] for sp in splits}
    for f, d in per.items():
        rc = d["rc"]
        n = len(rc)
        sb = np.zeros(n)
        ss = np.zeros(n)
        for b in range(4):
            m = rc == b
            if m.any():
                sb[m] = d["Vp"][m] @ wmat[b]
                ss[m] = d["Vm"][m] @ wmat[b]
        thr_a = thr_v[rc]
        ob_a = ob_v[rc]
        os_a = os_v[rc]
        for sp in splits:
            if sp == "train":
                mask = d["m_tr"]
            elif sp == "val":
                mask = d["m_va"]
            else:
                mask = np.ones(n, dtype=bool)
            ok = mask
            if eff is not None:
                which, me, hvo = eff
                ea = d[which]
                if hvo:
                    allowed = (ea >= me) | (rc != 3)
                else:
                    allowed = ea >= me
                ok = mask & allowed
            for t in simulate(d, sb, ss, thr_a, ob_a, os_a, ok):
                trades[sp].append(t)
    res = {}
    for sp in splits:
        days = TRAIN_DAYS if sp == "train" else (VAL_DAYS if sp == "val" else ALL_DAYS)
        res[sp] = agg(trades[sp], days)
    res["_trades"] = trades
    return res


def _attrs(trial, res):
    m_tr = res["train"]
    m_va = res["val"]
    trial.set_user_attr("train_net", round(m_tr["net"], 2))
    trial.set_user_attr("train_n", m_tr["n"])
    trial.set_user_attr("train_dstd", round(m_tr["day_std"], 2))
    trial.set_user_attr("val_net", round(m_va["net"], 2))
    trial.set_user_attr("val_n", m_va["n"])
    o = m_tr["net"] - 0.3 * m_tr["day_std"]
    if m_tr["n"] < 40:
        o -= 50.0 * (40 - m_tr["n"])
    return o


def pick_best(study, topk=8, min_train_n=40):
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


def fmt_res(r):
    return "n=%4d wr=%5.1f%% net=%+9.2f day_std=%.0f" % (
        r["n"], 100.0 * r["wins"] / max(r["n"], 1), r["net"], r["day_std"])


def tag_trades(trades, per):
    tick2p = {d["ticker"]: d for d in per.values()}
    rows = []
    for t in trades:
        d = tick2p.get(t[5])
        if d is None:
            continue
        i = t[6]
        rc = int(d["rc"][i])
        ap = float(d["ap"][i])
        if rc == 3:
            volx = "HIGH"
        elif ap != ap:
            volx = "NORMAL"
        elif ap >= 78:
            volx = "HIGH"
        elif ap <= 25:
            volx = "LOW"
        else:
            volx = "NORMAL"
        dirx = "UP" if rc == 0 else ("DOWN" if rc == 1 else "FLAT")
        rows.append(dict(net=t[4], sgn=t[2], rc=rc, dirx=dirx, volx=volx,
                         e30=float(d["eff30"][i]), e60=float(d["eff60"][i])))
    return rows


def tstat(sel):
    n = len(sel)
    if not n:
        return "n=   0"
    w = sum(1 for x in sel if x["net"] > 0)
    net = sum(x["net"] for x in sel)
    L = [x for x in sel if x["sgn"] == 1]
    S = [x for x in sel if x["sgn"] == -1]
    return "n=%4d wr=%5.1f%% net=%+9.2f | L %3d/%+8.2f S %3d/%+8.2f" % (
        n, 100.0 * w / n, net, len(L), sum(x["net"] for x in L),
        len(S), sum(x["net"] for x in S))


def clean(res):
    return {k: v for k, v in res.items() if k != "_trades"}


def main():
    global TRAIN_DAYS, VAL_DAYS, ALL_DAYS
    t_start = time.perf_counter()
    data_path = os.environ.get("Q2DATA") or os.path.join(HERE, "data", "q2optuna_data.pkl")
    if not os.path.exists(data_path):
        print("DATA NOT FOUND: %s" % data_path)
        sys.exit(1)
    with open(data_path, "rb") as fh:
        raw = pickle.load(fh)
    t0 = time.perf_counter()
    per = build_per(raw)
    if not per:
        print("no data")
        sys.exit(1)
    all_days_d = sorted({t.astimezone(MSK).date() for d in per.values() for t in d["ts"]})
    TRAIN_DAYS = [d.isoformat() for d in all_days_d if d <= TRAIN_LAST]
    VAL_DAYS = [d.isoformat() for d in all_days_d if d >= VAL_FIRST]
    ALL_DAYS = [d.isoformat() for d in all_days_d]
    n_bars = sum(len(d["ts"]) for d in per.values())

    print("=== OPTUNA REGIME: merged plan stages 02 / 10 / 11 / 12 (worker .2) ===")
    print("detector: FIXED RegimeDetector (live params); PRIMARY TF=15m; causal (bucket completes)")
    print("data: figis=%d | 1m bars=%d | %s -> %s MSK | prep %.1fs" % (
        len(per), n_bars,
        min(d["ts"][0] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(d["ts"][-1] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        time.perf_counter() - t0))
    print("TRAIN %s | VAL %s" % (",".join(TRAIN_DAYS), ",".join(VAL_DAYS)))
    print()
    print("=== MERGED 16-STAGE PLAN (ledger) ===")
    for line in PLAN:
        print("  " + line)

    results = {"when": datetime.now().isoformat(), "plan": PLAN,
               "data": {"figis": len(per), "bars": n_bars},
               "detector_tf": DET_TF}

    # ---------- 02a. detector coverage ----------
    print()
    print("=== 02a. ПОКРЫТИЕ ДЕТЕКТОРА (дневные бары 9:50-19:00 МСК) ===")
    cov_res = {}
    for tf in DET_TFS:
        cnt = np.zeros(4, dtype=np.int64)
        tot = 0
        for d in per.values():
            hm_a = np.asarray(d["hm"], dtype=int)
            sel = (hm_a >= DAY_START) & (hm_a < DAY_END)
            cnt += np.bincount(d["cov"][tf][sel].astype(int), minlength=4)
            tot += int(sel.sum())
        pcts = [100.0 * int(cnt[b]) / max(tot, 1) for b in range(4)]
        tag = " <- PRIMARY" if tf == DET_TF else ""
        print("  %4ds: TREND_UP %5.1f%% | TREND_DOWN %5.1f%% | FLAT(warmup incl) %5.1f%% | HIGH_VOL %5.1f%%%s" % (
            tf, pcts[0], pcts[1], pcts[2], pcts[3], tag))
        cov_res[str(tf)] = {"pct": [round(p, 1) for p in pcts], "bars": tot}
    results["coverage"] = cov_res
    print("  (живой бот использует H1: warmup 69 баров ~ 5.4 торговых дня -> на окне 12-19.09")
    print("   реальные состояния лишь у ~последних 2 дней; PRIMARY здесь 15м: warmup ~1.6 дня)")

    # ---------- 02b. REF parity + trades by regime ----------
    ref = run_eval(per, splits=("train", "val", "all"))
    rt = ref["train"]
    rv = ref["val"]
    ra = ref["all"]
    parity = (abs(rt["net"] - REF_TRAIN_NET) < 1.0 and abs(rv["net"] - REF_VAL_NET) < 1.0
              and abs(ra["net"] - REF_ALL_NET) < 1.0 and ra["n"] == REF_ALL_N)
    print()
    print("=== 02b. REF (паритет) ===")
    print("  TRAIN %s" % fmt_res(rt))
    print("  VAL   %s" % fmt_res(rv))
    print("  ALL   %s" % fmt_res(ra))
    print("  паритет vs optuna_votes_sim (train %+.2f / val %+.2f / all %+.2f n=%d): %s" % (
        REF_TRAIN_NET, REF_VAL_NET, REF_ALL_NET, REF_ALL_N, "MATCH" if parity else "MISMATCH (!!)"))

    tags = tag_trades(ref["_trades"]["all"], per)
    print()
    print("=== 02c. REF-сделки по режимам (детектор 15м на входе) ===")
    by_bucket = {}
    for b in range(4):
        sel = [x for x in tags if x["rc"] == b]
        by_bucket[BUCKETS[b]] = tstat(sel)
        print("  %-11s: %s" % (BUCKETS[b], tstat(sel)))
    results["ref_by_bucket"] = by_bucket

    print()
    print("=== 02d. REF-сделки: направление x волатильность (оси разделены) ===")
    by_dv = {}
    for dirx in ("UP", "DOWN", "FLAT"):
        for volx in ("LOW", "NORMAL", "HIGH"):
            sel = [x for x in tags if x["dirx"] == dirx and x["volx"] == volx]
            if sel:
                line = tstat(sel)
                by_dv["%s x %s" % (dirx, volx)] = line
                print("  %-4s x %-6s: %s" % (dirx, volx, line))
    results["ref_by_dirvol"] = by_dv

    print()
    print("=== 02e. REF-сделки: направленная эффективность входа (chaos-гипотеза) ===")
    e30s = sorted(x["e30"] for x in tags)
    if len(e30s) >= 8:
        q = [e30s[len(e30s) // 4], e30s[len(e30s) // 2], e30s[3 * len(e30s) // 4]]

        def qof(v):
            if v < q[0]:
                return 1
            if v < q[1]:
                return 2
            if v < q[2]:
                return 3
            return 4

        for k in (1, 2, 3, 4):
            sel = [x for x in tags if qof(x["e30"]) == k]
            print("  eff30 Q%d: %s" % (k, tstat(sel)))
    we = sorted(x["e30"] for x in tags if x["net"] > 0)
    le = sorted(x["e30"] for x in tags if x["net"] <= 0)
    hv_e = sorted(x["e30"] for x in tags if x["rc"] == 3)
    fl_e = sorted(x["e30"] for x in tags if x["rc"] != 3)

    def med(a):
        return a[len(a) // 2] if a else float("nan")

    print("  медиана eff30: победители %.3f | проигравшие %.3f | HIGH_VOL %.3f | прочие %.3f" % (
        med(we), med(le), med(hv_e), med(fl_e)))
    results["ref_eff"] = {"win_med_e30": round(med(we), 3), "loss_med_e30": round(med(le), 3),
                          "hv_med_e30": round(med(hv_e), 3), "other_med_e30": round(med(fl_e), 3)}

    base = {"mult": None, "thr": None, "ob": None, "os_": None, "eff": None}
    base_val = rv["net"]
    adopt_log = []
    results["ref"] = {"train": clean(rt), "val": clean(rv), "all": clean(ra), "parity": parity}

    # ---------- 10. regime multipliers (Optuna) ----------
    print()
    print("=== 10. REGIME MULTIPLIERS (Optuna 240tr: 4 бакета x {trend, meanrev}) ===")

    def objective_mult(trial):
        mult = []
        for b in range(4):
            tm = trial.suggest_float("tm_%s" % BUCKETS[b], 0.0, 2.0)
            mm = trial.suggest_float("mm_%s" % BUCKETS[b], 0.0, 2.0)
            mult.append((tm, mm))
        trial.set_user_attr("mult_eff", [[round(a, 3), round(b2, 3)] for a, b2 in mult])
        res = run_eval(per, mult=mult)
        return _attrs(trial, res)

    t0 = time.perf_counter()
    studyM = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=23))
    studyM.optimize(objective_mult, n_trials=240)
    bestM, candsM = pick_best(studyM, topk=12)
    print("  (%d tr, %.0fs) топ по VAL среди топ-12 train:" % (len(studyM.trials), time.perf_counter() - t0))
    for t in candsM[:6]:
        print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | %s" % (
            t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
            t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
            t.user_attrs.get("mult_eff")))
    stM = None
    if bestM is not None:
        wm = [[float(bestM.params["tm_%s" % BUCKETS[b]]), float(bestM.params["mm_%s" % BUCKETS[b]])]
              for b in range(4)]
        wres = run_eval(per, mult=wm)
        adopted = wres["val"]["net"] > base_val + ADOPT_MARGIN
        print("  WINNER mult=%s" % wm)
        print("  TRAIN %s | VAL %s (base val %+.2f, delta %+.2f) -> %s" % (
            fmt_res(wres["train"]), fmt_res(wres["val"]), base_val,
            wres["val"]["net"] - base_val, "ADOPT" if adopted else "keep REF"))
        stM = {"cfg": wm, "train": clean(wres["train"]), "val": clean(wres["val"]),
               "adopted": adopted, "n_trials": len(studyM.trials)}
        if adopted:
            base["mult"] = wm
            base_val = wres["val"]["net"]
        adopt_log.append(("10 mult", adopted, wres["val"]["net"]))
    results["stage10_mult"] = stM

    # ---------- 11. regime thresholds (Optuna) ----------
    print()
    print("=== 11. REGIME THRESHOLDS (Optuna 110tr: thr per bucket) ===")

    def objective_thr(trial):
        thr = [trial.suggest_float("thr_%s" % BUCKETS[b], 1.2, 3.0) for b in range(4)]
        trial.set_user_attr("thr_eff", [round(x, 3) for x in thr])
        res = run_eval(per, mult=base["mult"], thr=thr)
        return _attrs(trial, res)

    t0 = time.perf_counter()
    studyT = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=29))
    studyT.optimize(objective_thr, n_trials=110)
    bestT, candsT = pick_best(studyT, topk=8)
    print("  (%d tr, %.0fs) топ по VAL среди топ-8 train:" % (len(studyT.trials), time.perf_counter() - t0))
    for t in candsT[:5]:
        print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | thr=%s" % (
            t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
            t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
            t.user_attrs.get("thr_eff")))
    stT = None
    if bestT is not None:
        wthr = [float(bestT.params["thr_%s" % BUCKETS[b]]) for b in range(4)]
        wres = run_eval(per, mult=base["mult"], thr=wthr)
        adopted = wres["val"]["net"] > base_val + ADOPT_MARGIN
        print("  WINNER thr=%s" % [round(x, 2) for x in wthr])
        print("  TRAIN %s | VAL %s (base val %+.2f, delta %+.2f) -> %s" % (
            fmt_res(wres["train"]), fmt_res(wres["val"]), base_val,
            wres["val"]["net"] - base_val, "ADOPT" if adopted else "keep"))
        stT = {"cfg": wthr, "train": clean(wres["train"]), "val": clean(wres["val"]),
               "adopted": adopted, "n_trials": len(studyT.trials)}
        if adopted:
            base["thr"] = wthr
            base_val = wres["val"]["net"]
        adopt_log.append(("11 thr", adopted, wres["val"]["net"]))
    results["stage11_thr"] = stT

    # ---------- 12a. regime exits (Optuna) ----------
    print()
    print("=== 12a. REGIME EXITS (Optuna 130tr: ob/os per bucket, FLAT=70/30) ===")

    def objective_exit(trial):
        ob = [0.0] * 4
        os_ = [0.0] * 4
        ob[2] = 70.0
        os_[2] = 30.0
        ob[0] = trial.suggest_float("ob_TREND_UP", 58.0, 90.0)
        os_[0] = trial.suggest_float("os_TREND_UP", 10.0, 42.0)
        ob[1] = trial.suggest_float("ob_TREND_DOWN", 58.0, 90.0)
        os_[1] = trial.suggest_float("os_TREND_DOWN", 10.0, 42.0)
        ob[3] = trial.suggest_float("ob_HIGH_VOL", 58.0, 85.0)
        os_[3] = trial.suggest_float("os_HIGH_VOL", 15.0, 45.0)
        trial.set_user_attr("exit_eff", {"ob": [round(x, 1) for x in ob],
                                          "os": [round(x, 1) for x in os_]})
        res = run_eval(per, mult=base["mult"], thr=base["thr"], ob=ob, os_=os_)
        return _attrs(trial, res)

    t0 = time.perf_counter()
    studyE = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=31))
    studyE.optimize(objective_exit, n_trials=130)
    bestE, candsE = pick_best(studyE, topk=8)
    print("  (%d tr, %.0fs) топ по VAL среди топ-8 train:" % (len(studyE.trials), time.perf_counter() - t0))
    for t in candsE[:5]:
        ex = t.user_attrs.get("exit_eff") or {}
        print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | ob=%s os=%s" % (
            t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
            t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
            ex.get("ob"), ex.get("os")))
    stE = None
    if bestE is not None:
        wob = [70.0, 70.0, 70.0, 70.0]
        wos = [30.0, 30.0, 30.0, 30.0]
        wob[0] = float(bestE.params["ob_TREND_UP"])
        wos[0] = float(bestE.params["os_TREND_UP"])
        wob[1] = float(bestE.params["ob_TREND_DOWN"])
        wos[1] = float(bestE.params["os_TREND_DOWN"])
        wob[3] = float(bestE.params["ob_HIGH_VOL"])
        wos[3] = float(bestE.params["os_HIGH_VOL"])
        wres = run_eval(per, mult=base["mult"], thr=base["thr"], ob=wob, os_=wos)
        adopted = wres["val"]["net"] > base_val + ADOPT_MARGIN
        print("  WINNER ob=%s os=%s" % ([round(x, 1) for x in wob], [round(x, 1) for x in wos]))
        print("  TRAIN %s | VAL %s (base val %+.2f, delta %+.2f) -> %s" % (
            fmt_res(wres["train"]), fmt_res(wres["val"]), base_val,
            wres["val"]["net"] - base_val, "ADOPT" if adopted else "keep 70/30"))
        stE = {"cfg": {"ob": wob, "os": wos}, "train": clean(wres["train"]),
               "val": clean(wres["val"]), "adopted": adopted, "n_trials": len(studyE.trials)}
        if adopted:
            base["ob"] = wob
            base["os_"] = wos
            base_val = wres["val"]["net"]
        adopt_log.append(("12a exit", adopted, wres["val"]["net"]))
    results["stage12a_exit"] = stE

    # ---------- 12b. chaos filter (fixed variants) ----------
    print()
    print("=== 12b. CHAOS-ФИЛЬТР (eff=|move|/path; фикс-варианты поверх текущей базы) ===")
    variants = [
        ("eff30>=0.30 (все входы)", ("eff30", 0.30, False)),
        ("eff30>=0.30 (только HIGH_VOL)", ("eff30", 0.30, True)),
        ("eff30>=0.20 (все входы)", ("eff30", 0.20, False)),
        ("eff60>=0.25 (все входы)", ("eff60", 0.25, False)),
        ("eff60>=0.25 (только HIGH_VOL)", ("eff60", 0.25, True)),
        ("eff60>=0.20 (только HIGH_VOL)", ("eff60", 0.20, True)),
    ]
    chaos_rows = []
    best_c = None
    for name, eff in variants:
        res = run_eval(per, mult=base["mult"], thr=base["thr"], ob=base["ob"],
                       os_=base["os_"], eff=eff)
        chaos_rows.append({"name": name, "eff": list(eff),
                           "val": round(res["val"]["net"], 2), "n": res["val"]["n"],
                           "train": round(res["train"]["net"], 2)})
        print("  %-32s TRAIN %s | VAL %s (delta %+.2f)" % (
            name, fmt_res(res["train"]), fmt_res(res["val"]),
            res["val"]["net"] - base_val))
        if best_c is None or res["val"]["net"] > best_c[1]:
            best_c = (eff, res["val"]["net"])
    adopted_c = best_c is not None and best_c[1] > base_val + ADOPT_MARGIN
    if adopted_c:
        base["eff"] = best_c[0]
        base_val = best_c[1]
        print("  ADOPT: %s (val %+.2f)" % (list(best_c[0]), best_c[1]))
    else:
        print("  без фильтра (base val %+.2f)" % base_val)
    adopt_log.append(("12b chaos", adopted_c, best_c[1] if best_c else base_val))
    results["stage12b_chaos"] = {"rows": chaos_rows, "adopted": adopted_c,
                                 "cfg": list(base["eff"]) if base["eff"] else None}

    # ---------- final assembly + stability ----------
    fin = run_eval(per, mult=base["mult"], thr=base["thr"], ob=base["ob"],
                   os_=base["os_"], eff=base["eff"], splits=("train", "val", "all"))
    print()
    print("=== FINAL. СБОРКА (принятые стадии) + УСТОЙЧИВОСТЬ ===")
    print("  cfg: mult=%s" % (base["mult"],))
    print("       thr=%s | ob=%s | os=%s | eff=%s" % (
        [round(x, 2) for x in base["thr"]] if base["thr"] else None,
        [round(x, 1) for x in base["ob"]] if base["ob"] else None,
        [round(x, 1) for x in base["os_"]] if base["os_"] else None,
        base["eff"]))
    print("  TRAIN %s | VAL %s" % (fmt_res(fin["train"]), fmt_res(fin["val"])))
    print("  ALL   %s  (REF all: n=%d net=%+.2f)" % (fmt_res(fin["all"]), ra["n"], ra["net"]))
    print("  по дням (REF -> FINAL):")
    for dd in ALL_DAYS:
        r0 = ra["by_day"].get(dd, 0.0)
        c0 = fin["all"]["by_day"].get(dd, 0.0)
        print("    %s REF %+9.2f -> FIN %+9.2f (d %+9.2f)" % (dd, r0, c0, c0 - r0))
    tk_top = sorted(fin["all"]["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]
    tk_bot = sorted(fin["all"]["by_tk"].items(), key=lambda kv: kv[1][1])[:5]
    print("  лучшие тикеры : %s" % [(t, v[0], v[1]) for t, v in tk_top])
    print("  худшие тикеры: %s" % [(t, v[0], v[1]) for t, v in tk_bot])
    print("  overfit-чек: train %+.2f vs val %+.2f (gap %.0f); REF gap %.0f" % (
        fin["train"]["net"], fin["val"]["net"],
        fin["train"]["net"] - fin["val"]["net"],
        rt["net"] - rv["net"]))
    results["final"] = {
        "cfg": {"mult": base["mult"],
                "thr": base["thr"], "ob": base["ob"], "os": base["os_"],
                "eff": list(base["eff"]) if base["eff"] else None},
        "train": clean(fin["train"]), "val": clean(fin["val"]), "all": clean(fin["all"]),
        "by_day_ref": ra["by_day"], "by_day_fin": fin["all"]["by_day"],
        "adopt_log": [[a, b, round(c, 2)] for a, b, c in adopt_log],
    }

    # ---------- conclusions ----------
    concl = list(PRIOR_FINDINGS)
    concl.append("detector 15m coverage: TU %.1f%% TD %.1f%% FLAT %.1f%% HV %.1f%% (day bars)" % (
        cov_res[str(DET_TF)]["pct"][0], cov_res[str(DET_TF)]["pct"][1],
        cov_res[str(DET_TF)]["pct"][2], cov_res[str(DET_TF)]["pct"][3]))
    for b in range(4):
        concl.append("REF by regime %s: %s" % (BUCKETS[b], by_bucket[BUCKETS[b]]))
    for stage, adopted, val in adopt_log:
        concl.append("%s: %s (val %+.2f vs REF-val %+.2f)" % (
            stage, "ADOPTED" if adopted else "rejected", val, rv["net"]))
    concl.append("FINAL: train %+.2f / val %+.2f (REF val %+.2f) -> %s" % (
        fin["train"]["net"], fin["val"]["net"], rv["net"],
        "regime layer HELPS" if fin["val"]["net"] > rv["net"] + ADOPT_MARGIN
        else "regime layer NOT justified on VAL (REF stands)"))
    concl.append("NEXT: (1) stage 03 (optuna_params_sim) идёт параллельно - если он")
    concl.append("принимает нового чемпиона, перезапустить ЭТОТ скрипт на нём; (2) затем")
    concl.append("stage 13 joint; (3) walk-forward/monte-carlo/blind-test требуют свежих дней")
    print()
    print("=== CONCLUSIONS (results_optuna_regime.json) ===")
    for c in concl:
        print("  - %s" % c)
    results["conclusions"] = concl

    out_path = os.path.join(HERE, "results_optuna_regime.json")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        print("results saved: %s" % out_path)
    except Exception as e:
        print("results save FAILED: %s %s" % (type(e).__name__, str(e)[:80]))
    print()
    print("wall %.0fs | OPTUNA_REGIME_DONE" % (time.perf_counter() - t_start))


if __name__ == "__main__":
    main()
