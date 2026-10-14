#!/usr/bin/env python3
"""Deep-dip + confirmation screen over full m1 history.

Follow-up to [PRE-REG longrun-reversal]: the naked deep-dip edge is marginal
(dev best t~1.7). This screen asks whether simple confirmations, computed only
from data available at signal close[t], concentrate the edge to t>=2 in dev
AND keep it positive in check at both cost schemes.

Base event: ret30 = close[t]/close[t-30]-1 < thr, thr in {-0.02,-0.03}.
Confirmations (fixed grid, all honest multiple-comparison flagged):
  none, vol15 (vol_ratio>1.5), vol20 (>2.0), down3/down4 (consecutive down bars),
  rsi20/rsi25 (rsi14 oversold), lor01/lor02 (close in bottom of 60-bar range),
  vol15_down3.
Timing/costs identical to b2_reversal_longrun (entry open[t+1]+2bps slip,
exit close[t+1+H]-2bps, same-day, <=18:44, fees 5bps/side primary, 10bps stress).
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from b2_reversal_longrun import (
    _load_universe, _load_ticker, net_from, _t_stat, _block_bootstrap_ci,
    period_slice, PERIODS, CUTOFF_MIN,
)

THR = (-0.02, -0.03)
HORIZONS = (60, 120)
W = 30


def confirm_masks(df: pd.DataFrame) -> dict[str, np.ndarray]:
    n = len(df)
    c = df["close"].to_numpy(np.float64)
    hi = df["high"].to_numpy(np.float64)
    lo = df["low"].to_numpy(np.float64)
    vol = df["volume"].to_numpy(np.float64)
    out: dict[str, np.ndarray] = {"none": np.ones(n, dtype=bool)}

    vr = np.full(n, np.nan)
    if n > 21:
        vmean = pd.Series(vol).rolling(20).mean().to_numpy()
        vr = vol / vmean
    out["vol15"] = vr > 1.5
    out["vol20"] = vr > 2.0

    down = np.zeros(n, dtype=bool)
    if n > 1:
        down[1:] = c[1:] < c[:-1]
    for k, name in ((3, "down3"), (4, "down4")):
        m = np.ones(n, dtype=bool)
        for j in range(k):
            m &= np.roll(down, j)
        m[: k + 1] = False
        out[name] = m

    delta = np.zeros(n)
    delta[1:] = np.diff(c)
    gain = pd.Series(np.where(delta > 0, delta, 0.0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    loss = pd.Series(np.where(delta < 0, -delta, 0.0)).ewm(alpha=1 / 14, adjust=False).mean().to_numpy()
    rs = np.where(loss > 1e-12, gain / loss, 100.0)
    rsi = 100.0 - 100.0 / (1.0 + rs)
    out["rsi20"] = rsi < 20
    out["rsi25"] = rsi < 25

    pr = np.full(n, np.nan)
    if n > 60:
        hi60 = pd.Series(hi).rolling(60).max().to_numpy()
        lo60 = pd.Series(lo).rolling(60).min().to_numpy()
        rng = hi60 - lo60
        pr = np.where(rng > 1e-9, (c - lo60) / rng, np.nan)
    out["lor01"] = pr < 0.1
    out["lor02"] = pr < 0.2
    out["vol15_down3"] = out["vol15"] & out["down3"]
    return out


def screen_ticker(df: pd.DataFrame) -> dict:
    ts = df["ts"].to_numpy()
    open_arr = df["open"].to_numpy(np.float64)
    close_arr = df["close"].to_numpy(np.float64)
    day = df["ts"].dt.date.to_numpy()
    minutes = (df["ts"].dt.hour * 60 + df["ts"].dt.minute).to_numpy()
    day_int = (df["ts"].dt.tz_localize(None).dt.normalize() - pd.Timestamp("1970-01-01")).dt.days.to_numpy()
    n = len(df)
    close_s = pd.Series(close_arr)
    ret_w = (close_s / close_s.shift(W) - 1.0).to_numpy()
    confs = confirm_masks(df)
    split_flag = np.zeros(n, dtype=bool)
    ret1 = close_arr[1:] / close_arr[:-1] - 1.0
    split_flag[1:] = np.abs(ret1) > 0.30
    split_cum = np.cumsum(split_flag)

    results: dict[tuple, dict] = {}
    for h in HORIZONS:
        m = n - 1 - h
        if m <= 100:
            continue
        entry_open = open_arr[1 : m + 1]
        exit_close = close_arr[1 + h : 1 + h + m]
        sig_idx = np.arange(0, m)
        base_ok = (day[1 + h : 1 + h + m] == day[sig_idx]) & (minutes[1 + h : 1 + h + m] <= CUTOFF_MIN)
        no_split = (split_cum[sig_idx + 1 + h] - split_cum[sig_idx]) == 0
        net5 = net_from(entry_open, exit_close, 2e-4, 5e-4)
        net10 = net_from(entry_open, exit_close, 5e-4, 10e-4)
        sig_day = day_int[sig_idx]
        for thr in THR:
            dip = base_ok & no_split & (ret_w[sig_idx] < thr) & (np.abs(net5) <= 0.25)
            for cname, cmask in confs.items():
                full = dip & cmask[sig_idx]
                cnt = int(full.sum())
                if cnt == 0:
                    continue
                key = (thr, h, cname)
                acc = results.setdefault(
                    key, {"day_sum5": defaultdict(float), "day_sum10": defaultdict(float),
                          "day_n": defaultdict(int), "n": 0, "tick_sum5": defaultdict(float)}
                )
                dsel = sig_day[full]
                codes, inv = np.unique(dsel, return_inverse=True)
                s5 = np.bincount(inv, weights=net5[full])
                s10 = np.bincount(inv, weights=net10[full])
                nn = np.bincount(inv)
                for d, v5, v10, cc in zip(codes.tolist(), s5.tolist(), s10.tolist(), nn.tolist()):
                    acc["day_sum5"][d] += v5
                    acc["day_sum10"][d] += v10
                    acc["day_n"][d] += cc
                acc["n"] += cnt
    return results


def run(out_dir: Path) -> dict:
    universe = _load_universe()
    print(f"universe: {len(universe)} tickers", flush=True)
    agg: dict[tuple, dict] = {}
    for row in universe:
        df = _load_ticker(row["figi"], full=True)
        res = screen_ticker(df)
        for key, acc in res.items():
            a = agg.setdefault(
                key, {"day_sum5": defaultdict(float), "day_sum10": defaultdict(float),
                      "day_n": defaultdict(int), "n": 0}
            )
            for d, v in acc["day_sum5"].items():
                a["day_sum5"][d] += v
            for d, v in acc["day_sum10"].items():
                a["day_sum10"][d] += v
            for d, v in acc["day_n"].items():
                a["day_n"][d] += v
            a["n"] += acc["n"]
        print(f"  {row['ticker']}: combos={len(res)}", flush=True)

    rows = []
    for (thr, h, cname), a in sorted(agg.items()):
        days = np.array(sorted(a["day_n"].keys()), dtype=np.int64)
        n5 = np.array([a["day_sum5"][d] for d in days])
        n10 = np.array([a["day_sum10"][d] for d in days])
        cn = np.array([a["day_n"][d] for d in days])
        rec: dict = {"thr": thr, "H": h, "confirm": cname, "n_events": a["n"], "n_days": len(days)}
        for period in PERIODS:
            v5, c5 = period_slice(days, n5, cn, period)
            v10, _ = period_slice(days, n10, cn, period)
            tot = int(c5.sum())
            rec[f"{period}_n"] = tot
            rec[f"{period}_n_days"] = int(len(c5))
            rec[f"{period}_per_event5"] = float(v5.sum() / tot) if tot else float("nan")
            rec[f"{period}_per_event10"] = float(v10.sum() / tot) if tot else float("nan")
            rec[f"{period}_t5"] = _t_stat(v5)
            if period == "dev_2024_2025":
                lo, hi_ = _block_bootstrap_ci(v5)
                rec[f"{period}_ci5"] = [lo, hi_]
        rec["lives"] = bool(
            rec["dev_2024_2025_per_event5"] > 0 and rec["dev_2024_2025_n_days"] >= 100 and rec["dev_2024_2025_t5"] >= 2
            and rec["check_2026h1_per_event5"] > 0 and rec["check_2026h1_per_event10"] > 0
        )
        rows.append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "dip_confirm_grid.csv", index=False)
    living = [r for r in rows if r["lives"]]
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "base": {"W": W, "thr": THR, "H": HORIZONS},
        "confirmations": sorted({r["confirm"] for r in rows}),
        "n_combos": len(rows),
        "n_living": len(living),
        "living": living,
    }
    (out_dir / "dip_confirm_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(f"combos={len(rows)} living={len(living)} -> {out_dir}", flush=True)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    args = p.parse_args()
    run(Path(args.out))
