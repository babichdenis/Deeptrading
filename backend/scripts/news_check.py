#!/usr/bin/env python3
"""Проверка получения новостей (MOEX ISS + RSS) и фильтра по тикерам.

Запуск (на .4, из backend):
    .venv/bin/python3 scripts/news_check.py                      # все источники, топ-30
    .venv/bin/python3 scripts/news_check.py --tickers SBER,GAZP  # только упоминания
    .venv/bin/python3 scripts/news_check.py --source MOEX --limit 10
    .venv/bin/python3 scripts/news_check.py --universe           # тикеры из universe (БД)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.news import (  # noqa: E402
    RSS_SOURCES, fetch_news, filter_by_tickers, news_payload,
)


def _universe_tickers() -> list[str]:
    try:
        import asyncio
        from sqlalchemy import text
        from app.database import SessionLocal

        async def _q():
            async with SessionLocal() as db:
                rows = (await db.execute(text(
                    "SELECT ticker FROM universe WHERE eligible_tier = 'eligible'"
                ))).scalars().all()
            return [str(x) for x in rows]
        return asyncio.run(_q())
    except Exception:
        return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="", help="список через запятую: SBER,GAZP")
    ap.add_argument("--universe", action="store_true", help="взять тикеры из universe (БД)")
    ap.add_argument("--source", default="", help="MOEX | Ведомости | Коммерсант | Интерфакс | ТАСС")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()

    items = fetch_news()
    by_src: dict[str, int] = {}
    for x in items:
        by_src[x.source] = by_src.get(x.source, 0) + 1
    print(f"всего новостей: {len(items)} · источники: {by_src}")

    if args.source:
        _s = args.source.strip().lower()
        items = [x for x in items if x.source.lower() == _s]

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if args.universe and not tickers:
        tickers = _universe_tickers()
    if tickers:
        items = filter_by_tickers(items, tickers)
        print(f"фильтр по {tickers}: {len(items)}")

    for x in news_payload(items[:max(1, args.limit)]):
        tags = (" [" + ",".join(x["tickers"]) + "]") if x["tickers"] else ""
        print(f"[{x['source']}] {x['ts']} · {x['title'][:110]}{tags}")
        if x["url"]:
            print(f"    {x['url']}")


if __name__ == "__main__":
    main()
