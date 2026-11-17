from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.bot.close_confirmation import classify_close_confirmation


def trade(qty=20, price=Decimal('99.25'), identity='ORDER'):
    return SimpleNamespace(qty=qty, exit_price=price, client_order_id=identity)


def classify(value):
    return classify_close_confirmation(value, requested_units=20, client_order_id='ORDER')


def test_full_and_partial_are_distinct():
    full = classify(trade())
    partial = classify(trade(qty=8))
    assert full.fully_closed and full.remaining_units == 0
    assert partial.status == 'PARTIAL_FILLED' and not partial.fully_closed
    assert (partial.executed_units, partial.remaining_units) == (8, 12)


def test_unknown_is_not_zero_fill():
    result = classify(None)
    assert result.status == 'PENDING_RECONCILIATION'
    assert result.executed_units is None and result.remaining_units is None


@pytest.mark.parametrize('qty', [0, -1, 21, True, 20.0, None])
def test_invalid_quantity_cannot_confirm_closure(qty):
    assert classify(trade(qty=qty)).reason == 'execution_quantity_unknown'


@pytest.mark.parametrize('price', [None, 0, -1, float('nan'), float('inf'), True])
def test_invalid_price_preserves_reconciliation(price):
    result = classify(trade(price=price))
    assert result.reason == 'execution_price_unknown' and not result.fully_closed
    assert result.executed_units == 20


def test_other_order_cannot_close_this_intent():
    result = classify(trade(identity='OTHER'))
    assert result.reason == 'execution_identity_unknown'
    assert result.executed_units is None


def test_repeated_cumulative_receipt_is_not_added_twice():
    receipt = trade(qty=8)
    assert classify(receipt) == classify(receipt)
    assert classify(receipt).remaining_units == 12


def test_paper_fill_can_be_classified_without_broker_identity():
    result = classify_close_confirmation(trade(identity=None), requested_units=20)
    assert result.fully_closed
