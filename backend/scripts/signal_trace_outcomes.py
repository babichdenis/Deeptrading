#!/usr/bin/env python3
"""Signal Trace — outcomes (фаза 3, batch). См. DECISIONS.md 2026-10-01 17:30.

Для RAW-событий прогона считает по каноническим барам из БД:
  anchor      = первый 1m-бар на/после закрытия бара сигнала (вход = его open);
  future_return на горизонтах (минуты от anchor);
  MFE/MAE по пути (знаково по стороне: BUY — высокое хорошо, SELL — низкое);
  TP/SL-first (барьеры take/stop × ATR канона) → label TP_FIRST | SL_FIRST | TIMEOUT,
  при касании обоих уровней в одном баре — SL_FIRST (консервативно, как labeling);
  upsert в signal_trace_outcomes по (signal_id, outcome_cfg_hash).

Никогда не вызывается из рантайма.

Запуск (на .7, из backend/):
  .venv/bin/python scripts/signal_trace_outcomes.py --run-key "trace2 0901 20261001-1925"
  # + --write чтобы записать; без --write — dry-run (только сводка)
"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402

INTERVAL_TO_DB = {"1min": 1, "5min": 2, "10min": 8, "15min": 3, "30min": 9, "hour": 4}
TF_MINUTES = {"1min": 1, "5min": 5, "10min": 10, "15min": 15, "30min": 30, "hour": 60}


def _sync_engine():
    return create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)


def _cfg_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


def _load_1m(eng, figi: str, t0: datetime, t1: datetime):
    with eng.connect() as c:
        return c.execute(text(
            "SELECT ts, open, high, low, close FROM candles WHERE figi=:f AND interval=1 "
            "AND ts>=:a AND ts<=:b ORDER BY ts"
        ), {"f": figi, "a": t0, "b": t1}).all()


def _atr_at(eng, figi: str, ival: int, ts_signal: datetime, period: int) -> float | None:
    """ATR канона (indicatorhub._atr) на TF-барах до бара сигнала включительно."""
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT ts, open, high, low, close, volume FROM candles WHERE figi=:f AND interval=:i "
            "AND ts<=:t ORDER BY ts DESC LIMIT 200"
        ), {"f": figi, "i": ival, "t": ts_signal}).all()
    rows = list(reversed(rows))
    if len(rows) < period + 1:
        return None
    from app.engine.indicatorhub import _atr
    from app.engine.models import Candle
    bars = [Candle(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                   close=float(r[4]), volume=float(r[5] or 0)) for r in rows]
    vals = _atr(bars, period)
    value = vals[-1] if vals else None
    return float(value) if value else None


def _outcome(win, side: str, horizon_min: int, atr: float | None,
             take_atr: float, stop_atr: float) -> dict:
    """win — 1m-бары от anchor включительно (list of rows ts,o,h,l,c)."""
    anchor_ts, anchor = win[0][0], float(win[0][1])
    out = {
        "anchor_ts": anchor_ts, "future_return": None, "mfe": None, "mae": None,
        "hit_tp_ts": None, "hit_sl_ts": None, "label": "TIMEOUT",
    }
    if anchor <= 0 or not win:
        return out
    buy = str(side).upper() in ("BUY", "LONG")
    tp_px = sl_px = None
    if atr and atr > 0 and take_atr > 0 and stop_atr > 0:
        take_dist = take_atr * atr
        stop_dist = stop_atr * atr
        tp_px = anchor + take_dist if buy else anchor - take_dist
        sl_px = anchor - stop_dist if buy else anchor + stop_dist
    mfe = None
    mae = None
    tp_ts = sl_ts = None
    last_close = anchor
    for ts, _o, high, low, close in win:
        last_close = float(close)
        bar_mfe = ((float(high) - anchor) if buy else (anchor - float(low))) / anchor
        bar_mae = ((float(low) - anchor) if buy else (anchor - float(high))) / anchor
        mfe = bar_mfe if mfe is None else max(mfe, bar_mfe)
        mae = bar_mae if mae is None else min(mae, bar_mae)
        if tp_px is not None and tp_ts is None and sl_ts is None:
            hit_tp = (float(high) >= tp_px) if buy else (float(low) <= tp_px)
            hit_sl = (float(low) <= sl_px) if buy else (float(high) >= sl_px)
            if hit_tp and hit_sl:
                sl_ts = ts      # консервативно: SL_FIRST
            elif hit_sl:
                sl_ts = ts
            elif hit_tp:
                tp_ts = ts
    out["future_return"] = last_close / anchor - 1.0
    out["mfe"] = mfe
    out["mae"] = mae
    out["hit_tp_ts"] = tp_ts
    out["hit_sl_ts"] = sl_ts
    if sl_ts is not None:
        out["label"] = "SL_FIRST"
    elif tp_ts is not None:
        out["label"] = "TP_FIRST"
    return out


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-key", required=True, help="run_key прогона (test_name) из signal_trace_runs")
    p.add_argument("--horizons", default="1,5,15,30,60", help="минуты через запятую")
    p.add_argument("--take", type=float, default=1.5, help="верхний барьер в ATR")
    p.add_argument("--stop", dest="stop_atr", type=float, default=1.5, help="нижний барьер в ATR")
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--limit", type=int, default=5000, help="максимум сигналов")
    p.add_argument("--write", action="store_true", help="без флага — dry-run")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    horizons = [int(x) for x in str(args.horizons).split(",") if x.strip()]
    eng = _sync_engine()
    with eng.connect() as c:
        run = c.execute(text(
            "SELECT run_id, interval FROM signal_trace_runs WHERE run_key=:k ORDER BY started_at DESC LIMIT 1"
        ), {"k": args.run_key}).mappings().first()
        if not run:
            print(f"run не найден: {args.run_key!r}")
            return 1
        sigs = c.execute(text(
            "SELECT DISTINCT ON (signal_id) signal_id, figi, ticker, side, ts_bar "
            "FROM signal_trace_events WHERE run_id=:r AND stage='RAW' AND signal_id IS NOT NULL "
            "ORDER BY signal_id, seq"
        ), {"r": run["run_id"]}).all()

    interval_name = str(run["interval"] or "10min")
    ival = INTERVAL_TO_DB.get(interval_name, 8)
    tf_min = TF_MINUTES.get(interval_name, 10)
    if not sigs:
        print("RAW-сигналов нет")
        return 0
    sigs = sigs[: max(args.limit, 0)]
    print(f"run: {args.run_key} · interval {interval_name} · сигналов {len(sigs)} · "
          f"горизонты {horizons} · barriers {args.take}/{args.stop_atr}×ATR{args.atr_period} · write={args.write}")

    by_figi: dict[str, list] = {}
    for sig_id, figi, ticker, side, ts_bar in sigs:
        by_figi.setdefault(figi, []).append((sig_id, ticker, side, ts_bar))

    h_max = timedelta(minutes=max(horizons) + tf_min + 5)
    rows_to_write: list[dict] = []
    stats_label: dict[str, int] = {}
    stats_ticker: dict[str, int] = {}
    skipped = 0
    for figi, items in by_figi.items():
        ts_list = [it[3] for it in items]
        t0 = min(ts_list) - timedelta(minutes=5)
        t1 = max(ts_list) + h_max + timedelta(hours=4)
        bars = _load_1m(eng, figi, t0, t1)
        if not bars:
            skipped += len(items)
            continue
        bts = [r[0] for r in bars]
        atr_cache: dict[datetime, float | None] = {}
        for sig_id, ticker, side, ts_bar in items:
            try:
                anchor_target = ts_bar + timedelta(minutes=tf_min)
                i0 = bisect.bisect_left(bts, anchor_target)
                if i0 >= len(bars):
                    skipped += 1
                    continue
                if ts_bar not in atr_cache:
                    atr_cache[ts_bar] = _atr_at(eng, figi, ival, ts_bar, args.atr_period)
                atr = atr_cache[ts_bar]
                for h in horizons:
                    i1 = bisect.bisect_right(bts, bars[i0][0] + timedelta(minutes=h))
                    win = bars[i0:i1]
                    if not win:
                        continue
                    out = _outcome(win, side, h, atr, args.take, args.stop_atr)
                    cfg_hash = _cfg_hash({"kind": "st_outcome", "h": h, "take": args.take,
                                          "stop": args.stop_atr, "atr": args.atr_period,
                                          "anchor": "next_open"})
                    stats_label[out["label"]] = stats_label.get(out["label"], 0) + 1
                    stats_ticker[ticker or figi[-6:]] = stats_ticker.get(ticker or figi[-6:], 0) + 1
                    rows_to_write.append({
                        "signal_id": sig_id, "cfg": cfg_hash, "anchor": "next_open",
                        "anchor_ts": out["anchor_ts"], "h": h,
                        "ret": out["future_return"], "mfe": out["mfe"], "mae": out["mae"],
                        "tp": out["hit_tp_ts"], "sl": out["hit_sl_ts"], "label": out["label"],
                        "extra": {"horizon_min": h, "tf_min": tf_min, "atr": atr},
                    })
            except Exception as e:  # noqa: BLE001
                skipped += 1
                print(f"  ⚠ {ticker}: {type(e).__name__}: {str(e)[:100]}")

    print(f"outcomes: {len(rows_to_write)} строк · пропущено сигналов {skipped}")
    print("labels:", stats_label)
    print("по тикерам:", dict(sorted(stats_ticker.items(), key=lambda kv: -kv[1])[:15]))

    if args.write and rows_to_write:
        sql = text(
            "INSERT INTO signal_trace_outcomes (signal_id, outcome_cfg_hash, anchor, anchor_ts, "
            "horizon_bars, future_return, mfe, mae, hit_tp_ts, hit_sl_ts, label, extra) VALUES "
            "(:signal_id, :cfg, :anchor, :anchor_ts, :h, :ret, :mfe, :mae, :tp, :sl, :label, "
            "CAST(:extra AS JSON)) ON CONFLICT (signal_id, outcome_cfg_hash) DO UPDATE SET "
            "anchor_ts=EXCLUDED.anchor_ts, horizon_bars=EXCLUDED.horizon_bars, "
            "future_return=EXCLUDED.future_return, mfe=EXCLUDED.mfe, mae=EXCLUDED.mae, "
            "hit_tp_ts=EXCLUDED.hit_tp_ts, hit_sl_ts=EXCLUDED.hit_sl_ts, label=EXCLUDED.label, "
            "computed_at=now(), extra=EXCLUDED.extra"
        )
        with eng.begin() as c:
            for r in rows_to_write:
                c.execute(sql, {**r, "extra": json.dumps(r["extra"], ensure_ascii=False, default=str)})
        with eng.connect() as c:
            n = c.execute(text("SELECT count(*) FROM signal_trace_outcomes")).scalar()
        print(f"в БД signal_trace_outcomes: {n}")
    elif not args.write:
        print("(dry-run — не записано; добавьте --write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
