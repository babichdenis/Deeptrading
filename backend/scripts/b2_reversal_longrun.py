#!/usr/bin/env python3
"""Longrun mean-reversion screen over full m1 history (2024-01..2026-10-08).

Pre-registered in chat.md [PRE-REG longrun-reversal] 2026-10-14:
  signal on close[t]: retW = close[t]/close[t-W] - 1 < thr  (LONG only)
  entry fill  = open[t+1] * (1 + slip)
  exit  fill  = close[t+1+H] * (1 - slip), same trading day, exit bar <= 18:44 MSK
  fees 5 bps per side (primary, 5/2) and 10 bps per side (stress, 10/5)
  net = (exit_fill - entry_fill - fees) / open[t+1]

Liveness (rule class, not single best point):
  dev 2024-01..2025-12: mean_net_5/2 > 0, n_days >= 100, t_days >= 2
  check 2026-01..2026-07: mean_net_5/2 > 0 AND mean_net_10/5 > 0
  burned 2026-08..2026-10-08: reported separately, not in verdict.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
PG_USER = os.environ.get("PGUSER", "deeptrading")
PG_NAME = os.environ.get("PGDATABASE", "deeptrading")
PG_PASSWORD = os.environ.get("PGPASSWORD", "deeptrading")

WINDOWS = (30, 60, 120)
THRESHOLDS = (-0.005, -0.0075, -0.01, -0.015, -0.02, -0.03)
HORIZONS = (60, 120)
CUTOFF_MIN = 18 * 60 + 44

PERIODS = {
    "dev_2024_2025": ("2024-01-01", "2025-12-31"),
    "check_2026h1": ("2026-01-01", "2026-07-31"),
    "burned_2026q3": ("2026-08-01", "2026-10-08"),
}


def _load_universe() -> list[dict]:
    sql = (
        "SELECT c.figi, i.ticker, count(*) AS n, min(c.ts)::date AS d0, max(c.ts)::date AS d1 "
        "FROM candles c JOIN instruments i ON i.figi=c.figi "
        "WHERE c.interval=1 GROUP BY c.figi, i.ticker "
        "HAVING count(*) >= 300000 AND min(c.ts) <= '2024-03-01' "
        "ORDER BY count(*) DESC"
    )
    env = dict(os.environ, PGPASSWORD=PG_PASSWORD)
    out = subprocess.run(
        ["/usr/local/bin/psql", "-h", PG_HOST, "-U", PG_USER, "-d", PG_NAME, "-At", "-F", "|", "-c", sql],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    rows = []
    for line in out.strip().splitlines():
        figi, ticker, n, d0, d1 = line.split("|")
        rows.append({"figi": figi, "ticker": ticker, "n": int(n), "d0": d0, "d1": d1})
    return rows


def _load_ticker(figi: str, full: bool = False) -> pd.DataFrame:
    cols = "ts, open, close" if not full else "ts, open, high, low, close, volume"
    sql = f"SELECT {cols} FROM candles WHERE interval=1 AND figi='{figi}' ORDER BY ts"
    env = dict(os.environ, PGPASSWORD=PG_PASSWORD)
    out = subprocess.run(
        ["/usr/local/bin/psql", "-h", PG_HOST, "-U", PG_USER, "-d", PG_NAME, "-At", "-F", ",", "-c", sql],
        capture_output=True, text=True, env=env, check=True,
    ).stdout
    from io import StringIO
    names = cols.split(", ")
    df = pd.read_csv(StringIO(out), names=names, parse_dates=["ts"])
    return df.dropna(subset=["open", "close"]).reset_index(drop=True)


def net_from(entry_open: np.ndarray, exit_close: np.ndarray, slip: float, fee: float) -> np.ndarray:
    entry_fill = entry_open * (1.0 + slip)
    exit_fill = exit_close * (1.0 - slip)
    fees = fee * entry_fill + fee * exit_fill
    return (exit_fill - entry_fill - fees) / entry_open


def screen_ticker(df: pd.DataFrame) -> dict:
    ts = df["ts"].to_numpy()
    open_arr = df["open"].to_numpy(dtype=np.float64)
    close_arr = df["close"].to_numpy(dtype=np.float64)
    day = df["ts"].dt.date.to_numpy()
    minutes = (df["ts"].dt.hour * 60 + df["ts"].dt.minute).to_numpy()
    n = len(df)
    close_s = pd.Series(close_arr)
    rets = {w: (close_s / close_s.shift(w) - 1.0).to_numpy() for w in WINDOWS}
    day_int = (
        (df["ts"].dt.tz_localize(None).dt.normalize() - pd.Timestamp("1970-01-01")).dt.days.to_numpy()
    )

    results: dict[tuple, dict] = {}
    for h in HORIZONS:
        m = n - 1 - h
        if m <= 100:
            continue
        entry_open = open_arr[1 : m + 1]
        exit_close = close_arr[1 + h : 1 + h + m]
        sig_idx = np.arange(0, m)
        same_day = day[1 + h : 1 + h + m] == day[sig_idx]
        ok_exit = minutes[1 + h : 1 + h + m] <= CUTOFF_MIN
        base_ok = same_day & ok_exit
        sig_day = day_int[sig_idx]
        net5 = net_from(entry_open, exit_close, 2e-4, 5e-4)
        net10 = net_from(entry_open, exit_close, 5e-4, 10e-4)
        split_flag = np.zeros(n, dtype=bool)
        ret1 = close_arr[1:] / close_arr[:-1] - 1.0
        split_flag[1:] = np.abs(ret1) > 0.30
        split_cum = np.cumsum(split_flag)
        win_start = sig_idx
        win_end = sig_idx + 1 + h
        no_split = (split_cum[win_end] - split_cum[win_start]) == 0
        for w in WINDOWS:
            ret = rets[w][sig_idx]
            for thr in THRESHOLDS:
                mask = base_ok & no_split & (ret < thr) & (np.abs(net5) <= 0.25)
                n_split_excl = int((base_ok & (ret < thr) & ~no_split).sum())
                n_anom_excl = int((base_ok & no_split & (ret < thr) & (np.abs(net5) > 0.25)).sum())
                cnt = int(mask.sum())
                if cnt == 0:
                    continue
                key = (w, thr, h)
                acc = results.setdefault(
                    key, {"day_sum5": defaultdict(float), "day_sum10": defaultdict(float),
                          "day_n": defaultdict(int), "n": 0, "n_split_excl": 0, "n_anom_excl": 0,
                          "tick_sum5": 0.0, "tick_sum10": 0.0}
                )
                acc["n_split_excl"] += n_split_excl
                acc["n_anom_excl"] += n_anom_excl
                dsel = sig_day[mask]
                codes, inv = np.unique(dsel, return_inverse=True)
                s5 = np.bincount(inv, weights=net5[mask])
                s10 = np.bincount(inv, weights=net10[mask])
                nn = np.bincount(inv)
                for d, v5_, v10_, c_ in zip(codes.tolist(), s5.tolist(), s10.tolist(), nn.tolist()):
                    acc["day_sum5"][d] += v5_
                    acc["day_sum10"][d] += v10_
                    acc["day_n"][d] += c_
                acc["n"] += cnt
                acc["tick_sum5"] += float(net5[mask].sum())
                acc["tick_sum10"] += float(net10[mask].sum())
    return results


def _t_stat(daily: np.ndarray) -> float:
    if len(daily) < 2 or daily.std(ddof=1) == 0:
        return 0.0
    return float(daily.mean() / (daily.std(ddof=1) / np.sqrt(len(daily))))


def _block_bootstrap_ci(daily: np.ndarray, block: int = 5, n_boot: int = 2000, seed: int = 42) -> tuple[float, float]:
    rng = np.random.default_rng(seed)
    n = len(daily)
    if n < block:
        return (float("nan"), float("nan"))
    n_blocks = int(np.ceil(n / block))
    means = np.empty(n_boot)
    for b in range(n_boot):
        starts = rng.integers(0, n - block + 1, size=n_blocks)
        sample = np.concatenate([daily[s : s + block] for s in starts])[:n]
        means[b] = sample.mean()
    return (float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5)))


def period_slice(day_int: np.ndarray, values: np.ndarray, counts: np.ndarray, key: str) -> np.ndarray:
    lo, hi = PERIODS[key]
    lo_ts = np.datetime64(lo)
    hi_ts = np.datetime64(hi) + np.timedelta64(1, "D")
    lo_i = int((lo_ts - np.datetime64("1970-01-01")) / np.timedelta64(1, "D"))
    hi_i = int((hi_ts - np.datetime64("1970-01-01")) / np.timedelta64(1, "D"))
    m = (day_int >= lo_i) & (day_int < hi_i)
    return values[m], counts[m]


def run(out_dir: Path) -> dict:
    universe = _load_universe()
    print(f"universe: {len(universe)} tickers")
    agg: dict[tuple, dict] = {}
    for row in universe:
        df = _load_ticker(row["figi"])
        res = screen_ticker(df)
        for key, acc in res.items():
            a = agg.setdefault(
                key, {"day_sum5": defaultdict(float), "day_sum10": defaultdict(float),
                      "day_n": defaultdict(int), "n": 0, "n_split_excl": 0, "n_anom_excl": 0,
                      "tick_sum5": defaultdict(float), "tick_sum10": defaultdict(float)}
            )
            a["n_split_excl"] += acc.get("n_split_excl", 0)
            a["n_anom_excl"] += acc.get("n_anom_excl", 0)
            for d, v in acc["day_sum5"].items():
                a["day_sum5"][d] += v
            for d, v in acc["day_sum10"].items():
                a["day_sum10"][d] += v
            for d, v in acc["day_n"].items():
                a["day_n"][d] += v
            a["n"] += acc["n"]
            a["tick_sum5"][row["ticker"]] += acc["tick_sum5"]
            a["tick_sum10"][row["ticker"]] += acc["tick_sum10"]
        print(f"  {row['ticker']}: {row['n']} bars, combos={len(res)}")

    rows = []
    living = []
    for (w, thr, h), a in sorted(agg.items()):
        days = np.array(sorted(a["day_n"].keys()), dtype=np.int64)
        n5 = np.array([a["day_sum5"][d] for d in days])
        n10 = np.array([a["day_sum10"][d] for d in days])
        cn = np.array([a["day_n"][d] for d in days])
        rec: dict = {"W": w, "thr": thr, "H": h, "n_events": a["n"], "n_days": len(days),
                     "n_split_excl": a.get("n_split_excl", 0), "n_anom_excl": a.get("n_anom_excl", 0)}
        verdicts = {}
        for period in PERIODS:
            v5, c5 = period_slice(days, n5, cn, period)
            v10, _ = period_slice(days, n10, cn, period)
            tot_n = int(c5.sum())
            rec[f"{period}_n"] = tot_n
            rec[f"{period}_n_days"] = int(len(c5))
            rec[f"{period}_mean5"] = float(v5.mean()) if len(v5) else float("nan")
            rec[f"{period}_mean10"] = float(v10.mean()) if len(v10) else float("nan")
            rec[f"{period}_per_event5"] = float(v5.sum() / tot_n) if tot_n else float("nan")
            rec[f"{period}_per_event10"] = float(v10.sum() / tot_n) if tot_n else float("nan")
            rec[f"{period}_t5"] = _t_stat(v5)
            if period == "dev_2024_2025":
                lo, hi = _block_bootstrap_ci(v5)
                rec[f"{period}_ci5"] = [lo, hi]
            verdicts[period] = {
                "mean5": rec[f"{period}_mean5"],
                "mean10": rec[f"{period}_mean10"],
                "t5": rec[f"{period}_t5"],
                "n_days": rec[f"{period}_n_days"],
            }
        dev, chk = verdicts["dev_2024_2025"], verdicts["check_2026h1"]
        is_live = (
            dev["mean5"] > 0 and dev["n_days"] >= 100 and dev["t5"] >= 2
            and chk["mean5"] > 0 and chk["mean10"] > 0
        )
        rec["lives"] = bool(is_live)
        tick5 = a["tick_sum5"]
        top_ticker = max(tick5, key=tick5.get) if tick5 else None
        rec["top_ticker"] = top_ticker
        rec["top_ticker_share"] = float(tick5[top_ticker] / sum(v for v in tick5.values() if v > 0)) if top_ticker and sum(v for v in tick5.values() if v > 0) > 0 else None
        rows.append(rec)
        if is_live:
            living.append(rec)

    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "longrun_grid.csv", index=False)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "universe": [{"ticker": r["ticker"], "figi": r["figi"], "n": r["n"], "d0": r["d0"], "d1": r["d1"]} for r in universe],
        "grid": {"W": WINDOWS, "thr": THRESHOLDS, "H": HORIZONS,
                 "slip_primary": "2bps", "fee_primary": "5bps", "slip_stress": "5bps", "fee_stress": "10bps"},
        "n_combos": len(rows),
        "n_living": len(living),
        "living": living,
    }
    (out_dir / "longrun_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"combos={len(rows)} living={len(living)} -> {out_dir}")
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    args = p.parse_args()
    run(Path(args.out))


if __name__ == "__main__":
    main()
