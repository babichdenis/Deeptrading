#!/usr/bin/env python3
"""mtf_sim.py - cascade M10 -> M5 -> M1 (direction -> confirmation -> entry).

Offline test of the multi-TF pipeline on the worker bundle (machine .2):
  M10 = direction    (6 ensemble setups on 10m bars, quorum; cached per 10m bar)
  M5  = confirmation (4 cheap checks on closed 5m bars: ema9/ema21, macd hist,
                      rsi vs 50, last-close momentum)
  M1  = entry only   (3 cheap checks on 1m bars: micro-breakout, pullback to
                      EMA9 within 0.25*ATR, volume spike). M1 never sets direction.

USER REQUEST for this run: ALL cooldowns OFF and none of the live muzzles
(h1_align / orderbook / tf_conflict / rank_filter). The offline sim has no
gates at all by construction; every tested variant uses cooldown=0, except
one clearly labelled REF row (old offline champion, cooldown=15) kept purely
as a reproduction check (expected ~n=457 wr=71.8% net=+4046).

If a cascade variant wins, the live port = variant file with the same
bot-section as q2rsi2 (confirm_flip=0, reentry_cooldown_bars=0,
entry_h1_align=False, entry_tf_conflict=False, entry_ob_*=0, rank_enabled=False,
same_side_reentry_cooldown_bars=0).

Shared rules with all previous sims: day session 9:50-19:00 MSK, bar turnover
>= 50k RUR, close at day end, cost 0.045%/side, ~6400 RUR per position.

Also profiles CPU per stage (resample / M10 votes / M5 / M1) and estimates
live cost per 1m tick = m1 + m5/5 + m10/10 (incremental-friendly), plus the
candidate-pool funnel (bars -> directed -> M5 confirmed -> M1 entry).

Run on worker .2 (bundle at ~/q2opt):  cd q2opt & python -u mtf_sim.py
Run on .4: Q2DATA=backend/data/q2optuna_data.pkl python -u scripts/mtf_sim.py
"""
from __future__ import annotations

import os
import sys
import time
import pickle
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import timedelta, timezone

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("Q2ROOT") or HERE)

from app.engine.models import Candle  # noqa: E402
from app.services.ensemble import generate_signals, resample  # noqa: E402

MSK = timezone(timedelta(hours=3))
COST = 0.00045
NOTIONAL = 6400.0
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
LOOKBACK = 15        # minutes: M10 vote accumulation window (champion value)
TURNOVER = 50000.0   # min 1m bar turnover, RUR

# M10 setups = the champion q2 config, inlined (no json dependency in bundle).
M10_SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
]


def ema_list(vals, p):
    k = 2.0 / (p + 1)
    out = []
    e = vals[0] if vals else 0.0
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def macd_hist_list(closes):
    f = ema_list(closes, 12)
    s = ema_list(closes, 26)
    m = [a - b for a, b in zip(f, s)]
    g = ema_list(m, 9)
    return [a - b for a, b in zip(m, g)]


def rsi_np(closes, period=14):
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


