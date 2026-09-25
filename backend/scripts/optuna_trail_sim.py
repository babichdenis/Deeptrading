#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""optuna_trail_sim.py - stage T: EXIT params (bot-parity trailing) vs REF (RSI 70/30).

Data/inputs 1:1 with optuna_params_sim.py: q2optuna_data.pkl, champion setups,
quorum-2 (thr=2.0, opp=0), session 09:50-19:00 MSK, turnover>=50k, NOTIONAL 6400,
COST 0.045%/side, cd=15 (1m bars).

Exits: 1:1 port of bot trailing (app/engine/exits.py AtrStopPolicy) to 5m bars:
  initial SL = entry -/+ sl_mult*ATR14(5m, Wilder/TR)   [plan_entry]
  per-5m-bar order as bot _step_exit:
    (1) exit-check with CURRENT stop state:
        - not activated: SL by touch (gap->open, low/high->sl)
        - activated: close_based (gap->open, close->close)
    (2) activation: pnl_rub(close) >= act_mult * entry_commission
    (3) ratchet: dist = trail*ATR_now * compress(r) * vol_adj, floor min_atr*ATR;
        candidate = extreme14 -/+ dist; stop only improves (max/min).
  EOD: close at last 1m bar of the day (as REF).
compress: factor = max(min_factor, 1 - compress*max(0, r)), r = pnl_px/risk_now,
risk_now = ATR_now*sl_mult. vol: adj = clamp(sqrt(vr), 1-vb, 1+vb), vr = vol/mean50.

REF parity targets: train +2744.97 / val +1583.57 (params_sim).
Objective = train net - 0.3*day_std - 50*max(0,40-n); top-8 by train obj ->
pick best VAL; adopt if VAL > REF_VAL + 50 RUR.

Run on .2: cd ~/Dev/Deeptrading/backend/scripts && .venv/bin/python -u optuna_trail_sim.py
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
ROOT = os.path.dirname(HERE)  # .../backend
sys.path.insert(0, ROOT)

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
CD = 15
THR = 2.0
OPP = 0.0
REF_TRAIN_NET = 2744.97
REF_VAL_NET = 1583.57
ADOPT_MARGIN = 50.0

BOT_DEFAULT = {"sl_mult": 2.0, "trail": 2.5, "act_mult": 4.0,
               "compress": 1.0, "min_factor": 0.3, "min_atr": 0.5,
               "vol_boost": 0.3}
SPACE = {"sl_mult": (1.0, 3.0), "trail": (1.0, 4.0), "act_mult": (1.0, 8.0),
         "compress": (0.0, 2.0), "min_factor": (0.2, 0.9),
         "min_atr": (0.0, 2.0), "vol_boost": (0.0, 0.6)}

SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
]
SIDS = [s for s, _ in SETUPS]

per = {}
TRAIN_DAYS = []
VAL_DAYS = []


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


