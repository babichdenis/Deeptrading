"""B5b RE-RUN: Ensemble vote behavior (continuous strength + forward bars). (SHADOW, read-only)

Для каждой сделки July baseline (5b44f3b383df) и каждой из 7 функций на баре idx (5m):
  vote_direction = BUY/SELL/neutral  (по знаку меры функции)
  vote_strength  = |мера| / ATR14    (continuous, нормированная)
Окна: вход t0, t0+1, t0+2, t0+3, t0+5; выход tE, tE-1, tE-3, tE-5.
Агрегации: direction@t0/tE, consensus-strength vs outcome, strength dynamics winners vs losers,
signal_exit foreshadow. Point-in-time (состояние <= бара). No oracle.
Артефакты: reports/5b44f3b383df/entry_exit_vote_behavior.json (перезаписывает) + audit.csv
"""
import sys, os, json, csv, statistics, bisect
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
RUN_DIR = os.path.join(REPORTS, "5b44f3b383df")
FIGIS = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
T_FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
T_TO = datetime(2026, 8, 1, tzinfo=timezone.utc)
FNS = ["range_compression_breakout", "pullback_ema", "bollinger_reclaim", "rsi_reversal",
       "vwap_reclaim", "donchian_breakout", "macd_cross"]


def ema(series, period):
    out = []
    k = 2 / (period + 1)
    e = None
    for v in series:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def rsi(closes, period=14):
    out = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    ag, al = statistics.mean(gains[:period]), statistics.mean(losses[:period])
    out[period] = 100 - 100 / (1 + (ag / al if al > 0 else 999))
    for i in range(period + 1, len(closes)):
        ag = (ag * (period - 1) + gains[i - 1]) / period
        al = (al * (period - 1) + losses[i - 1]) / period
        out[i] = 100 - 100 / (1 + (ag / al if al > 0 else 999))
    return out


def atr(highs, lows, closes, period=14):
    out = [None] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
        if len(trs) > period:
            trs.pop(0)
        if i >= period:
            out[i] = sum(trs) / period
    return out


def vwap_series(ts_list, closes, lows, highs, vols):
    """Дневной VWAP (по дате MSK) — 5m бары."""
    out = [None] * len(closes)
    by_day = {}
    day_idx = []
    from zoneinfo import ZoneInfo
    msk = ZoneInfo("Europe/Moscow")
    for i, ts in enumerate(ts_list):
        d = ts.astimezone(msk).date().isoformat()
        day_idx.append(d)
    for i, d in enumerate(day_idx):
        acc = by_day.setdefault(d, [0.0, 0.0, 0.0])  # pv, v, cnt
        acc[0] += closes[i] * (vols[i] if vols[i] else 1)
        acc[1] += (vols[i] if vols[i] else 1)
        out[i] = acc[0] / acc[1] if acc[1] > 0 else None
    return out


def compute_feature_vectors(c5, ts_list):
    closes = [c.close for c in c5]
    highs = [c.high for c in c5]
    lows = [c.low for c in c5]
    vols = [c.volume for c in c5]
    atr14 = atr(highs, lows, closes, 14)
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    ef = ema(closes, 12)
    es = ema(closes, 26)
    esig = ema([(a - b) for a, b in zip(ef, es)], 9)
    hist = [(a - b) - s for a, b, s in zip(ef, es, esig)]
    rsi14 = rsi(closes, 14)
    vwap = vwap_series(ts_list, closes, lows, highs, vols)
    # rolling std of hist для z-score macd
    hstd = [None] * len(closes)
    for i in range(50, len(closes)):
        hstd[i] = statistics.pstdev(hist[i - 49:i + 1])

    def measure(fn, i):
        if i < 20 or i >= len(closes):
            return None
        a = atr14[i] or (highs[i] - lows[i] or 1e-9)
        if fn == "macd_cross":
            h = hist[i]
            z = h / hstd[i] if (hstd[i] and hstd[i] > 0) else h / a
            return z
        if fn == "donchian_breakout":
            hi = max(highs[i - 20:i]); lo = min(lows[i - 20:i])
            return (closes[i] - (hi + lo) / 2) / a
        if fn == "rsi_reversal":
            r = rsi14[i]
            if r is None:
                return None
            return (50 - r) / 50.0
        if fn == "bollinger_reclaim":
            ma = statistics.mean(closes[i - 19:i + 1])
            sd = statistics.pstdev(closes[i - 19:i + 1])
            return (ma - closes[i]) / (sd * 2 if sd > 0 else a)
        if fn == "pullback_ema":
            return (ema20[i] - ema50[i]) / (ema50[i] or 1e-9) * 1000
        if fn == "vwap_reclaim":
            v = vwap[i]
            if v is None or v == 0:
                return None
            return (v - closes[i]) / v * 1000
        if fn == "range_compression_breakout":
            hi = max(highs[i - 20:i]); lo = min(lows[i - 20:i])
            return (closes[i] - (hi + lo) / 2) / a
        return None

    return measure


