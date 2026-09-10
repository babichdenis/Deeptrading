from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def session_state(now: datetime | None = None) -> str:
    """Состояние торговой сессии МосБиржи по MSK-времени (§17 Preview_bot.md)."""
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    if now.weekday() >= 5:
        return "WEEKEND"
    t = now.time()
    if t < time(9, 50):
        return "PRE_MARKET"
    if t < time(10, 0):
        return "OPENING"
    if t < time(18, 45):
        return "TRADING"
    if t < time(19, 5):
        return "CLEARING"
    if t <= time(23, 50):
        return "EVENING"
    return "POST_MARKET"


def trading_session(now: datetime | None = None) -> str | None:
    """Торговая сессия бота (morning|day|evening) по расписанию MOEX (МСК).

    morning: 06:50–09:50, day: 09:50–19:00, evening: 19:00–23:50.
    Вне этих интервалов (ночь/выходные) — None.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(MSK)
    if now.weekday() >= 5:
        return None
    t = now.time()
    if time(6, 50) <= t < time(9, 50):
        return "morning"
    if time(9, 50) <= t < time(19, 0):
        return "day"
    if time(19, 0) <= t <= time(23, 50):
        return "evening"
    return None