def atr_wilder_tr(h, l, c, period=14):
    """Wilder ATR on true range (parity of exits.AtrStopPolicy._risk)."""
    n = len(c)
    trs = [0.0] * n
    for i in range(n):
        if i == 0:
            trs[i] = h[i] - l[i]
        else:
            trs[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    out = [None] * n
    if n < period:
        return out
    a = sum(trs[:period]) / period
    out[period - 1] = a
    for i in range(period, n):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
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


def merged_events(ev_ref):
    ev = defaultdict(lambda: [set(), set()])
    for sid in SIDS:
        for t, s in (ev_ref.get(sid) or {}).items():
            if s == "B":
                ev[t][0].add(sid)
            else:
                ev[t][1].add(sid)
    return [(t, ev[t][0], ev[t][1]) for t in sorted(ev)]


def build_cands(d, nb, ns):
    """Entry candidates (quorum/turnover/session); cd and split applied at runtime."""
    cl, vl, hm = d["cl"], d["vl"], d["hm"]
    out = []
    for i in range(60, len(cl)):
        if cl[i] * vl[i] < TURNOVER:
            continue
        h = hm[i]
        if not (DAY_START <= h < DAY_END):
            continue
        b, s = nb[i], ns[i]
        if b >= THR and s <= OPP:
            out.append((i, "L"))
        elif s >= THR and b <= OPP:
            out.append((i, "S"))
    return out


def prep():
    global per, TRAIN_DAYS, VAL_DAYS
    data_path = os.environ.get("Q2DATA") or os.path.join(ROOT, "data", "q2optuna_data.pkl")
    if not os.path.exists(data_path):
        print("DATA NOT FOUND: %s" % data_path)
        sys.exit(1)
    with open(data_path, "rb") as fh:
        raw = pickle.load(fh)
    t0 = time.perf_counter()
    for f in sorted(raw):
        rows = raw[f]["candles"]
        if len(rows) < 300:
            continue
        candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]), low=float(x[3]),
                          close=float(x[4]), volume=int(x[5] or 0)) for x in rows]
        ts = [c.ts for c in candles]
        cl = [float(c.close) for c in candles]
        vl = [float(c.volume or 0) for c in candles]
        day_l, hm_l, m_tr, m_va = [], [], [], []
        for t in ts:
            tm = t.astimezone(MSK)
            d0 = tm.date()
            day_l.append(d0.toordinal())
            hm_l.append(tm.hour * 60 + tm.minute)
            m_tr.append(d0 <= TRAIN_LAST)
            m_va.append(d0 >= VAL_FIRST)
        bars10 = resample(candles, 600)
        ev_ref = {sid: setup_events(sid, prm, bars10) for sid, prm in SETUPS}
        merged = merged_events(ev_ref)
        states = states_for(ts, merged, SIDS, LOOKBACK_MIN * 60)
        arr = np.array(states, dtype=np.int8)
        nb = (arr == 1).sum(axis=1).astype(float).tolist()
        ns = (arr == -1).sum(axis=1).astype(float).tolist()
        # --- 5m bars (bot TF): arrays for trailing sim ---
        b5 = resample(candles, 300)
        ts5 = [c.ts for c in b5]
        o5 = [float(c.open) for c in b5]
        h5 = [float(c.high) for c in b5]
        l5 = [float(c.low) for c in b5]
        c5 = [float(c.close) for c in b5]
        v5 = [float(c.volume or 0) for c in b5]
        n5 = len(ts5)
        atr5 = atr_wilder_tr(h5, l5, c5, 14)
        hmax14 = [max(h5[max(0, j - 13):j + 1]) for j in range(n5)]
        hmin14 = [min(l5[max(0, j - 13):j + 1]) for j in range(n5)]
        vmean50 = [sum(v5[max(0, j - 49):j + 1]) / len(v5[max(0, j - 49):j + 1]) for j in range(n5)]
        day5 = [t.astimezone(MSK).date().toordinal() for t in ts5]
        # 1m -> 5m index maps
        j_of_i = [0] * len(ts)
        j_end = [None] * n5
        j = 0
        for i, t in enumerate(ts):
            while j + 1 < n5 and ts5[j + 1] <= t:
                j += 1
            j_of_i[i] = j
        for i, jj in enumerate(j_of_i):
            if j_end[jj] is None or i > j_end[jj]:
                j_end[jj] = i
        day_last_1m = {}
        for i, dd in enumerate(day_l):
            day_last_1m[dd] = i
        d = dict(ticker=str(raw[f].get("ticker") or f), ts=ts, cl=cl, vl=vl,
                 r=[float(x) for x in rsi_wilder(cl, 14)], day=day_l, hm=hm_l,
                 m_tr=m_tr, m_va=m_va, nb=nb, ns=ns,
                 ts5=ts5, o5=o5, h5=h5, l5=l5, c5=c5, v5=v5, atr5=atr5,
                 hmax14=hmax14, hmin14=hmin14, vmean50=vmean50, day5=day5,
                 j_of_i=j_of_i, j_end=j_end, day_last_1m=day_last_1m)
        d["cand"] = build_cands(d, nb, ns)
        per[f] = d
    if not per:
        print("no data")
        sys.exit(1)
    all_days_d = sorted({t.astimezone(MSK).date() for d in per.values() for t in d["ts"]})
    TRAIN_DAYS = [d.isoformat() for d in all_days_d if d <= TRAIN_LAST]
    VAL_DAYS = [d.isoformat() for d in all_days_d if d >= VAL_FIRST]
    n_bars = sum(len(d["ts"]) for d in per.values())
    n_cand = sum(len(d["cand"]) for d in per.values())
    print("data: figis=%d | 1m bars=%d | cands=%d | prep %.1fs" % (
        len(per), n_bars, n_cand, time.perf_counter() - t0))
    print("TRAIN %s | VAL %s" % (",".join(TRAIN_DAYS), ",".join(VAL_DAYS)))


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
    return dict(n=n, wins=wins, net=round(net, 2), day_std=round(dstd, 1),
                L=[ls["L"][0], round(ls["L"][1], 2)],
                S=[ls["S"][0], round(ls["S"][1], 2)], mx=mx)


