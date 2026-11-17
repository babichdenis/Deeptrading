from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from app.bot.connected_admission import TrendObservation, entry_context_rejection

AT = datetime(2026,11,9,10,tzinfo=timezone.utc)


def evaluate(**changes):
    arguments = dict(signal_bars=120, member=True, as_of=AT, trends={m: TrendObservation(20, AT, True, .1) for m in (15,60)})
    arguments.update(changes)
    return entry_context_rejection(**arguments)


def test_ready_universe_and_both_trends_admit():
    assert evaluate() is None


def test_signal_warmup_has_priority_over_membership():
    assert evaluate(signal_bars=119, member=False) == 'signal_warmup'
    assert evaluate(member=False) == 'not_member'


def test_missing_trend_not_treated_as_neutral():
    assert evaluate(trends={}) == 'trend_missing_or_stale'


def test_exact_freshness_limit_and_expiration():
    trends = {m: TrendObservation(20, AT-timedelta(minutes=m), True, .1) for m in (15,60)}
    assert evaluate(trends=trends) is None
    trends[15] = replace(trends[15], closed_at=AT-timedelta(minutes=15,seconds=1))
    assert evaluate(trends=trends) == 'trend_missing_or_stale'


def test_future_closed_bar_cannot_admit():
    trends = {m: TrendObservation(20, AT+timedelta(seconds=1), True, .1) for m in (15,60)}
    assert evaluate(trends=trends) == 'trend_missing_or_stale'


@pytest.mark.parametrize('slope', [0., -.1, None, float('nan'), float('inf')])
def test_nonpositive_or_unknown_trend_rejects(slope):
    trends = {m: TrendObservation(20, AT, True, slope) for m in (15,60)}
    assert evaluate(trends=trends) == 'trend_gate'


def test_control_can_explicitly_disable_trends():
    assert evaluate(trends={}, require_trends=False) is None


@pytest.mark.parametrize('side,slope,accepted', [('BUY', .1, True), ('BUY', -.1, False), ('SELL', -.1, True), ('SELL', .1, False), ('SELL', 0., False), ('SELL', float('nan'), False)])
def test_directional_trend_gate(side, slope, accepted):
    trends = {m: TrendObservation(20, AT, True, slope) for m in (15, 60)}
    assert (evaluate(side=side, trends=trends) is None) == accepted


def test_short_still_requires_both_fresh_timeframes():
    trends = {m: TrendObservation(20, AT, True, -.1) for m in (15, 60)}
    trends[60] = replace(trends[60], closed_at=AT-timedelta(minutes=61))
    assert evaluate(side='SELL', trends=trends) == 'trend_missing_or_stale'


def test_invalid_direction_rejected_even_with_disabled_gate():
    with pytest.raises(ValueError):
        evaluate(side='SHORT', require_trends=False)
