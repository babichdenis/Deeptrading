import pytest
from unittest.mock import Mock

from tests.test_live_entry_submission_scope import broker, request, response, durable_storage
from tests.test_runtime_partial_entry_acceptance import test_partial_entry_retains_intent_and_never_resubmits as run_entry


@pytest.mark.asyncio
@pytest.mark.parametrize('price_is_filled', [False, True])
async def test_adapter_receipt_does_not_book_post_response_price(price_is_filled):
    value = broker()
    value._api_post_order = Mock(return_value=response())
    payload = request()
    payload['price_is_filled'] = price_is_filled
    receipt = await value.open_position(**payload)
    assert receipt.observed_units == 20
    assert value.last_execution is None


@pytest.mark.asyncio
@pytest.mark.parametrize('transport_error', [False, True])
async def test_runtime_retains_unknown_response_without_resubmission(monkeypatch, transport_error):
    await run_entry(monkeypatch, 10, 0, False, False,
                    submission_case='connection' if transport_error else None)
