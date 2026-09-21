#!/usr/bin/env python3
"""parity_sim.py — Stage 17: LIVE-vs-OFFLINE attribution for Q2ref.

Facts (why this stage):
  * offline REF champion, same window (results_optuna_params.json ref/all/by_day):
      14.09 +958.22 | 15.09 +936.15 | 16.09 +1093.23 | 17.09 +715.84 | 18.09 +985.39
  * LIVE Q2ref (.4, sandbox_trades, entries from 14.09 09:24 MSK, still running):
      14.09 -0.49 | 15.09 -601.89 | 16.09(partial) -185.54 -> total -787.92
  * gap ~ -3775 RUR on the same data -> parity break in the live port.

Cumulative one-factor-at-a-time (same 1m data: data/q2optuna_data.pkl 12-19.09, 26 figis):
  ref      : bar turnover>=50k, votes 15m window, cost 0.045%/side, notional 6400,
             close at MSK date change (evening hold), no stops  [self-check vs ref by_day]
  turn100k : min bar turnover 100k          (live min_bar_turnover=100000)
  eod1850  : force-close 18:50 MSK          (live sessions=['day'], eod_close_min_before=10)
  cost7    : cost 0.07%/side                (live commission 0.0005 + slippage_bps=2)
  look30   : votes 30m window               (live FRESH_MIN=30)
  stops6   : SL 6*ATR(14,5m) / TP 24*ATR    (live initial_sl_atr=6.0, atr_risk_reward=4.0)
  live4k   : stops6 with notional 4000      (live pos_pct=0.40 x equity 10000)

Output: per-day table + reason stats + trade dumps (stdout + parity_*_trades.csv).
Run on worker .2:  cd q2opt & python -u parity_sim.py
"""
from __future__ import annotations

import csv
import os
import pickle
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date, timedelta, timezone

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.environ.get("Q2ROOT") or HERE)

from app.engine.models import Candle  # noqa: E402
from app.services.ensemble import generate_signals, resample  # noqa: E402

MSK = timezone(timedelta(hours=3))
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
EOD_1850 = 18 * 60 + 50
OPP = 0.0
OB_THR = 70.0
OS_THR = 30.0
CD = 15
ENTRY_FIRST = date(2026, 9, 14)
ENTRY_LAST = date(2026, 9, 18)
DAYS = [(ENTRY_FIRST + timedelta(days=k)).isoformat() for k in range(5)]
EXPECTED_REF = {"2026-09-14": 958.22, "2026-09-15": 936.15, "2026-09-16": 1093.23,
                "2026-09-17": 715.84, "2026-09-18": 985.39}
LIVE_BY_DAY = {"2026-09-14": -0.49, "2026-09-15": -601.89, "2026-09-16": -185.54}

REF_SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
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


def events_for(setups, bars10):
    """[(ts, buy_set, sell_set)] of the champion config on 10m bars."""
    ev = defaultdict(lambda: [set(), set()])
    for sid, params in setups:
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
    return [(t, ev[t][0], ev[t][1]) for t in sorted(ev)]


def counts_for(ts, ev_list, lookback_sec):
    """(nbuy, nsell) per 1m bar = distinct setups voted in the window."""
    ev_t = [e[0] for e in ev_list]
    nb = []
    ns = []
    j = 0
    lb = timedelta(seconds=lookback_sec)
    for t in ts:
        while j < len(ev_list) and ev_t[j] <= t:
            j += 1
        lo_i = bisect_left(ev_t, t - lb)
        buy = set()
        sell = set()
        for k in range(lo_i, j):
            buy |= ev_list[k][1]
            sell |= ev_list[k][2]
        nb.append(len(buy))
        ns.append(len(sell))
    return nb, ns


def atr_series(bars, period=14):
    """Wilder ATR on bars of any TF (ts = bar close time)."""
    n = len(bars)
    out = [None] * n
    if n < period + 1:
        return out
    trs = []
    pc = None
    for b in bars:
        h, l, c = float(b.high), float(b.low), float(b.close)
        tr = (h - l) if pc is None else max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        pc = c
    a = sum(trs[:period]) / period
    out[period - 1] = a
    for i in range(period, n):
        a = (a * (period - 1) + trs[i]) / period
        out[i] = a
    return out


