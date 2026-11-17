from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.services.open_close_request import CloseAfterOpen, request_close_after_open, load_close_after_open, prepare_close_after_open
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_runtime_open_completion_postgres import initialized


async def ready(engine):
    journal, original, rt = await initialized(engine)
    await request_close_after_open(journal.sessions, original, CloseAfterOpen('STOP_LOSS', original.created_at + timedelta(minutes=3)))
    await rt._reconcile_pending_orders()
    return journal, original, rt


@pytest.mark.asyncio
async def test_handoff_uses_accounted_quantity_original_position_and_stable_id(pg_engine):
    journal, original, rt = await ready(pg_engine)
    rt._submit_close_reconciled = AsyncMock()
    await rt._resume_close_after_open_requests()
    call = rt._submit_close_reconciled.call_args
    assert call.args == ('FIGI', 'OFFLINE', 20, 105.0, 'STOP_LOSS')
    proof = call.kwargs['entry_proof']
    assert call.kwargs['close_order'].id == proof.pending.close_client_order_id
    assert proof.pending.intent == original and proof.units == 20
    row = await journal.load(original)
    assert proof.local_position_id == row.entry_accounting['local_position_id']
    assert rt.entries_paused
    rt.broker._api_post_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['quantity', 'side', 'price', 'scope'])
async def test_handoff_does_not_submit_mismatched_inventory(pg_engine, case):
    journal, original, rt = await ready(pg_engine)
    rt._submit_close_reconciled = AsyncMock()
    held = rt.broker.get_position.return_value
    if case == 'quantity': held.qty = 10
    elif case == 'side': held.side = 'SHORT'
    elif case == 'price': held.entry_price = 999
    else:
        async def switched(figi):
            rt.broker._account = 'OTHER'
            return held
        rt.broker.get_position.side_effect = switched
    await rt._resume_close_after_open_requests()
    rt._submit_close_reconciled.assert_not_awaited()
    assert rt.entries_paused


