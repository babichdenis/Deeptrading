#!/usr/bin/env python3
"""Series 5 Matrix — FULL DIAGNOSTICS per test.

Enriches every trade with regime (independent regime_at), flip detection,
entry/exit quorum votes and indicator values. Prints complete tables:
per-ticker, per-regime, regime×ticker, exit reasons, regime×exit, flip vs non-flip,
entry quorum votes, win/loss indicator distributions.

Regime + indicators computed ONCE per ticker and reused across all 6 configs.
"""
import asyncio, os, sys, json
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble, TF_SECONDS
from app.services.regime import _adx, RegimeDetector, regime_at
from app.engine.indicators import atr
from app.services.indicators import rsi, ema

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]

V2_PARAMS = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}

TICKERS_5 = ["SMLT", "NLMK", "GAZP", "LENT", "CHMF"]

GATE2_QUORUM = {
    "NEUTRAL": ["bollinger_reclaim", "rsi_reversal", "vwap_reclaim"],
    "HIGH_VOLATILITY": ALL_SIDS,
    "TREND_UP": ["pullback_ema", "macd_cross", "donchian_breakout", "rsi_reversal"],
    "TREND_DOWN": ["pullback_ema", "macd_cross", "donchian_breakout", "rsi_reversal"],
    "RANGE": ALL_SIDS,
}
MY_QUORUM = {
    "NEUTRAL": ["bollinger_reclaim", "rsi_reversal", "vwap_reclaim"],
    "HIGH_VOLATILITY": ALL_SIDS,
    "TREND_UP": ALL_SIDS,
    "TREND_DOWN": ALL_SIDS,
    "RANGE": ALL_SIDS,
}
REGIME_ORDER = ["NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]


def base_req(figi, lot, neutral_mode, volume_thr=1.0, per_regime_quorum=None):
    setups = [{"strategy_id": sid, "tf": "5min", "params": dict(V2_PARAMS.get(sid, {}))} for sid in ALL_SIDS]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
        "quorum": 2, "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(), "to_ts": DATE_TO.isoformat(),
    }
    if neutral_mode:
        req["neutral_mode"] = neutral_mode
    if volume_thr is not None:
        req["volume_filter_threshold"] = volume_thr
    if per_regime_quorum is not None:
        req["per_regime_quorum"] = per_regime_quorum
    return req


async def load_candles(figi):
    from app.services.signals import _load_candles as _lc
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


