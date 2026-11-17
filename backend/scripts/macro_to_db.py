"""Перенос макро-рядов (BRENT/GOLD/USDRUB/IMOEX) из CSV в candles.

Зачем: CSV-файлы в backend/data дважды склеены при докачке, поэтому штатная
загрузка исследования падает на «timestamps must strictly increase». Источником
истины должна быть база, как у остальных инструментов, а CSV — только выгрузкой.

Правила восстановления:
  * повтор одного ts с разными значениями = дописывание незакрытого бара; берём
    ПОСЛЕДНЮЮ версию (она актуальнее);
  * повтор с теми же значениями = дубль склейки, отбрасывается;
  * результат сортируется по времени;
  * OHLC проверяется на согласованность (иначе строка отклоняется и считается).

Макро-ряды пишутся в candles с interval=2 (5 минут) под своими идентификаторами.
В instruments они НЕ заводятся: иначе бот может включить их в торговый юниверс.
Так уже живёт индекс IMOEX (BBG00KDWPPW2) — он и используется для своего файла.

Запуск:
    python scripts/macro_to_db.py --dry-run
    python scripts/macro_to_db.py
"""
from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, text  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")

# имя фактора -> (файл, figi в candles)
FACTORS = {
    "BRENT": ("brent_5m.csv", "BRENT"),
    "GOLD": ("gold_5m.csv", "GOLD"),
    "USDRUB": ("usdrub_5m.csv", "USDRUB"),
    "IMOEX": ("imoex_5m.csv", "BBG00KDWPPW2"),
}
INTERVAL_5M = 2


def _engine():
    from app.config import get_settings

    s = get_settings()
    url = (f"postgresql://{s.postgres_user}:{s.postgres_password}"
           f"@{s.postgres_host}:{s.postgres_port}/{s.postgres_db}")
    return create_engine(url, pool_pre_ping=True)


def parse_csv(path: str) -> tuple[dict[datetime, tuple], dict[str, int]]:
    """Вернуть {ts: (open, high, low, close, volume)} и счётчики разбора."""
    stats = {"rows": 0, "duplicates": 0, "conflicts": 0, "bad_ohlc": 0, "reordered": 0}
    bar: dict[datetime, tuple] = {}
    prev: datetime | None = None
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            if not line.strip():
                continue
            fld = line.strip().rstrip(";").split(";")
            if len(fld) != 6:
                stats["bad_ohlc"] += 1
                continue
            stats["rows"] += 1
            ts = datetime.fromisoformat(fld[0].replace("Z", "+00:00")).astimezone(timezone.utc)
            high, low, opening, close, volume = (float(v) for v in fld[1:])
            if prev is not None and ts <= prev:
                stats["reordered"] += 1
            prev = ts
            if not all(math.isfinite(v) for v in (high, low, opening, close, volume)):
                stats["bad_ohlc"] += 1
                continue
            if not (0 < low <= min(opening, close) <= max(opening, close) <= high and volume >= 0):
                stats["bad_ohlc"] += 1
                continue
            values = (opening, high, low, close, int(volume))
            if ts in bar:
                if bar[ts] != values:
                    stats["conflicts"] += 1
                else:
                    stats["duplicates"] += 1
            bar[ts] = values  # последняя версия бара побеждает
    return bar, stats


def upsert(engine, figi: str, bar: dict[datetime, tuple], dry: bool) -> tuple[int, int]:
    items = sorted(bar.items())
    if dry:
        return len(items), 0
    written = 0
    with engine.begin() as db:
        for i in range(0, len(items), 1000):
            batch = [{"figi": figi, "iv": INTERVAL_5M, "ts": ts, "o": v[0], "h": v[1],
                      "l": v[2], "c": v[3], "v": v[4]} for ts, v in items[i:i + 1000]]
            db.execute(text("""
                INSERT INTO candles (figi, interval, ts, open, high, low, close, volume)
                VALUES (:figi, :iv, :ts, :o, :h, :l, :c, :v)
                ON CONFLICT (figi, interval, ts) DO UPDATE
                SET open=:o, high=:h, low=:l, close=:c, volume=:v
            """), batch)
            written += len(batch)
    return len(items), written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=DATA)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    engine = None if args.dry_run else _engine()
    total_unique = total_written = 0
    for name, (filename, figi) in FACTORS.items():
        path = os.path.join(args.data_dir, filename)
        if not os.path.exists(path):
            print(f"{name}: НЕТ файла {path}")
            continue
        bar, stats = parse_csv(path)
        unique, written = upsert(engine, figi, bar, args.dry_run)
        total_unique += unique
        total_written += written
        first, last = (min(bar), max(bar)) if bar else (None, None)
        print(f"{name} -> {figi}/interval={INTERVAL_5M}: строк {stats['rows']}, "
              f"уникальных {unique}, дублей {stats['duplicates']}, "
              f"обновлений бара {stats['conflicts']}, отброшено OHLC {stats['bad_ohlc']}, "
              f"нарушений порядка {stats['reordered']}, записано {written}, "
              f"диапазон {first} .. {last}")
    print(f"итого: уникальных {total_unique}, записано {total_written}"
          + (" (dry-run, база не менялась)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
