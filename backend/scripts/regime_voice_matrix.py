#!/usr/bin/env python3
"""Простая матрица для разбиения ансамбля.

Два режима:
  voices — какие голоса полезны в каком режиме (включая volume_drop):
           net, trades, WR%, PF, avg_net по каждому (режим, стратегия).
  sltp   — какие SL/TP лучше в каждом режиме (сетка sl_mult × rr).

Запуск:
  cd backend && PYTHONPATH=. .venv/bin/python3 scripts/regime_voice_matrix.py voices
  cd backend && PYTHONPATH=. .venv/bin/python3 scripts/regime_voice_matrix.py sltp --sls 3,4,5,6 --rrs 3,4,5,6
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import compute_ensemble, resample
from app.services.regime import RegimeDetector, regime_at
from app.bot.ensemble_strategy import V2_SETUPS

V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
VOL_PARAMS = {"ma_len": 20, "drop_ratio": 1.5}

FROM = datetime(2026, 7, 1, tzinfo=timezone.utc)
TO = datetime(2026, 9, 13, tzinfo=timezone.utc)

REGIME_ORDER = ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "NEUTRAL", "RANGE", "NO_REGIME"]


def build_req(figi, lot, sl=4.0, rr=4.0, session="all", bias="info", rs_filter=None):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in V2P]
    setups.append({"strategy_id": "volume_drop", "tf": "5min", "params": dict(VOL_PARAMS)})
    req = {
        "figi": figi, "bias_mode": bias, "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": session, "quorum": 2, "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups, "use_all_setups": False,
        "drop_useless": True, "neutral_mode": "semi_flip",
        "from_ts": FROM.isoformat(), "to_ts": TO.isoformat(),
    }
    if rs_filter:
        req["regime_setups_filter"] = rs_filter
    return req


async def load_tickers():
    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT DISTINCT i.figi, i.ticker, i.lot FROM instruments i
            JOIN universe u ON i.ticker = u.ticker AND u.eligible_tier = 'eligible'
            AND NOT (i.ticker = 'T' AND i.figi = 'BBG000BSJK37')
            ORDER BY i.ticker
        """))).fetchall()
    return [(r[0], r[1], int(r[2]) if r[2] else 1) for r in rows]


async def load_candles(only=None):
    out = {}
    _want = set(only) if only else None
    async with SessionLocal() as db:
        for figi, tkr, lot in await load_tickers():
            if _want is not None and tkr not in _want:
                continue
            c = await _lc(db, figi, 1, date_from=FROM, date_to=TO)
            if c and len(c) >= 200:
                out[tkr] = (figi, lot, c)
    return out


def _blank():
    return {"trades": 0, "wins": 0, "net": 0.0, "gw": 0.0, "gl": 0.0}


def _add(b, net):
    b["trades"] += 1
    b["wins"] += 1 if net > 0 else 0
    b["net"] += net
    if net > 0:
        b["gw"] += net
    else:
        b["gl"] += net


def _fmt(b):
    wr = b["wins"] / b["trades"] * 100 if b["trades"] else 0.0
    pf = b["gw"] / abs(b["gl"]) if b["gl"] else 0.0
    avg = b["net"] / b["trades"] if b["trades"] else 0.0
    return wr, pf, avg


def regime_timeline(candles):
    try:
        c5 = resample(candles, 300)
        return RegimeDetector().compute(c5) or []
    except Exception:
        return []


def regime_of(tl, ts_iso):
    if not ts_iso or not tl:
        return "NO_REGIME"
    try:
        r = regime_at(tl, datetime.fromisoformat(ts_iso))
    except Exception:
        return "NO_REGIME"
    return r["state"] if r else "NO_REGIME"


