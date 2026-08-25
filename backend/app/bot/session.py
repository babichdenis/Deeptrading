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
