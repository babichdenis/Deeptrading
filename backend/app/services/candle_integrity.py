"""Целостность свечей: дневные контрольные суммы, breadth-полнота, докачка.

См. DECISIONS.md 2026-10-01 20:30. Каноническая строка:
    epoch|open|high|low|close|volume   (numeric(20,8)::text, ts — unix-секунды)
Контрольная сумма группы (figi, interval, day_msk): count/min/max + пара md5:
    hash_xor = bit_xor(первых 64 бит md5), hash_sum = sum(вторых 64 бит, signed).
Период = композиция дневных строк (xor/sum композиционны).
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_MSK = ZoneInfo("Europe/Moscow")


# --------------------------------------------------------------------- referencia
def canonical_line(ts: datetime, open_: Any, high: Any, low: Any, close: Any, volume: Any) -> str:
    """Python-референс канонической строки (совпадает с SQL символ-в-символ)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch = int(ts.timestamp())
    nums = [str(Decimal(str(x)).quantize(Decimal("0.00000001")))
            for x in (open_, high, low, close, volume)]
    return "|".join([str(epoch)] + nums)


def md5_pair(line: str) -> tuple[int, int]:
    """(xor-часть, sum-часть): 64-битные half'ы md5 как signed bigint (bit(64)::bigint в PG)."""
    m = hashlib.md5(line.encode("utf-8")).hexdigest()

    def _signed(hex16: str) -> int:
        v = int(hex16, 16)
        return v - (1 << 64) if v >= (1 << 63) else v

    return _signed(m[:16]), _signed(m[16:])


def aggregate_rows(rows: list[tuple]) -> dict:
    """Python-референс агрегата по строкам (ts, o, h, l, c, v) одной группы."""
    if not rows:
        return {"rows": 0, "min_ts": None, "max_ts": None,
                "hash_xor": 0, "hash_sum": 0, "volume_sum": Decimal(0)}
    xor_acc = 0
    sum_acc = 0
    vol = Decimal(0)
    ts_min = ts_max = None
    for ts, o, h, low, c, v in rows:
        x, s = md5_pair(canonical_line(ts, o, h, low, c, v))
        xor_acc ^= (x & ((1 << 64) - 1))
        sum_acc += s
        vol += Decimal(str(v))
        ts_min = ts if ts_min is None or ts < ts_min else ts_min
        ts_max = ts if ts_max is None or ts > ts_max else ts_max
    return {"rows": len(rows), "min_ts": ts_min, "max_ts": ts_max,
            "hash_xor": xor_acc, "hash_sum": sum_acc, "volume_sum": vol}


def is_trading_minute(ts: datetime) -> bool:
    """06:50–23:50 МСК минус клиринг 18:45–19:05; без weekday-вето (суббота торгуется)."""
    m = ts.astimezone(_MSK)
    mins = m.hour * 60 + m.minute
    if not (6 * 60 + 50 <= mins < 23 * 60 + 50):
        return False
    return not (18 * 60 + 45 <= mins < 19 * 60 + 5)


def classify_status(cur: dict | None, base: dict | None, median_rows: int,
                    missing_active: int) -> str:
    """OK | DEFICIT_DAY | DEFICIT_INTRADAY | MISMATCH | NO_BASELINE."""
    if not cur or int(cur.get("rows", 0)) == 0:
        return "DEFICIT_DAY"
    if median_rows > 0 and int(cur["rows"]) < 0.5 * median_rows:
        return "DEFICIT_DAY"
    if missing_active > 0:
        return "DEFICIT_INTRADAY"
    if base is None:
        return "NO_BASELINE"
    same = (int(cur["hash_xor"]) == int(base["hash_xor"])
            and int(cur["hash_sum"]) == int(base["hash_sum"])
            and Decimal(str(cur["volume_sum"])) == Decimal(str(base["volume_sum"]))
            and cur["min_ts"] == base["min_ts"] and cur["max_ts"] == base["max_ts"])
    return "OK" if same else "MISMATCH"


# ------------------------------------------------------------------------- SQL
_DAY_SQL = text(
    "WITH base AS ("
    "  SELECT (c.ts AT TIME ZONE 'Europe/Moscow')::date AS day, c.ts, c.volume,"
    "         md5((extract(epoch FROM c.ts)::bigint)::text || '|' ||"
    "             (c.open::numeric(20,8))::text  || '|' || (c.high::numeric(20,8))::text || '|' ||"
    "             (c.low::numeric(20,8))::text   || '|' || (c.close::numeric(20,8))::text || '|' ||"
    "             (c.volume::numeric(20,8))::text) AS m"
    "  FROM candles c"
    "  WHERE c.figi = :figi AND c.interval = :ival AND c.ts >= :a AND c.ts < :b"
    ")"
    " SELECT day, count(*) AS rows, min(ts) AS min_ts, max(ts) AS max_ts,"
    "        bit_xor(('x'||substr(m,1,16))::bit(64)::bigint)::text  AS hash_xor,"
    "        sum(('x'||substr(m,17,16))::bit(64)::bigint)::text     AS hash_sum,"
    "        sum(volume)::text                                       AS volume_sum"
    " FROM base GROUP BY day ORDER BY day"
)