def atr_np(hi, lo, cl, period=14):
    n = len(cl)
    tr = np.empty(n)
    tr[0] = float(hi[0]) - float(lo[0])
    for i in range(1, n):
        tr[i] = max(float(hi[i]) - float(lo[i]),
                    abs(float(hi[i]) - float(cl[i - 1])),
                    abs(float(lo[i]) - float(cl[i - 1])))
    atr = np.full(n, np.nan)
    if n >= period:
        atr[period - 1] = float(tr[:period].mean())
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def m10_votes(ts, bars10):
    """Votes of the 6 setups on 10m bars -> per-1m-bar (nbuy, nsell) over LOOKBACK.

    Returns (nbuy, nsell, sig_ms, map_ms): sig_ms = cost of the 6 generate_signals
    (per-10m-bar work, cacheable), map_ms = offline window mapping (a live
    incremental bot does this in O(new events) ~ 0).
    """
    t0 = time.perf_counter()
    ev = defaultdict(lambda: [set(), set()])
    for sid, params in M10_SETUPS:
        try:
            sigs = generate_signals(sid, params, bars10)
        except Exception:
            sigs = []
        for sg in sigs:
            sds = str(sg.get("side", "")).upper()
            if "BUY" in sds:
                ev[sg["ts"]][0].add(sid)
            elif "SELL" in sds:
                ev[sg["ts"]][1].add(sid)
    sig_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    ev_list = [(t, ev[t][0], ev[t][1]) for t in sorted(ev)]
    ev_t = [e[0] for e in ev_list]
    n = len(ts)
    nbuy = np.zeros(n, dtype=np.int8)
    nsell = np.zeros(n, dtype=np.int8)
    j = 0
    lb = timedelta(minutes=LOOKBACK)
    for i in range(n):
        t = ts[i]
        while j < len(ev_list) and ev_t[j] <= t:
            j += 1
        lo_i = bisect_left(ev_t, t - lb)
        b = set()
        s = set()
        for k in range(lo_i, j):
            b |= ev_list[k][1]
            s |= ev_list[k][2]
        nbuy[i] = len(b)
        nsell[i] = len(s)
    map_ms = (time.perf_counter() - t0) * 1000.0
    return nbuy, nsell, sig_ms, map_ms