@pytest.mark.asyncio
async def test_existing_close_reservation_never_authorizes_resend(pg_engine):
    from app.services.broker_order_journal import CloseIntent

    journal, original, rt = await ready(pg_engine)
    pending = (await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id))[0]
    from app.services.close_position_snapshot import ClosePositionSnapshot
    proof = await prepare_close_after_open(journal.sessions, pending)
    snapshot = ClosePositionSnapshot(ticker='OFFLINE', position_side='LONG', position_units=20,
        entry_price=proof.entry_price, observed_at=rt._bot_now(), local_position_id=proof.local_position_id,
        entry_client_order_id=original.client_order_id)
    close = CloseIntent(mode=original.mode, broker_account_id=original.broker_account_id,
        client_order_id=pending.close_client_order_id, figi=original.figi, side='SELL',
        requested_units=20, lot_size=10, created_at=rt._bot_now(), position_snapshot=snapshot)
    assert await journal.reserve(close)
    assert await prepare_close_after_open(journal.sessions, pending) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('lost_response,concurrent,partial_cancel', [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
async def test_actual_handoff_reserves_and_posts_stable_close_once(pg_engine, monkeypatch, lost_response, concurrent, partial_cancel):
    from datetime import datetime
    from types import SimpleNamespace
    from sqlalchemy import text
    from t_tech.invest.schemas import PostOrderResponse, MoneyValue
    from tests.test_runtime_close_projection_postgres import state
    from tests.test_close_stage_settlement_postgres import NOW

    journal, original, rt = await ready(pg_engine)
    pending = (await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id))[0]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW + timedelta(hours=1)
    monkeypatch.setattr('app.bot.live_broker.datetime', Clock)
    class RuntimeClock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW + timedelta(hours=2)
    monkeypatch.setattr('app.bot.runtime.datetime', RuntimeClock)
    async with pg_engine.begin() as db:
        await db.execute(text("UPDATE paper_accounts SET name='default' WHERE id=1"))
    broker = rt.broker
    broker.config = SimpleNamespace(commission_rate=0.0005, use_margin=False)
    broker._log = lambda *args: None
    broker._get_lot = AsyncMock(return_value=10)
    rt._swing = set()
    rt._last_exit_bar = {}
    rt._opposite_count = {}
    rt._bot_now = lambda: NOW + timedelta(hours=2)
    held = broker.get_position.return_value
    held.ticker = 'OFFLINE'
    held.entry_time = NOW + timedelta(minutes=1)
    calls = []
    def post(**kwargs):
        calls.append(kwargs)
        if lost_response:
            raise TimeoutError('accepted close response lost')
        return PostOrderResponse(order_id='CLOSE_EXCHANGE', order_request_id=kwargs['order_id'],
            figi='FIGI', direction=2, lots_requested=kwargs['quantity'], lots_executed=1 if partial_cancel else 2,
            execution_report_status=3 if partial_cancel and len(calls) == 1 else 1,
            executed_order_price=MoneyValue(currency='rub', units=99, nano=0))
    broker._api_post_order = post
    remaining = SimpleNamespace(figi='FIGI', ticker='OFFLINE', side='LONG', qty=10, entry_price=105, entry_time=held.entry_time)
    broker.get_position = AsyncMock(side_effect=lambda figi: held if not calls else remaining if partial_cancel and len(calls) == 1 else None)
    report = state()
    report.order_id = 'CLOSE_EXCHANGE'
    report.order_request_id = pending.close_client_order_id
    report.order_date = NOW + timedelta(hours=1)
    for index, stage in enumerate(report.stages, 1):
        stage.trade_id = f'CLOSE_{index}'
        stage.execution_time = NOW + timedelta(hours=1, minutes=index)
    async def closing_report(*args):
        if not partial_cancel:
            return report
        result = state(count=1, cancelled=len(calls) == 1)
        result.order_id = 'CLOSE_EXCHANGE'
        result.order_request_id = calls[-1]['order_id']
        result.lots_requested = calls[-1]['quantity']
        result.execution_report_status = 3 if len(calls) == 1 else 1
        result.order_date = report.order_date
        result.stages[0].trade_id = f'CLOSE_PART_{len(calls)}'
        result.stages[0].execution_time = NOW + timedelta(hours=1, minutes=len(calls))
        return result
    broker.order_state = AsyncMock(side_effect=closing_report)
    if concurrent:
        import asyncio
        await asyncio.gather(rt._resume_close_after_open_requests(), rt._resume_close_after_open_requests())
    else:
        await rt._resume_close_after_open_requests()
    assert len(calls) == 1 and calls[0]['order_id'] == pending.close_client_order_id
    assert calls[0]['quantity'] == 2
    if partial_cancel:
        await rt._resume_close_after_open_requests()
        assert len(calls) == 2 and calls[1]['quantity'] == 1
        assert calls[1]['order_id'] != calls[0]['order_id']
    if lost_response:
        from tests.test_runtime_close_recovery_postgres import runtime
        await pg_engine.dispose()
        recovered = runtime(pg_engine)
        recovered.broker = broker
        recovered.config = rt.config
        recovered._bot_now = rt._bot_now
        recovered._held = {'FIGI'}
        recovered._swing = set()
        recovered._bar_counter = 100
        for name in ('_exit_plans', '_exit_side', '_exit_qty', '_exit_entry_px', '_trail_active',
                     '_trail_stop', '_exit_target', '_entry_bar_index', '_pos_leverage', '_peak_pnl',
                     '_gb_peak', '_trail_info', '_held_since', '_last_exit_bar', '_opposite_count'):
            setattr(recovered, name, {})
        assert await recovered._restore_close_intents() == 1
        assert await recovered._restore_open_intents() == 0
        assert len(await recovered._restore_close_after_open_requests()) == 1
        assert recovered.pending_orders['FIGI'].id == pending.close_client_order_id
        await recovered._reconcile_pending_orders()
        await recovered._resume_close_after_open_requests()
        rt = recovered
    from app.models.broker_order_intent import BrokerOrderIntent
    async with journal.sessions() as db:
        row = await db.get(BrokerOrderIntent, (original.mode, original.broker_account_id, pending.close_client_order_id))
        assert row.position_snapshot['entry_client_order_id'] == original.client_order_id
        assert row.position_snapshot['local_position_id'] == (await journal.load(original)).entry_accounting['local_position_id']
        assert row.accounted_units == row.runtime_accounted_units == (10 if partial_cancel else 20), (rt.events.log.call_args_list, rt.pending_orders)
    await rt._resume_close_after_open_requests()
    assert len(calls) == (2 if partial_cancel else 1) and not rt.pending_orders
    assert await load_close_after_open(journal.sessions, mode=original.mode, broker_account_id=original.broker_account_id) == []


@pytest.mark.asyncio
async def test_saved_exit_resumes_cancel_after_pending_open_recovery(pg_engine):
    journal, original, rt = await initialized(pg_engine)
    await request_close_after_open(journal.sessions, original, CloseAfterOpen('STOP_LOSS', original.created_at))
    await pg_engine.dispose()
    rt.broker.request_cancel_order = AsyncMock()
    await rt._resume_close_after_open_requests()
    rt.broker.request_cancel_order.assert_awaited_once_with(original.client_order_id,
        expected_scope=(original.mode, original.broker_account_id))
    assert not rt.pending_orders and rt.entries_paused
    row = await journal.load(original)
    assert row.runtime_accounted_units == 20
    rt.broker._api_post_order.assert_not_called()
