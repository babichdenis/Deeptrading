from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from tests.test_runtime_close_all_acceptance import close_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize('result',[
    dict(closed=1,positions=[dict(figi='FIGI',price=None,order_id='ORDER')],pending=[]),
    dict(closed=0,positions=[],pending=[dict(figi='FIGI',status='pending_unknown')]),
    dict(closed=0,positions=[],pending=[]),
])
async def test_manual_route_uses_runtime_and_reports_only_confirmed_closure(monkeypatch,result):
    from app.api.routes import bot
    rt=SimpleNamespace(close_all=AsyncMock(return_value=result))
    monkeypatch.setattr(bot,'runtime',rt)
    fallback=Mock(side_effect=AssertionError('must not switch to sandbox account'))
    monkeypatch.setattr(bot,'_get_sandbox_broker',fallback)
    response=await bot.bot_close_position('FIGI')
    assert response['closed']==bool(result['closed'])
    rt.close_all.assert_awaited_once_with(only_figi='FIGI',reason='manual_close')
    fallback.assert_not_called()


@pytest.mark.asyncio
async def test_selected_position_close_does_not_close_other_tickers():
    rt,figi=close_fixture('full')
    result=await rt.close_all(only_figi='OTHER',reason='manual_close')
    assert result['closed']==0
    rt.broker.close_position.assert_not_awaited()
    result=await rt.close_all(only_figi=figi,reason='manual_close')
    assert result['closed']==1
    assert rt.broker.close_position.call_args.args[2]=='manual_close'


@pytest.mark.asyncio
async def test_scope_change_during_portfolio_read_cannot_close_new_account():
    rt,figi=close_fixture('full')
    rt.broker.mode='live'
    rt.broker._account='FIRST'
    positions=rt.broker.positions.return_value
    async def changed():
        rt.broker._account='SECOND'
        return positions
    rt.broker.positions.side_effect=changed
    result=await rt.close_all(only_figi=figi,reason='manual_close')
    assert result['closed']==0 and result['pending'][0]['status']=='broker_scope_changed'
    rt.broker.close_position.assert_not_awaited()
