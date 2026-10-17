#!/usr/bin/env python3
"""Session-time distribution of holdout down3 events (SFIN + event carriers).

The holdout confirm returned only 4-5 events per rule over Oct 9-14, all
SFIN-led. This checks whether events cluster at the session open (where
ret30 compares against the prior day and the first bar can fake a down-run),
which would signal an artifact rather than a real dip.
"""
import sys, warnings
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]) + "/scripts")
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd
import subprocess, os
from io import StringIO
from b2_reversal_longrun import net_from, CUTOFF_MIN
from b2_dip_confirm import confirm_masks, W

H = 60
THR = -0.03
CARRIERS = ["SFIN", "SBER", "ROSN", "NVTK", "SMLT", "T", "SNGS", "SNGSP", "TRNFP", "MVID"]


def load(figi):
    sql = f"SELECT ts, open, high, low, close, volume FROM candles WHERE interval=1 AND figi='{figi}' ORDER BY ts"
    env = dict(os.environ, PGPASSWORD="deeptrading")
    out = subprocess.run(["psql", "-h", "127.0.0.1", "-U", "deeptrading", "-d", "deeptrading",
                          "-At", "-F", ",", "-c", sql], capture_output=True, text=True, env=env, check=True).stdout
    names = ["ts", "open", "high", "low", "close", "volume"]
    df = pd.read_csv(StringIO(out), names=names, parse_dates=["ts"])
    return df.dropna(subset=["open", "close"]).reset_index(drop=True)


def figi_of(ticker):
    sql = f"SELECT figi FROM instruments WHERE ticker='{ticker}'"
    env = dict(os.environ, PGPASSWORD="deeptrading")
    return subprocess.run(["psql", "-h", "127.0.0.1", "-U", "deeptrading", "-d", "deeptrading",
                           "-At", "-c", sql], capture_output=True, text=True, env=env, check=True).stdout.strip()


def main():
    rows = []
    for t in CARRIERS:
        figi = figi_of(t)
        if not figi:
            print(f"{t}: no figi"); continue
        df = load(figi)
        df = df[df["ts"] > pd.Timestamp("2026-10-08", tz="UTC")].reset_index(drop=True)
        if len(df) < 200:
            print(f"{t}: only {len(df)} holdout bars, skip"); continue
        open_arr = df["open"].to_numpy(np.float64)
        close_arr = df["close"].to_numpy(np.float64)
        day = df["ts"].dt.date.to_numpy()
        minutes = (df["ts"].dt.hour * 60 + df["ts"].dt.minute).to_numpy()
        n = len(df)
        cs = pd.Series(close_arr)
        ret_w = (cs / cs.shift(W) - 1.0).to_numpy()
        confs = confirm_masks(df)
        m = n - 1 - H
        eo = open_arr[1 : m + 1]
        ec = close_arr[1 + H : 1 + H + m]
        si = np.arange(0, m)
        base_ok = (day[1 + H : 1 + H + m] == day[si]) & (minutes[1 + H : 1 + H + m] <= CUTOFF_MIN)
        net5 = net_from(eo, ec, 2e-4, 5e-4)
        dip = base_ok & (ret_w[si] < THR) & (np.abs(net5) <= 0.25)
        full = dip & confs["down3"][si]
        idx = np.where(full)[0]
        for i in idx:
            rows.append({"ticker": t, "sig_minute": int(minutes[si[i]]), "net5": round(float(net5[si[i]]), 5)})
        print(f"{t}: holdout bars={len(df)}, down3 events={len(idx)}", flush=True)
    df = pd.DataFrame(rows)
    print("\n=== down3 events:", len(df), "===")
    if len(df):
        print("\nsig_minute distribution:")
        print(df["sig_minute"].value_counts().sort_index().to_string())
        print("\nartifact zone (sig_minute < 40):", int((df["sig_minute"] < 40).sum()))
        print("\nby ticker:")
        print(df.groupby("ticker").agg(n=("net5", "size"), mean5=("net5", "mean")).to_string())
        print("\nall events by minute:")
        print(df.sort_values("sig_minute").to_string())


if __name__ == "__main__":
    main()
