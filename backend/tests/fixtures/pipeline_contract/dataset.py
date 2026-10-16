"""Детерминированные обезличенные 1m-свечи для сквозных тестов пайплайна.

Никаких реальных тикеров/FIGI/сделок: только синтетический анонимный FIGI.
Функции чистые (без сети/БД/файлов), поэтому импорт модуля побочных эффектов
не даёт. Генерация идёт по календарю основной сессии MOEX (10:00..18:44 MSK),
чтобы получать полные 10min-бары и непрерывный торговый путь.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

ANON_FIGI = "ANONPIPELINEFIGI0001"
ANON_TICKER = "ANON"

SESSION_HHMM_START = (10, 0)
SESSION_HHMM_END = (18, 44)  # SESSION_MAX_MOD=1124 -> 18:44 включительно


def session_minutes(day: date) -> list[datetime]:
    """Минуты основной сессии [10:00, 18:44] MSK дня `day` в UTC (по порядку)."""
    if day.weekday() >= 5:
        raise ValueError(f"{day.isoformat()} — выходной, сессионных минут нет")
    start = datetime(day.year, day.month, day.day, *SESSION_HHMM_START, tzinfo=MSK)
    end = datetime(day.year, day.month, day.day, *SESSION_HHMM_END, tzinfo=MSK)
    out: list[datetime] = []
    t = start
    while t <= end:
        out.append(t.astimezone(UTC))
        t += timedelta(minutes=1)
    return out


def last_weekdays(n: int, *, before: date | None = None) -> list[date]:
    """`n` последних будних дней СТРОГО до `before` (по умолчанию сегодня MSK).

    Возвращает по возрастанию дат: [старший, ..., младший]. Гарантирует, что
    даты лежат в пределах окна прогрева (120 дней) и являются торговыми днями.
    """
    if before is None:
        before = datetime.now(MSK).date()
    out: list[date] = []
    d = before - timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return sorted(out)


def day_prices(day: date, *, base: float = 100.0,
               overrides: dict[str, float] | None = None) -> dict[datetime, float]:
    """Цена по каждой сессионной минуте дня.

    `overrides` — карта MSK-метки "ЧЧ:ММ" -> цена, действующая с этой минуты
    до следующей override-метки включительно. Позволяет собрать детерминированный
    сценарий (например: до 12:00 — 100.0, с 12:00 — 101.0).
    """
    minutes = session_minutes(day)
    if not overrides:
        return {ts: base for ts in minutes}
    marks: list[tuple[int, float]] = []
    for hhmm, px in overrides.items():
        h, m = hhmm.split(":")
        marks.append((int(h) * 60 + int(m), float(px)))
    marks.sort()
    p = base
    prices: dict[datetime, float] = {}
    idx = 0
    for ts in minutes:
        mod = ts.astimezone(MSK).hour * 60 + ts.astimezone(MSK).minute
        while idx < len(marks) and marks[idx][0] <= mod:
            p = marks[idx][1]
            idx += 1
        prices[ts] = p
    return prices


def rows_from_prices(figi: str, day: date, prices: dict[datetime, float], *,
                     tick: float = 0.01, volume: int = 1000) -> list[dict]:
    """Строки под `upsert_candles`: open=close=p, high/low на ±tick, volume>0."""
    rows: list[dict] = []
    for ts in session_minutes(day):
        p = prices[ts]
        rows.append({
            "figi": figi,
            "interval": 1,
            "ts": ts,
            "open": p,
            "high": p + tick,
            "low": p - tick,
            "close": p,
            "volume": volume,
        })
    return rows


def without(rows: Iterable[dict], drop_ts: set[datetime]) -> list[dict]:
    """Список строк без указанных минут (создание проверяемых дыр)."""
    return [r for r in rows if r["ts"] not in drop_ts]
