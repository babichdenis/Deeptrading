"""Скрининг движений: топ-ростов/падений за 2-3 месяца, неделю и вчера.

По дневным барам (interval=24). Вчерашний день = последний ЗАВЕРШЁННЫЙ день
(сегодняшний частичный бар исключается).

Запуск: python scripts/screen_movers.py [--top 8] [--universe] (по умолчанию — вся TQBR,
        что есть в БД дневными барами)
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def load():
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    rows = await c.fetch("""
        SELECT c.figi, i.ticker, c.ts, c.close
        FROM candles c JOIN instruments i ON i.figi = c.figi
        WHERE c.interval = 24 AND i.class_code = 'TQBR'
        ORDER BY c.figi, c.ts""")
    await c.close()
    by: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    for r in rows:
        f = str(r["figi"])
        names[f] = str(r["ticker"])
        by[f].append((r["ts"], float(r["close"] or 0)))
    return by, names


def horizon(by, names, days: int, top: int) -> None:
    msk = ZoneInfo("Europe/Moscow")
    today = datetime.now(timezone.utc).astimezone(msk).date()
    out = []
    for f, bars in by.items():
        # исключаем сегодняшний (частичный) бар
        bars = [b for b in bars if b[0].astimezone(msk).date() != today]
        if len(bars) < days + 1:
            continue
        now, old = bars[-1][1], bars[-days - 1][1]
        if old <= 0:
            continue
        out.append((names[f], round((now / old - 1) * 100, 1), round(now, 2)))
    out.sort(key=lambda x: -x[1])
    n = len(out)
    up = [x for x in out if x[1] > 0]
    dn = [x for x in out if x[1] < 0]
    label = {1: "вчера (1 день)", 5: "неделя (5 дней)", 21: "месяц (21 день)",
             63: "3 месяца (63 дня)"}.get(days, f"{days} дней")
    print(f"\n=== {label} | тикеров {n} | рост {len(up)} / падение {len(dn)}")
    print(f"  топ-{top} рост: " + ", ".join(f"{t} {ch:+.1f}%" for t, ch, _ in out[:top]))
    print(f"  топ-{top} падение: " + ", ".join(f"{t} {ch:+.1f}%" for t, ch, _ in out[-top:][::-1]))
    for thr in (5, 10, 20, 40):
        nu = sum(1 for x in out if x[1] >= thr)
        nd = sum(1 for x in out if x[1] <= -thr)
        print(f"  |Δ| ≥ {thr:>2}%:  рост {nu:>4} | падение {nd:>4}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()
    by, names = asyncio.run(load())
    print(f"тикеров с дневными барами: {len(by)}")
    for d in (1, 5, 21, 63):
        horizon(by, names, d, args.top)


if __name__ == "__main__":
    main()