def run_voices(cmap):
    agg = defaultdict(lambda: defaultdict(_blank))
    regime_tot = defaultdict(_blank)
    strat_tot = defaultdict(_blank)
    for tkr, (figi, lot, c) in cmap.items():
        try:
            res = compute_ensemble(c, build_req(figi, lot))
        except Exception as e:
            print("  err", tkr, type(e).__name__, str(e)[:60])
            continue
        if "error" in res:
            continue
        st = res.get("static", {})
        tl = regime_timeline(c)
        eps = (st.get("episodes") or {}).get("list", [])
        qmap = {q["event_id"]: (q.get("members_for") or []) for q in (st.get("quorum_list") or [])}
        for ep in eps:
            if ep.get("status") != "TRADED":
                continue
            net = ep.get("net_sum", ep.get("net") or 0.0) or 0.0
            reg = regime_of(tl, ep.get("entry_time"))
            members = qmap.get(ep.get("quorum_event_id"), []) or ["—"]
            _add(regime_tot[reg], net)
            for m in members:
                _add(agg[reg][m], net)
                _add(strat_tot[m], net)

    print("=" * 96)
    print("МАТРИЦА: голос × режим (net — сумма net сделок, где голос был в кворуме)")
    print("=" * 96)
    for reg in REGIME_ORDER:
        if reg not in agg:
            continue
        rt = regime_tot[reg]
        rwr, rpf, ravg = _fmt(rt)
        print("\n### %s  —  всего trades=%d  net=%+.0f  WR=%.1f%%  PF=%.2f  avg=%+.1f" % (
            reg, rt["trades"], rt["net"], rwr, rpf, ravg))
        print("  %-28s %7s %6s %8s %6s %10s %10s" % ("strategy", "trades", "WR%", "PF", "avg", "net", "verdict"))
        rows = sorted(agg[reg].items(), key=lambda kv: kv[1]["net"], reverse=True)
        for sid, b in rows:
            wr, pf, avg = _fmt(b)
            if b["net"] <= 0:
                verdict = "❌ DROP"
            elif avg >= ravg:
                verdict = "✅ keep+"
            else:
                verdict = "~ keep"
            print("  %-28s %7d %5.1f%% %8.2f %+7.1f %+10.0f  %s" % (
                sid, b["trades"], wr, pf, avg, b["net"], verdict))

    print("\n" + "=" * 96)
    print("ИТОГО по голосам (все режимы)")
    print("=" * 96)
    print("  %-28s %7s %6s %8s %6s %10s" % ("strategy", "trades", "WR%", "PF", "avg", "net"))
    for sid, b in sorted(strat_tot.items(), key=lambda kv: kv[1]["net"], reverse=True):
        wr, pf, avg = _fmt(b)
        print("  %-28s %7d %5.1f%% %8.2f %+7.1f %+10.0f" % (sid, b["trades"], wr, pf, avg, b["net"]))


def run_ablate(cmap, variants):
    tots = {label: _blank() for label, _ in variants}
    regs = {label: defaultdict(_blank) for label, _ in variants}
    for tkr, (figi, lot, c) in cmap.items():
        tl = regime_timeline(c)
        for label, rs in variants:
            try:
                res = compute_ensemble(c, build_req(figi, lot, rs_filter=rs))
            except Exception:
                continue
            if "error" in res:
                continue
            for t in res.get("static", {}).get("trades", []):
                net = t.get("net", 0.0)
                reg = regime_of(tl, t.get("entry_ts"))
                _add(tots[label], net)
                _add(regs[label][reg], net)

    print("=" * 108)
    print("ABLATION: режимные фильтры голосов (net по режимам, capital 10K/ticker, all sessions)")
    print("=" * 108)
    print("  %-34s %8s %8s %7s %6s | %s" % (
        "variant", "trades", "net", "WR%", "PF",
        " ".join("%9s" % r[:9] for r in REGIME_ORDER)))
    for label, _ in variants:
        b = tots[label]
        wr, pf, _ = _fmt(b)
        cells = " ".join("%+9.0f" % regs[label].get(r, _blank())["net"] for r in REGIME_ORDER)
        print("  %-34s %8d %+8.0f %6.1f%% %6.2f | %s" % (label, b["trades"], b["net"], wr, pf, cells))


def run_bias(cmap):
    agg = defaultdict(lambda: defaultdict(_blank))
    for tkr, (figi, lot, c) in cmap.items():
        try:
            res = compute_ensemble(c, build_req(figi, lot))
        except Exception as e:
            print("  err", tkr, type(e).__name__, str(e)[:60])
            continue
        if "error" in res:
            continue
        st = res.get("static", {})
        tl = regime_timeline(c)
        accepted = st.get("entries", [])
        amap = {(a.get("side"), a.get("quorum_event_id")): a for a in accepted}
        for ep in (st.get("episodes") or {}).get("list", []):
            if ep.get("status") != "TRADED":
                continue
            net = ep.get("net_sum", ep.get("net") or 0.0) or 0.0
            reg = regime_of(tl, ep.get("entry_time"))
            a = amap.get((ep.get("side"), ep.get("quorum_event_id")))
            ab = bool(a.get("against_bias")) if a else False
            _add(agg[reg]["против bias" if ab else "по bias"], net)

    print("=" * 96)
    print("МАТРИЦА: bias × режим (info-режим: против bias входим, но помечаем)")
    print("=" * 96)
    print("  %-18s %10s %8s %8s %8s | %10s %8s %8s %8s" % (
        "regime", "по bias N", "WR%", "PF", "net", "против N", "WR%", "PF", "net"))
    for reg in REGIME_ORDER:
        if reg not in agg:
            continue
        w = agg[reg].get("по bias", _blank())
        a = agg[reg].get("против bias", _blank())
        wwr, wpf, _ = _fmt(w)
        awr, apf, _ = _fmt(a)
        print("  %-18s %10d %7.1f%% %8.2f %+8.0f | %10d %7.1f%% %8.2f %+8.0f" % (
            reg, w["trades"], wwr, wpf, w["net"], a["trades"], awr, apf, a["net"]))
    tot_w = _blank()
    tot_a = _blank()
    for reg in agg:
        for k, b in agg[reg].items():
            tgt = tot_w if k == "по bias" else tot_a
            tgt["trades"] += b["trades"]
            tgt["wins"] += b["wins"]
            tgt["net"] += b["net"]
            tgt["gw"] += b["gw"]
            tgt["gl"] += b["gl"]
    wwr, wpf, _ = _fmt(tot_w)
    awr, apf, _ = _fmt(tot_a)
    print("  %-18s %10d %7.1f%% %8.2f %+8.0f | %10d %7.1f%% %8.2f %+8.0f" % (
        "ИТОГО", tot_w["trades"], wwr, wpf, tot_w["net"],
        tot_a["trades"], awr, apf, tot_a["net"]))


