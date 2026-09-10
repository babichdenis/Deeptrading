from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class SessionPolicyConfig:
    id: str = "moex_intraday_v1"
    version: str = "1.0.0"
    tz: str = "Europe/Moscow"
    open_time: str = "10:00"
    close_time: str = "18:45"
    entry_cutoff_bars: int = 0
    overnight: bool = True
    force_flat_at_session_end: bool = False


class SessionPolicy:
    def __init__(self, config: SessionPolicyConfig | None = None):
        self.config = config or SessionPolicyConfig()
        self.tz = ZoneInfo(self.config.tz)
        oh, om = self.config.open_time.split(":")
        ch, cm = self.config.close_time.split(":")
        self.open_t = time(int(oh), int(om))
        self.close_t = time(int(ch), int(cm))

    def local(self, ts: datetime) -> datetime:
        return ts.astimezone(self.tz)

    def session_date(self, ts: datetime) -> date:
        return self.local(ts).date()

    def minutes_to_close(self, ts: datetime) -> float | None:
        lt = self.local(ts)
        close_dt = datetime.combine(lt.date(), self.close_t, tzinfo=self.tz)
        seconds = (close_dt - lt).total_seconds()
        if seconds < 0:
            return None
        return seconds / 60

    def can_enter(self, ts: datetime, tf_minutes: int) -> tuple[bool, str]:
        """Разрешён ли НОВЫЙ вход в момент ts.

        Политика entry_session=main: вход разрешён ТОЛЬКО внутри окна
        open_time..close_time (10:00–18:45 MSK) в будни. Вне окна — запрет
        (вечерняя/утренняя сессия не является main; перенос открытой позиции
        обрабатывается отдельно через overnight/force_flat).
        """
        lt = self.local(ts)
        if lt.weekday() >= 5:
            return False, f"weekend {lt.date()}"

        if lt.time() < self.open_t or lt.time() > self.close_t:
            return False, f"outside main session {lt.strftime('%H:%M')}"

        cutoff_bars = self.config.entry_cutoff_bars
        if cutoff_bars <= 0:
            return True, ""

        minutes_left = self.minutes_to_close(ts)
        if minutes_left is None:
            return True, ""
        if minutes_left < cutoff_bars * tf_minutes:
            return (
                False,
                f"late entry: {minutes_left:.0f}min left < {cutoff_bars} bars x {tf_minutes}min",
            )
        return True, ""

# --- Shared session windows (used by runtime + ensemble_strategy) ---

SESSION_WINDOWS: dict[str, tuple[int, int]] = {
    "morning": (6 * 60 + 50, 9 * 60 + 50),
    "day": (9 * 60 + 50, 18 * 60 + 45),
    "evening": (19 * 60 + 5, 23 * 60 + 50),
}

_MSK_TZ = ZoneInfo("Europe/Moscow")
# Клиринговый разрыв между дневной (day) и вечерней (evening) сессиями.
_CLEARING: tuple[int, int] = (18 * 60 + 45, 19 * 60 + 5)


def is_clearing_gap(ts, sessions: list[str] | None = None) -> bool:
    """True, если ts попадает в клиринг day→evening (18:45–19:05 MSK).

    Разрыв выдерживается только когда торгуются ОБЕ сессии: day и evening.
    Если evening выключена — в 18:45 для бота уже конец торгов, клиринга нет.
    """
    if not sessions:
        sessions = ["day"]
    if "day" not in sessions or "evening" not in sessions:
        return False
    msk = ts.astimezone(_MSK_TZ)
    if msk.weekday() >= 5:
        return False
    mins = msk.hour * 60 + msk.minute
    a, b = _CLEARING
    return a <= mins < b


def should_force_close(ts, sessions: list[str] | None = None, overnight: bool = False) -> bool:
    """Закрывать ли позицию сейчас принудительно (EOD/ночь).

    Не закрываем:
      - внутри любой активной сессии;
      - в клиринге day→evening (18:45–19:05), когда day и evening активны;
      - если включено держание через ночь (overnight=True).
    """
    if is_session_active(ts, sessions):
        return False
    if is_clearing_gap(ts, sessions):
        return False
    if overnight:
        return False
    return True


def is_session_active(ts, sessions: list[str] | None = None) -> bool:
    """Check if timestamp falls within any of the given session windows (MSK).
    
    Unified function used by both runtime.py and ensemble_strategy.py.
    """
    if not sessions:
        sessions = ["day"]
    msk = ts.astimezone(_MSK_TZ)
    if msk.weekday() >= 5:
        return False
    mins = msk.hour * 60 + msk.minute
    for s in sessions:
        a, b = SESSION_WINDOWS.get(s, (0, 0))
        if a <= mins < b:
            return True
    return False
