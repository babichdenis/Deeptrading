import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import tempfile
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.broker_order_journal import BrokerOrderJournal, CloseIntent
from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperAccount, PaperTrade


class IsolatedPostgres:
    def __init__(self, binary, root):
        self.binary = Path(binary)
        self.data = root / 'data'
        self.log = root / 'postgres.log'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            self.port = listener.getsockname()[1]
        self.url = f'postgresql+asyncpg://journal_test@127.0.0.1:{self.port}/postgres'

    def command(self, executable, *args):
        command = str(self.binary / (executable + ('.exe' if os.name == 'nt' else '')))
        with tempfile.TemporaryFile() as output:
            result = subprocess.run([command, *map(str, args)], stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, timeout=45)
            if result.returncode:
                output.seek(0)
                raise RuntimeError(f'{executable} failed: {output.read().decode(errors="replace")}')

    def start(self):
        self.command('pg_ctl', '-D', self.data, '-l', self.log, '-w', '-t', '30',
                     '-o', f'-h 127.0.0.1 -p {self.port}', 'start')

    def stop(self):
        self.command('pg_ctl', '-D', self.data, '-w', '-t', '30', '-m', 'fast', 'stop')


@pytest.fixture(scope='module')
def isolated_postgres(tmp_path_factory):
    binary = os.environ.get('DEEPTRADING_TEST_PG_BIN')
    if not binary:
        pytest.skip('Set DEEPTRADING_TEST_PG_BIN to launch a fresh temporary cluster')
    if not (Path(binary) / ('initdb.exe' if os.name == 'nt' else 'initdb')).is_file():
        pytest.fail('Explicit PostgreSQL test binaries are unavailable')
    cluster = IsolatedPostgres(binary, tmp_path_factory.mktemp('journal_pg'))
    cluster.command('initdb', '-D', cluster.data, '-U', 'journal_test', '-A', 'trust', '--no-locale', '-E', 'UTF8')
    try:
        cluster.start()
        yield cluster
    finally:
        if (cluster.data / 'postmaster.pid').exists():
            cluster.stop()


@pytest_asyncio.fixture
async def pg_engine(isolated_postgres):
    schema = 'journal_' + uuid4().hex
    engine = create_async_engine(isolated_postgres.url)
    async with engine.begin() as db:
        await db.execute(text(f'CREATE SCHEMA {schema}'))
    await engine.dispose()
    engine = create_async_engine(isolated_postgres.url, connect_args={'server_settings': {'search_path': schema}})
    try:
        yield engine
    finally:
        await engine.dispose()


def migration():
    path = Path(__file__).resolve().parents[1] / 'alembic/versions/0006_broker_order_intents.py'
    spec = importlib.util.spec_from_file_location('pg_intent_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def apply_migration(connection):
    module = migration()
    module.op = Operations(MigrationContext.configure(connection))
    module.upgrade()


async def prepare(engine, *, source_width=64, request_column=True, account_column=True):
    async with engine.begin() as db:
        await db.run_sync(PaperAccount.__table__.create)
        await db.run_sync(PaperTrade.__table__.create)
        await db.execute(PaperAccount.__table__.insert().values(id=1, name='OFFLINE', initial_cash=20000, cash=20000))
        request = ', request_id VARCHAR(32)' if request_column else ''
        account = ', broker_account_id VARCHAR(40)' if account_column else ''
        await db.execute(text(f'CREATE TABLE bot_logs (source VARCHAR({source_width}){request})'))
        await db.execute(text(f'CREATE TABLE sandbox_trades (id INTEGER{account})'))


def intent(**changes):
    return replace(CloseIntent('live', 'OFFLINE_ACCOUNT', 'REQUEST_1', 'FIGI', 'SELL', 20, 10,
                               datetime(2026, 11, 16, tzinfo=timezone.utc)), **changes)


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy', [{'source_width':16}, {'request_column':False}, {'account_column':False}])
async def test_migration_refuses_incomplete_legacy_schema(pg_engine, legacy):
    await prepare(pg_engine, **legacy)
    with pytest.raises(DBAPIError, match='Historical revision 0002 schema is incomplete'):
        async with pg_engine.begin() as db:
            await db.run_sync(apply_migration)
    async with pg_engine.connect() as db:
        assert await db.scalar(text("SELECT to_regclass('broker_order_intents') IS NULL"))


@pytest.mark.asyncio
async def test_postgres_reservation_concurrency_scope_and_restart(pg_engine, isolated_postgres):
    await prepare(pg_engine)
    async with pg_engine.begin() as db:
        assert await db.scalar(text('SHOW fsync')) == 'on'
        assert await db.scalar(text('SHOW synchronous_commit')) == 'on'
        await db.run_sync(apply_migration)
    journal = BrokerOrderJournal(async_sessionmaker(pg_engine, expire_on_commit=False))
    candidates = [intent(client_order_id=f'REQUEST_{i}') for i in range(8)]
    results = await asyncio.gather(*(journal.reserve(candidate) for candidate in candidates))
    assert results.count(True) == 1
    winner = candidates[results.index(True)]
    assert await journal.reserve(intent(mode='sandbox'))
    assert await journal.reserve(intent(broker_account_id='OFFLINE_OTHER'))
    await pg_engine.dispose()
    await asyncio.to_thread(isolated_postgres.stop)
    await asyncio.to_thread(isolated_postgres.start)
    rows = await journal.unresolved(mode='live', broker_account_id='OFFLINE_ACCOUNT')
    assert len(rows) == 1 and rows[0].client_order_id == winner.client_order_id
    assert not await journal.reserve(winner)
    assert not await journal.reserve(intent(client_order_id='AFTER_RESTART'))
    assert await journal.observe(winner, broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    assert await journal.reserve(intent(client_order_id='AFTER_RELEASE'))


@pytest.mark.asyncio
async def test_postgres_concurrent_observations_cannot_regress_or_release_fill(pg_engine):
    await prepare(pg_engine)
    async with pg_engine.begin() as db:
        await db.run_sync(apply_migration)
    journal = BrokerOrderJournal(async_sessionmaker(pg_engine, expire_on_commit=False))
    assert await journal.reserve(intent())
    await asyncio.gather(
        journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='PARTIAL_FILLED', executed_units=10),
        journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20),
    )
    rows = await journal.unresolved(mode='live', broker_account_id='OFFLINE_ACCOUNT')
    assert len(rows) == 1 and rows[0].observed_units == 20
    assert not await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=0)
    assert not await journal.reserve(intent(client_order_id='REPEAT'))


