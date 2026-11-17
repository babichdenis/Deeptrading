#!/usr/bin/env python3
"""Сборка манифеста невалидных данных по фактическим нарушениям OHLC в БД.

Манифест фиксирует диапазоны, где минутный ряд (и построенные из него ТФ) записан
в других единицах и с переставленными полями: цена меньше биржевой примерно в 4.27
раза, объём больше примерно в 200 раз, минимум и закрытие перепутаны.

Запуск:
  .venv/Scripts/python.exe scripts/audit_data_invalidity.py --write
  .venv/Scripts/python.exe scripts/audit_data_invalidity.py          # только показать
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MANIFEST = Path(__file__).resolve().parents[1] / "configs" / "data_invalidity.json"
BAD = "high < low OR high < GREATEST(open,close) OR low > LEAST(open,close)"


def collect() -> dict:
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        rows = c.execute(text(
            "SELECT figi, interval, min(ts), max(ts), count(*) FROM candles "
            f"WHERE {BAD} GROUP BY figi, interval ORDER BY interval, figi")).all()
        tickers = dict(c.execute(text("SELECT figi, ticker FROM instruments")).all())
    ranges = []
    for figi, ival, mn, mx, n in rows:
        ranges.append({
            "figi": figi,
            "ticker": tickers.get(figi),
            "interval": int(ival),
            "ts_from": mn.isoformat(),
            "ts_to": mx.isoformat(),
            "bad_bars": int(n),
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "reason": "units_mismatch_and_low_close_swap",
        "note": (
            "Цена меньше биржевой (ISS) примерно в 4.27 раза, объём больше примерно в 200 раз, "
            "low и close перепутаны. Перестановкой полей не чинится: единицы остаются неверными."
        ),
        "ranges": ranges,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    blob = collect()
    ranges = blob["ranges"]
    by_interval: dict[int, int] = {}
    for r in ranges:
        by_interval[r["interval"]] = by_interval.get(r["interval"], 0) + 1
    print(f"пар (figi, interval) с нарушениями: {len(ranges)}")
    for ival in sorted(by_interval):
        print(f"  interval={ival}: {by_interval[ival]} бумаг")
    print(f"битых баров всего: {sum(r['bad_bars'] for r in ranges)}")
    widest = max(ranges, key=lambda r: r["bad_bars"]) if ranges else None
    if widest:
        print(f"самая тяжёлая: {widest['ticker']} interval={widest['interval']} "
              f"{widest['bad_bars']} баров {widest['ts_from'][:10]}..{widest['ts_to'][:10]}")
    if a.write:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        MANIFEST.write_text(json.dumps(blob, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"записано: {MANIFEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
