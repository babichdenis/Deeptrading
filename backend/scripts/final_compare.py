#!/usr/bin/env python3
"""Единый сравнение flip-конфигов на портфельной модели (пул 10K, 20% на позицию).

Читает exec-файлы portfolio_merge (уже портфельная модель с правильным обеспечением).
Досчитывает volume-ratio на момент входа из свечей БД.
Секции на КАЖДЫЙ конфиг:
  - общие метрики
  - per-ticker
  - per-regime
  - утро/день/вечер
  - volume win/loss
  - флипы vs не-флипы
  - SL/TP
И финальная сводная таблица всех конфигов.
"""
import argparse, asyncio, os, sys, json
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MSK = ZoneInfo("Europe/Moscow")
REGIMES = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE", "UNKNOWN"]


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


def enrich_volume(trades):
    """Досчитать volume_ratio на entry из 1m свечей БД."""
    from sqlalchemy import text
    from app.database import SessionLocal
    from app.services.signals import _load_candles as _lc

    lo = min(datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")) for t in trades) - timedelta(days=90)
    hi = max(datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00")) for t in trades) + timedelta(days=1)

    by_figi = defaultdict(list)
    for t in trades:
        by_figi[t.get("figi", t["ticker"])].append(t)

    for figi, tlist in by_figi.items():
        async def _load():
            async with SessionLocal() as db:
                return await _lc(db, figi, 1, date_from=lo, date_to=hi)
        candles = asyncio.run(_load())
        # build 5m resample for volume lookup
        c5 = resample_5m(candles)
        vols = [c.volume for c in c5]
        n = len(c5)
        # precompute vol_ratio per 5m bar
        vol_ratio = [None] * n
        for i in range(1, n):
            w = vols[max(0, i - 50):i]
            m = sum(w) / len(w) if w else 1.0
            vol_ratio[i] = vols[i] / max(m, 1e-9)
        for t in tlist:
            ets = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
            vr = lookup_vr(ets, c5, vol_ratio)
            t["vol_ratio"] = vr
    return trades


def resample_5m(candles):
    from collections import OrderedDict
    buckets = OrderedDict()
    for c in candles:
        key = int(c.ts.timestamp() // 300)
        if key not in buckets:
            buckets[key] = {"ts": datetime.fromtimestamp(key * 300, tz=timezone.utc),
                            "open": c.open, "high": c.high, "low": c.low,
                            "close": c.close, "volume": c.volume}
        else:
            b = buckets[key]
            b["high"] = max(b["high"], c.high)
            b["low"] = min(b["low"], c.low)
            b["close"] = c.close
            b["volume"] += c.volume
    return [type("C", (), b)() for b in buckets.values()]


def lookup_vr(ts, c5, arr):
    best_i, best_d = None, None
    for i, c in enumerate(c5):
        d = abs((c.ts - ts).total_seconds())
        if best_d is None or d < best_d:
            best_d, best_i = d, i
    if best_i is not None and best_d < 600:
        return arr[best_i]
    return None


def fmt_row(name, d):
    n = d["n"]
    wr = d["w"] / n * 100 if n else 0
    pf = d["gw"] / abs(d["gl"]) if d["gl"] else 0
    return "  %-12s %6d %6d %6d %5.1f%% %+10.0f %+10.0f %+10.0f %6.2f %+8.2f" % (
        name, n, d["w"], n - d["w"], wr, d["gw"], d["gl"], d["net"], pf,
        d["net"] / n if n else 0)


def agg():
    return {"n": 0, "w": 0, "gw": 0.0, "gl": 0.0, "net": 0.0}


def report_config(label, exec_path, lines):
    trades = load(exec_path)
    # exec уже портфельный — net на реальные qty
    trades.sort(key=lambda t: datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")))
    prev = {}
    for t in trades:
        tk = t["ticker"]
        t["_flip"] = False
        p = prev.get(tk)
        if p:
            gap = (datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
                   - datetime.fromisoformat(p["exit_ts"].replace("Z", "+00:00"))).total_seconds()
            if p["side"] != t["side"] and gap < 600:
                t["_flip"] = True
        prev[tk] = t

    # volume enrichment (по тикерам из сделок)
    try:
        trades = enrich_volume(trades)
        has_vol = True
    except Exception as e:
        print("vol enrich fail:", e)
        has_vol = False

    lines.append("=" * 130)
    lines.append("  %s  (%d сделок, портфель 10K, 20%% на позицию)" % (label, len(trades)))
    lines.append("=" * 130)

    # totals
    tot = agg()
    for t in trades:
        tot["n"] += 1; tot["net"] += t["net"]
        if t["net"] > 0: tot["w"] += 1; tot["gw"] += t["net"]
        else: tot["gl"] += t["net"]
    wr = tot["w"] / tot["n"] * 100 if tot["n"] else 0
    pf = tot["gw"] / abs(tot["gl"]) if tot["gl"] else 0
    lines.append("  TOTAL: n=%d WR=%.1f%% GW=%+.0f GL=%+.0f Net=%+.0f PF=%.2f Net/t=%.2f" % (
        tot["n"], wr, tot["gw"], tot["gl"], tot["net"], pf, tot["net"] / tot["n"] if tot["n"] else 0))

    # per ticker
    by_tk = defaultdict(agg)
    for t in trades:
        d = by_tk[t["ticker"]]
        d["n"] += 1; d["net"] += t["net"]
        if t["net"] > 0: d["w"] += 1; d["gw"] += t["net"]
        else: d["gl"] += t["net"]
    lines.append("")
    lines.append("  PER-TICKER:")
    lines.append("  %-8s %6s %5s %6s %10s %10s %10s %6s %8s" % (
        "Ticker", "N", "Win", "WR%", "GrossW", "GrossL", "Net", "PF", "Net/t"))
    for tk in sorted(by_tk, key=lambda x: -by_tk[x]["net"]):
        d = by_tk[tk]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        pf = d["gw"] / abs(d["gl"]) if d["gl"] else 0
        lines.append("  %-8s %6d %5d %5.1f%% %+10.0f %+10.0f %+10.0f %6.2f %+8.2f" % (
            tk, d["n"], d["w"], wr, d["gw"], d["gl"], d["net"], pf,
            d["net"] / d["n"] if d["n"] else 0))

    # per regime
    by_rg = defaultdict(agg)
    for t in trades:
        d = by_rg[t.get("regime", "UNKNOWN")]
        d["n"] += 1; d["net"] += t["net"]
        if t["net"] > 0: d["w"] += 1; d["gw"] += t["net"]
        else: d["gl"] += t["net"]
    lines.append("")
    lines.append("  PER-REGIME:")
    lines.append("  %-8s %6s %5s %6s %10s %10s %10s %6s %8s" % (
        "Regime", "N", "Win", "WR%", "GrossW", "GrossL", "Net", "PF", "Net/t"))
    for r in REGIMES:
        if r in by_rg:
            lines.append(fmt_row(r, by_rg[r]))

    # session split
    by_sess = defaultdict(agg)
    for t in trades:
        s = session_of(datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")))
        d = by_sess[s]
        d["n"] += 1; d["net"] += t["net"]
        if t["net"] > 0: d["w"] += 1; d["gw"] += t["net"]
        else: d["gl"] += t["net"]
    lines.append("")
    lines.append("  SESSIONS (утро/день/вечер):")
    lines.append("  %-8s %6s %5s %6s %10s %10s %10s %6s %8s" % (
        "Session", "N", "Win", "WR%", "GrossW", "GrossL", "Net", "PF", "Net/t"))
    for s in ["morning", "day", "evening"]:
        if s in by_sess:
            lines.append(fmt_row(s, by_sess[s]))

    # session x regime
    lines.append("")
    lines.append("  SESSION × REGIME (Net):")
    by_sr = defaultdict(agg)
    for t in trades:
        s = session_of(datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00")))
        key = (s, t.get("regime", "UNKNOWN"))
        d = by_sr[key]
        d["n"] += 1; d["net"] += t["net"]
        if t["net"] > 0: d["w"] += 1; d["gw"] += t["net"]
        else: d["gl"] += t["net"]
    for s in ["morning", "day", "evening"]:
        for r in REGIMES:
            key = (s, r)
            if key in by_sr:
                d = by_sr[key]
                lines.append(fmt_row("%s/%s" % (s[:4], r[:14]), d))

    # volume win/loss
    if has_vol:
        lines.append("")
        lines.append("  VOLUME RATIO (wins vs losses):")
        wv = [t["vol_ratio"] for t in trades if t["net"] > 0 and t.get("vol_ratio")]
        lv = [t["vol_ratio"] for t in trades if t["net"] <= 0 and t.get("vol_ratio")]
        wm = sum(wv) / len(wv) if wv else 0
        lm = sum(lv) / len(lv) if lv else 0
        lines.append("    Wins vol=%.2f (n=%d) | Losses vol=%.2f (n=%d)" % (wm, len(wv), lm, len(lv)))
        # by regime
        for r in REGIMES:
            tr = [t for t in trades if t.get("regime") == r]
            if not tr: continue
            wvr = [t["vol_ratio"] for t in tr if t["net"] > 0 and t.get("vol_ratio")]
            lvr = [t["vol_ratio"] for t in tr if t["net"] <= 0 and t.get("vol_ratio")]
            wmr = sum(wvr) / len(wvr) if wvr else 0
            lmr = sum(lvr) / len(lvr) if lvr else 0
            lines.append("    %-18s Wins=%.2f Losses=%.2f" % (r, wmr, lmr))

    # flip vs non-flip
    flips = [t for t in trades if t["_flip"]]
    nflips = [t for t in trades if not t["_flip"]]
    lines.append("")
    lines.append("  FLIP vs NON-FLIP:")
    for name, grp in [("FLIPS", flips), ("NON-FLIPS", nflips)]:
        if not grp: continue
        g = agg()
        for t in grp:
            g["n"] += 1; g["net"] += t["net"]
            if t["net"] > 0: g["w"] += 1; g["gw"] += t["net"]
            else: g["gl"] += t["net"]
        wr = g["w"] / g["n"] * 100 if g["n"] else 0
        pf = g["gw"] / abs(g["gl"]) if g["gl"] else 0
        lines.append("    %-10s n=%4d WR=%.1f%% Net=%+.0f PF=%.2f" % (name, g["n"], wr, g["net"], pf))

    # exit reasons
    by_ex = defaultdict(agg)
    for t in trades:
        d = by_ex[t.get("exit_reason", "?")]
        d["n"] += 1; d["net"] += t["net"]
        if t["net"] > 0: d["w"] += 1; d["gw"] += t["net"]
        else: d["gl"] += t["net"]
    lines.append("")
    lines.append("  EXIT REASONS:")
    for ex in sorted(by_ex, key=lambda x: -by_ex[x]["n"]):
        d = by_ex[ex]
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        lines.append("    %-16s n=%4d WR=%.1f%% GW=%+.0f GL=%+.0f Net=%+.0f" % (
            ex, d["n"], wr, d["gw"], d["gl"], d["net"]))
    lines.append("")
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True)
    ap.add_argument("--semi", required=True)
    ap.add_argument("--none", required=True)
    ap.add_argument("--margin", default="")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    lines = []
    res = {}
    res["FULL-FLIP"] = report_config("FULL-FLIP (all sessions)", args.full, lines)
    res["SEMI-FLIP"] = report_config("SEMI-FLIP (all sessions)", args.semi, lines)
    res["NO-FLIP"] = report_config("NO-FLIP (all sessions)", args.none, lines)
    if args.margin:
        res["FULL+MARGIN"] = report_config("FULL-FLIP + MARGIN", args.margin, lines)

    # final summary table
    lines.append("=" * 130)
    lines.append("  FINAL SUMMARY (портфель 10K, 20% на позицию)")
    lines.append("=" * 130)
    lines.append("  %-16s %7s %6s %6s %10s %10s %10s %6s %8s" % (
        "Config", "N", "Wins", "WR%", "GrossW", "GrossL", "Net", "PF", "Net/t"))
    for name, d in res.items():
        wr = d["w"] / d["n"] * 100 if d["n"] else 0
        pf = d["gw"] / abs(d["gl"]) if d["gl"] else 0
        lines.append("  %-16s %7d %6d %5.1f%% %+10.0f %+10.0f %+10.0f %6.2f %+8.2f" % (
            name, d["n"], d["w"], wr, d["gw"], d["gl"], d["net"], pf,
            d["net"] / d["n"] if d["n"] else 0))

    out = "\n".join(lines)
    print(out)
    with open(args.out, "w") as f:
        f.write(out + "\n")
    print("\nSaved ->", args.out)


if __name__ == "__main__":
    main()
