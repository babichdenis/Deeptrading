"""Weekend flat: позиция не должна переживать выходные (решение владельца 02.10)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.engine.sessions import (
    SESSION_WINDOWS,
    eod_close_due,
    is_weekend,
    is_session_active,
    weekend_close_due,
)

_MSK = timezone(timedelta(hours=3))


def _msk(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_MSK)


# 2025-09-05 — пятница, 2025-09-06/07 — выходные.
def test_weekend_close_due_friday_evening():
    # Пятница, конец дневной сессии (18:45+) — пора закрываться перед выходными.
    assert weekend_close_due(_msk(2025, 9, 5, 18, 45), ["day"]) is True
    assert weekend_close_due(_msk(2025, 9, 5, 23, 50), ["day"]) is True
    # С вечерней сессией — ждём её конца (23:50), а не дневной.
    assert weekend_close_due(_msk(2025, 9, 5, 19, 30), ["day", "evening"]) is False
    assert weekend_close_due(_msk(2025, 9, 5, 23, 50), ["day", "evening"]) is True


def test_weekend_close_due_not_before_friday_close():
    assert weekend_close_due(_msk(2025, 9, 5, 12, 0), ["day"]) is False
    assert weekend_close_due(_msk(2025, 9, 5, 18, 44), ["day"]) is False
    assert weekend_close_due(_msk(2025, 9, 4, 23, 55), ["day", "evening"]) is False  # четверг


def test_weekend_close_due_on_weekend_and_not_monday():
    assert weekend_close_due(_msk(2025, 9, 6, 3, 0), ["day"]) is True
    assert weekend_close_due(_msk(2025, 9, 7, 12, 0), ["day"]) is True
    assert weekend_close_due(_msk(2025, 9, 8, 10, 0), ["day"]) is False  # понедельник


def test_weekend_flag_survives_sessions_and_clearing():
    # Клиринг day→evening в пятницу — ещё НЕ выходные (вечерняя сессия жива).
    assert weekend_close_due(_msk(2025, 9, 5, 18, 50), ["day", "evening"]) is False
    assert is_session_active(_msk(2025, 9, 5, 18, 50), ["day", "evening"]) is False  # клиринг
    assert is_weekend(_msk(2025, 9, 6, 0, 1)) is True
    assert is_weekend(_msk(2025, 9, 5, 23, 59)) is False


def test_weekend_does_not_break_eod_for_weekdays():
    # eod_close_due не должен сломаться после рефакторинга на _last_session_end_msk.
    assert eod_close_due(_msk(2025, 9, 4, 18, 40), ["day"], minutes_before=10) is True
    assert eod_close_due(_msk(2025, 9, 4, 12, 0), ["day"], minutes_before=10) is False
    assert SESSION_WINDOWS["day"][1] == 18 * 60 + 45


def test_last_session_close_helper_picks_friday_bar():
    """Выход в уикенд-баре ценится по последней СЕССИОННОЙ свече."""
    from app.bot.runtime import PaperBotRuntime

    def _bar(ts, close):
        return SimpleNamespace(ts=ts, close=close)

    rt = PaperBotRuntime.__new__(PaperBotRuntime)  # без __init__: нужен только хелпер
    rt.config = SimpleNamespace(sessions=["day"])
    rt.tcs_to_bbg = {}
    rt.buffers = {
        "BBG001": [
            _bar(_msk(2025, 9, 5, 18, 43), 100.0),
            _bar(_msk(2025, 9, 5, 18, 44), 101.5),   # последняя дневная
            _bar(_msk(2025, 9, 5, 23, 49), 102.0),   # вечерняя (вне сессии при sessions=["day"])
            _bar(_msk(2025, 9, 6, 3, 0), 999.0),     # уикенд-бар (цена не рыночная)
        ]
    }
    assert rt._last_session_close("BBG001", 1.0) == 101.5
    # пустой буфер → fallback
    assert rt._last_session_close("NOPE", 7.0) == 7.0


def test_bot_config_has_weekend_flat_default_true():
    from app.bot.runtime import BOT_PERSIST_FIELDS, BotConfig

    assert BotConfig().weekend_flat is True
    assert "weekend_flat" in BOT_PERSIST_FIELDS