def delta_trade(units, **changes):
    values = dict(account_id=1, figi='FIGI', ticker='OFFLINE', side='LONG', qty=units,
                  entry_time=datetime(2026,11,16,10,tzinfo=timezone.utc), entry_price=100,
                  exit_time=datetime(2026,11,16,11,tzinfo=timezone.utc), exit_price=102,
                  gross_pnl=2*units, commission=1, slippage=0, net_pnl=2*units-1,
                  exit_reason='OFFLINE', strategy_id='OFFLINE')
    values.update(changes)
    return PaperTrade(**values)


async def settlement_journal(engine):
    await prepare(engine)
    async with engine.begin() as db:
        await db.run_sync(apply_migration)
    journal = BrokerOrderJournal(async_sessionmaker(engine, expire_on_commit=False))
    assert await journal.reserve(intent())
    return journal


@pytest.mark.asyncio
async def test_concurrent_settlement_records_one_trade_and_releases_full_fill(pg_engine):
    journal = await settlement_journal(pg_engine)
    assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    results = await asyncio.gather(*(
        journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=20, trade=delta_trade(20))
        for _ in range(8)
    ))
    assert sorted(result.applied_units for result in results) == [0]*7+[20]
    assert len({result.trade_id for result in results}) == 1
    assert all(result.released and result.remaining_units == 0 for result in results)
    async with pg_engine.connect() as db:
        assert await db.scalar(select(func.count()).select_from(PaperTrade)) == 1
        assert await db.scalar(select(func.count()).select_from(BrokerOrderFill)) == 1
    assert await journal.reserve(intent(client_order_id='NEXT'))


@pytest.mark.asyncio
async def test_partial_settlement_then_terminal_release_and_idempotent_reopen(pg_engine):
    journal = await settlement_journal(pg_engine)
    assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='PARTIAL_FILLED', executed_units=10)
    first = await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=10, trade=delta_trade(10))
    assert first.applied_units == 10 and first.remaining_units == 10 and not first.released
    assert not await journal.reserve(intent(client_order_id='NEXT'))
    assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='CANCELLED', executed_units=10)
    final = await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=10)
    assert final.applied_units == 0 and final.released and final.trade_id == first.trade_id
    await pg_engine.dispose()
    repeated = await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=10)
    assert repeated == final
    assert await journal.reserve(intent(client_order_id='NEXT'))


@pytest.mark.asyncio
async def test_two_cumulative_fills_record_only_incremental_quantity(pg_engine):
    journal = await settlement_journal(pg_engine)
    for units, status in [(10,'PARTIAL_FILLED'),(20,'FILLED')]:
        assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status=status, executed_units=units)
        result = await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=units, trade=delta_trade(10))
        assert result.applied_units == 10 and result.accounted_units == units
    async with pg_engine.connect() as db:
        assert await db.scalar(select(func.sum(PaperTrade.qty))) == 20
        assert await db.scalar(select(func.count()).select_from(BrokerOrderFill)) == 2
    with pytest.raises(ValueError, match='stored broker evidence'):
        await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=10)


