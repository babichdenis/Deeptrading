"""Хелпер point-in-time: последний ЗАКРЫТЫЙ бар на decision_time.

Бары 5m имеют ts = НАЧАЛО интервала; close/EMA/ADX известны только в КОНЦЕ (ts+5min).
Поэтому бар с ts <= decision_time может быть ещё не закрыт (lookahead).
Корректный «последний закрытый бар»: ts[j] + interval <= decision_time.
"""
import bisect
import math
from datetime import timedelta


def last_closed_bar(ts_list, decision_time, interval_minutes=5):
    """Возвращает индекс j последнего бара, чей КОНЕЦ интервала <= decision_time.

    ts_list: отсортированный список datetime баров (ts = начало интервала).
    decision_time: datetime.
    interval_minutes: длина бара (по умолчанию 5).
    """
    if isinstance(interval_minutes, bool) or not math.isfinite(interval_minutes) or interval_minutes <= 0:
        raise ValueError("interval_minutes must be finite and positive")
    if decision_time.utcoffset() is None:
        raise ValueError("decision_time must be timezone-aware")
    if ts_list and (ts_list[0].utcoffset() is None or ts_list[-1].utcoffset() is None):
        raise ValueError("bar timestamps must be timezone-aware")
    shift = decision_time - timedelta(minutes=interval_minutes)
    return bisect.bisect_right(ts_list, shift) - 1
