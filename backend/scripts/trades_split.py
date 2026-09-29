#!/usr/bin/env python3
"""Сессии и режимы по артефактам прогона (bt_ose_sweep real): net / сделки / WR.

Разбивка без каких-либо гейтов — чистая аналитика по сделкам:
  - сессии (МСК): утро 10:00-14:00, день 14:00-19:00, вечер 19:00-24:00, вне;
  - режим входа: ADX(14) канона IndicatorHub на TF прогона:
    <20 диапазон, 20-25 переход, >=25 тренд.

Запуск:
  .venv/bin/python scripts/trades_split.py reports/bt_ose_real_XXX.json
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine.candlehub import build_tf  # noqa: E402
from app.engine.indicatorhub import _adx  # noqa: E402
from app.engine.models import Candle  # noqa: E402

TF = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "30min": 1800, "1h": 3600, "hour": 3600}
SESSIONS = [("утро 10-14", 7, 11), ("день 14-19", 11, 16), ("вечер 19-24", 16, 21)]  # часы UTC


def _session(ts: datetime) -> str:
    for name, a, b in SESSIONS:
        if a <= ts.hour < b:
            return name
    return "вне сессий"


def parse():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("json", help="путь к reports/bt_ose_real_*.json (с trades_detail)")
    p.add_argument("--adx-period", type=int, default=14)
    p.add_argument("--trend-thr", type=float, default=25.0)
    p.add_argument("--range-thr", type=float, default=20.0)
    return p.parse_args()


def main() -> int:
    a = parse()
    d = json.load(open(a.json, encoding="utf-8"))
    meta = d.get("meta", {})
    interval = str(meta.get("interval") or "1h")
    period = meta.get("period") or []
    tf_s = TF.get(interval)
    if not tf_s:
        print(f"неизвестный интервал {interval}")
        return 1
    rows = [r for r in d.get("raw", []) if "error" not in r]
    detail: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for r in rows:
        for t in r.get("trades_detail") or []:
            detail[r.get("strategy") or r.get("sid")].append((r["ticker"], t))
    if not detail:
        print("нет trades_detail (прогон без artifacts?)")
        return 1
    tickers = sorted({tk for lst in detail.values() for tk, _ in lst})
    t0 = (datetime.fromisoformat(period[0]) - timedelta(days=3)).date().isoformat()
    t1 = f"{period[1]} 23:59:59+00"
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        fmap = {r[1]: r[0] for r in c.execute(text(
            "SELECT figi, upper(ticker) FROM instruments WHERE upper(ticker) = ANY(:t)"),
            {"t": tickers}).fetchall()}
    ts_by_tk: dict[str, list] = {}
    adx_by_tk: dict[str, list] = {}
    for tk in tickers:
        f = fmap.get(tk)
        if not f:
            continue
        with eng.connect() as c:
            raws = c.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE figi=:f AND interval=1 AND ts>=:t0 AND ts<=:t1 ORDER BY ts"),
                {"f": f, "t0": f"{t0} 00:00+00", "t1": t1}).fetchall()
        bars = [Candle(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                       close=float(r[4]), volume=float(r[5] or 0)) for r in raws]
        series = build_tf(bars, tf_s) if tf_s != 60 else bars
        ts_by_tk[tk] = [b.ts for b in series]
        adx_by_tk[tk] = _adx(series, a.adx_period)["adx"]
    print(f"файл: {a.json} · TF {interval} · период {period[0]}..{period[1]} · бумаг {len(ts_by_tk)}")
    for robot, lst in sorted(detail.items()):
        agg_s = defaultdict(lambda: [0, 0, 0.0])
        agg_r = defaultdict(lambda: [0, 0, 0.0])
        for tk, t in lst:
            ts = datetime.fromisoformat(t["entry_time"])
            ts_list = ts_by_tk.get(tk) or []
            idx = bisect.bisect_right(ts_list, ts) - 1
            ax = adx_by_tk[tk][idx] if 0 <= idx < len(adx_by_tk.get(tk) or []) else None
            if ax is None:
                rname = "нет данных"
            elif ax >= a.trend_thr:
                rname = "тренд"
            elif ax >= a.range_thr:
                rname = "переход"
            else:
                rname = "диапазон"
            for agg, key in ((agg_s, _session(ts)), (agg_r, rname)):
                agg[key][0] += 1
                agg[key][1] += 1 if t["net_pnl"] > 0 else 0
                agg[key][2] += t["net_pnl"]
        tot_n = sum(v[2] for v in agg_s.values())
        tot_c = sum(v[0] for v in agg_s.values())
        print(f"\n=== {robot}: {tot_c} сд, net={tot_n:+.1f} ===")
        print("  сессии (МСК):")
        for name in [s[0] for s in SESSIONS] + ["вне сессий"]:
            if name in agg_s:
                c, w, n = agg_s[name]
                print(f"    {name:12s}: {c:4d} сд, WR {100 * w / c:4.1f}%, net {n:+9.1f}")
        print("  режимы (ADX входа):")
        for name in ("тренд", "переход", "диапазон", "нет данных"):
            if name in agg_r:
                c, w, n = agg_r[name]
                print(f"    {name:10s}: {c:4d} сд, WR {100 * w / c:4.1f}%, net {n:+9.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
