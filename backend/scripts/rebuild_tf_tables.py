#!/usr/bin/env python3
"""Пересборка ТФ-таблиц БД на каноническую START-сетку (T-Invest-parity, REF-001b).

Для каждой (figi, interval) с существующими ТФ-строками: взять min/max ts, удалить
старые строки (исторически END-сетка) и пересобрать из 1m через
`candle_cache.resample_from_1m` (канон START; доказано против T-Invest 102/102).

Запуск:
  .venv/bin/python scripts/rebuild_tf_tables.py --dry
  .venv/bin/python scripts/rebuild_tf_tables.py --intervals 10min,hour
  .venv/bin/python scripts/rebuild_tf_tables.py            # всё (долго)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services.candle_cache import resample_from_1m  # noqa: E402

VALUE2NAME = {2: "5min", 3: "15min", 4: "hour", 5: "day", 8: "10min",
              9: "30min", 10: "2h", 11: "4h", 12: "week", 13: "month",
              24: "day"}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry", action="store_true", help="только план, без изменений")
    p.add_argument("--intervals", default="", help="имена ТФ через запятую (пусто = все известные)")
    p.add_argument("--figi", default="", help="один figi (для отладки)")
    p.add_argument("--from", dest="dfrom", default="", help="начало окна YYYY-MM-DD")
    p.add_argument("--to", dest="dto", default="", help="конец окна YYYY-MM-DD (не включая следующий день)")
    return p.parse_args()


async def main() -> int:
    a = parse_args()
    only = {x.strip() for x in a.intervals.split(",") if x.strip()}
    win_from = datetime.fromisoformat(a.dfrom).replace(tzinfo=timezone.utc) if a.dfrom else None
    win_to = (datetime.fromisoformat(a.dto).replace(tzinfo=timezone.utc) + timedelta(days=1)) if a.dto else None
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT figi, interval, count(*), min(ts), max(ts) FROM candles "
            "WHERE interval <> 1 GROUP BY figi, interval ORDER BY figi, interval"))).all()
        groups = 0
        done = 0
        for figi, ival, cnt, mn, mx in rows:
            name = VALUE2NAME.get(int(ival))
            if not name:
                print(f"skip {figi} interval={ival} ({cnt} rows) — нет канонического имени")
                continue
            if only and name not in only:
                continue
            if a.figi and figi != a.figi:
                continue
            span_from = max(mn, win_from) if win_from else mn
            span_to = min(mx, win_to) if win_to else mx
            if span_from > span_to:
                continue
            groups += 1
            print(f"{figi} {name}[{ival}]: {cnt} строк {mn:%Y-%m-%d}..{mx:%Y-%m-%d} "
                  f"→ окно {span_from:%Y-%m-%d}..{span_to:%Y-%m-%d}")
            if a.dry:
                continue
            q = "DELETE FROM candles WHERE figi=:f AND interval=:i"
            params = {"f": figi, "i": int(ival)}
            if win_from is not None and win_to is not None:
                q += " AND ts >= :a AND ts < :b"
                params |= {"a": win_from, "b": win_to}
            await db.execute(text(q), params)
            n = await resample_from_1m(db, figi, name, span_from, span_to)
            await db.commit()
            done += 1
            print(f"   → пересобрано {n}")
        print(f"\nгрупп: {groups} · пересобрано: {done}{' (dry)' if a.dry else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
