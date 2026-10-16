from datetime import datetime, timezone

from app.engine.models import Candle, Side
from app.engine.strategies_timegrid import TimeGridParams, TimeGridStrategy

_MONDAY = datetime(2025, 12, 8, 20, 45, tzinfo=timezone.utc)
_TUE_MORNING_OPEN = datetime(2025, 12, 9, 3, 50, tzinfo=timezone.utc)
_TUE_MORNING_SIGNAL = datetime(2025, 12, 9, 3, 55, tzinfo=timezone.utc)
_TUE_EVENING_OPEN = datetime(2025, 12, 9, 15, 40, tzinfo=timezone.utc)
_TUE_EVENING_SIGNAL = datetime(2025, 12, 9, 15, 45, tzinfo=timezone.utc)


def _bar(ts: datetime, open_: float, close: float) -> Candle:
    return Candle(ts=ts, open=open_, high=max(open_, close), low=min(open_, close), close=close)


def _morning(prev_close: float, day_open: float) -> list[Candle]:
    return [
        _bar(_MONDAY, prev_close - 1.0, prev_close),
        _bar(_TUE_MORNING_OPEN, day_open, day_open),
        _bar(_TUE_MORNING_SIGNAL, day_open, day_open),
    ]


def _evening(prev_close: float, day_open: float) -> list[Candle]:
    return [
        _bar(_MONDAY, prev_close - 1.0, prev_close),
        _bar(_TUE_EVENING_OPEN, day_open, day_open),
        _bar(_TUE_EVENING_SIGNAL, day_open, day_open),
    ]


def test_gap_filter_off_by_default():
    strat = TimeGridStrategy(TimeGridParams())
    sig = strat.on_bar(_morning(100.0, 105.0))
    assert sig is not None and sig.side is Side.SELL


def test_short_blocked_on_up_gap():
    strat = TimeGridStrategy(TimeGridParams(gap_skip_pct=1.0))
    assert strat.on_bar(_morning(100.0, 105.0)) is None


def test_short_allowed_on_down_gap():
    strat = TimeGridStrategy(TimeGridParams(gap_skip_pct=1.0))
    sig = strat.on_bar(_morning(100.0, 93.0))
    assert sig is not None and sig.side is Side.SELL


def test_short_allowed_on_small_gap():
    strat = TimeGridStrategy(TimeGridParams(gap_skip_pct=1.0))
    sig = strat.on_bar(_morning(100.0, 100.5))
    assert sig is not None and sig.side is Side.SELL


def test_long_blocked_on_down_gap():
    strat = TimeGridStrategy(TimeGridParams(gap_skip_pct=1.0))
    assert strat.on_bar(_evening(100.0, 94.0)) is None


def test_long_allowed_on_flat_open():
    strat = TimeGridStrategy(TimeGridParams(gap_skip_pct=1.0))
    sig = strat.on_bar(_evening(100.0, 100.2))
    assert sig is not None and sig.side is Side.BUY


def test_overnight_gap_uses_first_bar_of_current_day():
    strat = TimeGridStrategy(TimeGridParams())
    gap = strat._overnight_gap_pct(_morning(200.0, 210.0))
    assert gap is not None and abs(gap - 5.0) < 1e-9


def test_overnight_gap_none_without_previous_day():
    strat = TimeGridStrategy(TimeGridParams())
    only_today = [_bar(_TUE_MORNING_SIGNAL, 100.0, 100.0)]
    assert strat._overnight_gap_pct(only_today) is None
