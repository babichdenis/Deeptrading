#!/usr/bin/env python3
"""Сравнение конфигов flip по сессиям (утро/день/вечер MSK) + режимам.

Сессии MSK: morning 06:50-09:50, day 09:50-18:45, evening 19:05-23:50.
Читает jsonl-дампы per_ticker_dump (isolated, 10K/тикер).
"""
import json, sys, argparse
from datetime import datetime
from collections import defaultdict
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")

def session_of(ts):
    h = ts.astimezone(MSK).hour + ts.astimezone(MSK).minute / 60
    if 6.833 <= h < 9.833: return "morning"
    if 9.833 <= h < 18.75: return "day"
    return "evening"

def load(path):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out

def fmt_row(name, stats):
    n = stats["n"]
    wr = stats["w"] / n * 100 if n else 0
    pf = stats["gw"] / abs(stats["gl"]) if stats["gl"] else 0
    return "%-8s %6d %6d %6d %5.1f%% %+10.0f %+10.0f %+10.0f %6.2f %+8.1f" % (
        name, n, stats["w"], n - stats["w"], wr, stats["gw"], stats["gl"],
        stats["net"], pf, stats["net"] / n if n else 0)

def analyze(trades):
    out = defaultdict(lambda: {"n": 0, "w": 0, "gw": 0.0, "gl": 0.0, "net": 0.0})
    for t in trades:
        sess = session_of(datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")))
        net = t.get("net_at_10k", t.get("net", 0))
        d = out[sess]
        d["n"] += 1
        d["net"] += net
        if net > 0:
            d["w"] += 1
            d["gw"] += net
        else:
            d["gl"] += net
    return out

def analyze_regime(trades):
    out = defaultdict(lambda: {"n": 0, "w": 0, "gw": 0.0, "gl": 0.0, "net": 0.0})
    for t in trades:
        sess = session_of(datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")))
        reg = t.get("regime", "UNKNOWN")
        net = t.get("net_at_10k", t.get("net", 0))
        key = (sess, reg)
        d = out[key]
        d["n"] += 1
        d["net"] += net
        if net > 0:
            d["w"] += 1
            d["gw"] += net
        else:
            d["gl"] += net
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True)
    ap.add_argument("--semi", required=True)
    ap.add_argument("--none", required=True)
    args = ap.parse_args()

    configs = [("FULL-FLIP", args.full), ("SEMI-FLIP", args.semi), ("NO-FLIP", args.none)]
    regs = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN"]

    for label, path in configs:
        trades = load(path)
        print("=" * 120)
        print("  %s  (%d сделок, изолированно 10K/тикер)" % (label, len(trades)))
        print("=" * 120)
        st = analyze(trades)
        print("  %-8s %6s %6s %6s %6s %10s %10s %10s %6s %8s" % (
            "Session", "Trades", "Wins", "Loss", "WR%", "GrossW", "GrossL", "Net", "PF", "Net/t"))
        print("  " + "-" * 115)
        for s in ["morning", "day", "evening"]:
            if s in st:
                print("  " + fmt_row(s, st[s]))
        tot = {"n": 0, "w": 0, "gw": 0.0, "gl": 0.0, "net": 0.0}
        for d in st.values():
            for k in tot:
                if isinstance(tot[k], int):
                    tot[k] += d[k]
                else:
                    tot[k] += d[k]
        print("  " + "-" * 115)
        print("  " + fmt_row("TOTAL", tot))

        # Session x Regime
        rt = analyze_regime(trades)
        print()
        print("  Session × Regime:")
        print("  %-8s %-18s %6s %6s %6s %10s %10s %6s %8s" % (
            "Session", "Regime", "N", "Wins", "WR%", "GrossW", "GrossL", "Net", "Net/t"))
        for s in ["morning", "day", "evening"]:
            for r in regs:
                key = (s, r)
                if key not in rt:
                    continue
                d = rt[key]
                n = d["n"]
                wr = d["w"] / n * 100 if n else 0
                print("  %-8s %-18s %6d %6d %5.1f%% %+10.0f %+10.0f %+6.0f %8.1f" % (
                    s, r, n, d["w"], wr, d["gw"], d["gl"], d["net"],
                    d["net"] / n if n else 0))
        print()

if __name__ == "__main__":
    main()