_UPSERT_SQL = text(
    "INSERT INTO candle_integrity (figi, interval, day, rows, min_ts, max_ts, hash_xor, "
    "hash_sum, volume_sum, computed_at, checked_at, missing_active) VALUES "
    "(:figi, :ival, :day, :rows, :min_ts, :max_ts, CAST(:hash_xor AS BIGINT), "
    "CAST(:hash_sum AS NUMERIC), CAST(:volume_sum AS NUMERIC), now(), now(), :missing) "
    "ON CONFLICT (figi, interval, day) DO UPDATE SET rows=EXCLUDED.rows, "
    "min_ts=EXCLUDED.min_ts, max_ts=EXCLUDED.max_ts, hash_xor=EXCLUDED.hash_xor, "
    "hash_sum=EXCLUDED.hash_sum, volume_sum=EXCLUDED.volume_sum, "
    "computed_at=now(), checked_at=now(), missing_active=EXCLUDED.missing_active"
)


async def compute_day_aggregates(db: AsyncSession, figi: str, interval: int,
                                 dfrom: datetime, dto: datetime) -> dict[str, dict]:
    rows = (await db.execute(_DAY_SQL, {"figi": figi, "ival": interval, "a": dfrom, "b": dto})).all()
    return {r.day.isoformat(): {
        "rows": int(r.rows), "min_ts": r.min_ts, "max_ts": r.max_ts,
        "hash_xor": str(r.hash_xor), "hash_sum": str(r.hash_sum), "volume_sum": str(r.volume_sum),
    } for r in rows}