def m5_confirm(ts, candles):
    """(cntL, cntS) per 1m bar: 0..4 of {ema9>ema21, macd>0, rsi>50, close>prev}.

    5m buckets are built from 1m closes, label = bucket close time (floor to 5m);
    at 1m time t only buckets with label <= t are used -> no lookahead.
    """
    t0 = time.perf_counter()
    buckets = {}
    for c in candles:
        label = c.ts.replace(minute=(c.ts.minute // 5) * 5, second=0, microsecond=0)
        buckets[label] = float(c.close)
    labels = sorted(buckets)
    c5 = [buckets[x] for x in labels]
    e9 = ema_list(c5, 9)
    e21 = ema_list(c5, 21)
    h5 = macd_hist_list(c5)
    r5 = rsi_np(c5, 14)
    m = len(c5)
    cntL5 = np.zeros(m, dtype=np.int8)
    cntS5 = np.zeros(m, dtype=np.int8)
    for j in range(m):
        l = 0
        s = 0
        if e9[j] > e21[j]:
            l += 1
        elif e9[j] < e21[j]:
            s += 1
        if h5[j] > 0:
            l += 1
        elif h5[j] < 0:
            s += 1
        if r5[j] > 50.0:
            l += 1
        elif r5[j] < 50.0:
            s += 1
        if j > 0:
            if c5[j] > c5[j - 1]:
                l += 1
            elif c5[j] < c5[j - 1]:
                s += 1
        cntL5[j] = l
        cntS5[j] = s
    sig_ms = (time.perf_counter() - t0) * 1000.0

    t0 = time.perf_counter()
    n = len(ts)
    cntL = np.zeros(n, dtype=np.int8)
    cntS = np.zeros(n, dtype=np.int8)
    for i in range(n):
        j = bisect_right(labels, ts[i]) - 1
        if j >= 0:
            cntL[i] = cntL5[j]
            cntS[i] = cntS5[j]
    map_ms = (time.perf_counter() - t0) * 1000.0
    return cntL, cntS, m, sig_ms, map_ms


def m1_cond(candles):
    """(cntL, cntS) per 1m bar: 0..3 of {micro-breakout, pullback, volume}.

    M1 never sets direction: breakout is side-specific, pullback and volume
    spike are side-neutral "yes, now" conditions.
    """
    cl = np.array([float(c.close) for c in candles])
    vl = np.array([float(c.volume or 0) for c in candles])
    hi = np.array([float(c.high) for c in candles])
    lo = np.array([float(c.low) for c in candles])
    n = len(cl)
    e9 = ema_list([float(x) for x in cl], 9)
    atr = atr_np(hi, lo, cl, 14)
    cntL = np.zeros(n, dtype=np.int8)
    cntS = np.zeros(n, dtype=np.int8)
    for i in range(n):
        l = 0
        s = 0
        if i >= 5:
            w = cl[i - 5:i]
            if cl[i] > w.max():
                l += 1
            elif cl[i] < w.min():
                s += 1
        if np.isfinite(atr[i]) and atr[i] > 0:
            if abs(cl[i] - e9[i]) <= 0.25 * atr[i]:
                l += 1
                s += 1
        if i >= 20:
            ma = float(vl[i - 20:i].mean())
            if ma > 0 and vl[i] > 1.2 * ma:
                l += 1
                s += 1
        cntL[i] = l
        cntS[i] = s
    return cntL, cntS


def simulate(d, cfg, cooldown):
    ts = d["ts"]
    cl = d["cl"]
    vl = d["vl"]
    r1 = d["r1"]
    nbuy = d["nbuy"]
    nsell = d["nsell"]
    m5L = d["m5L"]
    m5S = d["m5S"]
    m1L = d["m1L"]
    m1S = d["m1S"]
    hm = d["hm"]
    day = d["day"]
    n = len(ts)
    mode = cfg.get("mode", "base")
    m10 = cfg.get("m10", "q2c")
    k5 = cfg.get("k5", 0)
    need1 = cfg.get("need1", 0)
    thr = cfg.get("thr", 0.0)
    exit_mode = cfg.get("exit", "rsi")
    trades = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    for i in range(60, n):
        if pos and day[i] != day[i - 1]:
            px = float(cl[i - 1])
            trades.append((ts[entry_i], ts[i - 1], sgn, "day_end",
                           sgn * (px - ep) * eq - COST * (ep + px) * eq))
            pos = 0
            last_exit = i - 1
        if pos == 0:
            if cooldown and i - last_exit <= cooldown:
                continue
            if float(cl[i]) * float(vl[i]) < TURNOVER:
                continue
            h = int(hm[i])
            if not (DAY_START <= h < DAY_END):
                continue
            b = int(nbuy[i])
            s2 = int(nsell[i])
            want = 0
            if m10 == "q2maj":
                if b >= 2 and b > s2:
                    want = 1
                elif s2 >= 2 and s2 > b:
                    want = -1
            else:
                if b >= 2 and s2 == 0:
                    want = 1
                elif s2 >= 2 and b == 0:
                    want = -1
            if want:
                ok = True
                if mode == "cascade":
                    c5 = int(m5L[i] if want == 1 else m5S[i])
                    if c5 < k5:
                        ok = False
                    else:
                        c1 = int(m1L[i] if want == 1 else m1S[i])
                        if c1 < need1:
                            ok = False
                elif mode == "score":
                    m10s = (b if want == 1 else s2) / 6.0
                    m5s = (int(m5L[i]) if want == 1 else int(m5S[i])) / 4.0
                    m1s = (int(m1L[i]) if want == 1 else int(m1S[i])) / 3.0
                    if 0.45 * m10s + 0.35 * m5s + 0.20 * m1s < thr:
                        ok = False
                if ok:
                    sgn = want
                    ep = float(cl[i])
                    eq = max(1, round(NOTIONAL / ep))
                    pos = 1
                    entry_i = i
        else:
            ex = False
            if sgn == 1 and float(r1[i]) > 70.0:
                ex = True
            elif sgn == -1 and float(r1[i]) < 30.0:
                ex = True
            if not ex and exit_mode == "rsi|flip":
                if sgn == 1 and int(nsell[i]) >= 2 and int(nbuy[i]) == 0:
                    ex = True
                elif sgn == -1 and int(nbuy[i]) >= 2 and int(nsell[i]) == 0:
                    ex = True
            if ex:
                px = float(cl[i])
                trades.append((ts[entry_i], ts[i], sgn, "exit",
                               sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
    return trades


def agg(trades):
    n = len(trades)
    wins = sum(1 for t in trades if t[4] > 0)
    net = sum(t[4] for t in trades)
    by_day = defaultdict(float)
    by_side = defaultdict(lambda: [0, 0.0])
    by_tk = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_day[t[0].astimezone(MSK).strftime("%d.%m")] += t[4]
        k = "L" if t[2] == 1 else "S"
        by_side[k][0] += 1
        by_side[k][1] += t[4]
        by_tk[t[5]][0] += 1
        by_tk[t[5]][1] += t[4]
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
    hold = sorted((t[1] - t[0]).total_seconds() / 60.0 for t in trades)
    return dict(n=n, wins=wins, net=net, by_day=by_day, by_side=by_side,
                by_tk=by_tk, mx=mx, hold=hold, ndays=len(by_day))


VARIANTS = [
    # prev-run refs (cd=0 = ALL cooldowns OFF, gates OFF - as user asked)
    ("REF q2c rsi cd=15 (champion)",
     dict(mode="base", m10="q2c", exit="rsi"), 15),
    ("BASE q2c rsi cd=0 (prev run)",
     dict(mode="base", m10="q2c", exit="rsi"), 0),
    ("CASCADE q2c M5>=2 M1-any rsi|flip cd=0 (prev best)",
     dict(mode="cascade", m10="q2c", k5=2, need1=1, exit="rsi|flip"), 0),
    # THE QUESTION: does M5/M1 confirmation improve the CHAMPION (cd=15)?
    ("CASCADE q2c M5>=2 M1-any rsi cd=15",
     dict(mode="cascade", m10="q2c", k5=2, need1=1, exit="rsi"), 15),
    ("CASCADE q2c M5>=2 M1-any rsi|flip cd=15",
     dict(mode="cascade", m10="q2c", k5=2, need1=1, exit="rsi|flip"), 15),
    ("CASCADE q2c M5>=3 M1-any rsi cd=15",
     dict(mode="cascade", m10="q2c", k5=3, need1=1, exit="rsi"), 15),
    ("CASCADE q2c M5>=2 M1-2of3 rsi cd=15",
     dict(mode="cascade", m10="q2c", k5=2, need1=2, exit="rsi"), 15),
    ("CASCADE q2maj M5>=2 M1-any rsi cd=15",
     dict(mode="cascade", m10="q2maj", k5=2, need1=1, exit="rsi"), 15),
    ("SCORE>=0.60 dir=q2c rsi cd=15",
     dict(mode="score", m10="q2c", thr=0.60, exit="rsi"), 15),
    # ablations: M5-only / M1-only on the champion
    ("M5-only q2c M5>=2 rsi cd=15",
     dict(mode="cascade", m10="q2c", k5=2, need1=0, exit="rsi"), 15),
    ("M1-only q2c M1-any rsi cd=15",
     dict(mode="cascade", m10="q2c", k5=0, need1=1, exit="rsi"), 15),
]


def main():
    t_start = time.perf_counter()
    data_path = os.environ.get("Q2DATA") or os.path.join(HERE, "data", "q2optuna_data.pkl")
    if not os.path.exists(data_path):
        print("DATA NOT FOUND: %s" % data_path)
        print("set Q2DATA=/path/to/q2optuna_data.pkl")
        sys.exit(1)
    with open(data_path, "rb") as fh:
        raw = pickle.load(fh)
    t_load = time.perf_counter() - t_start

    per = {}
    stage_ms = {"resample": 0.0, "m10sig": 0.0, "m10map": 0.0,
                "m5sig": 0.0, "m5map": 0.0, "m1": 0.0, "sim": 0.0}
    n10_tot = n5_tot = n1_tot = 0

    for f in sorted(raw):
        rows = raw[f]["candles"]
        if len(rows) < 300:
            continue
        candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]),
                          low=float(x[3]), close=float(x[4]), volume=int(x[5] or 0))
                   for x in rows]
        ts = [c.ts for c in candles]
        cl = np.array([c.close for c in candles], dtype=float)
        vl = np.array([float(c.volume or 0) for c in candles], dtype=float)
        hm = []
        day = []
        for t in ts:
            tm = t.astimezone(MSK)
            hm.append(tm.hour * 60 + tm.minute)
            day.append(tm.toordinal())

        t0 = time.perf_counter()
        bars10 = resample(candles, 600)
        stage_ms["resample"] += (time.perf_counter() - t0) * 1000.0

        nbuy, nsell, s_ms, m_ms = m10_votes(ts, bars10)
        stage_ms["m10sig"] += s_ms
        stage_ms["m10map"] += m_ms

        m5L, m5S, n5, s_ms, m_ms = m5_confirm(ts, candles)
        stage_ms["m5sig"] += s_ms
        stage_ms["m5map"] += m_ms

        t0 = time.perf_counter()
        r1 = rsi_np([float(c.close) for c in candles], 14)
        m1L, m1S = m1_cond(candles)
        stage_ms["m1"] += (time.perf_counter() - t0) * 1000.0

        n10_tot += len(bars10)
        n5_tot += n5
        n1_tot += len(ts)
        per[f] = dict(ticker=str(raw[f].get("ticker") or f), ts=ts, cl=cl, vl=vl,
                      hm=hm, day=day, nbuy=nbuy, nsell=nsell,
                      m5L=m5L, m5S=m5S, m1L=m1L, m1S=m1S, r1=r1)

    if not per:
        print("no data")
        sys.exit(1)

    all_ts = [d["ts"][0] for d in per.values()] + [d["ts"][-1] for d in per.values()]
    print("=== MTF CASCADE SIM: M10 direction -> M5 confirmation -> M1 entry ===")
    print("gates OFF always (h1/orderbook/tf/rank): offline by construction")
    print("run 2: DOES M5/M1 CONFIRMATION IMPROVE THE CHAMPION (cd=15)?")
    print("+ ablations M5-only / M1-only + prev cd=0 rows for continuity")
    print("data: %s -> %s MSK | figis=%d | 1m=%d 5m=%d 10m=%d | load %.1fs" % (
        min(all_ts).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(all_ts).astimezone(MSK).strftime("%d.%m %H:%M"),
        len(per), n1_tot, n5_tot, n10_tot, t_load))

    # --- candidate-pool funnel (the 30 -> 12 -> 7 -> 3 idea, measured) ---
    n_bars = n_dir = n_m5_2 = n_m5_3 = n_casc_any = n_casc_2 = 0
    for d in per.values():
        for i in range(60, len(d["ts"])):
            if not (DAY_START <= d["hm"][i] < DAY_END):
                continue
            if float(d["cl"][i]) * float(d["vl"][i]) < TURNOVER:
                continue
            n_bars += 1
            b = int(d["nbuy"][i])
            s2 = int(d["nsell"][i])
            dirn = 0
            if b >= 2 and s2 == 0:
                dirn = 1
            elif s2 >= 2 and b == 0:
                dirn = -1
            if not dirn:
                continue
            n_dir += 1
            c5 = int(d["m5L"][i] if dirn == 1 else d["m5S"][i])
            c1 = int(d["m1L"][i] if dirn == 1 else d["m1S"][i])
            if c5 >= 2:
                n_m5_2 += 1
                if c1 >= 1:
                    n_casc_any += 1
                if c1 >= 2:
                    n_casc_2 += 1
            if c5 >= 3:
                n_m5_3 += 1
    print()
    print("=== CASCADE FUNNEL (day-session bars with turnover ok) ===")
    print("  1m bars             : %7d" % n_bars)
    print("  + M10 direction     : %7d (%.2f%% of bars)" % (n_dir, 100.0 * n_dir / max(n_bars, 1)))
    print("  + M5 confirm >=2/4  : %7d (%.1f%% of directed)" % (n_m5_2, 100.0 * n_m5_2 / max(n_dir, 1)))
    print("  + M5 confirm >=3/4  : %7d (%.1f%% of directed)" % (n_m5_3, 100.0 * n_m5_3 / max(n_dir, 1)))
    print("  + M1 any  (M5>=2)   : %7d" % n_casc_any)
    print("  + M1 2of3 (M5>=2)   : %7d" % n_casc_2)

    # --- grid ---
    results = []
    for name, cfg, cd in VARIANTS:
        t0 = time.perf_counter()
        trades = []
        for f, d in per.items():
            for tr in simulate(d, cfg, cd):
                trades.append(tr + (d["ticker"],))
        stage_ms["sim"] += (time.perf_counter() - t0) * 1000.0
        results.append((name, agg(trades), trades))

    print()
    print("=== GRID (cost %.3f%%/side, ~%.0f RUR/pos, session 9:50-19:00, day-end close) ===" % (
        100 * COST, NOTIONAL))
    rows_sorted = sorted(results, key=lambda x: -x[1]["net"])
    for name, a, _tr in rows_sorted:
        print("  %-42s n=%4d wr=%5.1f%% net=%+9.2f avg=%+7.2f | L %3d/%+8.1f S %3d/%+8.1f | conc %2d | t/day %5.1f" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
            a["net"] / max(a["n"], 1),
            a["by_side"]["L"][0], a["by_side"]["L"][1],
            a["by_side"]["S"][0], a["by_side"]["S"][1],
            a["mx"], a["n"] / max(a["ndays"], 1)))

    print()
    print("=== TOP-3 DETAILS ===")
    for name, a, _tr in rows_sorted[:3]:
        print("--- %s: n=%d wr=%.1f%% net=%+.2f ---" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"]))
        print("  by day: " + "  ".join("%s:%+.0f" % kv for kv in sorted(a["by_day"].items())))
        if a["hold"]:
            print("  hold, min: med=%.0f p90=%.0f max=%.0f" % (
                a["hold"][len(a["hold"]) // 2],
                a["hold"][min(len(a["hold"]) - 1, int(len(a["hold"]) * 0.9))],
                a["hold"][-1]))
        print("  worst: %s" % [(t, v[0], round(v[1], 1)) for t, v in
                               sorted(a["by_tk"].items(), key=lambda kv: kv[1][1])[:5]])
        print("  best : %s" % [(t, v[0], round(v[1], 1)) for t, v in
                               sorted(a["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]])

    base0 = next((r for r in results if r[0].startswith("BASE")), None)
    ref = next((r for r in results if r[0].startswith("REF")), None)
    best_casc = next((r for r in rows_sorted if r[0].startswith("CASCADE")), None)
    print()
    print("=== COMPARE ===")
    if base0:
        print("  BASE (q2c entry, cd=0, no muzzles): n=%d net=%+.2f" % (base0[1]["n"], base0[1]["net"]))
    if best_casc:
        print("  BEST CASCADE                      : n=%d net=%+.2f  (%s)" % (
            best_casc[1]["n"], best_casc[1]["net"], best_casc[0]))
        if base0:
            print("  cascade minus base                : %+.2f" % (best_casc[1]["net"] - base0[1]["net"]))
    if ref:
        print("  REF reproduction check: n=%d wr=%.1f%% net=%+.2f (expected ~457 / ~71.8 / ~+4046)" % (
            ref[1]["n"], 100.0 * ref[1]["wins"] / max(ref[1]["n"], 1), ref[1]["net"]))

    print()
    print("=== CPU PROFILE (offline full-series; live incremental ~1 op/bar) ===")
    nf = len(per)
    print("  resample(10m)          : total %8.1f ms | %6.1f ms/figi" % (
        stage_ms["resample"], stage_ms["resample"] / nf))
    print("  M10 votes (per 10m bar): total %8.1f ms | %6.1f ms/figi" % (
        stage_ms["m10sig"], stage_ms["m10sig"] / nf))
    print("  M5 indicators (per 5m) : total %8.1f ms | %6.1f ms/figi" % (
        stage_ms["m5sig"], stage_ms["m5sig"] / nf))
    print("  M1 checks (per 1m bar) : total %8.1f ms | %6.1f ms/figi" % (
        stage_ms["m1"], stage_ms["m1"] / nf))
    print("  (offline window-mapping artifacts: m10map %.0f ms, m5map %.0f ms - live = O(new events) ~ 0)" % (
        stage_ms["m10map"], stage_ms["m5map"]))
    per_tick = (stage_ms["m1"] / max(n1_tot, 1)
                + stage_ms["m5sig"] / max(n5_tot, 1) / 5.0
                + stage_ms["m10sig"] / max(n10_tot, 1) / 10.0)
    print("  est. live cost: %.3f ms per 1m tick per ticker -> %.1f ms per trading minute for %d tickers" % (
        per_tick, per_tick * nf, nf))
    print("  sim loops (%d variants): %.1f s | total wall %.1f s" % (
        len(VARIANTS), stage_ms["sim"] / 1000.0, time.perf_counter() - t_start))
    print()
    print("MTF_SIM_DONE")


if __name__ == "__main__":
    main()
