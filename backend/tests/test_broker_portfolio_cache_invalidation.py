import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.bot.live_broker import LiveBroker


@pytest.mark.parametrize("mode", ["live", "sandbox"])
@pytest.mark.parametrize("failed", [False, True])
def test_order_attempt_invalidates_portfolio_even_when_response_is_unknown(mode, failed):
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = mode
    broker.config = SimpleNamespace(use_margin=False)
    broker._portfolio_lock = threading.Lock()
    stale = object()
    fresh = object()
    broker._portfolio_snapshot = stale
    broker._portfolio_ts = time.monotonic()
    broker._portfolio_ttl = 15.0
    broker._get_portfolio_with_retry = Mock(return_value=fresh)
    failure = TimeoutError("offline unknown result")
    response = object()
    post = Mock(side_effect=failure if failed else None, return_value=response)
    other = Mock(side_effect=AssertionError("wrong mode"))
    services = SimpleNamespace(
        orders=SimpleNamespace(post_order=post if mode == "live" else other),
        sandbox=SimpleNamespace(post_sandbox_order=post if mode == "sandbox" else other),
    )
    broker._get_services = lambda: services
    assert broker._get_portfolio_safe() is stale
    if failed:
        with pytest.raises(TimeoutError) as caught:
            broker._api_post_order(order_id="offline-intent")
        assert caught.value is failure
    else:
        assert broker._api_post_order(order_id="offline-intent") is response
    assert broker._get_portfolio_safe() is fresh
    broker._get_portfolio_with_retry.assert_called_once_with()
    expected = {"order_id": "offline-intent"}
    if mode == "live":
        expected["confirm_margin_trade"] = False
    post.assert_called_once_with(**expected)
    other.assert_not_called()
