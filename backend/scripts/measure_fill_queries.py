"""Замер числа DB-запросов и объёма pre-SELECT при fill_gaps_detailed.

Синтетический пакет: несколько FIGI, редкие timestamp (дыры разной ширины).
Считает SQL-запросы через событийный слушатель SQLAlchemy.
Результат — JSON-артефакт (не изменяет код, только измерение).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.candle import Candle as DBCandle
from app.services import candle_hub_orchestrator as cho
from app.services import tinvest

_UTC = timezone.utc
FIGIS = ["MSRFIGI0000000A", "MSRFIGI0000000B", "MSRFIGI0000000C"]
BASE = datetime(2026, 10, 5, tzinfo=_UTC).replace(hour=12, minute=0)


def _row(figi: str, ts: datetime, o: float = 100.0) -> dict:
    return {
        "figi": figi, "ts": ts, "interval": 1,
        "open": o, "high": o + 0.1, "low": o - 0.1, "close": o + 0.05,
        "volume": 1000.0,
    }


async def main() -> None:
    url = "postgresql+asyncpg://deeptrading:deeptrading@127.0.0.1:5432/deeptrading_b1_test"
    engine = create_async_engine(url)
    stats = {"queries": 0, "selects": 0}

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        stats["queries"] += 1
        if statement.lstrip().upper().startswith("SELECT"):
            stats["selects"] += 1

    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # чистка + seed: три FIGI, у каждого дыры разной ширины (редкие timestamp)
    async with Session() as db:
        await db.execute(DBCandle.__table__.delete().where(DBCandle.figi.in_(FIGIS)))
        await db.commit()

        for figi, gaps in zip(FIGIS, [[1, 2, 3], [5], [1, 4, 7, 9]]):
            present = []
            for m in range(0, 12):
                if m not in gaps:
                    present.append(_row(figi, BASE + timedelta(minutes=m)))
            await tinvest.upsert_candles(db, present)
            await db.commit()
            # gap_report читает БД (pre-SELECT) — засеиваем hub из БД
            orch = cho.CandleHubOrchestrator()
            await orch.seed_from_db(db, figi)

    # замер: fill_gaps_detailed для каждого FIGI, источник возвращает дыры
    async with Session() as db:
        results = {}
        for figi, gaps in zip(FIGIS, [[1, 2, 3], [5], [1, 4, 7, 9]]):
            orch = cho.CandleHubOrchestrator()
            await orch.seed_from_db(db, figi)
            stats["queries"] = 0

            def fetch(figi_, interval, a, b, _figi=figi, _gaps=gaps):  # noqa: ANN001
                lo = int((a - BASE).total_seconds() // 60)
                hi = int((b - BASE).total_seconds() // 60)
                return [_row(_figi, BASE + timedelta(minutes=m), o=101.0)
                        for m in range(lo, hi + 1) if m in _gaps]

            cho_orig = cho.fetch_candles
            cho.fetch_candles = fetch
            try:
                r = await orch.fill_gaps_detailed(db, figi)
            finally:
                cho.fetch_candles = cho_orig
            # фиксируем счётчик ДО commit и контрольного count-запроса (не артефакт)
            queries_fill = stats["queries"]
            selects_fill = stats["selects"]
            await db.commit()

            n_db = int((await db.execute(
                select(func.count()).select_from(DBCandle)
                .where(DBCandle.figi == figi))).scalar_one())
            results[figi] = {
                "gaps_expected": len(gaps),
                "committed": r.committed,
                "fully_filled": r.fully_filled,
                "queries_during_fill": queries_fill,
                "selects_during_fill": selects_fill,
                "rows_in_db": n_db,
                "bars_in_hub": len(orch.hub.series(figi, "1min")),
            }

    out = {
        "note": "синтетический пакет: 3 FIGI, дыры [3,1,4] минуты; редкие timestamp",
        "per_figi": results,
        "total_queries_all_fills": sum(v["queries_during_fill"] for v in results.values()),
    }
    dest = Path("reports/architect_20261011/code_first_r1/b1/query_measurement.json")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