async def breadth_active_minutes(db: AsyncSession, figis: list[str], interval: int,
                                 dfrom: datetime, dto: datetime) -> set[datetime]:
    """Минуты, где бар есть у >= max(3, 20%) фиг из figis (активность рынка)."""
    if not figis:
        return set()
    k = max(3, len(figis) // 5)
    rows = (await db.execute(text(
        "SELECT ts FROM candles WHERE interval=:ival AND ts >= :a AND ts < :b "
        "AND figi = ANY(:figis) GROUP BY ts HAVING count(DISTINCT figi) >= :k"
    ), {"ival": interval, "a": dfrom, "b": dto, "figis": figis, "k": k})).scalars().all()
    return set(rows)


async def present_minutes(db: AsyncSession, figi: str, interval: int,
                          dfrom: datetime, dto: datetime) -> set[datetime]:
    rows = (await db.execute(text(
        "SELECT ts FROM candles WHERE figi=:figi AND interval=:ival AND ts >= :a AND ts < :b"
    ), {"figi": figi, "ival": interval, "a": dfrom, "b": dto})).scalars().all()
    return set(rows)


# ---------------------------------------------------------------------- операции
async def baseline(db: AsyncSession, figis: list[str], interval: int,
                   dfrom: datetime, dto: datetime) -> int:
    written = 0
    for figi in figis:
        agg = await compute_day_aggregates(db, figi, interval, dfrom, dto)
        for day, a in agg.items():
            await db.execute(_UPSERT_SQL, {
                "figi": figi, "ival": interval, "day": datetime.fromisoformat(day), "rows": a["rows"],
                "min_ts": a["min_ts"], "max_ts": a["max_ts"], "hash_xor": int(a["hash_xor"]),
                "hash_sum": int(a["hash_sum"]), "volume_sum": Decimal(a["volume_sum"]), "missing": None,
            })
            written += 1
    await db.commit()
    return written


async def load_baseline(db: AsyncSession, figis: list[str], interval: int,
                        dfrom: datetime, dto: datetime) -> dict[tuple[str, str], dict]:
    rows = (await db.execute(text(
        "SELECT figi, to_char(day, 'YYYY-MM-DD') AS day, rows, min_ts, max_ts, hash_xor::text, "
        "hash_sum::text, volume_sum::text FROM candle_integrity "
        "WHERE interval=:ival AND day >= :a AND day < :b AND figi = ANY(:figis)"
    ), {"ival": interval, "a": dfrom.replace(tzinfo=None), "b": dto.replace(tzinfo=None), "figis": figis})).all()
    return {(r.figi, r.day): {
        "rows": int(r.rows), "min_ts": r.min_ts, "max_ts": r.max_ts,
        "hash_xor": r.hash_xor, "hash_sum": r.hash_sum, "volume_sum": r.volume_sum,
    } for r in rows}


async def check(db: AsyncSession, figis: list[str], interval: int,
                dfrom: datetime, dto: datetime, *, auto_baseline: bool = True) -> list[dict]:
    """Сверка текущих контрольных сумм с baseline + breadth-полнота.

    Возвращает [{figi, day, status, rows, missing_active}] по проблемным и NO_BASELINE.
    """
    active = await breadth_active_minutes(db, figis, interval, dfrom, dto)
    # Активные минуты вне торгового окна (клиринг/ночь/угол сессии) не считаем дырами.
    active = {ts for ts in active if is_trading_minute(ts)}
    active_by_day: dict[str, set[datetime]] = {}
    for ts in active:
        active_by_day.setdefault(ts.astimezone(_MSK).date().isoformat(), set()).add(ts)
    base = await load_baseline(db, figis, interval, dfrom, dto)
    out: list[dict] = []
    for figi in figis:
        agg = await compute_day_aggregates(db, figi, interval, dfrom, dto)
        present = await present_minutes(db, figi, interval, dfrom, dto)
        counts = sorted(int(a["rows"]) for a in agg.values())
        median_rows = counts[len(counts) // 2] if counts else 0
        days = sorted(set(agg) | {d for (f, d) in base if f == figi} | set(active_by_day))
        for day in days:
            cur = agg.get(day)
            day_active = active_by_day.get(day, set())
            missing = len(day_active - present) if cur else len(day_active)
            # Допуск: единичные минуты без сделок/на границе — не дыра (< max(3, 1%)).
            if missing < max(3, len(day_active) // 100):
                missing = 0
            base_day = base.get((figi, day))
            st = classify_status(cur, base_day, median_rows, missing)
            if st == "NO_BASELINE" and auto_baseline and cur is not None:
                await db.execute(_UPSERT_SQL, {
                    "figi": figi, "ival": interval, "day": datetime.fromisoformat(day), "rows": cur["rows"],
                    "min_ts": cur["min_ts"], "max_ts": cur["max_ts"], "hash_xor": int(cur["hash_xor"]),
                    "hash_sum": int(cur["hash_sum"]), "volume_sum": Decimal(cur["volume_sum"]),
                    "missing": missing or None,
                })
            if st != "OK":
                out.append({"figi": figi, "day": day, "status": st,
                            "rows": int(cur["rows"]) if cur else 0, "missing_active": missing})
    if auto_baseline:
        await db.commit()
    return out


async def repair(db: AsyncSession, figis: list[str], interval: int, dfrom: datetime,
                 dto: datetime, *, report: list[dict] | None = None) -> int:
    """Докачка T-Invest по проблемным фигам (весь диапазон) + сброс baseline для пересчёта."""
    from app.services.tinvest import INTERVAL_NAMES, fetch_candles, to_thread, upsert_candles

    rep = report if report is not None else await check(db, figis, interval, dfrom, dto)
    bad_figis = sorted({r["figi"] for r in rep if r["status"] in ("DEFICIT_DAY", "MISMATCH")})
    total = 0
    for figi in bad_figis:
        rows = await to_thread(fetch_candles, figi, INTERVAL_NAMES["1min"], dfrom, dto)
        if rows:
            total += await upsert_candles(db, rows)
        await db.execute(text(
            "DELETE FROM candle_integrity WHERE figi=:f AND interval=:i AND day >= :a AND day < :b"
        ), {"f": figi, "i": interval, "a": dfrom.replace(tzinfo=None), "b": dto.replace(tzinfo=None)})
    if bad_figis:
        await db.commit()
    return total


async def preflight_universe(figis: list[str], dfrom: datetime, dto: datetime, *,
                             fix: bool = True, interval: int = 1) -> list[dict]:
    """Vochdog-прогон для окна теста: check (+repair) и повторный check."""
    from app.database import SessionLocal

    async with SessionLocal() as db:
        rep = await check(db, figis, interval, dfrom, dto)
        if fix and any(r["status"] in ("DEFICIT_DAY", "MISMATCH") for r in rep):
            await repair(db, figis, interval, dfrom, dto, report=rep)
            rep = await check(db, figis, interval, dfrom, dto)
        return rep


async def invalidate(db: AsyncSession, figis: list[str], interval: int, days: list) -> int:
    """Сбросить baseline затронутых (figi, day) после записи свечей (писатели).

    См. DECISIONS 2026-10-01 20:30, п.2: mismatch после правок не должен выглядеть
    как порча — при следующей проверке это будет NO_BASELINE (перезапись).
    """
    if not figis or not days:
        return 0
    res = await db.execute(text(
        "DELETE FROM candle_integrity WHERE figi = ANY(:f) AND interval = :i "
        "AND (day)::date = ANY(:d)"
    ), {"f": list(figis), "i": int(interval), "d": list(days)})
    return int(res.rowcount or 0)