def run_sltp(cmap, sls, rrs):
    grid = defaultdict(lambda: defaultdict(_blank))
    for tkr, (figi, lot, c) in cmap.items():
        tl = regime_timeline(c)
        for sl in sls:
            for rr in rrs:
                try:
                    res = compute_ensemble(c, build_req(figi, lot, sl, rr))
                except Exception:
                    continue
                if "error" in res:
                    continue
                for t in res.get("static", {}).get("trades", []):
                    _add(grid[regime_of(tl, t.get("entry_ts"))][(sl, rr)], t.get("net", 0.0))

    print("=" * 110)
    print("МАТРИЦА: SL/TP × режим (net; в скобках trades)  — capital 10K/ticker, all sessions")
    print("=" * 110)
    header = "  %-18s" % "regime" + "".join("  sl%.0f/rr%.0f" % (sl, rr) for sl in sls for rr in rrs)
    print(header)
    for reg in REGIME_ORDER:
        if reg not in grid:
            continue
        cells = []
        best = None
        for sl in sls:
            for rr in rrs:
                b = grid[reg].get((sl, rr))
                if b is None:
                    cells.append("     —      ")
                    continue
                cells.append("  %+8.0f(%d)" % (b["net"], b["trades"]))
                if best is None or b["net"] > best[0]:
                    best = (b["net"], sl, rr)
        print("  %-18s" % reg + "".join(cells))
        if best:
            print("  %-18s  → лучший sl%.0f/rr%.0f = %+.0f" % ("", best[1], best[2], best[0]))


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["voices", "sltp", "bias", "ablate", "all"])
    ap.add_argument("--sls", default="3,4,5,6")
    ap.add_argument("--rrs", default="3,4,5,6")
    ap.add_argument("--tickers", default="")
    args = ap.parse_args()

    only = [t.strip() for t in args.tickers.split(",") if t.strip()] or None
    print("Загрузка свечей...")
    cmap = await load_candles(only)
    print("тикеров: %d, период 2026-07-01 → 2026-09-13\n" % len(cmap))
    if args.mode in ("voices", "all"):
        run_voices(cmap)
    if args.mode in ("bias", "all"):
        run_bias(cmap)
    if args.mode in ("ablate", "all"):
        allreg = ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "NEUTRAL", "RANGE"]
        not_td = [r for r in allreg if r != "TREND_DOWN"]
        not_td_rng = [r for r in allreg if r not in ("TREND_DOWN", "RANGE")]
        no_rng = [r for r in allreg if r != "RANGE"]
        no_neu = [r for r in allreg if r != "NEUTRAL"]
        variants = [
            ("A0 baseline (all)", None),
            ("A1 vd off TREND_DOWN", {"volume_drop": not_td}),
            ("A2 vd off TD+RANGE", {"volume_drop": not_td_rng}),
            ("A3 A2 + vwap off NEUTRAL", {"volume_drop": not_td_rng, "vwap_reclaim": no_neu}),
            ("A4 A3 + range off NEUTRAL", {"volume_drop": not_td_rng, "vwap_reclaim": no_neu,
                                           "range_compression_breakout": no_neu}),
            ("A5 A4 + donchian off NEUTRAL", {"volume_drop": not_td_rng, "vwap_reclaim": no_neu,
                                              "range_compression_breakout": no_neu,
                                              "donchian_breakout": no_neu}),
            ("A6 vd off RANGE only", {"volume_drop": no_rng}),
        ]
        run_ablate(cmap, variants)
    if args.mode in ("sltp", "all"):
        sls = [float(x) for x in args.sls.split(",")]
        rrs = [float(x) for x in args.rrs.split(",")]
        run_sltp(cmap, sls, rrs)


if __name__ == "__main__":
    asyncio.run(main())