def build(raw):
    per = {}
    for f in sorted(raw):
        rows = raw[f]["candles"]
        if len(rows) < 300:
            continue
        candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]),
                          low=float(x[3]), close=float(x[4]),
                          volume=int(x[5] or 0)) for x in rows]
        ts = [c.ts for c in candles]
        cl = [float(c.close) for c in candles]
        hi = [float(c.high) for c in candles]
        lo = [float(c.low) for c in candles]
        vl = [float(c.volume or 0) for c in candles]
        bars10 = resample(candles, 600)
        ev_list = events_for(REF_SETUPS, bars10)
        votes = {}
        for lb in (15, 30):
            nb, ns = counts_for(ts, ev_list, lb * 60)
            votes[lb] = (nb, ns)
        bars5 = resample(candles, 300)
        ts5 = [b.ts for b in bars5]
        a5 = atr_series(bars5, 14)
        day = []
        hm = []
        blind = []
        for t in ts:
            tm = t.astimezone(MSK)
            d0 = tm.date()
            day.append(d0.toordinal())
            hm.append(tm.hour * 60 + tm.minute)
            blind.append(ENTRY_FIRST <= d0 <= ENTRY_LAST)
        per[f] = dict(ticker=str(raw[f].get("ticker") or f), ts=ts, cl=cl, hi=hi, lo=lo,
                      vl=vl, r=[float(x) for x in rsi_wilder(cl, 14)],
                      day=day, hm=hm, blind=blind, votes=votes, ts5=ts5, atr5=a5)
    return per


def simulate(d, thr, lookback, turnover, cost, notional, eod, stops):
    """Env-parity sim. Trade = (entry_ts, exit_ts, sgn, reason, net, ticker, ep, xp, eq)."""
    ts = d["ts"]
    cl = d["cl"]
    vl = d["vl"]
    hi = d["hi"]
    lo = d["lo"]
    r = d["r"]
    day = d["day"]
    hm = d["hm"]
    blind = d["blind"]
    tk = d["ticker"]
    nbuy, nsell = d["votes"][lookback]
    ts5 = d["ts5"]
    atr5 = d["atr5"]
    n = len(ts)
    out = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    sl_mult, tp_mult = stops if stops else (0.0, 0.0)
    sl = tp = 0.0

    def close(i, px, reason):
        return (ts[entry_i], ts[i], sgn, reason,
                sgn * (px - ep) * eq - cost * (ep + px) * eq, tk, ep, px, eq)

    for i in range(1, n):
        if pos:
            if eod == "date":
                if day[i] != day[i - 1]:
                    out.append(close(i - 1, cl[i - 1], "day_end"))
                    pos = 0
                    last_exit = i - 1
            else:
                if hm[i] >= EOD_1850:
                    out.append(close(i, cl[i], "eod"))
                    pos = 0
                    last_exit = i
        if i < 60:
            continue
        if pos == 0:
            if not blind[i]:
                continue
            if i - last_exit <= CD:
                continue
            if cl[i] * vl[i] < turnover:
                continue
            h = hm[i]
            if not (DAY_START <= h < DAY_END):
                continue
            if eod != "date" and h >= EOD_1850:
                continue
            b = nbuy[i]
            s = nsell[i]
            if b >= thr and s <= OPP:
                want = 1
            elif s >= thr and b <= OPP:
                want = -1
            else:
                continue
            sgn = want
            ep = cl[i]
            eq = max(1, round(notional / ep))
            pos = 1
            entry_i = i
            sl = tp = 0.0
            if sl_mult:
                k = bisect_right(ts5, ts[i]) - 1
                a = atr5[k] if k >= 0 else None
                if a and a > 0:
                    sl = ep - sgn * sl_mult * a
                    tp = ep + sgn * tp_mult * a
        else:
            if sl:
                px = 0.0
                rs = ""
                if sgn == 1:
                    if lo[i] <= sl:
                        px, rs = sl, "atr_stop"
                    elif tp and hi[i] >= tp:
                        px, rs = tp, "take_profit"
                else:
                    if hi[i] >= sl:
                        px, rs = sl, "atr_stop"
                    elif tp and lo[i] <= tp:
                        px, rs = tp, "take_profit"
                if px:
                    out.append(close(i, px, rs))
                    pos = 0
                    last_exit = i
                    continue
            if (sgn == 1 and r[i] > OB_THR) or (sgn == -1 and r[i] < OS_THR):
                out.append(close(i, cl[i], "exit"))
                pos = 0
                last_exit = i
    if pos:
        out.append(close(n - 1, cl[-1], "day_end"))
    return out


