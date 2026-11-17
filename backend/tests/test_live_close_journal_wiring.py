"""LiveBroker close path: durable reservation before send, observation, no trade before reconcile.

Receipt-only submission: the adapter returns a typed `CloseSubmission` after the durable
observation. A `PaperTrade` appears only when reconciliation applies the observed units, so the
reservation must stay unresolved and accounted units must remain zero here.
"""
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from t_tech.invest.schemas import OrderExecutionReportStatus as Status

from app.bot.live_broker import DEFAULT_ACCOUNT, LiveBroker, LivePosition
from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperAccount, PaperTrade

FIGI = 'OFFLINE_JOURNAL_FIGI'
ACCOUNT = 'OFFLINE_ACCOUNT'
ENTRY = datetime(2026, 11, 15, 10, tzinfo=timezone.utc)
FILL = Status.EXECUTION_REPORT_STATUS_FILL
PARTIAL = Status.EXECUTION_REPORT_STATUS_PARTIALLYFILL
CANCELLED = Status.EXECUTION_REPORT_STATUS_CANCELLED
NEW = Status.EXECUTION_REPORT_STATUS_NEW


def sqlite_trade_table():
    """BIGINT PK is not a rowid alias in sqlite, so the test copy uses Integer."""
    table = PaperTrade.__table__.to_metadata(MetaData())
    table.c.id.type = Integer()
    for constraint in list(table.foreign_key_constraints):
        table.constraints.discard(constraint)
    for column in table.columns:
        column.foreign_keys.clear()
    return table


async def make_broker(tmp_path, *, units=20, lot=1, status=FILL, order_id='BR-1', storage='ok', entry=ENTRY,
                      response_figi=FIGI):
    db_path = tmp_path / 'journal.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    async with engine.begin() as connection:
        for table in (PaperAccount.__table__, BrokerOrderIntent.__table__,
                      BrokerOrderFill.__table__, sqlite_trade_table()):
            await connection.run_sync(lambda sync, t=table: t.create(sync, checkfirst=True))
        await connection.execute(PaperAccount.__table__.insert().values(
            id=1, name=DEFAULT_ACCOUNT, initial_cash=0, cash=0,
            created_at=ENTRY, updated_at=ENTRY))
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = 'sandbox'
    broker._account = ACCOUNT
    broker.sessions = sessions
    broker.config = SimpleNamespace(commission_rate=0.0005)
    broker._log = lambda *args: None
    pos = LivePosition(FIGI, 'TEST', 'LONG', 20, 100.0, ENTRY, None, None)
    if entry is not None:
        pos.entry_time_source = 'execution_ledger'
        pos.entry_time_confirmed = entry
    broker.get_position = _async(pos)
    broker._get_lot = _async(lot)
    seen = {}

    def post(**kwargs):
        with sqlite3.connect(db_path) as raw:
            seen['reserved_before_send'] = raw.execute(
                'select count(*) from broker_order_intents').fetchone()[0]
        seen['request_id'] = kwargs.get('order_id')
        seen['calls'] = seen.get('calls', 0) + 1
        return SimpleNamespace(
            order_id=order_id, order_request_id=kwargs.get('order_id'),
            figi=response_figi, direction=kwargs.get('direction'),
            lots_requested=kwargs.get('quantity'), lots_executed=units,
            execution_report_status=status, executed_order_price=SimpleNamespace(units=99, nano=0))

    broker._api_post_order = post
    if storage != 'ok':
        def broken():
            raise RuntimeError('schema unavailable')
        broker.sessions = broken
    broker._probe = seen
    broker._engine = engine
    return broker


def _async(value):
    async def call(*args, **kwargs):
        return value
    return call


async def close(broker, **kwargs):
    params = {"entry_time": ENTRY, "entry_time_source": "execution_ledger"}
    params.update(kwargs)
    return await broker.close_position(FIGI, 99.0, 'test_close', **params)


async def counted(broker, table):
    async with broker.sessions() as db:
        return (await db.execute(select(table))).scalars().all()