def main() -> None:
    from app.services.research_pack import _load_candles
    from app.services.ensemble import resample, TF_SECONDS
    print("loading candles ...", flush=True)
    sig_by_figi = {}
    for figi in FIGIS:
        cs = _load_candles(figi, T_FROM, T_TO)
        c5 = resample(cs, TF_SECONDS["5min"])
        ts5 = [c.ts for c in c5]
        measure = compute_feature_vectors(c5, ts5)
        sig_by_figi[figi] = {"ts5": ts5, "measure": measure}
    print("features ready", flush=True)

    EPS = 0.02  # порог neutral

    def vote_at(figi, ts, fn):
        d = sig_by_figi[figi]
        j = bisect.bisect_right(d["ts5"], ts) - 1
        if j < 0:
            return None, 0.0
        m = d["measure"](fn, j)
        if m is None:
            return None, 0.0
        if abs(m) < EPS:
            return "neutral", abs(m)
        return ("buy" if m > 0 else "sell"), abs(m)

    trades = list(csv.DictReader(open(os.path.join(RUN_DIR, "trades.csv"))))
    audit = []
    for t in trades:
        figi = t["figi"]
        if figi not in sig_by_figi:
            continue
        d = sig_by_figi[figi]
        dt = datetime.fromisoformat(t["decision_time"].replace("Z", "+00:00"))
        exit_dt = datetime.fromisoformat(t["exit_time"].replace("Z", "+00:00"))
        je = bisect.bisect_right(d["ts5"], dt) - 1
        jx = bisect.bisect_right(d["ts5"], exit_dt) - 1
        if je < 20 or jx <= je or jx >= len(d["ts5"]):
            continue
        net = float(t["net_rub"])
        mfe = float(t["mfe_r"]) if t["mfe_r"] else None
        mae = float(t["mae_r"]) if t["mae_r"] else None
        for fn in FNS:
            for off in (0, 1, 2, 3, 5):
                k = je + off
                if k >= len(d["ts5"]):
                    continue
                vd, vs = vote_at(figi, d["ts5"][k], fn)
                audit.append({"trade_id": t["trade_id"], "figi": figi, "side": t["side"],
                              "exit_type": t["exit_reason"], "function": fn,
                              "rel_bar": f"+{off}", "vote_direction": vd, "vote_strength": round(vs, 4),
                              "net": net, "mfe_r": mfe, "mae_r": mae})
            for back in (5, 3, 1, 0):
                k = jx - back
                if k < 0 or k >= len(d["ts5"]):
                    continue
                vd, vs = vote_at(figi, d["ts5"][k], fn)
                audit.append({"trade_id": t["trade_id"], "figi": figi, "side": t["side"],
                              "exit_type": t["exit_reason"], "function": fn,
                              "rel_bar": f"-{back}", "vote_direction": vd, "vote_strength": round(vs, 4),
                              "net": net, "mfe_r": mfe, "mae_r": mae})
    print(f"audit rows: {len(audit)}", flush=True)

    # direction distribution @t0 и @tE per function
    per_fn = {}
    for fn in FNS:
        rows0 = [a for a in audit if a["function"] == fn and a["rel_bar"] == "+0"]
        rowsE = [a for a in audit if a["function"] == fn and a["rel_bar"] == "-0"]
        d0 = {"buy": 0, "sell": 0, "neutral": 0}
        dE = {"buy": 0, "sell": 0, "neutral": 0}
        for a in rows0:
            d0[a["vote_direction"] or "neutral"] += 1
        for a in rowsE:
            dE[a["vote_direction"] or "neutral"] += 1
        per_fn[fn] = {"direction_t0": d0, "direction_tE": dE}

    # consensus-strength @t0 vs outcome
    consensus = {}
    for a in audit:
        if a["rel_bar"] != "+0":
            continue
        c = consensus.setdefault(a["trade_id"], {"agree_strength": 0.0, "agree_n": 0, "total_n": 0, "net": a["net"]})
        c["total_n"] += 1
        if a["vote_direction"] in ("buy", "sell"):
            agree = (a["vote_direction"] == "buy" and a["side"] == "LONG") or \
                    (a["vote_direction"] == "sell" and a["side"] == "SHORT")
            if agree:
                c["agree_strength"] += a["vote_strength"]
                c["agree_n"] += 1
    cons_groups = {}
    for tid, c in consensus.items():
        agree_ratio = (c["agree_n"] / c["total_n"]) if c["total_n"] else 0
        strength = c["agree_strength"]
        key = ("agree>=5" if agree_ratio >= 5 / 7 else
               ("agree>=4" if agree_ratio >= 4 / 7 else
                ("agree>=3" if agree_ratio >= 3 / 7 else ("agree>=2" if agree_ratio >= 2 / 7 else "agree<2"))))
        cons_groups.setdefault(key, []).append((strength, c["net"]))
    consensus_by = {}
    for k, v in cons_groups.items():
        nets = [x[1] for x in v]
        strs = [x[0] for x in v]
        consensus_by[k] = {"count": len(v), "net": round(sum(nets), 2),
                           "net_per_trade": round(sum(nets) / len(v), 2),
                           "mean_agree_strength": round(statistics.mean(strs), 3)}

    # strength dynamics winners vs losers (mean trajectory t0..t0+5)
    dyn = {}
    for off in (0, 1, 2, 3, 5):
        rows_off = [a for a in audit if a["rel_bar"] == f"+{off}"]
        w, l = [], []
        for a in rows_off:
            if a["vote_direction"] not in ("buy", "sell"):
                continue
            agree = (a["vote_direction"] == "buy" and a["side"] == "LONG") or \
                    (a["vote_direction"] == "sell" and a["side"] == "SHORT")
            if not agree:
                continue
            (w if a["net"] > 0 else l).append(a["vote_strength"])
        dyn[f"t0+{off}"] = {"winners_mean_strength": round(statistics.mean(w), 3) if w else None,
                            "losers_mean_strength": round(statistics.mean(l), 3) if l else None,
                            "winners_n": len(w), "losers_n": len(l)}

    # signal_exit foreshadow: средняя сила согласных в окне tE-5..tE
    foreshadow = {}
    for fn in FNS:
        rows_ex = [a for a in audit if a["function"] == fn and a["rel_bar"].startswith("-")]
        se = [a for a in rows_ex if a["exit_type"] == "signal_exit"]
        other = [a for a in rows_ex if a["exit_type"] != "signal_exit"]
        def contra(rows):
            vals = [a["vote_strength"] for a in rows if a["vote_direction"] in ("buy", "sell")
                    and not ((a["vote_direction"] == "buy" and a["side"] == "LONG") or
                             (a["vote_direction"] == "sell" and a["side"] == "SHORT"))]
            return round(statistics.mean(vals), 3) if vals else None
        foreshadow[fn] = {"signal_exit_contra_strength": contra(se),
                          "non_signal_contra_strength": contra(other)}

    report = {"schema": "entry_exit_vote_behavior_v2", "run_id": "5b44f3b383df",
              "config_hash": "1c7f75dc44c2aa67", "period": "2026-07-01..2026-07-31",
              "note": "SHADOW/read-only; continuous strength = |мера|/ATR14; point-in-time; no oracle",
              "per_function_direction": per_fn,
              "consensus_t0_vs_outcome": consensus_by,
              "strength_dynamics_winners_vs_losers": dyn,
              "signal_exit_foreshadow": foreshadow,
              "n_trades": len({a["trade_id"] for a in audit}),
              "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()}
    with open(os.path.join(RUN_DIR, "entry_exit_vote_behavior.json"), "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(RUN_DIR, "entry_exit_vote_audit.csv"), "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(audit[0].keys()))
        wr.writeheader(); wr.writerows(audit)
    print(json.dumps({"audit_rows": len(audit), "n_trades": report["n_trades"],
                      "consensus": consensus_by, "dyn_t0": dyn.get("t0+0"),
                      "dyn_t5": dyn.get("t0+5")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
