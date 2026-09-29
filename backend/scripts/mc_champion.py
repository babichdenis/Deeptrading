#!/usr/bin/env python3
"""mc_champion.py - Stage 15: MONTE-CARLO risk of the REF champion.

Context (stage 16/A verdict: REF STANDS):
  blind 01-11.09 MSK (untouched days): REF +3961.84 (n=487, wr 68.6%,
  PF 2.03, MaxDD 451) vs JOINT +3539.75 -> champion stays REF.

Sample: the champion's BLIND trades (results_blind_test.json -> "trades_ref")
- honest out-of-sample: those days were never used in any optimization.

Protocol (pre-registered):
  A) iid TRADE bootstrap - resample trades with replacement
  B) DAY-BLOCK bootstrap - resample trading days with replacement and
     concatenate each day's trades in original order (preserves intra-day
     clustering / correlation of trades)
  horizons: 9 days (= sample length), 21 days (month), 63 days (quarter);
     method A scales trade count proportionally (n_trades/days * horizon)
  10,000 paths per (method, horizon); fixed seed; no compounding
     (fixed ~6400 RUR/position); MaxDD on the trade-level equity curve.

Metrics per path: net, MaxDD, WR. Across paths: p05/p25/p50/p75/p95
quantiles, P(net < 0), P(MaxDD > 1000 / 2000).

Output: console + results_mc_champion.json (next to the script).
Run on worker .8 (Windows, py 3.12): pure stdlib, no app imports.
"""

import json
import os
import random
import statistics
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "results_blind_test.json")
OUT = os.path.join(HERE, "results_mc_champion.json")

MSK = timedelta(hours=3)
SEED = 20260921
N_PATHS = 10_000
HORIZONS = [9, 21, 63]


def load_trades():
    """trades_ref -> [(day_msk, ts, pnl)], sorted chronologically."""
    with open(SRC, encoding="utf-8") as fh:
        d = json.load(fh)
    tr = d.get("trades_ref") or []
    out = []
    for t in tr:
        raw = str(t.get("in", ""))
        ts = None
        try:
            ts = datetime.fromisoformat(raw.replace(" ", "T"))
        except ValueError:
            ts = None
        day = (ts + MSK).date().isoformat() if ts is not None else raw[:10]
        out.append((day, ts, float(t["pnl"])))
    out.sort(key=lambda x: (x[0], x[1] is None, x[1]))
    return out, d


def maxdd(pnls):
    eq = peak = 0.0
    dd = 0.0
    for p in pnls:
        eq += p
        if eq > peak:
            peak = eq
        d = peak - eq
        if d > dd:
            dd = d
    return dd


def path_metrics(pnls):
    n = len(pnls)
    return {
        "net": sum(pnls),
        "maxdd": maxdd(pnls),
        "wr": (sum(1 for p in pnls if p > 0) / n) if n else 0.0,
        "n": n,
    }


def summarize(ms):
    nets = sorted(m["net"] for m in ms)
    dds = sorted(m["maxdd"] for m in ms)

    def q(vs, qq):
        return vs[min(len(vs) - 1, int(round(qq * (len(vs) - 1))))]

    return {
        "paths": len(ms),
        "n_trades_mean": statistics.fmean(m["n"] for m in ms),
        "net": {"p05": q(nets, .05), "p25": q(nets, .25), "p50": q(nets, .50),
                "p75": q(nets, .75), "p95": q(nets, .95),
                "mean": statistics.fmean(nets)},
        "maxdd": {"p05": q(dds, .05), "p25": q(dds, .25), "p50": q(dds, .50),
                  "p75": q(dds, .75), "p95": q(dds, .95),
                  "mean": statistics.fmean(dds)},
        "wr_mean": statistics.fmean(m["wr"] for m in ms),
        "p_loss": sum(1 for x in nets if x < 0) / len(nets),
        "p_dd_gt_1000": sum(1 for x in dds if x > 1000.0) / len(dds),
        "p_dd_gt_2000": sum(1 for x in dds if x > 2000.0) / len(dds),
    }


