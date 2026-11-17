from types import SimpleNamespace

import pytest
from t_tech.invest.schemas import OrderExecutionReportStatus as Status

from app.bot.live_broker import LiveBroker


@pytest.mark.parametrize('reported,executed,expected',[
    (Status.EXECUTION_REPORT_STATUS_FILL,20,'FILLED'),
    (Status.EXECUTION_REPORT_STATUS_REJECTED,0,'REJECTED'),
    (Status.EXECUTION_REPORT_STATUS_CANCELLED,0,'CANCELLED'),
    (Status.EXECUTION_REPORT_STATUS_NEW,0,'NEW'),
    (Status.EXECUTION_REPORT_STATUS_PARTIALLYFILL,10,'PARTIAL_FILLED'),
    (Status.EXECUTION_REPORT_STATUS_CANCELLED,10,'CANCELLED'),
])
def test_close_status_preserves_actual_sdk_terminal_state(reported,executed,expected):
    assert LiveBroker._close_evidence_status(SimpleNamespace(execution_report_status=reported),executed,20)==expected


@pytest.mark.parametrize('reported',[0,True,1.5,'1',99])
def test_invalid_status_does_not_authorize_release(reported):
    with pytest.raises(ValueError):
        LiveBroker._close_evidence_status(SimpleNamespace(execution_report_status=reported),20,20)


def test_filled_status_requires_exact_confirmed_quantity():
    with pytest.raises(ValueError):
        LiveBroker._close_evidence_status(SimpleNamespace(execution_report_status=Status.EXECUTION_REPORT_STATUS_FILL),10,20)


@pytest.mark.asyncio
@pytest.mark.parametrize('reported',[Status.EXECUTION_REPORT_STATUS_NEW,Status.EXECUTION_REPORT_STATUS_UNSPECIFIED])
async def test_actual_close_never_retries_new_or_unknown_zero_fill(tmp_path,reported):
    from unittest.mock import Mock
    from tests.test_live_close_scope_acceptance import make_broker, FIGI, close_kwargs
    from app.models.broker_order_intent import BrokerOrderIntent
    from app.models.paper import PaperTrade
    broker=await make_broker(tmp_path,units=0,status=reported)
    post=Mock(wraps=broker._api_post_order)
    broker._api_post_order=post
    try:
        receipt=await broker.close_position(FIGI,99,'OFFLINE',**{**close_kwargs(),'client_order_id':'FIRST'})
        assert (receipt is not None)==(reported==Status.EXECUTION_REPORT_STATUS_NEW)
        assert await broker.close_position(FIGI,99,'OFFLINE',**{**close_kwargs(),'client_order_id':'SECOND'}) is None
        assert post.call_count==1
        async with broker.sessions() as db:
            rows=(await db.execute(BrokerOrderIntent.__table__.select())).all()
            trades=(await db.execute(PaperTrade.__table__.select())).all()
        assert len(rows)==1 and rows[0].unresolved_figi==FIGI
        assert rows[0].status=='PENDING_RECONCILIATION' and trades==[]
    finally:
        await broker._engine.dispose()


@pytest.mark.asyncio
async def test_actual_partial_cancel_releases_only_after_delta_is_accounted(tmp_path):
    from tests.test_live_close_scope_acceptance import make_broker, FIGI, close_kwargs
    from app.models.broker_order_intent import BrokerOrderIntent
    broker=await make_broker(tmp_path,units=10,status=Status.EXECUTION_REPORT_STATUS_CANCELLED)
    try:
        trade=await broker.close_position(FIGI,99,'OFFLINE',**{**close_kwargs(),'client_order_id':'FIRST'})
        assert trade is not None and trade.observed_units==10
        async with broker.sessions() as db:
            (row,)=(await db.execute(BrokerOrderIntent.__table__.select())).all()
        assert row.accounted_units==0 and row.observed_units==10
        assert row.broker_status=='CANCELLED' and row.unresolved_figi==FIGI
    finally:
        await broker._engine.dispose()
