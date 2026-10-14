#!/usr/bin/env python3
"""Independent confirmation of the 6 living dip rules on the fresh holdout.

The longrun screen (b2_dip_confirm.py) pre-selected these combos on
dev 2024-2025 and check 2026H1. That check window ends 2026-07-31; the
burned window ends 2026-10-08. Fresh candles downloaded after 2026-10-08
give a genuinely untouched holdout Oct 9..14 2026.

This script re-runs the SAME net/timing/confirmation code but slices the
aggregate by ts > 2026-10-08, i.e. only events whose signal bar is in the
fresh holdout. No rules are re-selected; we report the pre-declared 6.

Verdict for each rule on the holdout:
  holdout n, days, per_event_5/2, per_event_10/5, daily t.
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
    _load_universe, _load_ticker, net_from, _t_stat, _block_bootstrap_ci, CUTOFF_MIN,
)
from b2_dip_confirm import confirm_masks, THR, HORIZONS, W

HOLDOUT_START = "2026-10-09"
LIVING = (
    "down3", "down4", "vol15", "vol20", "lor01", "vol15_down3",
)
THR_PICK = -0.03
H_PICK = 60


def screen_holdout(df: pd.DataFrame) -> dict:
    open_arr = df["open"].to_numpy(np.float64)
    close_arr = df["close"].to_numpy(np.float64)
    day = df["ts"].dt.date.to_numpy()
    minutes = (df["ts"].dt.hour * 60 + df["ts"].dt.minute).to_numpy()
    day_int = (
        (df["ts"].dt.tz_localize(None).dt.normalize() - pd.Timestamp("1970-01-01")).dt.days.to_numpy()
    )
    n = len(df)
    close_s = pd.Series(close_arr)
    ret_w = (close_s / close_s.shift(W) - 1.0).to_numpy()
    confs = confirm_masks(df)
    split_flag = np.zeros(n, dtype=bool)
    ret1 = close_arr[1:] / close_arr[:-1] - 1.0
    split_flag[1:] = np.abs(ret1) > 0.30
    split_cum = np.cumsum(split_flag)

    results: dict[tuple, dict] = {}
    h = H_PICK
    m = n - 1 - h
    if m <= 100:
        return results
    entry_open = open_arr[1 : m + 1]
    exit_close = close_arr[1 + h : 1 + h + m]
    sig_idx = np.arange(0, m)
    base_ok = (day[1 + h : 1 + h + m] == day[sig_idx]) & (minutes[1 + h : 1 + h + m] <= CUTOFF_MIN)
    no_split = (split_cum[sig_idx + 1 + h] - split_cum[sig_idx]) == 0
    net5 = net_from(entry_open, exit_close, 2e-4, 5e-4)
    net10 = net_from(entry_open, exit_close, 5e-4, 10e-4)
    sig_day = day_int[sig_idx]
    thr = THR_PICK
    dip = base_ok & no_split & (ret_w[sig_idx] < thr) & (np.abs(net5) <= 0.25)
    for cname in LIVING:
        cmask = confs[cname]
        full = dip & cmask[sig_idx]
        cnt = int(full.sum())
        if cnt == 0:
            continue
        acc = results.setdefault(
            cname, {"day_sum5": defaultdict(float), "day_sum10": defaultdict(float),
                    "day_n": defaultdict(int), "n": 0}
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
    lo_ts = np.datetime64(HOLDOUT_START)
    lo_i = int((lo_ts - np.datetime64("1970-01-01")) / np.timedelta64(1, "D"))
    universe = _load_universe()
    print(f"universe: {len(universe)} tickers; holdout start {HOLDOUT_START}", flush=True)
    agg: dict[str, dict] = {}
    for row in universe:
        df = _load_ticker(row["figi"], full=True)
        res = screen_holdout(df)
        for cname, acc in res.items():
            a = agg.setdefault(
                cname, {"day_sum5": defaultdict(float), "day_sum10": defaultdict(float),
                        "day_n": defaultdict(int), "n": 0, "tick_sum5": defaultdict(float)}
            )
            for d, v in acc["day_sum5"].items():
                a["day_sum5"][d] += v
            for d, v in acc["day_sum10"].items():
                a["day_sum10"][d] += v
            for d, v in acc["day_n"].items():
                a["day_n"][d] += v
            a["n"] += acc["n"]
            for d, v in acc["day_sum5"].items():
                a["tick_sum5"][row["ticker"]] += v
        print(f"  {row['ticker']}: holdout combos={len(res)}", flush=True)

    rows = []
    for cname in LIVING:
        a = agg.get(cname)
        if not a:
            rows.append({"confirm": cname, "thr": THR_PICK, "H": H_PICK, "n_events": 0})
            continue
        days = np.array(sorted(a["day_n"].keys()), dtype=np.int64)
        sel = days >= lo_i
        days_h = days[sel]
        n5 = np.array([a["day_sum5"][d] for d in days_h])
        n10 = np.array([a["day_sum10"][d] for d in days_h])
        cn = np.array([a["day_n"][d] for d in days_h])
        tot = int(cn.sum())
        per_event5 = float(n5.sum() / tot) if tot else float("nan")
        per_event10 = float(n10.sum() / tot) if tot else float("nan")
        lo, hi = _block_bootstrap_ci(n5)
        tick5 = a["tick_sum5"]
        top_ticker = max(tick5, key=tick5.get) if tick5 else None
        pos = sum(1 for v in n5 if v > 0)
        rows.append({
            "confirm": cname, "thr": THR_PICK, "H": H_PICK,
            "holdout_n": tot, "holdout_n_days": int(len(days_h)),
            "holdout_per_event5": per_event5, "holdout_per_event10": per_event10,
            "holdout_t5": _t_stat(n5), "holdout_ci5": [lo, hi],
            "holdout_days_positive": int(pos),
            "top_ticker": top_ticker,
            "top_ticker_share": float(tick5[top_ticker] / sum(v for v in tick5.values() if v > 0)) if top_ticker and sum(v for v in tick5.values() if v > 0) > 0 else None,
        })

    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "holdout_confirm.csv", index=False)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "holdout_start": HOLDOUT_START,
        "rules": [{"thr": THR_PICK, "H": H_PICK, "confirm": c} for c in LIVING],
        "n_rules": len(rows),
        "results": rows,
    }
    (out_dir / "holdout_confirm_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    print(f"rules={len(rows)} -> {out_dir}", flush=True)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    args = p.parse_args()
    run(Path(args.out))
