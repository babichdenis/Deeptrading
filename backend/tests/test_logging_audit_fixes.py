from __future__ import annotations

from collections import deque
from types import SimpleNamespace

import pytest


class _DummyEvents:
    def __init__(self) -> None:
        self.items = []

    def log(self, event_type: str, **payload):
        self.items.append((event_type, payload))
        return payload


@pytest.mark.asyncio
async def test_live_broker_open_position_unknown_fill_returns_none(monkeypatch):
    from app.bot.live_broker import LiveBroker

    broker = LiveBroker.__new__(LiveBroker)
    broker.config = SimpleNamespace(commission_rate=0.003)
    broker._account = "acc"
    broker._api_post_order = lambda **_kwargs: SimpleNamespace(executed_order_price=None)

    result = await LiveBroker.open_position(
        broker,
        figi="FIGI123",
        ticker="TEST",
        side="BUY",
        qty=1,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        strategy_id="s1",
    )

    assert result is None


@pytest.mark.asyncio
async def test_live_broker_close_position_blocks_unknown_lot(monkeypatch):
    from app.bot.live_broker import LiveBroker, LivePosition

    broker = LiveBroker.__new__(LiveBroker)
    broker.config = SimpleNamespace(commission_rate=0.003)
    broker._account = "acc"
    async def _get_lot_stub(_figi: str) -> int:
        return 0

    async def _pos_stub(_figi: str):
        return LivePosition(
            figi="FIGI123",
            ticker="TEST",
            side="LONG",
            qty=10,
            entry_price=100.0,
            entry_time=None,
            stop_loss=None,
            take_profit=None,
        )

    broker._get_lot = _get_lot_stub
    broker.get_position = _pos_stub

    result = await LiveBroker.close_position(broker, "FIGI123", 101.0, "signal_exit")

    assert result is None


@pytest.mark.asyncio
async def test_execute_pending_marks_unknown_open_for_reconciliation(monkeypatch):
    from app.bot.runtime import PaperBotRuntime

    rt = PaperBotRuntime.__new__(PaperBotRuntime)

    calls = {"open": 0}

    async def _open_stub(**_kwargs):
        calls["open"] += 1
        return None

    rt.broker = SimpleNamespace(open_position=_open_stub)
    rt.entries_paused = False
    rt.pending_orders = {}
    rt.events = _DummyEvents()
    rt._log_persist_queue = deque()
    rt._trace = None
    rt._approvals_since = {}
    rt._ai_reject_until = {}
    rt._ai_reject_reason = {}
    rt._held = set()
    rt._exit_plans = {}
    rt._exit_side = {}
    rt._exit_entry_px = {}
    rt._entry_bar_index = {}
    rt._clear_exit_state = lambda *_a, **_k: None
    rt._st_close = lambda *_a, **_k: None
    rt._trace_order_reject = lambda *_a, **_k: None
    rt._held_since = {}
    rt._bar_counter = 0
    rt.config = SimpleNamespace(
        use_ensemble=False,
        initial_sl_atr=4.0,
        atr_risk_reward=4.0,
        commission_rate=0.003,
        slippage_bps=2.0,
        sl_mode="fixed",
        stop_pct=0.02,
        target_pct=0.04,
        strategy_id="s1",
    )
    order = SimpleNamespace(
        id="o1",
        action="open",
        status="APPROVED",
        ticker="TEST",
        side="BUY",
        qty=1,
        meta={},
        to_dict=lambda: {},
    )
    rt.pending_orders["FIGI123"] = order
    c = SimpleNamespace(open=100.0, ts=None)

    result = await rt._execute_pending("FIGI123", c)

    assert result is False
    assert order.status == "PENDING_RECONCILIATION"
    assert "FIGI123" in rt.pending_orders
    assert any(ev[0] == "ORDER_RECONCILIATION_REQUIRED" for ev in rt.events.items)

    result2 = await rt._execute_pending("FIGI123", c)

    assert result2 is False
    assert order.status == "PENDING_RECONCILIATION"
    assert calls["open"] == 1
    assert any(ev[0] == "ORDER_RECONCILIATION_REQUIRED" for ev in rt.events.items)


@pytest.mark.asyncio
async def test_submit_order_blocks_new_orders_while_reconciliation_pending():
    """RECHECK P0: unresolved-ордер нельзя вытеснить новым — guard в _submit_order."""
    from app.bot.runtime import PaperBotRuntime

    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._trace = None
    rt.pending_orders = {"FIGI123": SimpleNamespace(id="o1", status="PENDING_RECONCILIATION")}
    rt.events = _DummyEvents()
    rt._log = lambda *_a, **_k: None

    await rt._submit_order("FIGI123", "TEST", "open", "BUY")

    assert "FIGI123" in rt.pending_orders
    assert rt.pending_orders["FIGI123"].status == "PENDING_RECONCILIATION"
    assert any(ev[0] == "ORDER_BLOCKED_RECONCILIATION" for ev in rt.events.items)


def test_live_broker_degraded_marks_clear_on_success():
    """RECHECK P1: успешный вызов сбрасывает stale degraded-метку."""
    from app.bot.live_broker import LiveBroker

    broker = LiveBroker.__new__(LiveBroker)
    broker.degraded = {"margin_attributes": {"ts": 0.0, "error": "X"}}
    broker._mark_ok("margin_attributes")
    assert "margin_attributes" not in broker.degraded


def test_filter_sparse_signals_no_ctx_no_nameerror():
    """RECHECK CI: F821 Undefined name ctx — регресс-тест без контекста."""
    from datetime import datetime, timezone

    from app.services.ensemble import _filter_sparse_signals

    sigs = [{"ts": datetime.now(timezone.utc), "side": "BUY"}]
    out = _filter_sparse_signals(sigs, [], 300, 0.5, 0.0)
    assert out == []