# ---------------- TRAIL (bot-parity) ----------------

def _trail_exit(d, i0, side, p):
    """Walk position from entry i0 to SL/trail-stop/EOD. -> (px, ts, idx1m) | None.
    Per-bar order = bot _step_exit: (1) exit-check current state ->
    (2) activation -> (3) ratchet stop update."""
    cl, ts = d["cl"], d["ts"]
    o5, c5, l5, h5 = d["o5"], d["c5"], d["l5"], d["h5"]
    atr5, hmax14, hmin14, vmean50 = d["atr5"], d["hmax14"], d["hmin14"], d["vmean50"]
    n5 = len(d["ts5"])
    sgn = 1 if side == "L" else -1
    ep = cl[i0]
    eq = max(1, round(NOTIONAL / ep))
    thr_act = p["act_mult"] * COST * ep * eq
    j0 = d["j_of_i"][i0]
    j = j0 + 1
    if j >= n5:
        return None
    day0 = d["day"][i0]
    a0 = atr5[j0] or ep * 0.01
    sl = ep - a0 * p["sl_mult"] if sgn > 0 else ep + a0 * p["sl_mult"]
    stop = sl
    active = False
    while j < n5:
        if d["day5"][j] != day0:
            li = d["day_last_1m"][day0]
            return cl[li], ts[li], li
        a = atr5[j]
        # (1) exit-check with current state (SL priority; close_based after activation)
        px = None
        if active:
            if sgn > 0:
                if o5[j] <= stop:
                    px = o5[j]
                elif c5[j] <= stop:
                    px = c5[j]
            else:
                if o5[j] >= stop:
                    px = o5[j]
                elif c5[j] >= stop:
                    px = c5[j]
        else:
            if sgn > 0:
                if o5[j] <= sl:
                    px = o5[j]
                elif l5[j] <= sl:
                    px = sl
            else:
                if o5[j] >= sl:
                    px = o5[j]
                elif h5[j] >= sl:
                    px = sl
        if px is not None:
            ei = d["j_end"][j]
            if ei is None:
                ei = i0
            return px, d["ts5"][j], ei
        # (2) activation by commission (pnl on bar close)
        if not active and p["act_mult"] > 0:
            pnl_px = (c5[j] - ep) if sgn > 0 else (ep - c5[j])
            if pnl_px * eq >= thr_act:
                active = True
        # (3) ratchet update (parity update_stop)
        if active and a:
            risk_now = a * p["sl_mult"]
            if risk_now > 0:
                pnl_px = (c5[j] - ep) if sgn > 0 else (ep - c5[j])
                r_ = pnl_px / risk_now
                fac = max(p["min_factor"], 1.0 - p["compress"] * max(0.0, r_))
                dist = p["trail"] * a * fac
                if p["vol_boost"] > 0 and vmean50[j] > 0:
                    vr = d["v5"][j] / vmean50[j]
                    adj = min(1.0 + p["vol_boost"], max(1.0 - p["vol_boost"], vr ** 0.5))
                    dist *= adj
                dist = max(dist, p["min_atr"] * a)
                if sgn > 0:
                    stop = max(stop, hmax14[j] - dist)
                else:
                    stop = min(stop, hmin14[j] + dist)
        j += 1
    return cl[-1], ts[-1], len(cl) - 1


def trail_trades(f, p, mask):
    d = per[f]
    cl, ts = d["cl"], d["ts"]
    out = []
    last_exit = -10 ** 9
    for i, side in d["cand"]:
        if mask is not None and not mask[i]:
            continue
        if i - last_exit <= CD:
            continue
        res = _trail_exit(d, i, side, p)
        if res is None:
            continue
        px, tpx, idx = res
        ep = cl[i]
        eq = max(1, round(NOTIONAL / ep))
        sgn = 1 if side == "L" else -1
        pnl = sgn * (px - ep) * eq - COST * (ep + px) * eq
        out.append((ts[i], tpx, sgn, pnl, d["ticker"]))
        last_exit = idx
    return out


def eval_trail(p, splits=("train", "val")):
    trades = {"train": [], "val": []}
    for f in per:
        if "train" in splits:
            trades["train"] += trail_trades(f, p, per[f]["m_tr"])
        if "val" in splits:
            trades["val"] += trail_trades(f, p, per[f]["m_va"])
    res = {}
    for split in splits:
        days = TRAIN_DAYS if split == "train" else VAL_DAYS
        res[split] = agg(trades[split], days)
    return res


