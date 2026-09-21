#!/usr/bin/env python3
"""blind_test.py - Stage 16/A: BLIND / OUT-OF-SAMPLE test: REF vs JOINT.

Protocol (pre-registered; NO optimization, NO parameter changes):
  * data: 1m bars 25.08-12.09 MSK; 25-31.08 = indicator/vote warmup;
    ENTRIES allowed only on 2026-09-01..11 MSK - these 11 days were never
    used in any stage of the optimization pipeline (12-19.09 was);
  * universe: the same 26 figis as every stage (keys of blind_data.pkl);
  * REF   = q2c quorum-2 @ champion params, thr=2.0, opp=0,
            exit RSI(14) 1m 70/30, cd=15;
  * JOINT = stage-03 winner params, thr=1.2096, same exit/opp/cd;
  * env parity with ALL previous sims: day session 9:50-19:00 MSK entries,
    bar turnover >= 50k RUR, ~6400 RUR/position, cost 0.045%/side,
    close at MSK date change, votes = 10m-bar signals in a 15m window;
  * decision rule (fixed in advance): the candidate is adopted ONLY if its
    blind net beats REF; any degradation on untouched data kills it.

In-sample reference (results_optuna_params.json):
  REF   train +2744.97 / val +1583.57
  JOINT train +3477.42 / val +1779.52  (val delta +195.95)

Run on worker .2:  cd q2opt & python -u blind_test.py
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

MSK = timezone(timedelta(hours=3))
COST = 0.00045
NOTIONAL = 6400.0
TURNOVER = 50000.0
LOOKBACK_MIN = 15
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
OPP = 0.0
OB_THR = 70.0
OS_THR = 30.0
CD = 15
BLIND_FIRST = date(2026, 9, 1)
BLIND_LAST = date(2026, 9, 11)

REF_SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("vwap_reclaim", {"k": 2.0}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
]
JOINT_SETUPS = [
    ("rsi_reversal", {"period": 17, "oversold": 38.8, "overbought": 65.3}),
    ("bollinger_reclaim", {"period": 25, "k": 1.01}),
    ("vwap_reclaim", {"k": 2.67}),
    ("macd_cross", {"fast": 9, "slow": 29, "signal_period": 12}),
    ("donchian_breakout", {"period": 81}),
    ("volume_drop", {"ma_len": 31, "drop_ratio": 1.15}),
]
JOINT_THR = 1.2095819693493357
SIDS = [s for s, _ in REF_SETUPS]


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
    """[(ts, buy_set, sell_set)] of one config on 10m bars (champion parity)."""
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
    """(nbuy, nsell) per 1m bar = how many setups voted in the window (sets)."""
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


def build(raw):
    per = {}
    ev_stats = {"REF": defaultdict(int), "JOINT": defaultdict(int)}
    for f in sorted(raw):
        rows = raw[f]["candles"]
        if len(rows) < 300:
            continue
        candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]),
                          low=float(x[3]), close=float(x[4]),
                          volume=int(x[5] or 0)) for x in rows]
        ts = [c.ts for c in candles]
        cl = [float(c.close) for c in candles]
        vl = [float(c.volume or 0) for c in candles]
        bars10 = resample(candles, 600)
        votes = {}
        for tag, setups in (("REF", REF_SETUPS), ("JOINT", JOINT_SETUPS)):
            ev_list = events_for(setups, bars10)
            nb, ns = counts_for(ts, ev_list, LOOKBACK_MIN * 60)
            votes[tag] = (nb, ns)
            for t, b, s in ev_list:
                if BLIND_FIRST <= t.astimezone(MSK).date() <= BLIND_LAST:
                    for sid in b:
                        ev_stats[tag][sid + ":B"] += 1
                    for sid in s:
                        ev_stats[tag][sid + ":S"] += 1
        day = []
        hm = []
        blind = []
        for t in ts:
            tm = t.astimezone(MSK)
            d0 = tm.date()
            day.append(d0.toordinal())
            hm.append(tm.hour * 60 + tm.minute)
            blind.append(BLIND_FIRST <= d0 <= BLIND_LAST)
        per[f] = dict(
            ticker=str(raw[f].get("ticker") or f),
            ts=ts, cl=cl, vl=vl,
            r=[float(x) for x in rsi_wilder(cl, 14)],
            day=day, hm=hm, blind=blind, votes=votes,
        )
    return per, ev_stats


def simulate(d, nbuy, nsell, thr):
    """Trades of one figi (env parity: turnover/session/EOD/cost/cd/exit)."""
    ts = d["ts"]
    cl = d["cl"]
    vl = d["vl"]
    r = d["r"]
    day = d["day"]
    hm = d["hm"]
    blind = d["blind"]
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
                        sgn * (px - ep) * eq - COST * (ep + px) * eq, tk))
            pos = 0
            last_exit = i - 1
        if i < 60:
            continue
        if pos == 0:
            if not blind[i]:
                continue
            if i - last_exit <= CD:
                continue
            if cl[i] * vl[i] < TURNOVER:
                continue
            h = hm[i]
            if not (DAY_START <= h < DAY_END):
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
            eq = max(1, round(NOTIONAL / ep))
            pos = 1
            entry_i = i
        else:
            if (sgn == 1 and r[i] > OB_THR) or (sgn == -1 and r[i] < OS_THR):
                px = cl[i]
                out.append((ts[entry_i], ts[i], sgn, "exit",
                            sgn * (px - ep) * eq - COST * (ep + px) * eq, tk))
                pos = 0
                last_exit = i
    if pos:
        px = cl[-1]
        out.append((ts[entry_i], ts[-1], sgn, "day_end",
                    sgn * (px - ep) * eq - COST * (ep + px) * eq, tk))
    return out


def agg(trades, days_iso):
    n = len(trades)
    wins = [t for t in trades if t[4] > 0]
    losses = [t for t in trades if t[4] <= 0]
    net = sum(t[4] for t in trades)
    gp = sum(t[4] for t in wins)
    gl = sum(t[4] for t in losses)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t[0].astimezone(MSK).date().isoformat()] += t[4]
    daily = [by_day.get(dd, 0.0) for dd in days_iso]
    dstd = statistics.stdev(daily) if len(daily) > 1 else 0.0
    sharpe = (statistics.mean(daily) / dstd) if dstd > 0 else 0.0
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for t in sorted(trades, key=lambda x: x[0]):
        cum += t[4]
        if cum > peak:
            peak = cum
        if peak - cum > mdd:
            mdd = peak - cum
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
    ls = defaultdict(lambda: [0, 0.0])
    tk = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = "L" if t[2] == 1 else "S"
        ls[k][0] += 1
        ls[k][1] += t[4]
        tk[t[5]][0] += 1
        tk[t[5]][1] += t[4]
    return dict(
        n=n, wins=len(wins), wr=round(100.0 * len(wins) / max(n, 1), 1),
        net=round(net, 2),
        avg=round(net / n, 2) if n else 0.0,
        avg_win=round(gp / len(wins), 2) if wins else 0.0,
        avg_loss=round(gl / len(losses), 2) if losses else 0.0,
        pf=round(gp / abs(gl), 2) if gl < 0 else None,
        max_dd=round(mdd, 2), max_conc=mx,
        daily_sharpe=round(sharpe, 2), day_std=round(dstd, 2),
        by_day={k: round(v, 2) for k, v in by_day.items()},
        L=[ls["L"][0], round(ls["L"][1], 2)],
        S=[ls["S"][0], round(ls["S"][1], 2)],
        by_tk={k: [v[0], round(v[1], 2)] for k, v in tk.items()},
        _tr=[{"in": t[0].isoformat(), "out": t[1].isoformat(),
              "side": t[2], "why": t[3], "pnl": round(t[4], 2), "tk": t[5]}
             for t in trades],
    )


def fmt(m):
    return ("n=%4d wr=%5.1f%% net=%+9.2f avg=%+7.2f | PF %s | MaxDD %8.2f | "
            "daySharpe %5.2f | maxConc %2d | L %3d/%+8.2f S %3d/%+8.2f" % (
                m["n"], m["wr"], m["net"], m["avg"],
                ("%4.2f" % m["pf"]) if m["pf"] else "  --",
                m["max_dd"], m["daily_sharpe"], m["max_conc"],
                m["L"][0], m["L"][1], m["S"][0], m["S"][1]))


def clean(m):
    return {k: v for k, v in m.items() if k != "_tr"}


def main():
    t0 = time.perf_counter()
    data_path = os.environ.get("BLIND_DATA") or os.path.join(
        HERE, "data", "blind_data.pkl")
    if not os.path.exists(data_path):
        print("DATA NOT FOUND: %s" % data_path)
        sys.exit(1)
    with open(data_path, "rb") as fh:
        raw = pickle.load(fh)
    per, ev_stats = build(raw)
    if not per:
        print("no data")
        sys.exit(1)
    blind_days = sorted({t.astimezone(MSK).date() for d in per.values()
                         for t in d["ts"]
                         if BLIND_FIRST <= t.astimezone(MSK).date() <= BLIND_LAST})
    days_iso = [d.isoformat() for d in blind_days]
    n_bars = sum(len(d["ts"]) for d in per.values())

    print("=== STAGE 16/A: BLIND TEST 2026-09-01..11 MSK (REF vs JOINT) ===")
    print("protocol: no optimization, no parameter changes, identical execution")
    print("data: figis=%d | 1m bars=%d | %s -> %s MSK | prep %.1fs" % (
        len(per), n_bars,
        min(d["ts"][0] for d in per.values()).astimezone(MSK).strftime("%d.%m"),
        max(d["ts"][-1] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        time.perf_counter() - t0))
    print("blind days: %d (%s .. %s)" % (
        len(days_iso), days_iso[0] if days_iso else "-",
        days_iso[-1] if days_iso else "-"))

    print()
    print("--- sanity: 10m-signal counts per setup INSIDE the blind window ---")
    for tag in ("REF", "JOINT"):
        row = []
        for sid in SIDS:
            row.append("%s B%d/S%d" % (sid[:6], ev_stats[tag].get(sid + ":B", 0),
                                       ev_stats[tag].get(sid + ":S", 0)))
        print("  %-5s: %s" % (tag, " | ".join(row)))

    ref_trades = []
    joint_trades = []
    for f, d in per.items():
        nb, ns = d["votes"]["REF"]
        ref_trades += simulate(d, nb, ns, 2.0)
        nb, ns = d["votes"]["JOINT"]
        joint_trades += simulate(d, nb, ns, JOINT_THR)
    ref = agg(ref_trades, days_iso)
    jnt = agg(joint_trades, days_iso)

    print()
    print("=== RESULTS (entries 01-11.09 MSK only; identical env) ===")
    print("  REF  : %s" % fmt(ref))
    print("  JOINT: %s" % fmt(jnt))
    print()
    print("  in-sample reference: REF val +1583.57 | JOINT val +1779.52 (delta +195.95)")
    print("  BLIND: REF %+.2f | JOINT %+.2f | delta %+.2f" % (
        ref["net"], jnt["net"], jnt["net"] - ref["net"]))

    print()
    print("  day         REF net   JOINT net     delta")
    better = worse = same = 0
    for dd in days_iso:
        r0 = ref["by_day"].get(dd, 0.0)
        j0 = jnt["by_day"].get(dd, 0.0)
        dl = j0 - r0
        if dl > 0.005:
            better += 1
        elif dl < -0.005:
            worse += 1
        else:
            same += 1
        print("  %s %+9.2f %+11.2f %+9.2f" % (dd, r0, j0, dl))
    print("  days JOINT better/worse/equal: %d/%d/%d" % (better, worse, same))

    print()
    print("  REF  worst tickers: %s" % sorted(
        ref["by_tk"].items(), key=lambda kv: kv[1][1])[:5])
    print("  REF  best  tickers: %s" % sorted(
        ref["by_tk"].items(), key=lambda kv: -kv[1][1])[:5])
    print("  JOIN worst tickers: %s" % sorted(
        jnt["by_tk"].items(), key=lambda kv: kv[1][1])[:5])
    print("  JOIN best  tickers: %s" % sorted(
        jnt["by_tk"].items(), key=lambda kv: -kv[1][1])[:5])

    adopted = jnt["net"] > ref["net"]
    verdict = ("JOINT ADOPTED (beats REF on untouched data)" if adopted
               else "REF STANDS (candidate degraded on untouched data)")
    print()
    print("=== VERDICT (pre-registered rule: adopt only if blind net > REF) ===")
    print("  %s" % verdict)

    results = {
        "when": datetime.now().isoformat(),
        "protocol": "blind 01-11.09 MSK, no optimization, identical execution",
        "blind_days": days_iso,
        "ref": clean(ref),
        "joint": clean(jnt),
        "delta": round(jnt["net"] - ref["net"], 2),
        "days_better_worse_equal": [better, worse, same],
        "adopted": adopted,
        "verdict": verdict,
        "in_sample_reference": {
            "ref": {"train": 2744.97, "val": 1583.57},
            "joint": {"train": 3477.42, "val": 1779.52},
        },
        "trades_ref": ref["_tr"],
        "trades_joint": jnt["_tr"],
    }
    out_path = os.path.join(HERE, "results_blind_test.json")
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, ensure_ascii=False, indent=1, default=str)
        print("results saved: %s" % out_path)
    except Exception as e:
        print("results save FAILED: %s %s" % (type(e).__name__, str(e)[:80]))
    print()
    print("wall %.0fs | BLIND_TEST_DONE" % (time.perf_counter() - t0))


if __name__ == "__main__":
    main()