@pytest.mark.asyncio
async def test_close_reserves_before_send_and_records_observation(tmp_path):
    broker = await make_broker(tmp_path)
    receipt = await close(broker)
    assert broker._probe['reserved_before_send'] == 1
    assert receipt is not None and receipt.observed_units == 20
    assert receipt.client_order_id == broker._probe['request_id']
    assert receipt.broker_order_id == 'BR-1'
    (row,) = await counted(broker, BrokerOrderIntent)
    assert row.observed_units == 20 and row.accounted_units == 0
    assert row.status == 'PENDING_RECONCILIATION' and row.unresolved_figi == FIGI
    assert await counted(broker, PaperTrade) == []
    await broker._engine.dispose()


@pytest.mark.asyncio
async def test_close_blocks_duplicate_unresolved_request(tmp_path):
    broker = await make_broker(tmp_path)
    async with broker.sessions() as db:
        db.add(BrokerOrderIntent(mode='sandbox', broker_account_id=ACCOUNT, client_order_id='preexisting',
                                 figi=FIGI, unresolved_figi=FIGI, side='SELL', requested_units=20,
                                 lot_size=1, created_at=ENTRY, status='PENDING_RECONCILIATION'))
        await db.commit()
    assert await close(broker) is None
    assert 'reserved_before_send' not in broker._probe
    await broker._engine.dispose()


@pytest.mark.asyncio
async def test_close_fails_closed_without_storage(tmp_path):
    broker = await make_broker(tmp_path, storage='broken')
    assert await close(broker) is None
    assert 'reserved_before_send' not in broker._probe
    await broker._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [CANCELLED, NEW])
async def test_zero_fill_records_no_trade(tmp_path, status):
    broker = await make_broker(tmp_path, units=0, status=status)
    receipt = await close(broker)
    assert receipt is not None and receipt.observed_units == 0
    (row,) = await counted(broker, BrokerOrderIntent)
    assert await counted(broker, PaperTrade) == []
    assert row.accounted_units == 0
    if status == CANCELLED:
        assert row.unresolved_figi is None      # confirmed cancel with nothing to apply releases
    else:
        # NEW zero is not terminal: the reservation must stay, and a retry must not resend.
        assert row.unresolved_figi == FIGI
        assert await close(broker) is None
        assert len(await counted(broker, BrokerOrderIntent)) == 1
        assert broker._probe['calls'] == 1
    await broker._engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [PARTIAL, CANCELLED])
async def test_partial_execution_is_observed_before_accounting(tmp_path, status):
    broker = await make_broker(tmp_path, units=10, status=status)
    receipt = await close(broker)
    assert receipt is not None and receipt.observed_units == 10
    (row,) = await counted(broker, BrokerOrderIntent)
    assert row.observed_units == 10 and row.accounted_units == 0
    assert row.unresolved_figi == FIGI          # slot stays until the delta is applied
    assert await counted(broker, PaperTrade) == []
    await broker._engine.dispose()


@pytest.mark.asyncio
async def test_response_for_another_instrument_is_not_recorded(tmp_path):
    broker = await make_broker(tmp_path, response_figi='SOME_OTHER_FIGI')
    assert await close(broker) is None
    (row,) = await counted(broker, BrokerOrderIntent)
    assert row.observed_units == 0 and row.unresolved_figi == FIGI
    assert await counted(broker, PaperTrade) == []
    await broker._engine.dispose()


@pytest.mark.asyncio
async def test_unknown_entry_time_still_observes_without_accounting(tmp_path):
    broker = await make_broker(tmp_path, entry=None)
    receipt = await broker.close_position(FIGI, 99.0, 'test_close')
    assert receipt is not None and receipt.observed_units == 20
    (row,) = await counted(broker, BrokerOrderIntent)
    assert row.observed_units == 20 and row.unresolved_figi == FIGI
    assert row.accounted_units == 0
    assert await counted(broker, PaperTrade) == []
    await broker._engine.dispose()
