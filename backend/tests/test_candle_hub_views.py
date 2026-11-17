from datetime import datetime,timedelta,timezone
from decimal import Decimal

import pytest
from app.services.candle_hub_history import read_history_range
from app.services.candle_hub_views import historical_timeframe_views

START=datetime(2026,11,9,9,tzinfo=timezone.utc)
END=START+timedelta(hours=2)


class Rows:
    def __init__(self,rows):
        self.rows=rows

    def all(self):
        return self.rows


class Database:
    def __init__(self,rows):
        self.rows=rows
        self.calls=0

    async def execute(self,query):
        self.calls+=1
        return Rows(self.rows)


def records(count,missing):
    return [(START+timedelta(minutes=i),Decimal('100'),Decimal('101'),Decimal('99'),Decimal('100'),10) for i in range(count) if i not in missing]


@pytest.mark.asyncio
@pytest.mark.parametrize('missing',[set(),{7,8,69}])
@pytest.mark.parametrize('full',[True,False])
async def test_shared_snapshot_matches_independent_hub_reads(missing,full):
    db=Database(records(120,missing))
    raw=await read_history_range(db,'TEST','1min',START,END,as_of=END)
    views=historical_timeframe_views(raw,'TEST',('5min','15min','hour'),start=START,end=END,as_of=END,require_full_minutes=full)
    assert db.calls==1
    for tf,view in views.items():
        expected=await read_history_range(db,'TEST',tf,START,END,as_of=END,require_full_minutes=full)
        assert view==expected


@pytest.mark.asyncio
async def test_unclosed_hour_keeps_not_visible_contract():
    as_of=START+timedelta(minutes=30)
    db=Database(records(30,set()))
    raw=await read_history_range(db,'TEST','1min',START,END,as_of=as_of)
    views=historical_timeframe_views(raw,'TEST',('hour',),start=START,end=END,as_of=as_of)
    expected=await read_history_range(db,'TEST','hour',START,END,as_of=as_of)
    assert views['hour']==expected


@pytest.mark.asyncio
async def test_known_closed_session_preserves_evidence():
    db=Database([])
    raw=await read_history_range(db,'TEST','1min',START,END,as_of=END,expected_minutes=[],calendar_evidence='synthetic closed session')
    view=historical_timeframe_views(raw,'TEST',('hour',),start=START,end=END,as_of=END)['hour']
    assert view.status=='no_trading' and view.coverage_known
    assert view.calendar_evidence=='synthetic closed session'


@pytest.mark.asyncio
async def test_snapshot_from_larger_range_is_rejected():
    raw=await read_history_range(Database(records(120,set())),'TEST','1min',START,END,as_of=END)
    with pytest.raises(ValueError,match='outside requested range'):
        historical_timeframe_views(raw,'TEST',('hour',),start=START+timedelta(hours=1),end=END,as_of=END)


@pytest.mark.asyncio
async def test_aggregated_input_is_rejected():
    raw=await read_history_range(Database(records(120,set())),'TEST','5min',START,END,as_of=END)
    with pytest.raises(ValueError,match='Unaggregated'):
        historical_timeframe_views(raw,'TEST',('hour',),start=START,end=END,as_of=END)


@pytest.mark.asyncio
async def test_source_error_matches_independent_reads():
    from sqlalchemy.exc import SQLAlchemyError

    class FailedDatabase:
        async def execute(self,query):
            raise SQLAlchemyError('synthetic unavailable source')

    for elapsed in (30,120):
        as_of=START+timedelta(minutes=elapsed)
        raw=await read_history_range(FailedDatabase(),'TEST','1min',START,END,as_of=as_of)
        view=historical_timeframe_views(raw,'TEST',('hour',),start=START,end=END,as_of=as_of)['hour']
        expected=await read_history_range(FailedDatabase(),'TEST','hour',START,END,as_of=as_of)
        assert view==expected
