import pytest

from app.bot.feed import STEP_SEC, closed_candle_timeout


@pytest.mark.parametrize("interval,expected", [("1min", 75), ("5min", 315), ("15min", 915), ("hour", 3615)])
def test_silence_timeout_allows_a_full_closed_bar(interval, expected):
    assert closed_candle_timeout(interval) == expected
    assert closed_candle_timeout(interval) > STEP_SEC[interval]


def test_unknown_interval_is_not_silently_assumed_minute():
    with pytest.raises(KeyError):
        closed_candle_timeout("unknown")
