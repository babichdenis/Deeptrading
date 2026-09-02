"""Обучение ML meta-filter прямо из CSV-архивов T-Invest (без БД).

Использование:
    python scripts/ml_train_csv.py --csv-dir /tmp/hist --extra-dir /tmp/hist/2025 \
        --train-from 2025-08-01 --train-to 2026-05-31 \
        --val-from 2026-06-01 --val-to 2026-06-30 \
        --oos-from 2026-07-01 --oos-to 2026-08-25

Читает CSV (uid;ts;open;close;high;low;volume;) прямо из файлов,
строит candidates (raw/quorum/entry) через ансамбль, размечает сделки движком
(ATR Stop + intrabar exit + costs), обучает LogisticRegression и печатает отчёт.
Oracle используется только как диагностика (coverage), в X не попадает.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.models import Candle as EngineCandle  # noqa: E402
from app.services.ml_meta import UID_FIGI_ALIASES, build_dataset, train_meta_filter  # noqa: E402


def load_dir(csv_dir: str, figis: set[str] | None = None,
             t_from: datetime | None = None, t_to: datetime | None = None) -> dict[str, list[EngineCandle]]:
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    out: dict[str, list[EngineCandle]] = {}
    for path in files:
        uid = os.path.basename(path).split("_")[0]
        figi = UID_FIGI_ALIASES.get(uid)
        if not figi or (figis and figi not in figis):
            continue
        with open(path) as f:
            for line in f:
                parts = line.strip().rstrip(";").split(";")
                if len(parts) < 7:
                    continue
                _, ts, o, c, h, l, v = parts[:7]
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if t_from and ts_dt < t_from:
                    continue
                if t_to and ts_dt > t_to:
                    continue
                try:
                    out.setdefault(figi, []).append(EngineCandle(
                        ts=ts_dt, open=float(o), high=float(h),
                        low=float(l), close=float(c), volume=float(v)))
                except ValueError:
                    continue
    for figi in out:
        out[figi].sort(key=lambda c: c.ts)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/tmp/hist")
    ap.add_argument("--extra-dir", default=None, help="вторая папка (например 2025/)")
    ap.add_argument("--figis", default="", help="через запятую; пусто = все 5")
    ap.add_argument("--train-from", default="2025-08-01")
    ap.add_argument("--train-to", default="2026-05-31")
    ap.add_argument("--val-from", default="2026-06-01")
    ap.add_argument("--val-to", default="2026-06-30")
    ap.add_argument("--oos-from", default="2026-07-01")
    ap.add_argument("--oos-to", default="2026-08-25")
    ap.add_argument("--max-hold-bars", type=int, default=60)
    ap.add_argument("--purge-bars", type=int, default=60)
    ap.add_argument("--out-json", default=None, help="сохранить отчёт в файл")
    args = ap.parse_args()

    figis = set(args.figis.split(",")) if args.figis else None
    t0 = time.time()
    print("Читаю CSV...", flush=True)
    candles = load_dir(args.csv_dir, figis, datetime.fromisoformat(args.train_from).replace(tzinfo=timezone.utc),
                       datetime.fromisoformat(args.oos_to).replace(tzinfo=timezone.utc))
    if args.extra_dir:
        extra = load_dir(args.extra_dir, figis, datetime.fromisoformat(args.train_from).replace(tzinfo=timezone.utc),
                         datetime.fromisoformat(args.oos_to).replace(tzinfo=timezone.utc))
        for figi, cs in extra.items():
            candles.setdefault(figi, []).extend(cs)
        for figi in candles:
            candles[figi].sort(key=lambda c: c.ts)
    print(f"Свечей по FIGI: { {k: len(v) for k, v in candles.items()} } ({time.time()-t0:.0f}с)", flush=True)

    print("Строю датасет кандидатов...", flush=True)
    t1 = time.time()
    rows, meta = build_dataset(candles, None, args.max_hold_bars)
    print(f"Датасет: {meta['total']} строк {meta['by_stage']} ({time.time()-t1:.0f}с)", flush=True)
    if meta["total"] < 200:
        print("Слишком мало кандидатов — аборт")
        sys.exit(1)

    print("Обучаю (walk-forward train/val/OOS)...", flush=True)
    t2 = time.time()
    res = train_meta_filter(
        rows,
        datetime.fromisoformat(args.train_from).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(args.train_to).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(args.val_from).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(args.val_to).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(args.oos_from).replace(tzinfo=timezone.utc),
        datetime.fromisoformat(args.oos_to).replace(tzinfo=timezone.utc),
        purge_bars=args.purge_bars,
    )
    print(f"Обучение: {time.time()-t2:.0f}с", flush=True)
    res["dataset"] = meta

    if "error" in res:
        print("ОШИБКА:", res["error"])
        sys.exit(1)

    import json
    report = {k: v for k, v in res.items() if k != "candidate_counts" or True}
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    if args.out_json:
        with open(args.out_json, "w") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)
        print(f"Отчёт сохранён: {args.out_json}")


if __name__ == "__main__":
    main()
