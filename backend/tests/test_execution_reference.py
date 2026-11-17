from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.bot.execution_reference import ExecutionReference
from app.bot.live_broker import LiveBroker

NOW = datetime(2026, 11, 16, 10, tzinfo=timezone.utc)


def test_reference_at_next_interval_boundary_is_valid_without_next_closed_bar():
    ref = ExecutionReference('FIGI', 100, NOW, NOW + timedelta(seconds=1))
    assert ref.validate(figi='FIGI', decision_at=NOW, as_of=NOW + timedelta(seconds=1)) is ref


@pytest.mark.parametrize('changes', [
    {'figi': 'OTHER'}, {'price': 0}, {'price': float('nan')},
    {'market_at': NOW - timedelta(microseconds=1)},
    {'market_at': NOW + timedelta(seconds=2)},
    {'market_at': NOW.replace(tzinfo=None)},
    {'received_at': NOW - timedelta(seconds=1)},
])
def test_invalid_reference_cannot_authorize_dispatch(changes):
    ref = replace(ExecutionReference('FIGI', 100, NOW, NOW), **changes)
    with pytest.raises(ValueError):
        ref.validate(figi='FIGI', decision_at=NOW, as_of=NOW)


def test_reference_expires_while_waiting_for_dispatch():
    ref = ExecutionReference('FIGI', 100, NOW, NOW)
    with pytest.raises(ValueError, match='stale'):
        ref.validate(figi='FIGI', decision_at=NOW, as_of=NOW + timedelta(seconds=31))


@pytest.mark.asyncio
async def test_adapter_preserves_exchange_time_and_excludes_ambiguous_instruments():
    broker = object.__new__(LiveBroker)
    def item(figi):
        return SimpleNamespace(figi=figi, price=SimpleNamespace(units=100, nano=500000000), time=NOW)
    services = SimpleNamespace(market_data=SimpleNamespace(
        get_last_prices=Mock(return_value=SimpleNamespace(last_prices=[
            item('FIGI'), item('DUP'), item('DUP'), item('OTHER')]))))
    broker._get_services = lambda: services
    broker._mark_ok = Mock()
    broker._mark_degraded = Mock()
    result = await broker.execution_references(['FIGI', 'DUP'])
    assert set(result) == {'FIGI'}
    assert result['FIGI'].price == 100.5 and result['FIGI'].market_at == NOW
    assert result['FIGI'].received_at.tzinfo is not None
    broker._mark_degraded.assert_not_called()


@pytest.mark.asyncio
async def test_adapter_error_returns_no_dispatch_reference():
    broker = object.__new__(LiveBroker)
    broker._get_services = Mock(side_effect=TimeoutError)
    broker._mark_degraded = Mock()
    result = await broker.execution_references(['FIGI'])
    assert result == {}
    broker._mark_degraded.assert_called_once()
