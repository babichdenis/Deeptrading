from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app.bot.runtime import PaperBotRuntime
from app.bot.runtime_models import BotOrder


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['PENDING_RECONCILIATION', 'PARTIAL_FILLED'])
async def test_local_cancel_does_not_forget_submitted_order(status):
    order=BotOrder(created_at=datetime(2026,11,16,tzinfo=timezone.utc),
        id='OFFLINE_REQUEST',figi='TEST',ticker='TEST',action='close',side='SELL',
        qty=20,status=status,meta={'requested_units':20})
    runtime=PaperBotRuntime.__new__(PaperBotRuntime)
    runtime.pending_orders={'TEST':order}
    runtime._approvals_since={}
    runtime.events=SimpleNamespace(log=Mock())
    result=await runtime.cancel_pending()
    assert runtime.pending_orders.get('TEST') is order
    assert order.status==status
    assert result['cancelled']==0
    assert not any(call.args[0]=='ORDER_CANCELLED' for call in runtime.events.log.call_args_list)


@pytest.mark.asyncio
async def test_local_cancel_still_removes_unsent_approval():
    order=BotOrder(created_at=datetime(2026,11,16,tzinfo=timezone.utc),
        id='OFFLINE_UNSENT',figi='TEST',ticker='TEST',action='open',side='BUY',
        qty=20,status='PENDING_APPROVAL')
    runtime=PaperBotRuntime.__new__(PaperBotRuntime)
    runtime.pending_orders={'TEST':order}
    runtime._approvals_since={order.id:1}
    runtime.events=SimpleNamespace(log=Mock())
    result=await runtime.cancel_pending()
    assert result['cancelled']==1 and result['pending_reconciliation']==0
    assert not runtime.pending_orders and not runtime._approvals_since
    assert order.status=='CANCELLED'