@pytest.mark.asyncio
async def test_failed_trade_write_rolls_back_accounting_and_keeps_reservation(pg_engine):
    journal = await settlement_journal(pg_engine)
    assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    with pytest.raises(DBAPIError):
        await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=20, trade=delta_trade(20, account_id=999))
    rows = await journal.unresolved(mode='live', broker_account_id='OFFLINE_ACCOUNT')
    assert len(rows) == 1 and rows[0].accounted_units == 0
    async with pg_engine.connect() as db:
        assert await db.scalar(select(func.count()).select_from(PaperTrade)) == 0
        assert await db.scalar(select(func.count()).select_from(BrokerOrderFill)) == 0
    assert not await journal.reserve(intent(client_order_id='NEXT'))
    assert (await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=20, trade=delta_trade(20))).released


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [{'figi':'OTHER'}, {'side':'SHORT'}, {'qty':10}, {'exit_price':float('nan')}])
async def test_invalid_delta_trade_does_not_change_accounting(pg_engine, changes):
    journal = await settlement_journal(pg_engine)
    assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    with pytest.raises(ValueError):
        await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=20, trade=delta_trade(20, **changes))
    assert (await journal.unresolved(mode='live', broker_account_id='OFFLINE_ACCOUNT'))[0].accounted_units == 0


@pytest.mark.asyncio
async def test_fill_ledger_failure_rolls_back_already_flushed_trade(pg_engine):
    journal = await settlement_journal(pg_engine)
    assert await journal.observe(intent(), broker_order_id='EXCHANGE', broker_status='FILLED', executed_units=20)
    def fail_fill_insert(mapper, connection, target):
        raise RuntimeError('OFFLINE injected fill ledger failure')
    event.listen(BrokerOrderFill, 'before_insert', fail_fill_insert)
    try:
        with pytest.raises(RuntimeError, match='OFFLINE injected'):
            await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=20, trade=delta_trade(20))
    finally:
        event.remove(BrokerOrderFill, 'before_insert', fail_fill_insert)
    async with pg_engine.connect() as db:
        assert await db.scalar(select(func.count()).select_from(PaperTrade)) == 0
        assert await db.scalar(select(func.count()).select_from(BrokerOrderFill)) == 0
    rows = await journal.unresolved(mode='live', broker_account_id='OFFLINE_ACCOUNT')
    assert len(rows) == 1 and rows[0].accounted_units == 0
    assert (await journal.settle(intent(), broker_order_id='EXCHANGE', cumulative_units=20, trade=delta_trade(20))).released


@pytest.mark.asyncio
@pytest.mark.parametrize('entry_price', ['100', '100.123456789'])
async def test_original_position_snapshot_survives_reopen_and_cannot_be_replaced(pg_engine, entry_price):
    from decimal import Decimal
    from app.services.close_position_snapshot import ClosePositionSnapshot
    await prepare(pg_engine)
    async with pg_engine.begin() as db:
        await db.run_sync(apply_migration)
    journal = BrokerOrderJournal(async_sessionmaker(pg_engine, expire_on_commit=False))
    saved = ClosePositionSnapshot('OFFLINE','LONG',20,Decimal(entry_price),
                                  datetime(2026,11,16,11,tzinfo=timezone.utc),
                                  datetime(2026,11,16,10,tzinfo=timezone.utc),'execution_ledger',
                                  'OFFLINE','OFFLINE',1,Decimal('0.003'),'configured_estimate')
    original = intent(created_at=saved.observed_at, position_snapshot=saved)
    assert await journal.reserve(original)
    assert not await journal.reserve(replace(original, position_snapshot=replace(saved, entry_price=Decimal('90'))))
    await pg_engine.dispose()
    row = (await journal.unresolved(mode='live',broker_account_id='OFFLINE_ACCOUNT'))[0]
    restored = CloseIntent.from_record(row)
    assert restored == original
    assert await journal.observe(restored,broker_order_id='EXCHANGE',broker_status='FILLED',executed_units=20)
    with pytest.raises(ValueError,match='saved original position'):
        await journal.settle(restored,broker_order_id='EXCHANGE',cumulative_units=20,trade=delta_trade(20,entry_price=90))
    assert (await journal.settle(restored,broker_order_id='EXCHANGE',cumulative_units=20,
                                 trade=delta_trade(20,entry_price=Decimal(entry_price).quantize(Decimal('0.000001'))))).released


@pytest.mark.asyncio
async def test_unknown_original_entry_time_cannot_be_fabricated_during_recovery(pg_engine):
    from decimal import Decimal
    from app.services.close_position_snapshot import ClosePositionSnapshot
    await prepare(pg_engine)
    async with pg_engine.begin() as db:
        await db.run_sync(apply_migration)
    journal = BrokerOrderJournal(async_sessionmaker(pg_engine, expire_on_commit=False))
    saved = ClosePositionSnapshot('OFFLINE','LONG',20,Decimal('100'),datetime(2026,11,16,11,tzinfo=timezone.utc))
    original = intent(created_at=saved.observed_at,position_snapshot=saved)
    assert await journal.reserve(original)
    assert await journal.observe(original,broker_order_id='EXCHANGE',broker_status='FILLED',executed_units=20)
    with pytest.raises(ValueError,match='Confirmed entry time unavailable'):
        await journal.settle(original,broker_order_id='EXCHANGE',cumulative_units=20,trade=delta_trade(20))
    row = (await journal.unresolved(mode='live',broker_account_id='OFFLINE_ACCOUNT'))[0]
    assert row.accounted_units == 0 and row.observed_units == 20
