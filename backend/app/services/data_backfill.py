"""Точечное восполнение 1m-свечей в БД для набора инструментов.

MOEX ISS (параллельно) -> fallback T-Invest -> upsert в таблицу candles.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.services.macro_candle_source import MacroInstrument, read_closed_macro_candles

MSK = ZoneInfo("Europe/Moscow")

def _candidate_days(want_from: datetime, want_to: datetime) -> list[datetime]:
    if any(at.utcoffset() is None for at in (want_from, want_to)) or want_from > want_to:
        raise ValueError("Expected ordered timezone-aware coverage bounds")
    start = want_from.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    end = want_to.astimezone(MSK).replace(hour=0, minute=0, second=0, microsecond=0)
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _moex_candles(ticker: str, d: str) -> list[dict]:
    start = datetime.fromisoformat(d).replace(tzinfo=MSK)
    end = min(start + timedelta(days=1), datetime.now(timezone.utc))
    instrument = MacroInstrument("stock", "shares", "TQBR", ticker)
    return read_closed_macro_candles(instrument, start, end) if start < end else []


def _fetch_day_moex(ticker: str, day_msk: datetime) -> list[dict]:
    return _moex_candles(ticker, day_msk.strftime("%Y-%m-%d"))


async def _write_moex_day(figi: str, day_msk: datetime, rows: list[dict]) -> int:
    from app.database import SessionLocal
    from app.services.tinvest import upsert_candles_detailed

    if not rows:
        return 0
    start = day_msk.astimezone(timezone.utc)
    end = (day_msk + timedelta(days=1)).astimezone(timezone.utc)
    async with SessionLocal() as db:
        outcome = await upsert_candles_detailed(
            db, [dict(row, figi=figi, interval=1) for row in rows], window=(start, end)
        )
    return outcome.accepted


async def _backfill_day_tinvest(figi: str, day_msk: datetime) -> int:
    from app.services.tinvest import CandleInterval, fetch_candles
    from app.services.candle_cache import upsert_candles
    from app.database import SessionLocal

    day_start_utc = day_msk.astimezone(timezone.utc)
    day_end_utc = (day_msk + timedelta(days=1)).astimezone(timezone.utc)

    rows = await asyncio.to_thread(
        fetch_candles, figi, CandleInterval.CANDLE_INTERVAL_1_MIN,
        day_start_utc, day_end_utc,
    )
    if not rows:
        return 0
    async with SessionLocal() as db:
        return await upsert_candles(db, rows)


async def _candle_counts_by_day(figi: str, want_from: datetime, want_to: datetime) -> dict:
    from app.database import SessionLocal

    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT (ts AT TIME ZONE 'Europe/Moscow')::date AS day, count(*) AS candles FROM candles "
            "WHERE figi=:f AND interval=1 AND ts>=:a AND ts<=:b GROUP BY 1 ORDER BY 1"
        ), {"f": figi, "a": want_from, "b": want_to})).all()
    return {day: candles for day, candles in rows}


async def run_backfill(
    instruments: list[tuple[str, str]],
    days: int = 35,
    workers: int = 4,
    log=print,
) -> dict:
    now = datetime.now(timezone.utc)
    want_from = now - timedelta(days=days)
    want_to = now
    candidate_days = _candidate_days(want_from, want_to)

    report = {"instruments": len(instruments), "days_expected": len(candidate_days), "coverage_basis": "calendar_day_presence"}
    total_added = 0
    for figi, ticker in instruments:
        try:
            have = await _candle_counts_by_day(figi, want_from, want_to)
        except Exception as e:
            report.setdefault("errors", []).append(f"{figi}: coverage {e}")
            continue
        report.setdefault("initial_minutes_by_day", {})[figi] = {
            day.date().isoformat(): have.get(day.date(), 0) for day in candidate_days
        }
        missing = [d for d in candidate_days if d.date() not in have]
        if not missing:
            report.setdefault("complete", []).append(figi)
            continue
        log(f"[{figi}] {ticker or '?'} missing {len(missing)} days: "
            f"{missing[0]:%d.%m}..{missing[-1]:%d.%m}")
        added_instr = 0
        sources = {"moex": 0, "tinvest": 0, "empty": 0}
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(missing)))) as pool:
            moex_days = await asyncio.gather(
                *(loop.run_in_executor(pool, _fetch_day_moex, ticker, day) for day in missing),
                return_exceptions=True,
            )
        for day, rows in zip(missing, moex_days, strict=True):
            errors = {}
            if isinstance(rows, Exception):
                errors["moex"] = type(rows).__name__
                rows = []
            ma = await _write_moex_day(figi, day, rows)
            if ma > 0:
                sources["moex"] += 1
                added_instr += ma
                continue
            try:
                ta = await _backfill_day_tinvest(figi, day)
            except Exception as error:
                errors["tinvest"] = type(error).__name__
                ta = 0
            if errors:
                report.setdefault("source_errors", []).append(
                    {"figi": figi, "day": day.date().isoformat(), "sources": errors}
                )
            if ta > 0:
                sources["tinvest"] += 1
                added_instr += ta
            elif errors:
                sources["error"] = sources.get("error", 0) + 1
                log(f"    {day:%d.%m} — сбой источника свечей")
            else:
                sources["empty"] += 1
                log(f"    {day:%d.%m} — пусто в обоих источниках")
        report.setdefault("by_figi", {})[figi] = {
            "missing_days": len(missing), "added": added_instr, "sources": sources,
        }
        total_added += added_instr
        log(f"    added {added_instr} bars")
    report["total_added"] = total_added
    return report
