from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.sandbox_order_evidence import parse_sandbox_order_aggregate

NOW = datetime(2026, 11, 16, 10, 5, 6, tzinfo=timezone.utc)


def response():
    return SimpleNamespace(order_request_id='REQUEST', order_id='BROKER', figi='NLMK',
        direction=1, lots_requested=5, lots_executed=50, execution_report_status=1,
        stages=[], order_date=NOW, currency='rub',
        total_order_amount=SimpleNamespace(currency='rub', units=3749, nano=0),
        executed_order_price=SimpleNamespace(currency='rub', units=3749, nano=0),
        executed_commission=SimpleNamespace(currency='rub', units=1, nano=874500000))


def parse(value, **changes):
    args=dict(mode='sandbox', broker_account_id='OFFLINE', client_order_id='REQUEST',
              figi='NLMK', side='BUY', requested_lots=5, lot_size=10, as_of=NOW)
    args.update(changes)
    return parse_sandbox_order_aggregate(value, **args)


def test_observed_sandbox_shape_retains_aggregate_provenance():
    evidence = parse(response())
    assert evidence.units == 50 and evidence.average_price == Decimal('74.98')
    assert evidence.commission == Decimal('1.8745')
    assert evidence.source == 'sandbox_order_aggregate'
    assert not hasattr(evidence, 'fills') and not hasattr(evidence, 'first_execution_time')


@pytest.mark.parametrize('changes', [{'mode':'live'}, {'lot_size':1}, {'figi':'OTHER'},
    {'requested_lots':50}, {'side':'SELL'}, {'as_of':NOW.replace(tzinfo=None)}])
def test_scope_and_lot_mismatch_cannot_be_normalized(changes):
    with pytest.raises(ValueError): parse(response(), **changes)


@pytest.mark.parametrize('field,value', [('lots_executed',49), ('lots_executed',5),
    ('execution_report_status',5), ('stages',[object()]), ('order_id',''),
    ('currency','usd'), ('order_date',NOW.replace(tzinfo=None))])
def test_only_verified_complete_sandbox_shape_is_supported(field,value):
    raw=response()
    setattr(raw,field,value)
    with pytest.raises(ValueError): parse(raw)


def test_per_unit_price_cannot_be_mistaken_for_aggregate():
    raw=response()
    raw.executed_order_price=SimpleNamespace(currency='rub',units=74,nano=980000000)
    with pytest.raises(ValueError): parse(raw)