async def get_eligible():
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot
            FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            ORDER BY i.ticker
        """))).fetchall()
    return [(r.figi, r.ticker, r.lot) for r in rows]


def resample(candles, tf_sec):
    from app.services.ensemble import cached_resample
    return cached_resample(candles, tf_sec)


def compute_indicators(c5):
    closes = [c.close for c in c5]; highs = [c.high for c in c5]
    lows = [c.low for c in c5]; volumes = [c.volume for c in c5]
    n = len(c5)
    rsi_vals = rsi(closes, 16) if n > 16 else [None] * n
    atr_vals = atr(c5, 14) if n > 14 else [None] * n
    atr_pct = [None] * n
    for i in range(n):
        if atr_vals[i] is not None and closes[i] > 0:
            atr_pct[i] = atr_vals[i] / closes[i] * 100
    ema50 = ema(closes, 50) if n > 50 else [None] * n
    ema_slope = [None] * n
    for i in range(5, n):
        if ema50[i] is not None and ema50[i-5] is not None and ema50[i-5] > 0:
            ema_slope[i] = (ema50[i] - ema50[i-5]) / ema50[i-5] * 100
    adx_vals = _adx(c5, 14)
    if n <= 14: adx_vals = [None] * n
    adx_vals = ([None] * (n - len(adx_vals)) + adx_vals) if len(adx_vals) < n else adx_vals
    vol_ratio = [None] * n
    for i in range(1, n):
        window = volumes[max(0, i-50):i]
        mean = sum(window) / len(window) if window else 1.0
        vol_ratio[i] = volumes[i] / max(mean, 1e-9)
    return {"rsi": rsi_vals, "atr_pct": atr_pct, "ema_slope": ema_slope,
            "adx": adx_vals, "vol_ratio": vol_ratio}


def lookup(ts, c5, arr):
    best_i, best_d = None, None
    for i, c in enumerate(c5):
        d = abs((c.ts - ts).total_seconds())
        if best_d is None or d < best_d: best_d, best_i = d, i
    if best_i is not None and best_d < 600:
        return arr[best_i]
    return None


def enrich_ticker(figi, ticker, candles, lot, ind, regime_row, neutral_mode, vol_thr, prq):
    """Run one config, enrich trades with regime/flip/votes/indicators."""
    res = compute_ensemble(candles, base_req(figi, lot, neutral_mode, vol_thr, prq))
    static = res.get("static", {})
    trades = static.get("trades", [])
    quorum_list = static.get("quorum_list", [])
    entries = static.get("entries", [])
    vol_rejected = sum(1 for r in static.get("rejected", [])
                       if str(r.get("reason", "")).startswith("VOL_FILTER:"))
    c5 = resample(candles, 300)

    # Exact mapping: quorum_event_id -> members_for
    q_by_id = {q["event_id"]: q.get("members_for", []) for q in quorum_list}
    # Accepted entries by (side) sorted by ts, for reverse-lookup
    ent_by_side = {"BUY": [], "SELL": []}
    for e in entries:
        ent_by_side.setdefault(e["side"], []).append(e)

    enriched = []
    prev_exit_side = None
    prev_exit_ts = None
    for i, t in enumerate(trades):
        entry_ts = datetime.fromisoformat(t["entry_ts"])
        exit_ts = datetime.fromisoformat(t["exit_ts"])
        regime = regime_at(regime_row, entry_ts)
        regime_name = regime["state"] if regime else "UNKNOWN"

        is_flip = False
        if prev_exit_side is not None and prev_exit_side != t["side"]:
            gap = abs((entry_ts - prev_exit_ts).total_seconds())
            if gap < 600: is_flip = True

        # Entry votes: accepted signal with max ts <= entry_ts (same side)
        entry_votes = []
        want_side = "BUY" if t["side"] == "LONG" else "SELL"
        cand = [e for e in ent_by_side.get(want_side, []) if e["ts"] <= t["entry_ts"]]
        if cand:
            best = max(cand, key=lambda e: e["ts"])
            entry_votes = q_by_id.get(best.get("quorum_event_id", ""), [])

        # Exit votes: opposite-side quorum event closest to exit time
        exit_votes = []
        exit_side = "SELL" if t["side"] == "LONG" else "BUY"
        best_q, best_d = None, None
        for q in quorum_list:
            if q["side"] != exit_side: continue
            d = abs((datetime.fromisoformat(q["ts"]) - exit_ts).total_seconds())
            if best_d is None or d < best_d: best_d, best_q = d, q
        if best_q is not None and best_d < 900:
            exit_votes = best_q.get("members_for", [])

        enriched.append({
            "ticker": ticker, "side": t["side"], "regime": regime_name,
            "is_flip": is_flip, "entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
            "entry_px": t["entry_px"], "exit_px": t["exit_px"],
            "stop": t.get("stop"), "target": t.get("target"),
            "gross": t.get("gross", 0), "net": t.get("net", 0),
            "bars_held": t.get("bars_held", 0), "exit_reason": t.get("exit_reason"),
            "win": t.get("net", 0) > 0,
            "rsi": lookup(entry_ts, c5, ind["rsi"]),
            "atr_pct": lookup(entry_ts, c5, ind["atr_pct"]),
            "ema_slope": lookup(entry_ts, c5, ind["ema_slope"]),
            "adx": lookup(entry_ts, c5, ind["adx"]),
            "vol_ratio": lookup(entry_ts, c5, ind["vol_ratio"]),
            "entry_votes": sorted(entry_votes), "entry_votes_n": len(entry_votes),
            "exit_votes": sorted(exit_votes), "exit_votes_n": len(exit_votes),
        })
        prev_exit_side = t["side"]
        prev_exit_ts = exit_ts

    return enriched, vol_rejected


def fmt_table(title, rows, headers, widths):
    """rows = list of tuples of str. Print aligned table."""
    print()
    print("  " + "-" * 130)
    print("  " + title)
    print("  " + "-" * 130)
    line = "  "
    for h, w in zip(headers, widths):
        line += str(h).ljust(w)
    print(line)
    for r in rows:
        line = "  "
        for val, w in zip(r, widths):
            line += str(val).ljust(w)
        print(line)


def report_test(label, enriched_all, vol_rej):
    print()
    print("=" * 130)
    print("  " + label)
    print("=" * 130)
    if not enriched_all:
        print("  NO TRADES"); return None

    total = len(enriched_all)
    wins = [t for t in enriched_all if t["win"]]
    losses = [t for t in enriched_all if not t["win"]]
    gw = sum(t["net"] for t in wins); gl = sum(t["net"] for t in losses)
    net = gw + gl
    wr = len(wins) / total * 100
    pf = gw / abs(gl) if gl else 0
    print("  TOTAL: Trades=%d | Wins=%d | Losses=%d | WR=%.1f%% | PF=%.2f | Vol_filtered=%d" % (
        total, len(wins), len(losses), wr, pf, vol_rej))
    print("  Gross Win=%+.2f | Gross Loss=%+.2f | Net=%+.2f" % (gw, gl, net))
    print("  Avg Win=%+.2f | Avg Loss=%+.2f | Avg Net=%+.2f" % (
        gw / len(wins) if wins else 0, gl / len(losses) if losses else 0, net / total))

    # Per ticker
    by_tk = defaultdict(list)
    for t in enriched_all: by_tk[t["ticker"]].append(t)
    rows = []
    for tk in TICKERS_5:
        if tk not in by_tk: continue
        ts = by_tk[tk]; w = [t for t in ts if t["win"]]; l = [t for t in ts if not t["win"]]
        tgw = sum(t["net"] for t in w); tgl = sum(t["net"] for t in l)
        tnet = tgw + tgl
        rows.append((tk, len(ts), len(w), len(l), "%.1f%%" % (len(w)/len(ts)*100),
                     "%+.0f" % tgw, "%+.0f" % tgl, "%+.0f" % tnet,
                     "%.2f" % (tgw/abs(tgl) if tgl else 0), "%+.2f" % (tnet/len(ts))))
    fmt_table("PER TICKER", rows,
              ["Ticker", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "AvgNet"],
              [8, 7, 6, 6, 6, 10, 10, 10, 6, 8])

    # Per regime
    by_rg = defaultdict(list)
    for t in enriched_all: by_rg[t["regime"]].append(t)
    rows = []
    for rn in REGIME_ORDER + ["UNKNOWN"]:
        if rn not in by_rg: continue
        ts = by_rg[rn]; w = [t for t in ts if t["win"]]; l = [t for t in ts if not t["win"]]
        tgw = sum(t["net"] for t in w); tgl = sum(t["net"] for t in l)
        tnet = tgw + tgl
        rows.append((rn, len(ts), len(w), len(l), "%.1f%%" % (len(w)/len(ts)*100 if ts else 0),
                     "%+.0f" % tgw, "%+.0f" % tgl, "%+.0f" % tnet,
                     "%.2f" % (tgw/abs(tgl) if tgl else 0), "%+.2f" % (tnet/len(ts) if ts else 0)))
    fmt_table("PER REGIME", rows,
              ["Regime", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "AvgNet"],
              [18, 7, 6, 6, 6, 10, 10, 10, 6, 8])

    # Regime × ticker (net)
    rt = defaultdict(lambda: defaultdict(float))
    for t in enriched_all: rt[t["regime"]][t["ticker"]] += t["net"]
    rows = []
    for rn in REGIME_ORDER:
        if rn not in rt: continue
        row = [rn]
        for tk in TICKERS_5:
            row.append("%+.0f" % rt[rn].get(tk, 0))
        rows.append(row)
    fmt_table("PER REGIME × TICKER (Net)", rows, ["Regime"] + TICKERS_5, [18] + [10]*5)

    # Exit reasons
    by_ex = defaultdict(list)
    for t in enriched_all: by_ex[t["exit_reason"]].append(t)
    rows = []
    for ex in sorted(by_ex, key=lambda x: -sum(t["net"] for t in by_ex[x])):
        ts = by_ex[ex]; w = [t for t in ts if t["win"]]; l = [t for t in ts if not t["win"]]
        tgw = sum(t["net"] for t in w); tgl = sum(t["net"] for t in l)
        rows.append((ex, len(ts), len(w), len(l), "%.1f%%" % (len(w)/len(ts)*100),
                     "%+.0f" % tgw, "%+.0f" % tgl, "%+.0f" % (tgw + tgl)))
    fmt_table("EXIT REASONS", rows,
              ["Reason", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net"],
              [22, 7, 6, 6, 6, 10, 10, 10])

    # Regime × exit reason (net)
    rx = defaultdict(lambda: defaultdict(float))
    for t in enriched_all: rx[t["regime"]][t["exit_reason"]] += t["net"]
    ex_reasons = []
    for t in enriched_all:
        if t["exit_reason"] not in ex_reasons: ex_reasons.append(t["exit_reason"])
    rows = []
    for rn in REGIME_ORDER + ["UNKNOWN"]:
        if rn not in rx: continue
        row = [rn]
        for ex in ex_reasons:
            row.append("%+.0f" % rx[rn].get(ex, 0))
        rows.append(row)
    fmt_table("REGIME × EXIT REASON (Net)", rows, ["Regime"] + ex_reasons, [18] + [10]*len(ex_reasons))

    # Flip vs non-flip
    flips = [t for t in enriched_all if t["is_flip"]]
    nflips = [t for t in enriched_all if not t["is_flip"]]
    rows = []
    for name, grp in [("FLIP", flips), ("NON-FLIP", nflips)]:
        w = [t for t in grp if t["win"]]; l = [t for t in grp if not t["win"]]
        tgw = sum(t["net"] for t in w); tgl = sum(t["net"] for t in l)
        rows.append((name, len(grp), len(w), len(l), "%.1f%%" % (len(w)/len(grp)*100 if grp else 0),
                     "%+.0f" % tgw, "%+.0f" % tgl, "%+.0f" % (tgw+tgl),
                     "%.2f" % (tgw/abs(tgl) if tgl else 0), "%+.2f" % ((tgw+tgl)/len(grp) if grp else 0)))
    fmt_table("FLIP vs NON-FLIP", rows,
              ["Type", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "AvgNet"],
              [10, 7, 6, 6, 6, 10, 10, 10, 6, 8])

    # Entry quorum votes
    ev = defaultdict(lambda: {"n": 0, "wins": 0, "gwin": 0.0, "gloss": 0.0})
    for t in enriched_all:
        for sid in t["entry_votes"]:
            ev[sid]["n"] += 1
            if t["win"]: ev[sid]["wins"] += 1; ev[sid]["gwin"] += t["net"]
            else: ev[sid]["gloss"] += t["net"]
    rows = []
    for sid in sorted(ev, key=lambda x: -ev[x]["n"]):
        d = ev[sid]
        rows.append((sid, d["n"], d["wins"], "%.1f%%" % (d["wins"]/d["n"]*100 if d["n"] else 0),
                     "%+.0f" % d["gwin"], "%+.0f" % d["gloss"], "%+.0f" % (d["gwin"]+d["gloss"])))
    fmt_table("ENTRY QUORUM VOTES (who enters)", rows,
              ["Strategy", "Votes", "InWins", "WR%", "GrossWin", "GrossLoss", "Net"],
              [28, 6, 7, 6, 10, 10, 10])

    # Exit quorum votes
    ev = defaultdict(lambda: {"n": 0, "wins": 0, "gwin": 0.0, "gloss": 0.0})
    for t in enriched_all:
        for sid in t["exit_votes"]:
            ev[sid]["n"] += 1
            if t["win"]: ev[sid]["wins"] += 1; ev[sid]["gwin"] += t["net"]
            else: ev[sid]["gloss"] += t["net"]
    rows = []
    for sid in sorted(ev, key=lambda x: -ev[x]["n"]):
        d = ev[sid]
        rows.append((sid, d["n"], d["wins"], "%.1f%%" % (d["wins"]/d["n"]*100 if d["n"] else 0),
                     "%+.0f" % d["gwin"], "%+.0f" % d["gloss"], "%+.0f" % (d["gwin"]+d["gloss"])))
    fmt_table("EXIT QUORUM VOTES (who closes)", rows,
              ["Strategy", "Votes", "InWins", "WR%", "GrossWin", "GrossLoss", "Net"],
              [28, 6, 7, 6, 10, 10, 10])

    # Win/loss indicators
    print()
    print("  " + "-" * 130)
    print("  ENTRY INDICATORS: WINS vs LOSSES")
    print("  " + "-" * 130)
    def avg(lst, key):
        vals = [t[key] for t in lst if t.get(key) is not None]
        return sum(vals)/len(vals) if vals else 0
    for rn in ["ALL", "NEUTRAL", "HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN"]:
        grp = enriched_all if rn == "ALL" else [t for t in enriched_all if t["regime"] == rn]
        w = [t for t in grp if t["win"]]; l = [t for t in grp if not t["win"]]
        if not grp: continue
        r = [rn]
        for key in ["rsi", "atr_pct", "ema_slope", "adx", "vol_ratio"]:
            r.append("%.2f/%.2f" % (avg(w, key), avg(l, key)))
        print("    %-18s RSI(w/l)=%s ATR%c(w/l)=%s EMA(w/l)=%s ADX(w/l)=%s VOL(w/l)=%s" % (
            rn, r[1], '%', r[2], r[3], r[4], r[5]))

    return {"label": label, "trades": total, "wins": len(wins), "losses": len(losses),
            "net": net, "wr": wr, "pf": pf, "gw": gw, "gl": gl, "vol_rej": vol_rej}


async def main():
    eligible = await get_eligible()
    m = {t: (f, l) for f, t, l in eligible}
    candles_map = {}
    for ticker in TICKERS_5:
        if ticker not in m:
            print("%s NOT eligible" % ticker); continue
        figi, lot = m[ticker]
        candles = await load_candles(figi)
        candles_map[ticker] = (figi, candles, lot)
        px = candles[-1].close if candles else 0
        print("Loaded %s: %d candles px=%.0f lot=%d" % (ticker, len(candles), px, lot))

    # Precompute regime + indicators per ticker once
    ticker_ctx = {}
    for ticker, (figi, candles, lot) in candles_map.items():
        c5 = resample(candles, 300)
        detector = RegimeDetector()
        regime_row = detector.compute(c5)
        ind = compute_indicators(c5)
        ticker_ctx[ticker] = {"figi": figi, "candles": candles, "lot": lot,
                              "regime_row": regime_row, "ind": ind}

    tests = [
        ("1: Semi-flip + Volume (baseline quorum)", "semi_flip", 1.0, None),
        ("2: Full flip + Volume (baseline quorum)", None, 1.0, None),
        ("3: Semi-flip + Volume + Gate2 quorum", "semi_flip", 1.0, GATE2_QUORUM),
        ("4: Full flip + Volume + Gate2 quorum", None, 1.0, GATE2_QUORUM),
        ("5: Semi-flip + Volume + My quorum", "semi_flip", 1.0, MY_QUORUM),
        ("6: Full flip + Volume + My quorum", None, 1.0, MY_QUORUM),
    ]

    all_results = []
    for label, neutral_mode, vol_thr, prq in tests:
        enriched_all = []
        vol_rej_total = 0
        for ticker, ctx in ticker_ctx.items():
            enr, vol_rej = enrich_ticker(
                ctx["figi"], ticker, ctx["candles"], ctx["lot"], ctx["ind"],
                ctx["regime_row"], neutral_mode, vol_thr, prq)
            enriched_all.extend(enr)
            vol_rej_total += vol_rej
            print("  [%s] %s: %d trades, %d vol_filtered" % (label, ticker, len(enr), vol_rej))
        r = report_test(label, enriched_all, vol_rej_total)
        if r: all_results.append(r)
        sys.stdout.flush()

    print()
    print("=" * 130)
    print("  FINAL SUMMARY")
    print("=" * 130)
    print("  %-50s %6s %5s %5s %6s %12s %12s %12s %5s %6s %6s" % (
        "Test", "Trades", "Wins", "Loss", "WR%", "GrossWin", "GrossLoss", "Net", "PF", "AvgNet", "VolRej"))
    for r in all_results:
        print("  %-50s %6d %5d %5d %5.1f%% %+12.0f %+12.0f %+12.0f %5.2f %+6.2f %6d" % (
            r["label"], r["trades"], r["wins"], r["losses"], r["wr"],
            r["gw"], r["gl"], r["net"], r["pf"], r["net"]/r["trades"], r["vol_rej"]))

    base = all_results[0]["net"] if all_results else 0
    print()
    print("  DELTA vs Test 1:")
    for r in all_results:
        print("    %-50s %+12.0f (%+.1f%%)" % (r["label"], r["net"]-base, (r["net"]-base)/abs(base)*100 if base else 0))


if __name__ == "__main__":
    asyncio.run(main())
