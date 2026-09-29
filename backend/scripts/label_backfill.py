#!/usr/bin/env python3
"""Заливка разметки (triple-barrier + zigzag) в БД по всем ТФ (research-слой).

Ядро — app/engine/labeling.py (чистые функции, как индикаторы). Этот скрипт —
батч-потребитель: считает по тикерам×ТФ и пишет в таблицы labels / ideal_trades
(cfg_hash в ключе — разные параметры разметки сосуществуют). Позже live-резолвер
дозревает хвост (mature=false) тем же ядром.

Запуск (из backend/, на машине с БД):
  .venv/bin/python scripts/label_backfill.py --from 2026-09-01 --to 2026-09-24 \
      --tf 10min --tickers SBER,LKOH,TATN --write
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine.candlehub import build_tf  # noqa: E402
from app.engine.labeling import (  # noqa: E402
    BarrierConfig,
    ZigzagConfig,
    triple_barrier,
    zigzag_trades,
)
from app.engine.models import Candle  # noqa: E402

TF_SECONDS = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "30min": 1800, "1h": 3600}

DDL = """
CREATE TABLE IF NOT EXISTS labels (
  figi text NOT NULL,
  tf_seconds int NOT NULL,
  cfg_hash text NOT NULL,
  ts timestamptz NOT NULL,
  label smallint NOT NULL,
  mature boolean NOT NULL,
  t1_ts timestamptz,
  t1_bars int,
  upper real, lower real, entry_px real, ret_atr real,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (figi, tf_seconds, cfg_hash, ts)
);
CREATE TABLE IF NOT EXISTS ideal_trades (
  figi text NOT NULL,
  tf_seconds int NOT NULL,
  cfg_hash text NOT NULL,
  entry_ts timestamptz NOT NULL,
  side text NOT NULL,
  exit_ts timestamptz,
  entry_px real, exit_px real,
  bars int, ret_pct real, ret_atr real,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (figi, tf_seconds, cfg_hash, entry_ts)
);
"""


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="dfrom", required=True)
    p.add_argument("--to", dest="dto", required=True)
    p.add_argument("--tf", default="10min", choices=sorted(TF_SECONDS))
    p.add_argument("--tickers", default="")
    p.add_argument("--figis", default="")
    p.add_argument("--top-tickers", type=int, default=5)
    p.add_argument("--take", type=float, default=1.5, help="верхний барьер в ATR")
    p.add_argument("--stop", dest="stop_atr", type=float, default=1.5, help="нижний барьер в ATR (симметрия)")
    p.add_argument("--max-bars", type=int, default=24)
    p.add_argument("--atr-period", type=int, default=14)
    p.add_argument("--min-move", type=float, default=3.0, help="минимальный ход зигзага в ATR")
    p.add_argument("--kinds", default="barrier,zigzag")
    p.add_argument("--warmup-days", type=int, default=5)
    p.add_argument("--write", action="store_true", help="без флага — dry-run (только статистика)")
    return p.parse_args()


def _sync_engine():
    return create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)


def _cfg_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


def _resolve_figis(eng, args) -> list[tuple[str, str]]:
    t0, t1 = f"{args.dfrom} 00:00:00+00", f"{args.dto} 23:59:59+00"
    with eng.connect() as c:
        if args.tickers:
            tks = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
            rows = c.execute(text("SELECT figi, ticker FROM instruments WHERE upper(ticker) = ANY(:t)"),
                             {"t": tks}).fetchall()
            return [(r[0], r[1]) for r in rows]
        if args.figis:
            fl = [f.strip() for f in args.figis.split(",") if f.strip()]
            rows = c.execute(text("SELECT figi, ticker FROM instruments WHERE figi = ANY(:f)"),
                             {"f": fl}).fetchall()
            return [(r[0], r[1] or r[0][-6:]) for r in rows]
        rows = c.execute(text(
            "SELECT figi, ticker FROM candles WHERE interval=1 AND ts>=:t0 AND ts<=:t1 "
            "GROUP BY figi, ticker ORDER BY count(*) DESC LIMIT :k"),
            {"t0": t0, "t1": t1, "k": args.top_tickers}).fetchall()
        return [(r[0], r[1] or r[0][-6:]) for r in rows]


def _load_bars(eng, figi: str, t0: str, t1: str) -> list[Candle]:
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT ts, open, high, low, close, volume FROM candles "
            "WHERE figi=:f AND interval=1 AND ts>=:t0 AND ts<=:t1 ORDER BY ts"),
            {"f": figi, "t0": t0, "t1": t1}).fetchall()
    return [Candle(ts=r[0], open=float(r[1]), high=float(r[2]), low=float(r[3]),
                   close=float(r[4]), volume=float(r[5] or 0)) for r in rows]


def main() -> int:
    args = parse_args()
    tf_s = TF_SECONDS[args.tf]
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    bar_cfg = BarrierConfig(take_atr=args.take, stop_atr=args.stop_atr,
                            max_bars=args.max_bars, atr_period=args.atr_period)
    zig_cfg = ZigzagConfig(min_move_atr=args.min_move, atr_period=args.atr_period)
    bar_hash = _cfg_hash({"kind": "barrier", "take": args.take, "stop": args.stop_atr,
                          "max_bars": args.max_bars, "atr": args.atr_period})
    zig_hash = _cfg_hash({"kind": "zigzag", "min_move": args.min_move, "atr": args.atr_period})
    eng = _sync_engine()
    t_warm = (datetime.fromisoformat(args.dfrom) - timedelta(days=args.warmup_days)).date().isoformat()
    figis = _resolve_figis(eng, args)
    if not figis:
        print("нет тикеров")
        return 1
    print(f"Разметка {args.dfrom}..{args.dto} · TF {args.tf} · бумаг {len(figis)} · "
          f"kinds={sorted(kinds)} · write={args.write}")
    t_all = time.time()
    tot_labels = tot_zig = 0
    label_stats = {-1: 0, 0: 0, 1: 0}
    if args.write:
        with eng.begin() as c:
            for stmt in DDL.strip().split(";"):
                if stmt.strip():
                    c.execute(text(stmt))
    for f, tk in figis:
        bars = _load_bars(eng, f, f"{t_warm} 00:00+00", f"{args.dto} 23:59:59+00")
        if len(bars) < 100:
            print(f"  {tk}: мало данных ({len(bars)} 1m) — пропуск")
            continue
        series = build_tf(bars, tf_s) if tf_s != 60 else bars
        if args.write:
            with eng.begin() as c:
                c.execute(text("DELETE FROM labels WHERE figi=:f AND tf_seconds=:tf AND cfg_hash=:h"),
                          {"f": f, "tf": tf_s, "h": bar_hash})
                c.execute(text("DELETE FROM ideal_trades WHERE figi=:f AND tf_seconds=:tf AND cfg_hash=:h"),
                          {"f": f, "tf": tf_s, "h": zig_hash})
                if "barrier" in kinds:
                    for r in triple_barrier(series, bar_cfg):
                        c.execute(text(
                            "INSERT INTO labels (figi, tf_seconds, cfg_hash, ts, label, mature, "
                            "t1_ts, t1_bars, upper, lower, entry_px, ret_atr) VALUES "
                            "(:f, :tf, :h, :ts, :l, :m, :t1, :tb, :up, :lo, :ep, :ra) "
                            "ON CONFLICT (figi, tf_seconds, cfg_hash, ts) DO UPDATE SET "
                            "label=EXCLUDED.label, mature=EXCLUDED.mature, t1_ts=EXCLUDED.t1_ts, "
                            "t1_bars=EXCLUDED.t1_bars, upper=EXCLUDED.upper, lower=EXCLUDED.lower, "
                            "entry_px=EXCLUDED.entry_px, ret_atr=EXCLUDED.ret_atr"),
                            {"f": f, "tf": tf_s, "h": bar_hash, "ts": r["ts"], "l": r["label"],
                             "m": r["mature"], "t1": r["t1_ts"] or None, "tb": r["t1_bars"],
                             "up": r["upper"], "lo": r["lower"], "ep": r["entry_px"], "ra": r["ret_atr"]})
                if "zigzag" in kinds:
                    for r in zigzag_trades(series, zig_cfg):
                        c.execute(text(
                            "INSERT INTO ideal_trades (figi, tf_seconds, cfg_hash, entry_ts, side, "
                            "exit_ts, entry_px, exit_px, bars, ret_pct, ret_atr) VALUES "
                            "(:f, :tf, :h, :e, :s, :x, :ep, :xp, :b, :rp, :ra) "
                            "ON CONFLICT (figi, tf_seconds, cfg_hash, entry_ts) DO UPDATE SET "
                            "side=EXCLUDED.side, exit_ts=EXCLUDED.exit_ts, entry_px=EXCLUDED.entry_px, "
                            "exit_px=EXCLUDED.exit_px, bars=EXCLUDED.bars, ret_pct=EXCLUDED.ret_pct, "
                            "ret_atr=EXCLUDED.ret_atr"),
                            {"f": f, "tf": tf_s, "h": zig_hash, "e": r["entry_ts"], "s": r["side"],
                             "x": r["exit_ts"], "ep": r["entry_px"], "xp": r["exit_px"],
                             "b": r["bars"], "rp": r["ret_pct"], "ra": r["ret_atr"]})
        if "barrier" in kinds:
            rows = triple_barrier(series, bar_cfg)
            for r in rows:
                label_stats[r["label"]] += 1
            tot_labels += len(rows)
        if "zigzag" in kinds:
            zt = zigzag_trades(series, zig_cfg)
            tot_zig += len(zt)
        zt_n = len(zigzag_trades(series, zig_cfg)) if "zigzag" in kinds else 0
        print(f"  {tk}: баров {len(series)} · меток {len(series)} · идеальных сделок {zt_n}")
    print(f"\nИТОГО: меток {tot_labels} (+1/0/−1 = {label_stats[1]}/{label_stats[0]}/{label_stats[-1]}) · "
          f"идеальных сделок {tot_zig} · {time.time() - t_all:.1f}s")
    if args.write:
        with eng.connect() as c:
            nl = c.execute(text("SELECT count(*) FROM labels WHERE cfg_hash=:h"), {"h": bar_hash}).scalar()
            nz = c.execute(text("SELECT count(*) FROM ideal_trades WHERE cfg_hash=:h"), {"h": zig_hash}).scalar()
        print(f"в БД: labels={nl} (cfg={bar_hash}) · ideal_trades={nz} (cfg={zig_hash})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