def main():
    trades, d = load_trades()
    if not trades:
        raise SystemExit("FATAL: no trades_ref in results_blind_test.json")
    ref = d.get("ref") or {}

    days = {}
    for day, _ts, pnl in trades:
        days.setdefault(day, []).append(pnl)
    day_names = sorted(days)
    pnls_all = [p for _d, _t, p in trades]
    base = path_metrics(pnls_all)
    per_day_mean = len(trades) / len(day_names)

    print("=== STAGE 15: MONTE-CARLO of REF champion (blind trades 01-11.09 MSK) ===")
    print("verdict 16/A:", d.get("verdict"))
    refk = {k: ref.get(k) for k in ("net", "n", "wr", "pf", "maxdd")}
    print("ref(blind, saved):", refk)
    print("sample: trades=%d days=%d (mean %.1f trades/day)" % (
        len(trades), len(day_names), per_day_mean))
    for dn in day_names:
        print("  %s: n=%3d net=%+9.2f" % (dn, len(days[dn]), sum(days[dn])))
    print("recomputed: net=%+.2f n=%d wr=%.1f%% maxdd=%.2f" % (
        base["net"], base["n"], base["wr"] * 100, base["maxdd"]))

    results = {
        "when": datetime.now().isoformat(timespec="seconds"),
        "stage": "15 monte-carlo champion REF (blind trades 01-11.09 MSK)",
        "verdict_16A": d.get("verdict"),
        "sample": {
            "trades": len(trades),
            "days": len(day_names),
            "day_counts": {dn: len(days[dn]) for dn in day_names},
            "day_nets": {dn: sum(days[dn]) for dn in day_names},
            "net": base["net"], "maxdd": base["maxdd"], "wr": base["wr"],
        },
        "protocol": {
            "paths": N_PATHS, "seed": SEED, "horizons_days": HORIZONS,
            "methods": ["iid_trade", "day_block"],
            "note": ("fixed sizing ~6400 RUR/pos, no compounding; "
                     "MaxDD on trade-level equity; iid_trade scales "
                     "trade count = per_day_mean * horizon"),
        },
        "mc": {},
    }

    rng = random.Random(SEED)
    for method in ("iid_trade", "day_block"):
        for h in HORIZONS:
            ms = []
            for _ in range(N_PATHS):
                if method == "iid_trade":
                    k = int(round(per_day_mean * h))
                    pnls = [pnls_all[rng.randrange(len(pnls_all))]
                            for _ in range(k)]
                else:
                    pnls = []
                    for _ in range(h):
                        dn = day_names[rng.randrange(len(day_names))]
                        pnls.extend(days[dn])
                ms.append(path_metrics(pnls))
            s = summarize(ms)
            results["mc"]["%s_%dd" % (method, h)] = s
            print("\n--- %s | horizon %d days ---" % (method, h))
            print("  net  : p05 %+9.1f | p25 %+9.1f | p50 %+9.1f | p75 %+9.1f | p95 %+9.1f | mean %+9.1f" % (
                s["net"]["p05"], s["net"]["p25"], s["net"]["p50"],
                s["net"]["p75"], s["net"]["p95"], s["net"]["mean"]))
            print("  maxdd: p05 %8.1f | p25 %8.1f | p50 %8.1f | p75 %8.1f | p95 %8.1f" % (
                s["maxdd"]["p05"], s["maxdd"]["p25"], s["maxdd"]["p50"],
                s["maxdd"]["p75"], s["maxdd"]["p95"]))
            print("  P(net<0)=%.1f%%  P(DD>1000)=%.1f%%  P(DD>2000)=%.1f%%  wr_mean=%.1f%%  n_mean=%.0f" % (
                s["p_loss"] * 100, s["p_dd_gt_1000"] * 100,
                s["p_dd_gt_2000"] * 100, s["wr_mean"] * 100, s["n_trades_mean"]))

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, ensure_ascii=False, indent=1)
    print("\nsaved: %s" % OUT)
    print("MC_DONE")


if __name__ == "__main__":
    main()
