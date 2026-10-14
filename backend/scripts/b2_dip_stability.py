#!/usr/bin/env python3
"""Stability check for living deep-dip candidates: per-ticker and per-month breakdown."""
from __future__ import annotations

import json
import sys
import warnings
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).parent))

from b2_reversal_longrun import (  # noqa: E402
    _load_universe, _load_ticker, net_from, CUTOFF_MIN,
)
from b2_dip_confirm import confirm_masks  # noqa: E402

CANDS = ["down3", "down4", "vol20", "vol15", "lor01", "vol15_down3"]
THR = -0.03
H = 60
W = 30


def main(out_path: str) -> None:
    tick_tot: dict = defaultdict(lambda: defaultdict(float))
    tick_n: dict = defaultdict(lambda: defaultdict(int))
    month_tot: dict = defaultdict(lambda: defaultdict(float))
    month_n: dict = defaultdict(lambda: defaultdict(int))
    for row in _load_universe():
        df = _load_ticker(row["figi"], full=True)
        o = df["open"].to_numpy(np.float64)
        c = df["close"].to_numpy(np.float64)
        day = df["ts"].dt.date.to_numpy()
        minutes = (df["ts"].dt.hour * 60 + df["ts"].dt.minute).to_numpy()
        n = len(df)
        ret_w = (pd.Series(c) / pd.Series(c).shift(W) - 1.0).to_numpy()
        confs = confirm_masks(df)
        sf = np.zeros(n, bool)
        sf[1:] = np.abs(c[1:] / c[:-1] - 1) > 0.30
        sc = np.cumsum(sf)
        m = n - 1 - H
        sig = np.arange(0, m)
        base_ok = (day[1 + H:1 + H + m] == day[sig]) & (minutes[1 + H:1 + H + m] <= CUTOFF_MIN)
        no_split = (sc[sig + 1 + H] - sc[sig]) == 0
        net5 = net_from(o[1:m + 1], c[1 + H:1 + H + m], 2e-4, 5e-4)
        dip = base_ok & no_split & (ret_w[sig] < THR) & (np.abs(net5) <= 0.25)
        months = df["ts"].dt.tz_localize(None).dt.strftime("%Y-%m").to_numpy()[sig]
        for cname in CANDS:
            full = dip & confs[cname][sig]
            if not full.any():
                continue
            tick_tot[cname][row["ticker"]] += float(net5[full].sum())
            tick_n[cname][row["ticker"]] += int(full.sum())
            for mth, val in zip(months[full].tolist(), net5[full].tolist()):
                month_tot[cname][mth] += val
                month_n[cname][mth] += 1
        print(f"  {row['ticker']} done", file=sys.stderr, flush=True)

    result = {}
    for cname in CANDS:
        tt = dict(tick_tot[cname])
        tn = dict(tick_n[cname])
        mt = dict(month_tot[cname])
        mn = dict(month_n[cname])
        tot = sum(tt.values())
        n_ev = sum(tn.values())
        top = max(tt, key=tt.get) if tt else None
        mkeys = sorted(mt)
        means = [mt[k] / mn[k] for k in mkeys if mn[k]]
        result[cname] = {
            "n_events": n_ev,
            "tot_net": tot,
            "per_event_bps": tot / n_ev * 1e4 if n_ev else None,
            "tickers_pos": sum(1 for v in tt.values() if v > 0),
            "tickers_total": len(tt),
            "top_ticker": top,
            "top_ticker_share": tt[top] / tot if top and tot > 0 else None,
            "tickers": {k: {"net": v, "n": tn[k], "per_event_bps": v / tn[k] * 1e4} for k, v in sorted(tt.items(), key=lambda x: -x[1])},
            "months_pos": sum(1 for x in means if x > 0),
            "months_total": len(means),
            "monthly_bps": {k: mt[k] / mn[k] * 1e4 for k in mkeys if mn[k]},
        }
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"written -> {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
