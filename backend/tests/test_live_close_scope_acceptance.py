from unittest.mock import Mock
from datetime import datetime, timezone
from sqlalchemy import select
from app.models.paper import PaperAccount

import pytest

from app.models.broker_order_intent import BrokerOrderIntent
from app.services.broker_order_journal import BrokerOrderJournal
from tests.test_live_close_journal_wiring import make_broker as legacy_broker, FIGI, ACCOUNT


async def make_broker(tmp_path, **kwargs):
    broker=await legacy_broker(tmp_path, **kwargs)
    async with broker.sessions() as db:
        if await db.scalar(select(PaperAccount).where(PaperAccount.name=='default')) is None:
            db.add(PaperAccount(id=1,name='default',initial_cash=20000,cash=20000))
            await db.commit()
    original=broker._api_post_order
    def post(**request):
        response=original(**request)
        response.order_request_id=request['order_id']
        response.figi=request['instrument_id']
        response.direction=request['direction']
        response.lots_requested=request['quantity']
        return response
    broker._api_post_order=post
    return broker


def close_kwargs():
    return dict(client_order_id='REQUEST',entry_time=datetime(2020,1,1,tzinfo=timezone.utc),entry_time_source='broker_history')


@pytest.mark.asyncio
@pytest.mark.parametrize('field,new_value',[('_account','OTHER'),('mode','live')])
async def test_scope_change_during_reservation_never_sends_to_new_scope(tmp_path,monkeypatch,field,new_value):
    broker=await make_broker(tmp_path)
    reserve=BrokerOrderJournal.reserve
    async def changed(journal,intent):
        result=await reserve(journal,intent)
        setattr(broker,field,new_value)
        return result
    monkeypatch.setattr(BrokerOrderJournal,'reserve',changed)
    post=Mock(wraps=broker._api_post_order)
    broker._api_post_order=post
    try:
        assert await broker.close_position(FIGI,99,'OFFLINE',**close_kwargs()) is None
        post.assert_not_called()
        async with broker.sessions() as db:
            (row,)=(await db.execute(BrokerOrderIntent.__table__.select())).all()
        assert row.mode=='sandbox' and row.broker_account_id==ACCOUNT and row.unresolved_figi==FIGI
    finally:
        await broker._engine.dispose()


@pytest.mark.asyncio
async def test_scope_change_after_send_does_not_settle_under_new_scope(tmp_path):
    broker=await make_broker(tmp_path)
    original=broker._api_post_order
    def post(**request):
        assert request['account_id']==ACCOUNT
        response=original(**request)
        broker._account='OTHER'
        return response
    broker._api_post_order=Mock(side_effect=post)
    try:
        assert await broker.close_position(FIGI,99,'OFFLINE',**close_kwargs()) is None
        async with broker.sessions() as db:
            (row,)=(await db.execute(BrokerOrderIntent.__table__.select())).all()
        assert row.accounted_units==0 and row.unresolved_figi==FIGI and row.broker_account_id==ACCOUNT
    finally:
        await broker._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase',['position','lot'])
async def test_scope_change_in_preflight_leaves_no_reservation_or_send(tmp_path,phase):
    broker=await make_broker(tmp_path)
    method=broker.get_position if phase=='position' else broker._get_lot
    async def changed(*args):
        result=await method(*args)
        broker._account='OTHER'
        return result
    if phase=='position':broker.get_position=changed
    else:broker._get_lot=changed
    post=Mock(wraps=broker._api_post_order)
    broker._api_post_order=post
    try:
        assert await broker.close_position(FIGI,99,'OFFLINE',**close_kwargs()) is None
        post.assert_not_called()
        async with broker.sessions() as db:
            assert (await db.execute(BrokerOrderIntent.__table__.select())).all()==[]
    finally:
        await broker._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['live','sandbox'])
async def test_unchanged_scope_still_submits_and_records_observation_once(tmp_path,mode):
    broker=await make_broker(tmp_path)
    broker.mode=mode
    post=Mock(wraps=broker._api_post_order)
    broker._api_post_order=post
    try:
        trade=await broker.close_position(FIGI,99,'OFFLINE',**close_kwargs())
        assert trade is not None and trade.observed_units==20
        assert post.call_count==1
        assert post.call_args.kwargs['account_id']==ACCOUNT
        assert post.call_args.kwargs['expected_scope']==(mode,ACCOUNT)
        async with broker.sessions() as db:
            (row,)=(await db.execute(BrokerOrderIntent.__table__.select())).all()
        assert row.mode==mode and row.broker_account_id==ACCOUNT and row.accounted_units==0 and row.observed_units==20 and row.unresolved_figi==FIGI
    finally:
        await broker._engine.dispose()


@pytest.mark.parametrize('mode',['live','sandbox'])
@pytest.mark.parametrize('change',[False,True])
def test_actual_dispatch_checks_scope_after_service_lookup(mode,change):
    from types import SimpleNamespace
    from app.bot.live_broker import LiveBroker
    broker=LiveBroker.__new__(LiveBroker)
    broker.mode=mode
    broker._account=ACCOUNT
    broker.config=SimpleNamespace(use_margin=False)
    live=Mock(return_value='LIVE')
    sandbox=Mock(return_value='SANDBOX')
    def services():
        if change:broker.mode='sandbox' if mode=='live' else 'live'
        return SimpleNamespace(orders=SimpleNamespace(post_order=live),sandbox=SimpleNamespace(post_sandbox_order=sandbox))
    broker._get_services=Mock(side_effect=services)
    broker._flush_portfolio=Mock()
    if change:
        with pytest.raises(ValueError,match='during_service_lookup'):
            broker._api_post_order(expected_scope=(mode,ACCOUNT),account_id=ACCOUNT,order_id='REQUEST')
        live.assert_not_called()
        sandbox.assert_not_called()
    else:
        assert broker._api_post_order(expected_scope=(mode,ACCOUNT),account_id=ACCOUNT,order_id='REQUEST')==mode.upper()
        selected,other=(live,sandbox) if mode=='live' else (sandbox,live)
        assert selected.call_count==1 and 'expected_scope' not in selected.call_args.kwargs
        other.assert_not_called()