# ---------------- REF (RSI-exit, exact copy of params_sim.simulate) ----------------

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
            if sgn == 1 and r[i] >= ob:
                px = cl[i]
                out.append((ts[entry_i], ts[i], sgn,
                            sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
            elif sgn == -1 and r[i] <= os_:
                px = cl[i]
                out.append((ts[entry_i], ts[i], sgn,
                            sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
    return out


def eval_ref(splits=("train", "val")):
    trades = {"train": [], "val": []}
    for f in per:
        d = per[f]
        if "train" in splits:
            trades["train"] += simulate(d, d["nb"], d["ns"], THR, OPP, 70.0, 30.0, CD, d["m_tr"])
        if "val" in splits:
            trades["val"] += simulate(d, d["nb"], d["ns"], THR, OPP, 70.0, 30.0, CD, d["m_va"])
    res = {}
    for split in splits:
        days = TRAIN_DAYS if split == "train" else VAL_DAYS
        res[split] = agg(trades[split], days)
    return res


def _fmt(tag, res):
    tr, va = res["train"], res["val"]
    return ("%s train n=%3d net=%+9.2f std=%7.1f L=%+8.1f S=%+8.1f mx=%2d | "
            "val n=%3d net=%+9.2f L=%+8.1f S=%+8.1f" % (
                tag, tr["n"], tr["net"], tr["day_std"], tr["L"][1], tr["S"][1], tr["mx"],
                va["n"], va["net"], va["L"][1], va["S"][1]))


def objective(trial):
    p = {k: trial.suggest_float(k, *SPACE[k]) for k in SPACE}
    tr = eval_trail(p, splits=("train",))["train"]
    obj = tr["net"] - 0.3 * tr["day_std"] - 50.0 * max(0.0, 40 - tr["n"])
    trial.set_user_attr("train", tr)
    return obj


def main():
    t0 = time.perf_counter()
    prep()
    print()
    ref = eval_ref()
    print(_fmt("REF RSI70/30 ", ref))
    bd = eval_trail(BOT_DEFAULT)
    print(_fmt("BOT-DEFAULT  ", bd))
    n_trials = int(os.environ.get("N_TRIALS", "150"))
    n_jobs = int(os.environ.get("N_JOBS", "4"))
    print("\noptuna: %d trials x %d jobs ..." % (n_trials, n_jobs))
    study = optuna.create_study(direction="maximize", study_name="trail_exit_v1")
    study.optimize(objective, n_trials=n_trials, n_jobs=n_jobs, show_progress_bar=False)
    top = sorted([t for t in study.trials if t.value is not None],
                 key=lambda t: t.value, reverse=True)[:8]
    print("\ntop-8 by train objective -> val:")
    scored = []
    for t in top:
        rv = eval_trail(t.params, splits=("val",))["val"]
        scored.append((t.value, rv, t.params))
        print("  obj=%+9.2f | val net=%+9.2f n=%3d | %s" % (
            t.value, rv["net"], rv["n"],
            " ".join("%s=%.2f" % (k, v) for k, v in t.params.items())))
    scored.sort(key=lambda x: x[1]["net"], reverse=True)
    best_obj, best_val, best_p = scored[0]
    full = eval_trail(best_p)
    print()
    print(_fmt("BEST (full)  ", full))
    print(_fmt("REF RSI70/30 ", ref))
    ref_val = ref["val"]["net"]  # сравнение с REF на тех же данных/окне
    adopt = best_val["net"] > ref_val + ADOPT_MARGIN
    print("\nverdict: best VAL %.2f vs same-data REF VAL %.2f (margin %.0f) -> %s" % (
        best_val["net"], ref_val, ADOPT_MARGIN, "ADOPT" if adopt else "KEEP REF"))
    out = {"ts": datetime.now(timezone.utc).isoformat(),
           "ref": ref, "bot_default": bd, "best_params": best_p,
           "best_full": full, "best_val": best_val,
           "ref_train_net": REF_TRAIN_NET, "ref_val_net": ref_val,
           "adopt": bool(adopt),
           "top8": [{"obj": o, "params": pp, "val_net": vv["net"]}
                    for o, vv, pp in scored]}
    path = os.path.join(ROOT, "results_optuna_trail.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print("saved:", path, "| total %.1fs" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
