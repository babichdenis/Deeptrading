#!/usr/bin/env python3
"""Калибровка разметки (IS-подбор → OOS-проверка) без оптимизации под PnL.

Цель: выбрать параметры triple-barrier и зигзага, при которых разметка:
  - не вырождена (баланс классов, вменяемая длительность);
  - стабильна между IS и OOS (распределения не разъезжаются);
  - для зигзага: колени не шум (медиана длительности >= 3 баров, вменяемый поток).

Запуск:
  .venv/bin/python scripts/label_calibrate.py \
      --is-from 2026-07-01 --is-to 2026-08-31 \
      --oos-from 2026-09-01 --oos-to 2026-09-21 --tf 10min,1h
"""
from __future__ import annotations

import argparse
import statistics as st
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine.candlehub import build_tf  # noqa: E402
from app.engine.labeling import BarrierConfig, ZigzagConfig, triple_barrier, zigzag_trades  # noqa: E402
from app.engine.models import Candle  # noqa: E402

TF_SECONDS = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "30min": 1800, "1h": 3600, "hour": 3600}
DEFAULT_TICKERS = "SBER,GAZP,LKOH,ROSN,T,NVTK,SMLT,GMKN,MTSS,CHMF"


def parse():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--is-from", required=True)
    p.add_argument("--is-to", required=True)
    p.add_argument("--oos-from", required=True)
    p.add_argument("--oos-to", required=True)
    p.add_argument("--tf", default="10min,1h")
    p.add_argument("--tickers", default=DEFAULT_TICKERS)
    p.add_argument("--min-moves", default="2,2.5,3,4,5")
    p.add_argument("--takes", default="1,1.5,2,2.5")
    p.add_argument("--stops", default="0.75,1,1.5")
    p.add_argument("--max-bars", default="12,24,48")
    p.add_argument("--atr-period", type=int, default=14)
    return p.parse_args()


def _load(eng, figi: str, t0: str, t1: str) -> list[Candle]:
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE figi=:f AND interval=1 AND ts>=:t0 AND ts<=:t1 ORDER BY ts"),
            {"f": figi, "t0": t0 + " 00:00+00", "t1": t1 + " 23:59:59+00"}).fetchall()
    return [Candle(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                   close=float(r[4]), volume=float(r[5] or 0)) for r in rows]


def _slice(series: list, d0: str, d1: str) -> list:
    lo = datetime.fromisoformat(d0 + "T00:00:00+00:00")
    hi = datetime.fromisoformat(d1 + "T23:59:59+00:00")
    return [b for b in series if lo <= b.ts <= hi]


def _zig_stats(series_by_tk: dict[str, list], mm: float, atr_period: int) -> dict:
    legs, per_day = [], 0.0
    days_seen: set[str] = set()
    total = 0
    for tk, series in series_by_tk.items():
        trades = zigzag_trades(series, ZigzagConfig(min_move_atr=mm, atr_period=atr_period))
        total += len(trades)
        legs += [t["bars"] for t in trades]
        for t in trades:
            days_seen.add(t["entry_ts"][:10])
    n_tk = max(1, len(series_by_tk))
    per_day = total / max(1, len(days_seen)) / n_tk
    legs_sorted = sorted(legs)
    def _q(p: float):
        if not legs_sorted:
            return 0
        return legs_sorted[min(len(legs_sorted) - 1, int(p * len(legs_sorted)))]
    return {
        "legs": total, "per_day": round(per_day, 1),
        "med": _q(0.5), "p25": _q(0.25), "p75": _q(0.75),
        "short_pct": round(100 * sum(1 for x in legs if x < 3) / len(legs), 0) if legs else None,
    }


def _bar_stats(series_by_tk: dict[str, list], take: float, stop: float, max_bars: int, atr_period: int) -> dict:
    cnt = {-1: 0, 0: 0, 1: 0}
    bars_hit = []
    for series in series_by_tk.values():
        for r in triple_barrier(series, BarrierConfig(take_atr=take, stop_atr=stop,
                                                      max_bars=max_bars, atr_period=atr_period)):
            cnt[r["label"]] += 1
            if r["label"] != 0:
                bars_hit.append(r["t1_bars"])
    tot = sum(cnt.values()) or 1
    return {
        "up": round(100 * cnt[1] / tot), "flat": round(100 * cnt[0] / tot), "dn": round(100 * cnt[-1] / tot),
        "med_bars_hit": st.median(bars_hit) if bars_hit else None,
    }


def main() -> int:
    a = parse()
    tfs = [t.strip() for t in a.tf.split(",") if t.strip()]
    tickers = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        fmap = {r[1]: r[0] for r in c.execute(text(
            "SELECT figi, upper(ticker) FROM instruments WHERE upper(ticker) = ANY(:t)"),
            {"t": tickers}).fetchall()}
    print(f"бумаг: {len(fmap)} · IS {a.is_from}..{a.is_to} · OOS {a.oos_from}..{a.oos_to}")
    t0 = time.time()
    bars_by_tk: dict[str, list[Candle]] = {}
    for tk in tickers:
        f = fmap.get(tk)
        if not f:
            continue
        bars_by_tk[tk] = _load(eng, f, (date.fromisoformat(a.is_from) - timedelta(days=5)).isoformat(), a.oos_to)
    print(f"данные загружены: {time.time() - t0:.1f}s")
    for tf in tfs:
        tf_s = TF_SECONDS[tf]
        series_all = {tk: (build_tf(b, tf_s) if tf_s != 60 else b) for tk, b in bars_by_tk.items()}
        is_s = {tk: _slice(s, a.is_from, a.is_to) for tk, s in series_all.items()}
        oos_s = {tk: _slice(s, a.oos_from, a.oos_to) for tk, s in series_all.items()}
        print(f"\n================ TF {tf} ================")
        print("ЗИГЗАГ (min_move×ATR) | IS: колен/день/тик · медиана | OOS: колен/день/тик · медиана")
        for mm in [float(x) for x in a.min_moves.split(",")]:
            si = _zig_stats(is_s, mm, a.atr_period)
            so = _zig_stats(oos_s, mm, a.atr_period)
            print(f"  {mm:4.1f} | IS {si['legs']:5d} · {si['per_day']:4.1f}/д · мед {si['med']:3} (p25 {si['p25']}, p75 {si['p75']}, <3бар {si['short_pct']}%)"
                  f" || OOS {so['legs']:4d} · {so['per_day']:4.1f}/д · мед {so['med']:3}")
        print("БАРЬЕРЫ (take/stop/max_bars) | IS: +1/0/−1 % (мед.бар) | OOS: +1/0/−1 %")
        for take in [float(x) for x in a.takes.split(",")]:
            for stop in [float(x) for x in a.stops.split(",")]:
                for mb in [int(x) for x in a.max_bars.split(",")]:
                    bi = _bar_stats(is_s, take, stop, mb, a.atr_period)
                    bo = _bar_stats(oos_s, take, stop, mb, a.atr_period)
                    print(f"  t{take:4.2f}/s{stop:4.2f}/h{mb:3d} | IS {bi['up']:3d}/{bi['flat']:3d}/{bi['dn']:3d} (мед {bi['med_bars_hit']})"
                          f" || OOS {bo['up']:3d}/{bo['flat']:3d}/{bo['dn']:3d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