VARIANTS = [
    ("ref",      dict(thr=2.0, lookback=15, turnover=50000.0, cost=0.00045, notional=6400.0, eod="date", stops=None)),
    ("turn100k", dict(thr=2.0, lookback=15, turnover=100000.0, cost=0.00045, notional=6400.0, eod="date", stops=None)),
    ("eod1850",  dict(thr=2.0, lookback=15, turnover=100000.0, cost=0.00045, notional=6400.0, eod="1850", stops=None)),
    ("cost7",    dict(thr=2.0, lookback=15, turnover=100000.0, cost=0.00070, notional=6400.0, eod="1850", stops=None)),
    ("look30",   dict(thr=2.0, lookback=30, turnover=100000.0, cost=0.00070, notional=6400.0, eod="1850", stops=None)),
    ("stops6",   dict(thr=2.0, lookback=30, turnover=100000.0, cost=0.00070, notional=6400.0, eod="1850", stops=(6.0, 24.0))),
    ("live4k",   dict(thr=2.0, lookback=30, turnover=100000.0, cost=0.00070, notional=4000.0, eod="1850", stops=(6.0, 24.0))),
]


def by_day(trades):
    bd = defaultdict(float)
    for t in trades:
        bd[t[0].astimezone(MSK).date().isoformat()] += t[4]
    return bd


def main():
    t0 = time.time()
    pkl = os.path.join(HERE, "data", "q2optuna_data.pkl")
    with open(pkl, "rb") as fh:
        raw = pickle.load(fh)
    per = build(raw)
    print("figis: %d | build %.1fs" % (len(per), time.time() - t0))
    hb = defaultdict(int)
    for d in per.values():
        for t in d["ts"]:
            h = t.astimezone(MSK).hour
            if 10 <= h < 19:
                hb["day_10-19"] += 1
            elif h >= 19 or h < 6:
                hb["evening_19-24"] += 1
            else:
                hb["morning_06-10"] += 1
    print("bar coverage (MSK hours):", dict(hb))

    results = {}
    for name, vp in VARIANTS:
        tr = []
        for f, d in per.items():
            tr += simulate(d, **vp)
        results[name] = tr

    hdr = "%-9s %5s %9s" % ("variant", "n", "net") + "".join(" %10s" % dd[5:] for dd in DAYS)
    print()
    print(hdr)
    print("-" * len(hdr))
    for name, _vp in VARIANTS:
        tr = results[name]
        bd = by_day(tr)
        row = "%-9s %5d %+9.2f" % (name, len(tr), sum(t[4] for t in tr))
        for dd in DAYS:
            row += " %+10.2f" % bd.get(dd, 0.0)
        print(row)
    print("%-9s %5s %+9.2f" % ("LIVE", "?", sum(LIVE_BY_DAY.values())) +
          "".join(" %+10.2f" % LIVE_BY_DAY.get(dd, 0.0) for dd in DAYS) +
          "  (14-16.09, 16th partial)")

    print()
    bd = by_day(results["ref"])
    ok = all(abs(bd.get(k, 0.0) - v) < 1.0 for k, v in EXPECTED_REF.items())
    print("SELF-CHECK vs results_optuna_params.json ref/all/by_day:", "MATCH" if ok else "DIFF!")
    for k in DAYS:
        print("  %s: sim=%+9.2f expected=%+9.2f" % (k, bd.get(k, 0.0), EXPECTED_REF[k]))

    print()
    for name in ("ref", "stops6"):
        ra = defaultdict(lambda: [0, 0.0])
        for t in results[name]:
            ra[t[3]][0] += 1
            ra[t[3]][1] += t[4]
        print("reasons[%s]:" % name, {k: (v[0], round(v[1], 1)) for k, v in sorted(ra.items())})

    for name in ("ref", "stops6"):
        fn = os.path.join(HERE, "parity_%s_trades.csv" % name)
        with open(fn, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ticker", "entry_msk", "exit_msk", "side",
                        "entry_px", "exit_px", "qty", "reason", "net"])
            for t in sorted(results[name], key=lambda x: x[0]):
                w.writerow([t[5],
                            t[0].astimezone(MSK).strftime("%Y-%m-%d %H:%M"),
                            t[1].astimezone(MSK).strftime("%Y-%m-%d %H:%M"),
                            "L" if t[2] == 1 else "S",
                            round(t[6], 4), round(t[7], 4), t[8], t[3], round(t[4], 2)])
        print("written:", fn)

    for name in ("ref", "stops6"):
        print("--- trades[%s] ---" % name)
        for t in sorted(results[name], key=lambda x: x[0]):
            print("TR|%s|%s|%s|%s|%.2f|%.2f|%d|%s|%+.2f" % (
                t[5], t[0].astimezone(MSK).strftime("%d.%m %H:%M"),
                t[1].astimezone(MSK).strftime("%d.%m %H:%M"),
                "L" if t[2] == 1 else "S", t[6], t[7], t[8], t[3], t[4]))
    print("done in %.1fs" % (time.time() - t0))


if __name__ == "__main__":
    main()
