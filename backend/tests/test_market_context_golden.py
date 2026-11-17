from datetime import datetime, timedelta, timezone

import pytest

from app.engine.market_context import MarketContextPolicy
from app.engine.models import Signal, Side, PositionState, DecisionAction
from scripts.research.architect_context_clock import ExecutionClockContextPolicy


T = datetime(2026,9,1,10,tzinfo=timezone.utc)
M = timedelta(minutes=1)


def snapshots(relative, market):
    return [{'as_of':T.isoformat(),'features':{'A':{'valid':True,'relative_return':relative,'moex_return':market}}}]


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
@pytest.mark.parametrize('relative', [-.1, 0, .1])
@pytest.mark.parametrize('market', [-.1, 0, .1])
def test_original_mode_matches_existing_policy(side, relative, market):
    data=snapshots(relative, market)
    old=ExecutionClockContextPolicy('A',data,execution_timeframe=M)
    new=MarketContextPolicy('A',data,execution_timeframe=M,market_mode='opposed_or_flat')
    signal=Signal('fixture',side,T)
    assert new.decide(signal,PositionState.FLAT,0)==old.decide(signal,PositionState.FLAT,0)


def test_market_direction_modes_are_distinct_without_changing_relative_rule():
    signal=Signal('fixture',Side.SELL,T)
    data=snapshots(-.1,-.05)
    for mode,expected in [('opposed_or_flat',False),('aligned',True),('any',True)]:
        p=MarketContextPolicy('A',data,execution_timeframe=M,market_mode=mode)
        assert (p.decide(signal,PositionState.FLAT,0)[0] is DecisionAction.ACCEPT_ENTRY)==expected


def test_future_context_never_changes_earlier_decision_and_exit_remains_available():
    signal=Signal('fixture',Side.SELL,T)
    data=snapshots(-.1,-.05);data[0]['as_of']=(T+2*M).isoformat()
    p=MarketContextPolicy('A',data,execution_timeframe=M,market_mode='aligned')
    assert p.decide(signal,PositionState.FLAT,0)[0] is DecisionAction.NO_SIGNAL
    assert p.decide(signal,PositionState.LONG,0)[0] is DecisionAction.ACCEPT_EXIT
