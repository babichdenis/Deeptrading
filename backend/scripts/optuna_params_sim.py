#!/usr/bin/env python3
"""optuna_params_sim.py - stage 4 of the AGREED STAGED PLAN: per-setup PARAMS.

AGREED STAGED PLAN ledger (history in results_optuna_votes.json, worker .2):
  [x] 0  fixed env: cost 0.045%/side, ~6400 RUR/pos, session 9:50-19:00 MSK,
         bar turnover >= 50k RUR, EOD close, universe = 26 paper-test figis
  [x] 1  baseline REF: q2c entries (quorum-2 of 6 setups @ champion params,
         10m votes, 15m window) + RSI(14) 1m 70/30 exit + cd=15
         -> TRAIN(12-16.09) +2744.97 / VAL(17-19.09) +1583.57
  [x] 2  individual setups (vote level): volume_drop train +4199/val +707,
         bollinger_reclaim val +2080 (best single val)
  [x] 3  ablation: -volume_drop -2117 train; -donchian -1228 train
  [ ] 4  THIS SCRIPT: per-setup PARAM Optuna (one setup at a time, others
         frozen at champion) -> COMBINED -> SUBSET on/off -> small JOINT
         (13 setup params + thr) -> NEIGHBOUR stability of the champion
  [x] 5  vote WEIGHTS (Optuna A, 220 tr): REJECTED - val +612 vs REF +1584
  [x] 6  TF cascade M10->M5->M1 + cd=0 (mtf_sim): REJECTED (+3818 / +437
         vs REF +4294 full-period)
  [x] 7  entry threshold/edge - covered by stage A (thr/opp searched)
  [x] 8  EXIT ob/os/cd (Optuna B, 160 tr): REJECTED - val -225;
         RSI 70/30 cd=15 near-optimal
  [x] 9  per-ticker enable (Optuna C, 120 tr): NOT justified on val
  [ ] 10 small joint - the JOINT stage below (14 params, guarded)
  [ ] 11 walk-forward - needs more days loaded
  [ ] 12 monte-carlo - after a champion survives VAL
  [ ] 13 param stability - NEIGHBOURS section below
  [x] 14 stability by day/ticker - redone here for the new champion

BUGFIX vs optuna_votes_sim.py: there the day_std penalty was INERT (agg got
date objects as `days` while by_day keys were iso strings -> pstdev of zeros;
see "day_std=0" in its REF line). Fixed here: days are iso strings, the
objective really penalizes day-spread now. A/B/C conclusions were VAL-based
and stand.

Guards (same protocol as before):
  * objective = TRAIN net - 0.3*pstdev(daily nets) - 50*max(0, 40-n);
  * search on TRAIN (12-16.09), SELECT on VAL (17-19.09): winner = best VAL
    among top-8 train trials; train->val degradation reported for winners;
  * adoption margin: a stage wins only if VAL > REF VAL + 50 RUR;
  * no untouched TEST window left in 12-19.09 -> any champion needs a blind
    re-run on fresh data before going live.

Run on .2:  cd q2opt & python -u optuna_params_sim.py
Run on .4:  Q2DATA=backend/data/q2optuna_data.pkl python -u scripts/optuna_params_sim.py
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

SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
]
SIDS = [s for s, _ in SETUPS]
N_TRIALS_P = {1: 60, 2: 90, 3: 110}   # by number of params in the setup
N_TRIALS_SUBSET = 80
N_TRIALS_JOINT = 150
REF_EXIT = {"thr": 2.0, "opp": 0.0, "ob": 70.0, "os": 30.0, "cd": 15}
ADOPT_MARGIN = 50.0
REF_TRAIN_NET = 2744.97
REF_VAL_NET = 1583.57

PRIOR_FINDINGS = [
    "REF (q2c quorum-2 @ champ params + RSI 70/30 + cd15): train +2744.97 / val +1583.57",
    "vote WEIGHTS (A, 220tr): REJECTED - val +612 vs REF +1584 (train-winners overfit)",
    "EXIT ob/os/cd (B, 160tr): REJECTED - val -225; 70/30 cd15 near-optimal",
    "per-ticker enable (C, 120tr): NOT justified on val",
    "M10->M5->M1 cascade + cd=0 (mtf_sim): REJECTED (+3818 / +437 vs REF +4294 full-period)",
    "individuals: volume_drop train +4199/val +707; bollinger val +2080 (best single val)",
    "BUGFIX: day_std penalty was inert in optuna_votes_sim (dates vs iso-keys); fixed here",
]

per: dict = {}
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


def setup_events(sid, params, bars10):
    """{ts: 'B'|'S'} for one setup (champion semantics: one side per bar)."""
    d = {}
    try:
        sigs = generate_signals(sid, params, bars10)
    except Exception:
        sigs = []
    for sg in sigs:
        sds = str(sg.get("side", "")).upper()
        if "BUY" in sds:
            d[sg["ts"]] = "B"
        elif "SELL" in sds:
            d[sg["ts"]] = "S"
    return d


def merged_events(ev_ref, override=None, disabled=None):
    """Merge per-setup event dicts -> [(ts, buy_set, sell_set)] (as before)."""
    ev = defaultdict(lambda: [set(), set()])
    for sid in SIDS:
        if disabled and sid in disabled:
            continue
        d = ev_ref.get(sid) or {}
        if override and sid in override:
            d = override[sid] or {}
        for t, s in d.items():
            if s == "B":
                ev[t][0].add(sid)
            else:
                ev[t][1].add(sid)
    return [(t, ev[t][0], ev[t][1]) for t in sorted(ev)]


# ---------------- simulation (parity with optuna_votes_sim) ----------------

def simulate(d, nbuy, nsell, thr, opp, ob, os_, cd, mask):
    cl = d["cl"]
    vl = d["vl"]
    r = d["r"]
    day = d["day"]
    hm = d["hm"]
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
            b = nbuy[i]
            s = nsell[i]
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


def agg(trades, days=None):
    """days = list of iso-date strings (BUGFIX: real day_std now)."""
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


def eval_config(params_by_sid=None, disabled=None, thr=REF_EXIT["thr"],
                opp=REF_EXIT["opp"], ob=REF_EXIT["ob"], os_=REF_EXIT["os"],
                cd=REF_EXIT["cd"], splits=("train", "val")):
    trades = {"train": [], "val": [], "all": []}
    for f, d in per.items():
        override = None
        if params_by_sid:
            override = {}
            for sid, p in params_by_sid.items():
                if p is not None:
                    override[sid] = setup_events(sid, p, d["bars10"])
        ev_list = merged_events(d["ev_ref"], override=override, disabled=disabled)
        states = states_for(d["ts"], ev_list, SIDS, LOOKBACK_MIN * 60)
        arr = np.array(states, dtype=np.int8)
        nbuy = (arr == 1).sum(axis=1).astype(float).tolist()
        nsell = (arr == -1).sum(axis=1).astype(float).tolist()
        for split in splits:
            if split == "train":
                mask = d["m_tr"]
            elif split == "val":
                mask = d["m_va"]
            else:
                mask = None
            for t in simulate(d, nbuy, nsell, thr, opp, ob, os_, cd, mask):
                trades[split].append(t + (d["ticker"],))
    res = {}
    for split in splits:
        days = TRAIN_DAYS if split == "train" else (VAL_DAYS if split == "val" else ALL_DAYS)
        res[split] = agg(trades[split], days)
    return res


def _attrs(trial, res):
    m_tr = res["train"]
    m_va = res["val"]
    trial.set_user_attr("train_net", m_tr["net"])
    trial.set_user_attr("train_n", m_tr["n"])
    trial.set_user_attr("train_dstd", m_tr["day_std"])
    trial.set_user_attr("val_net", m_va["net"])
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


# ---------------- search spaces ----------------

def _sp_rsi(t):
    return {"period": t.suggest_int("period", 8, 24),
            "oversold": round(t.suggest_float("oversold", 20.0, 40.0), 1),
            "overbought": round(t.suggest_float("overbought", 60.0, 90.0), 1)}


def _sp_boll(t):
    return {"period": t.suggest_int("period", 8, 30),
            "k": round(t.suggest_float("k", 0.6, 2.4), 2)}


def _sp_vwap(t):
    return {"k": round(t.suggest_float("k", 0.5, 3.0), 2)}


def _sp_macd(t):
    fast = t.suggest_int("fast", 6, 16)
    slow = t.suggest_int("slow", 20, 40)
    return {"fast": fast, "slow": max(slow, fast + 4),
            "signal_period": t.suggest_int("signal_period", 5, 13)}


def _sp_donch(t):
    return {"period": t.suggest_int("period", 20, 90)}


def _sp_vol(t):
    return {"ma_len": t.suggest_int("ma_len", 10, 40),
            "drop_ratio": round(t.suggest_float("drop_ratio", 1.1, 2.5), 2)}


SPACE = {
    "rsi_reversal": _sp_rsi,
    "bollinger_reclaim": _sp_boll,
    "vwap_reclaim": _sp_vwap,
    "macd_cross": _sp_macd,
    "donchian_breakout": _sp_donch,
    "volume_drop": _sp_vol,
}
NPARAMS = {"rsi_reversal": 3, "bollinger_reclaim": 2, "vwap_reclaim": 1,
           "macd_cross": 3, "donchian_breakout": 1, "volume_drop": 2}


def joint_space(trial):
    p = {}
    p["rsi_reversal"] = {"period": trial.suggest_int("rsi_p", 8, 24),
                         "oversold": round(trial.suggest_float("rsi_os", 20.0, 40.0), 1),
                         "overbought": round(trial.suggest_float("rsi_ob", 60.0, 90.0), 1)}
    p["bollinger_reclaim"] = {"period": trial.suggest_int("boll_p", 8, 30),
                              "k": round(trial.suggest_float("boll_k", 0.6, 2.4), 2)}
    p["vwap_reclaim"] = {"k": round(trial.suggest_float("vwap_k", 0.5, 3.0), 2)}
    fast = trial.suggest_int("macd_f", 6, 16)
    slow = trial.suggest_int("macd_s", 20, 40)
    p["macd_cross"] = {"fast": fast, "slow": max(slow, fast + 4),
                       "signal_period": trial.suggest_int("macd_g", 5, 13)}
    p["donchian_breakout"] = {"period": trial.suggest_int("donch_p", 20, 90)}
    p["volume_drop"] = {"ma_len": trial.suggest_int("vol_m", 10, 40),
                        "drop_ratio": round(trial.suggest_float("vol_r", 1.1, 2.5), 2)}
    thr = trial.suggest_float("thr", 1.2, 2.6)
    return p, thr


def neighbours(params_by_sid, thr):
    """One-param nudges of the champion: (label, params_copy, thr)."""
    out = []
    for sid, params in params_by_sid.items():
        for key, val in params.items():
            if isinstance(val, bool):
                continue
            if isinstance(val, int):
                steps = (-1, 1)
            else:
                steps = (-0.1, 0.1) if key in ("k", "drop_ratio") else (-1.0, 1.0)
            for st in steps:
                p2 = {s: dict(q) for s, q in params_by_sid.items()}
                p2[sid][key] = round(val + st, 2)
                out.append(("%s.%s%+g" % (sid[:6], key, st), p2, thr))
    return out


# ---------------- main ----------------

def main():
    global per, TRAIN_DAYS, VAL_DAYS, ALL_DAYS
    t_start = time.perf_counter()
    data_path = os.environ.get("Q2DATA") or os.path.join(HERE, "data", "q2optuna_data.pkl")
    if not os.path.exists(data_path):
        print("DATA NOT FOUND: %s" % data_path)
        sys.exit(1)
    with open(data_path, "rb") as fh:
        raw = pickle.load(fh)

    t0 = time.perf_counter()
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
        bars10 = resample(candles, 600)
        ev_ref = {}
        for sid, params in SETUPS:
            ev_ref[sid] = setup_events(sid, params, bars10)
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
            r=[float(x) for x in rsi_wilder(cl, 14)],
            day=day_l, hm=hm_l, m_tr=m_tr, m_va=m_va,
            bars10=bars10, ev_ref=ev_ref,
        )
    if not per:
        print("no data")
        sys.exit(1)
    all_days_d = sorted({t.astimezone(MSK).date() for d in per.values() for t in d["ts"]})
    TRAIN_DAYS = [d.isoformat() for d in all_days_d if d <= TRAIN_LAST]
    VAL_DAYS = [d.isoformat() for d in all_days_d if d >= VAL_FIRST]
    ALL_DAYS = [d.isoformat() for d in all_days_d]
    n_bars = sum(len(d["ts"]) for d in per.values())
    print("=== OPTUNA PARAMS: stage 4 (per-setup PARAMS -> COMBINED -> SUBSET -> JOINT) ===")
    print("data: figis=%d | 1m bars=%d | %s -> %s MSK | prep %.1fs" % (
        len(per), n_bars,
        min(d["ts"][0] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(d["ts"][-1] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        time.perf_counter() - t0))
    print("TRAIN %s | VAL %s | objective = train net - 0.3*day_std - 50*max(0,40-n)" % (
        ",".join(TRAIN_DAYS), ",".join(VAL_DAYS)))

    results = {"when": datetime.now().isoformat(),
               "data": {"figis": len(per), "bars": n_bars},
               "train_days": TRAIN_DAYS, "val_days": VAL_DAYS}

    # Checkpoint/RESUME: фоновые запуски на .2 умирают вместе с ssh-сессией,
    # поэтому стейдж P чекпоинтится после КАЖДОГО сетапа, subset/joint - после
    # своего завершения; повторный запуск подхватывает частичный results-файл.
    out_path = os.path.join(HERE, "results_optuna_params.json")
    stageP: dict = {}
    if os.path.exists(out_path):
        try:
            _old = json.load(open(out_path, encoding="utf-8"))
            if _old.get("stageP") and not _old.get("partial"):
                print("RESUME: полный прогон уже сохранён - для повтора удали %s" % out_path)
                sys.exit(0)
            if _old.get("partial") and isinstance(_old.get("stageP"), dict) and _old["stageP"]:
                stageP = _old["stageP"]
                print("RESUME: stage P готов для %d/%d сетапов: %s" % (
                    len(stageP), len(SIDS), ",".join(sorted(stageP))))
        except Exception as _e:
            print("resume skipped (%s)" % type(_e).__name__)

    # ---------- 0. REF reproduction (parity check) ----------
    ref = eval_config(splits=("train", "val", "all"))
    parity = (abs(ref["train"]["net"] - REF_TRAIN_NET) < 1.0
              and abs(ref["val"]["net"] - REF_VAL_NET) < 1.0)
    print()
    print("=== 0. REF (champion params, quorum-2, rsi 70/30, cd=15) ===")
    print("  TRAIN n=%d wr=%.1f%% net=%+.2f day_std=%.0f | VAL n=%d wr=%.1f%% net=%+.2f" % (
        ref["train"]["n"], 100.0 * ref["train"]["wins"] / max(ref["train"]["n"], 1),
        ref["train"]["net"], ref["train"]["day_std"],
        ref["val"]["n"], 100.0 * ref["val"]["wins"] / max(ref["val"]["n"], 1),
        ref["val"]["net"]))
    print("  parity vs optuna_votes_sim (train %+.2f / val %+.2f): %s" % (
        REF_TRAIN_NET, REF_VAL_NET, "MATCH" if parity else "MISMATCH (!!)"))
    results["ref"] = {"train": ref["train"], "val": ref["val"], "all": ref["all"]}

    # ---------- 1. STAGE P: per-setup params ----------
    print()
    print("=== 1. STAGE P: per-setup PARAM Optuna (others frozen at champion) ===")
    adopted_params = {sid: v["params"] for sid, v in stageP.items() if v.get("adopted")}
    for k, sid in enumerate(SIDS):
        if sid in stageP:
            print("  %-20s: SKIP (checkpoint)" % sid)
            continue
        t0 = time.perf_counter()
        n_trials = N_TRIALS_P[NPARAMS[sid]]

        def objective(trial, _sid=sid):
            params = SPACE[_sid](trial)
            trial.set_user_attr("params_eff", params)
            res = eval_config(params_by_sid={_sid: params})
            return _attrs(trial, res)

        study = optuna.create_study(direction="maximize",
                                    sampler=optuna.samplers.TPESampler(seed=101 + k))
        study.optimize(objective, n_trials=n_trials)
        best, cands = pick_best(study, topk=8)
        if best is None:
            print("  %-20s: no complete trials" % sid)
            continue
        print("  %-20s (%d tr, %.0fs) top by VAL among top-8 train:" % (
            sid, len(study.trials), time.perf_counter() - t0))
        for t in cands[:5]:
            print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | %s" % (
                t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
                t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
                json.dumps(t.user_attrs.get("params_eff", {}))))
        w_params = best.user_attrs.get("params_eff")
        w_res = eval_config(params_by_sid={sid: w_params})
        adopted = w_res["val"]["net"] > ref["val"]["net"] + ADOPT_MARGIN
        stageP[sid] = {"params": w_params,
                       "train": {k2: v for k2, v in w_res["train"].items() if k2 != "by_tk"},
                       "val": {k2: v for k2, v in w_res["val"].items() if k2 != "by_tk"},
                       "adopted": adopted, "n_trials": len(study.trials)}
        if adopted:
            adopted_params[sid] = w_params
        print("    WINNER %s | TRAIN n=%d net=%+.2f | VAL n=%d net=%+.2f (REF val %+.2f, delta %+.2f) -> %s" % (
            json.dumps(w_params), w_res["train"]["n"], w_res["train"]["net"],
            w_res["val"]["n"], w_res["val"]["net"], ref["val"]["net"],
            w_res["val"]["net"] - ref["val"]["net"],
            "ADOPT" if adopted else "keep champion params"))
        results["stageP"] = stageP
        results["partial"] = True
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        except Exception:
            pass
    results["stageP"] = stageP

    # ---------- 2. COMBINED ----------
    print()
    print("=== 2. COMBINED (stage-P winners together) ===")
    all_winners = {sid: stageP[sid]["params"] for sid in SIDS if sid in stageP}
    comb_all = eval_config(params_by_sid=all_winners) if len(all_winners) == len(SIDS) else ref
    comb_ad = eval_config(params_by_sid=dict(adopted_params)) if adopted_params else ref
    for name, c in (("all 6 winners", comb_all), ("adopted only", comb_ad)):
        print("  %-14s TRAIN n=%d net=%+.2f | VAL n=%d net=%+.2f (REF val %+.2f, delta %+.2f)" % (
            name, c["train"]["n"], c["train"]["net"], c["val"]["n"], c["val"]["net"],
            ref["val"]["net"], c["val"]["net"] - ref["val"]["net"]))
    results["combined"] = {
        "all": {"train": comb_all["train"], "val": comb_all["val"]},
        "adopted": {"train": comb_ad["train"], "val": comb_ad["val"]},
        "params_all": all_winners,
    }

    # ---------- 3. SUBSET on/off ----------
    print()
    print("=== 3. SUBSET on/off (Optuna, champion params, quorum-2 of remaining) ===")

    def objective_s(trial):
        disabled = set()
        for idx, sid in enumerate(SIDS):
            if trial.suggest_categorical("on_%d" % idx, [1, 0]) == 0:
                disabled.add(sid)
        trial.set_user_attr("disabled", sorted(disabled))
        res = eval_config(disabled=(disabled or None))
        return _attrs(trial, res)

    t0 = time.perf_counter()
    study_s = optuna.create_study(direction="maximize",
                                  sampler=optuna.samplers.TPESampler(seed=17))
    study_s.optimize(objective_s, n_trials=N_TRIALS_SUBSET)
    best_s, cands_s = pick_best(study_s, topk=8)
    sub_cfg = None
    sub_res = None
    if best_s is not None:
        print("  (%d tr, %.0fs) top by VAL among top-8 train:" % (
            len(study_s.trials), time.perf_counter() - t0))
        for t in cands_s[:5]:
            print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | off=%s" % (
                t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
                t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
                ",".join(t.user_attrs.get("disabled", [])) or "(none)"))
        dis = set(best_s.user_attrs.get("disabled") or [])
        sub_cfg = dict(disabled=(dis or None))
        sub_res = eval_config(disabled=sub_cfg["disabled"])
        adopted_s = sub_res["val"]["net"] > ref["val"]["net"] + ADOPT_MARGIN
        print("  WINNER off=%s | TRAIN n=%d net=%+.2f | VAL n=%d net=%+.2f (delta %+.2f) -> %s" % (
            ",".join(sorted(dis)) or "(none)", sub_res["train"]["n"], sub_res["train"]["net"],
            sub_res["val"]["n"], sub_res["val"]["net"],
            sub_res["val"]["net"] - ref["val"]["net"],
            "ADOPT" if adopted_s else "keep all 6"))
        results["subset"] = {"disabled": sorted(dis),
                             "train": sub_res["train"], "val": sub_res["val"],
                             "adopted": adopted_s, "n_trials": len(study_s.trials)}
        results["partial"] = True
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        except Exception:
            pass
    else:
        print("  no complete trials")
        results["subset"] = None

    # ---------- 4. JOINT ----------
    print()
    print("=== 4. JOINT (13 setup params + thr, Optuna, %d trials) ===" % N_TRIALS_JOINT)

    def objective_j(trial):
        p, thr = joint_space(trial)
        trial.set_user_attr("params_eff", p)
        trial.set_user_attr("thr_eff", thr)
        res = eval_config(params_by_sid=p, thr=thr)
        return _attrs(trial, res)

    t0 = time.perf_counter()
    study_j = optuna.create_study(direction="maximize",
                                  sampler=optuna.samplers.TPESampler(seed=19))
    study_j.optimize(objective_j, n_trials=N_TRIALS_JOINT)
    best_j, cands_j = pick_best(study_j, topk=12)
    j_cfg = None
    j_res = None
    if best_j is not None:
        print("  (%d tr, %.0fs) top by VAL among top-12 train:" % (
            len(study_j.trials), time.perf_counter() - t0))
        for t in cands_j[:6]:
            print("    val=%+9.2f train=%+9.2f n_tr=%3d n_va=%3d | thr=%.2f" % (
                t.user_attrs.get("val_net", 0), t.user_attrs.get("train_net", 0),
                t.user_attrs.get("train_n", 0), t.user_attrs.get("val_n", 0),
                t.user_attrs.get("thr_eff", 2.0)))
        j_params = best_j.user_attrs.get("params_eff")
        j_thr = float(best_j.user_attrs.get("thr_eff", 2.0))
        j_cfg = dict(params=j_params, thr=j_thr)
        j_res = eval_config(params_by_sid=j_params, thr=j_thr)
        adopted_j = j_res["val"]["net"] > ref["val"]["net"] + ADOPT_MARGIN
        print("  WINNER thr=%.2f" % j_thr)
        for sid in SIDS:
            print("    %-20s %s (champ %s)" % (
                sid, json.dumps(j_params[sid]), json.dumps(dict(CHAMP_P(sid)))))
        print("  TRAIN n=%d net=%+.2f | VAL n=%d net=%+.2f (REF val %+.2f, delta %+.2f) -> %s" % (
            j_res["train"]["n"], j_res["train"]["net"], j_res["val"]["n"], j_res["val"]["net"],
            ref["val"]["net"], j_res["val"]["net"] - ref["val"]["net"],
            "ADOPT" if adopted_j else "keep champion"))
        results["joint"] = {"params": j_params, "thr": j_thr,
                            "train": j_res["train"], "val": j_res["val"],
                            "adopted": adopted_j, "n_trials": len(study_j.trials)}
        results["partial"] = True
        try:
            with open(out_path, "w", encoding="utf-8") as fh:
                json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        except Exception:
            pass
    else:
        print("  no complete trials")
        results["joint"] = None

    # ---------- 5. overall champion by VAL ----------
    print()
    print("=== 5. OVERALL (by VAL) ===")
    entries = [("REF", ref["val"]["net"], dict(params=None, disabled=None, thr=REF_EXIT["thr"]))]
    if len(all_winners) == len(SIDS):
        entries.append(("COMBINED-all", comb_all["val"]["net"],
                        dict(params=dict(all_winners), disabled=None, thr=REF_EXIT["thr"])))
    if adopted_params:
        entries.append(("COMBINED-adopted", comb_ad["val"]["net"],
                        dict(params=dict(adopted_params), disabled=None, thr=REF_EXIT["thr"])))
    if sub_res is not None:
        entries.append(("SUBSET", sub_res["val"]["net"],
                        dict(params=None, disabled=sub_cfg["disabled"], thr=REF_EXIT["thr"])))
    if j_res is not None:
        entries.append(("JOINT", j_res["val"]["net"],
                        dict(params=j_cfg["params"], disabled=None, thr=j_cfg["thr"])))
    entries.sort(key=lambda x: -x[1])
    for name, v, _c in entries:
        print("  %-18s val=%+9.2f" % (name, v))
    champ_name, champ_val, champ_cfg = entries[0]
    champ_adopted = champ_val > ref["val"]["net"] + ADOPT_MARGIN
    print("  CHAMPION: %s (val %+.2f vs REF %+.2f) -> %s" % (
        champ_name, champ_val, ref["val"]["net"],
        "ADOPTED" if champ_adopted else "REF stands"))

    champ = eval_config(params_by_sid=champ_cfg["params"], disabled=champ_cfg["disabled"],
                        thr=champ_cfg["thr"], splits=("train", "val", "all"))
    results["champion"] = {"name": champ_name, "cfg": {k: v for k, v in champ_cfg.items()},
                           "train": champ["train"], "val": champ["val"], "all": champ["all"],
                           "adopted": champ_adopted}

    # ---------- 6. stability by day ----------
    print()
    print("=== 6. STABILITY: champion vs REF, full period by day ===")
    print("  day         REF net   champ net    delta")
    for d in ALL_DAYS:
        r0 = ref["all"]["by_day"].get(d, 0.0)
        c0 = champ["all"]["by_day"].get(d, 0.0)
        print("  %s %+9.2f %+11.2f %+9.2f" % (d, r0, c0, c0 - r0))
    print("  TOTAL: REF %+.2f (n=%d) | champion %+.2f (n=%d)" % (
        ref["all"]["net"], ref["all"]["n"], champ["all"]["net"], champ["all"]["n"]))
    tk_top = sorted(champ["all"]["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]
    tk_bot = sorted(champ["all"]["by_tk"].items(), key=lambda kv: kv[1][1])[:5]
    print("  champion best tickers : %s" % [(t, v[0], v[1]) for t, v in tk_top])
    print("  champion worst tickers: %s" % [(t, v[0], v[1]) for t, v in tk_bot])

    # ---------- 7. neighbour stability ----------
    nb_res = None
    if champ_cfg["params"]:
        print()
        print("=== 7. NEIGHBOUR STABILITY (each param nudged; VAL net) ===")
        vals = [champ_val]
        rows = []
        for label, p2, thr2 in neighbours(champ_cfg["params"], champ_cfg["thr"]):
            r2 = eval_config(params_by_sid=p2, thr=thr2, splits=("val",))
            vals.append(r2["val"]["net"])
            rows.append({"nudge": label, "val": round(r2["val"]["net"], 2)})
            print("    %-26s val=%+9.2f" % (label, r2["val"]["net"]))
        med = statistics.median(vals)
        verdict = "PLATEAU (trust)" if min(vals) > ref["val"]["net"] else "SPIKY (suspect overfit)"
        print("    champion %+.2f | neighbours med %+.2f min %+.2f max %+.2f -> %s" % (
            champ_val, med, min(vals), max(vals), verdict))
        nb_res = {"rows": rows, "min": round(min(vals), 2), "med": round(med, 2),
                  "verdict": verdict}
        results["neighbours"] = nb_res

    # ---------- conclusions ----------
    concl = list(PRIOR_FINDINGS)
    for sid in SIDS:
        if sid in stageP:
            concl.append("P %-20s best %s -> val %+.2f (delta %+.2f) %s" % (
                sid, json.dumps(stageP[sid]["params"]), stageP[sid]["val"]["net"],
                stageP[sid]["val"]["net"] - ref["val"]["net"],
                "ADOPTED" if stageP[sid]["adopted"] else "rejected"))
    concl.append("COMBINED-all: train %+.2f val %+.2f | COMBINED-adopted: train %+.2f val %+.2f (REF val %+.2f)" % (
        comb_all["train"]["net"], comb_all["val"]["net"],
        comb_ad["train"]["net"], comb_ad["val"]["net"], ref["val"]["net"]))
    if sub_res is not None:
        concl.append("SUBSET off=%s: val %+.2f (delta %+.2f)" % (
            ",".join(results["subset"]["disabled"]) or "none",
            sub_res["val"]["net"], sub_res["val"]["net"] - ref["val"]["net"]))
    if j_res is not None:
        concl.append("JOINT thr=%.2f: train %+.2f val %+.2f (delta %+.2f)" % (
            j_cfg["thr"], j_res["train"]["net"], j_res["val"]["net"],
            j_res["val"]["net"] - ref["val"]["net"]))
    concl.append("CHAMPION: %s val %+.2f vs REF %+.2f -> %s" % (
        champ_name, champ_val, ref["val"]["net"],
        "ADOPTED" if champ_adopted else "REF stands (params already near-optimal)"))
    concl.append("overfit check: champion train %+.2f vs val %+.2f (gap %.0f); REF gap %.0f" % (
        champ["train"]["net"], champ["val"]["net"],
        champ["train"]["net"] - champ["val"]["net"],
        ref["train"]["net"] - ref["val"]["net"]))
    if nb_res is not None:
        concl.append("neighbours: min %+.2f med %+.2f -> %s" % (
            nb_res["min"], nb_res["med"], nb_res["verdict"]))
    concl.append("NEXT: (1) if champion ADOPTED with val gain > +150 -> port to live bot variant "
                 "(bot-section like q2rsi2) and cross-check on .4 replay; (2) blind test on "
                 "fresh data is MANDATORY (no TEST window left in 12-19.09); "
                 "(3) walk-forward + monte-carlo when more days are loaded")
    print()
    print("=== CONCLUSIONS (recorded to results_optuna_params.json) ===")
    for c in concl:
        print("  - %s" % c)
    results["conclusions"] = concl

    results["partial"] = False
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        print("results saved: %s" % out_path)
    except Exception as e:
        print("results save FAILED: %s %s" % (type(e).__name__, str(e)[:80]))
    print()
    print("wall %.0fs | OPTUNA_PARAMS_DONE" % (time.perf_counter() - t_start))


def CHAMP_P(sid):
    for s, p in SETUPS:
        if s == sid:
            return p
    return {}


if __name__ == "__main__":
    main()
