from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.open_order_intent import OpenIntent


def intent():
    return OpenIntent('live', 'OFFLINE', 'REQUEST', 'FIGI', 'BUY', 20, 10,
                      datetime(2026, 11, 16, tzinfo=timezone.utc), ticker='TEST', strategy_id='TEST',
                      stop_loss=Decimal('95'), take_profit=Decimal('110'))


@pytest.mark.parametrize('changes', [dict(stop_loss=Decimal('NaN')), dict(stop_loss=95),
    dict(take_profit=Decimal('90')), dict(strategy_id=''), dict(ticker=''), dict(requested_units=15),
    dict(lot_size=True), dict(created_at=datetime(2026, 11, 16))])
def test_invalid_opening_context_cannot_be_reserved(changes):
    with pytest.raises(ValueError): replace(intent(), **changes).validate()


@pytest.mark.parametrize('mutation', ['version', 'missing_level', 'numeric_level', 'close', 'snapshot'])
def test_corrupted_recovery_plan_is_not_guessed(mutation):
    original = intent()
    row = SimpleNamespace(**original.__dict__, action='open', entry_plan=original.entry_payload(), position_snapshot=None)
    if mutation == 'version': row.entry_plan['version'] = True
    elif mutation == 'missing_level': del row.entry_plan['stop_loss']
    elif mutation == 'numeric_level': row.entry_plan['stop_loss'] = 95
    elif mutation == 'close': row.action = 'close'
    else: row.position_snapshot = {}
    with pytest.raises(ValueError): OpenIntent.from_record(row)


def test_legacy_plan_remains_unknown_instead_of_current_policy_defaults():
    original = intent()
    payload = original.entry_payload()
    payload['version'] = 1
    del payload['exit_policy']
    row = SimpleNamespace(**original.__dict__, action='open', entry_plan=payload, position_snapshot=None)
    assert OpenIntent.from_record(row).exit_policy is None
