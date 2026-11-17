from datetime import datetime, timedelta, timezone

from app.bot.entry_confirmation import LongEntryIntent, evaluate_long_entry
from app.engine.models import Candle

AT = datetime(2026, 11, 9, 10, tzinfo=timezone.utc)
INTENT = LongEntryIntent('SBER', AT, 101., 99.)


def confirmation(low=99.5, close=102., ts=AT + timedelta(minutes=5)):
    return Candle(ts=ts, open=100., high=103., low=low, close=close, volume=100)


def test_unclosed_confirmation_never_admits_entry():
    assert evaluate_long_entry(INTENT, confirmation(), AT + timedelta(minutes=9)).state == 'WAIT'


def test_exact_closed_confirmation_admits_entry():
    assert evaluate_long_entry(INTENT, confirmation(), AT + timedelta(minutes=10)).state == 'CONFIRM'


def test_signal_low_break_cancels_even_if_close_breaks_high():
    result = evaluate_long_entry(INTENT, confirmation(low=98.), AT + timedelta(minutes=10))
    assert result.state == 'CANCEL'
    assert result.reason == 'signal_low_broken'


def test_high_touch_without_close_above_is_not_confirmation():
    assert evaluate_long_entry(INTENT, confirmation(close=101.), AT + timedelta(minutes=10)).state == 'CANCEL'


def test_missing_bar_does_not_allow_later_revival():
    assert evaluate_long_entry(INTENT, None, AT + timedelta(minutes=10)).state == 'CANCEL'
    assert evaluate_long_entry(INTENT, confirmation(), AT + timedelta(minutes=15)).reason == 'expired'


def test_wrong_timestamp_is_not_substituted():
    assert evaluate_long_entry(INTENT, confirmation(ts=AT), AT + timedelta(minutes=10)).reason == 'missing_exact_confirmation'


def test_current_sell_quorum_vetoes_previous_buy():
    assert evaluate_long_entry(INTENT, confirmation(), AT + timedelta(minutes=10), sell_veto=True).reason == 'current_sell_quorum'
