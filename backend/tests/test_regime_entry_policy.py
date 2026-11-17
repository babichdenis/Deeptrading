from datetime import datetime, timedelta, timezone
import pytest
from app.bot.regime_entry_policy import regime_entry_rejection

AT = datetime(2026,11,9,10,tzinfo=timezone.utc)


def evaluate(label, votes, closed_at=AT):
    return regime_entry_rejection(label=label, closed_at=closed_at, as_of=AT, buy_setups=votes)


def test_range_reclaim_and_pullback_admit():
    assert evaluate('RANGE',['bollinger_reclaim','pullback_ema']) is None


def test_trend_breakout_and_pullback_admit():
    assert evaluate('TREND_UP',['donchian_breakout','pullback_ema']) is None


def test_two_breakouts_do_not_replace_second_role():
    assert evaluate('TREND_UP',['donchian_breakout','range_compression_breakout']) == 'missing_pullback_confirmation'


def test_reclaim_pair_not_trend_breakout():
    assert evaluate('TREND_UP',['bollinger_reclaim','pullback_ema']) == 'missing_breakout_trigger'


def test_breakout_pair_not_range_reclaim():
    assert evaluate('RANGE',['donchian_breakout','pullback_ema']) == 'missing_reclaim_trigger'


@pytest.mark.parametrize('label',['TREND_DOWN','HIGH_VOLATILITY','NEUTRAL',None])
def test_unapproved_regime_never_defaults_to_range(label):
    assert evaluate(label,['bollinger_reclaim','pullback_ema']) == 'regime_not_allowed'


@pytest.mark.parametrize('closed_at',[None,AT+timedelta(seconds=1),AT-timedelta(hours=1,seconds=1)])
def test_missing_future_stale_regime_never_admits(closed_at):
    assert evaluate('RANGE',['bollinger_reclaim','pullback_ema'],closed_at) == 'regime_missing_or_stale'
