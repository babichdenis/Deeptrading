"""Runtime forwards proven entry provenance and the local position link into the close snapshot.

Submission is receipt-only: the broker adapter returns a durable observation, no `PaperTrade`
exists until reconciliation applies it. A row that merely matches instrument, mode and account
is not identity evidence, and a residual is proven only from durable linked close allocations.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import Integer, MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from t_tech.invest.schemas import OrderExecutionReportStatus as Status

import app.bot.runtime as runtime_module
from app.bot.live_broker import DEFAULT_ACCOUNT, LiveBroker, LivePosition
from app.bot.runtime import PaperBotRuntime
from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperAccount, PaperTrade
from app.models.sandbox_trade import SandboxTrade

FIGI = 'PROVENANCE_FIGI'
ACCOUNT = 'PROVENANCE_ACCOUNT'
ENTRY = datetime(2026, 11, 15, 9, tzinfo=timezone.utc)
QTY = 20
PRICE = 100.0
LOT = 1
REQUEST = 'ENTRY-1'
RESIDUAL = QTY - 8


def sqlite_table(model):
    """BIGINT PK is not a rowid alias in sqlite, so the test copy uses Integer."""
    table = model.__table__.to_metadata(MetaData())
    table.c.id.type = Integer()
    for constraint in list(table.foreign_key_constraints):
        table.constraints.discard(constraint)
    for column in table.columns:
        column.foreign_keys.clear()
    return table


def stage_meta(*, side='BUY', qty=QTY, lot=LOT, request=REQUEST, fills=None,
               source='broker_history', confirmed=None):
    """Entry-record metadata of the shape the entry side persists."""
    stages = fills if fills is not None else [
        {'trade_id': 'T-1', 'units': qty // 2, 'price': str(PRICE), 'executed_at': ENTRY.isoformat()},
        {'trade_id': 'T-2', 'units': qty - qty // 2, 'price': str(PRICE),
         'executed_at': (ENTRY + timedelta(minutes=1)).isoformat()},
    ]
    first_time = min(datetime.fromisoformat(stage['executed_at']) for stage in stages) \
        if stages else ENTRY
    return {
        'client_order_id': request,
        'lot_size': lot,
        'entry_time_source': source,
        'entry_time_confirmed': (confirmed if confirmed is not None else first_time.isoformat()),
        'entry_fill_evidence': {
            'version': 1, 'client_order_id': request, 'broker_order_id': 'BR-E1',
            'figi': FIGI, 'side': side, 'currency': 'rub',
            'requested_lots': qty // lot, 'executed_lots': qty // lot, 'lot_size': lot,
            'fills': stages,
        },
    }


async def fixture(tmp_path, *, meta, record_account=ACCOUNT, record_mode='sandbox',
                  record_side='BUY', record_qty=QTY, record_price=PRICE, closes=()):
    db_path = tmp_path / 'provenance.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    async with engine.begin() as connection:
        for table in (PaperAccount.__table__, BrokerOrderIntent.__table__, BrokerOrderFill.__table__,
                      sqlite_table(SandboxTrade), sqlite_table(PaperTrade)):
            await connection.run_sync(lambda sync, t=table: t.create(sync, checkfirst=True))
        await connection.execute(PaperAccount.__table__.insert().values(
            id=1, name=DEFAULT_ACCOUNT, initial_cash=0, cash=0, created_at=ENTRY, updated_at=ENTRY))
        if meta is not None:
            await connection.execute(SandboxTrade.__table__.insert().values(
                id=1, figi=FIGI, ticker='TEST', side=record_side, qty=record_qty, entry_time=ENTRY,
                entry_price=record_price, exit_time=None, mode=record_mode,
                broker_account_id=record_account, test_name=None, meta=json.dumps(meta)))
        for index, close in enumerate(closes):
            await connection.execute(BrokerOrderIntent.__table__.insert().values(
                mode='sandbox', broker_account_id=ACCOUNT,
                client_order_id=close.get('client_order_id', f'CLOSE-{index + 1}'),
                figi=FIGI, side='SELL', requested_units=close.get('observed', 0),
                lot_size=LOT, status='PENDING_RECONCILIATION', created_at=ENTRY,
                observed_units=close.get('observed', 0),
                accounted_units=close.get('accounted', close.get('observed', 0)),
                position_snapshot=close.get('snapshot', {
                    'local_position_id': 1, 'entry_client_order_id': REQUEST})))
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def make_runtime(sessions, *, lots=QTY):
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = 'sandbox'
    broker._account = ACCOUNT
    broker.sessions = sessions
    broker.config = SimpleNamespace(commission_rate=0.0005)
    broker._log = lambda *args: None
    broker.get_position = _async(
        LivePosition(FIGI, 'TEST', 'LONG', QTY, PRICE, ENTRY, None, None))
    broker._get_lot = _async(LOT)
    sent = []

    def post(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(order_id='BR-1', order_request_id=kwargs['order_id'],
                               figi=FIGI, direction=kwargs['direction'],
                               lots_requested=kwargs['quantity'], lots_executed=lots,
                               execution_report_status=Status.EXECUTION_REPORT_STATUS_FILL,
                               executed_order_price=SimpleNamespace(units=99, nano=0))

    broker._api_post_order = post
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(commission_rate=0.0005, test_name=None)
    rt.broker = broker
    rt.pending_orders = {}
    rt.events = SimpleNamespace(log=lambda *args, **kwargs: None)
    rt._log = lambda *args, **kwargs: None
    rt._bot_now = lambda: datetime.now(timezone.utc)
    rt._in_trading_session = lambda: True
    rt._sent = sent
    return rt, broker


def _async(value):
    async def call(*args, **kwargs):
        return value
    return call


async def read_state(sessions):
    async with sessions() as db:
        intents = (await db.execute(select(BrokerOrderIntent))).scalars().all()
        trades = (await db.execute(select(PaperTrade))).scalars().all()
    return intents, trades


@pytest.mark.asyncio
@pytest.mark.parametrize('record_side', ['BUY', 'LONG'])
async def test_proven_entry_and_link_reach_the_snapshot_without_immediate_trade(
        tmp_path, monkeypatch, record_side):
    """Order-direction (BUY) and position-direction (LONG) encodings both identify the cycle."""
    engine, sessions = await fixture(tmp_path, meta=stage_meta(side=record_side),
                                     record_side=record_side)
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        outcome = await rt._submit_close_reconciled(FIGI, 'TEST', QTY, 99.0, 'stop', paper_filled=False)
        assert outcome['status'] in ('pending_unknown', 'reconciled_closed')
        intents, trades = await read_state(sessions)
        (intent,) = intents
        assert trades == []
        assert intent.observed_units == QTY and intent.accounted_units == 0
        snapshot = intent.position_snapshot
        assert datetime.fromisoformat(snapshot['entry_time']) == ENTRY
        assert snapshot['entry_time_source'] == 'broker_history'
        assert snapshot['local_position_id'] == 1
        assert snapshot['entry_client_order_id'] == REQUEST
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('meta,record_account', [
    (stage_meta(source='unknown', confirmed=None), ACCOUNT),
    (stage_meta(), 'FOREIGN_ACCOUNT'),
])
async def test_unproven_or_foreign_entry_time_keeps_snapshot_unlinked(
        tmp_path, monkeypatch, meta, record_account):
    engine, sessions = await fixture(tmp_path, meta=meta, record_account=record_account)
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        outcome = await rt._submit_close_reconciled(FIGI, 'TEST', QTY, 99.0, 'stop', paper_filled=False)
        assert outcome['status'] != 'closed'
        intents, trades = await read_state(sessions)
        (intent,) = intents
        assert trades == []
        assert intent.observed_units == QTY and intent.accounted_units == 0
        snapshot = intent.position_snapshot
        assert snapshot['entry_time'] is None
        assert snapshot['entry_time_source'] == 'unknown'
        assert snapshot['local_position_id'] is None
        assert snapshot['entry_client_order_id'] is None
    finally:
        await engine.dispose()


WRONG_CYCLE = [
    pytest.param({'record_side': 'SELL'}, id='side'),
    pytest.param({'record_side': 'BUY', 'record_qty': -QTY}, id='negative-quantity'),
    pytest.param({'record_qty': 10}, id='quantity'),
    pytest.param({'record_price': 101.0}, id='price'),
    pytest.param({'meta': 'foreign-request'}, id='entry-request'),
    pytest.param({'meta': 'wrong-lot'}, id='lot'),
    pytest.param({'meta': 'short-stages'}, id='stage-units'),
    pytest.param({'meta': 'no-stages'}, id='no-stages'),
    pytest.param({'meta': 'no-evidence'}, id='no-evidence'),
    pytest.param({'meta': 'foreign-figi-in-evidence'}, id='evidence-figi'),
    pytest.param({'meta': 'tampered-confirmed'}, id='confirmed-not-first-stage'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', WRONG_CYCLE)
async def test_record_of_another_cycle_is_not_used_as_identity(tmp_path, monkeypatch, mutation):
    """A recent scoped row with a time source is not proof for this position cycle."""
    kwargs = dict(mutation)
    tag = kwargs.pop('meta', None)
    meta = stage_meta()
    if tag == 'foreign-request':
        meta['entry_fill_evidence']['client_order_id'] = 'OTHER-ENTRY'
    elif tag == 'wrong-lot':
        meta['entry_fill_evidence']['lot_size'] = 10
        meta['entry_fill_evidence']['requested_lots'] = QTY // 10
        meta['entry_fill_evidence']['executed_lots'] = QTY // 10
        meta['entry_fill_evidence']['fills'] = [
            {'trade_id': 'T-1', 'units': 10, 'price': str(PRICE), 'executed_at': ENTRY.isoformat()},
            {'trade_id': 'T-2', 'units': 10, 'price': str(PRICE), 'executed_at': ENTRY.isoformat()}]
    elif tag == 'short-stages':
        meta['entry_fill_evidence']['fills'] = [
            {'trade_id': 'T-1', 'units': 6, 'price': str(PRICE), 'executed_at': ENTRY.isoformat()}]
    elif tag == 'no-stages':
        meta['entry_fill_evidence']['fills'] = []
    elif tag == 'no-evidence':
        meta['entry_fill_evidence'] = None
    elif tag == 'foreign-figi-in-evidence':
        meta['entry_fill_evidence']['figi'] = 'OTHER_FIGI'
    elif tag == 'tampered-confirmed':
        meta['entry_time_confirmed'] = (ENTRY + timedelta(hours=3)).isoformat()
    engine, sessions = await fixture(tmp_path, meta=meta, **kwargs)
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        proven = await rt._entry_provenance(FIGI, ('sandbox', ACCOUNT), side='LONG', units=QTY,
                                            entry_price=PRICE, lot=LOT)
        assert proven == (None, 'unknown', None, None)
        outcome = await rt._submit_close_reconciled(FIGI, 'TEST', QTY, 99.0, 'stop', paper_filled=False)
        assert outcome['status'] != 'closed'
        intents, trades = await read_state(sessions)
        (intent,) = intents
        assert trades == []
        assert intent.observed_units == QTY and intent.accounted_units == 0
        assert intent.position_snapshot['entry_time'] is None
        assert intent.position_snapshot['local_position_id'] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('local_position_id,entry_request', [(0, REQUEST), (1, ''), (-3, REQUEST)])
async def test_unusable_link_is_not_attached_to_the_snapshot(
        tmp_path, local_position_id, entry_request):
    """A partial or invalid identity pair never becomes a guessed link."""
    engine, sessions = await fixture(tmp_path, meta=stage_meta())
    rt, broker = make_runtime(sessions)
    try:
        receipt = await broker.close_position(FIGI, 99.0, 'stop', client_order_id='dc62a612-41e9-459f-b7cc-ef0cf6922304',
                                              expected_units=QTY, entry_time=ENTRY,
                                              entry_time_source='broker_history',
                                              local_position_id=local_position_id,
                                              entry_client_order_id=entry_request)
        assert receipt is not None and receipt.observed_units == QTY
        intents, trades = await read_state(sessions)
        (intent,) = intents
        assert trades == []
        assert intent.position_snapshot['local_position_id'] is None
        assert intent.position_snapshot['entry_client_order_id'] is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_residual_after_partial_close_is_proven_from_linked_allocation(tmp_path, monkeypatch):
    """The remaining quantity is this cycle only because a linked close proves the closed part."""
    engine, sessions = await fixture(tmp_path, meta=stage_meta(),
                                     closes=[{'client_order_id': 'CLOSE-1', 'observed': 8}])
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        proven = await rt._entry_provenance(FIGI, ('sandbox', ACCOUNT), side='LONG', units=RESIDUAL,
                                            entry_price=PRICE, lot=LOT)
        assert proven == (ENTRY, 'broker_history', 1, REQUEST)
    finally:
        await engine.dispose()


RESIDUAL_REJECTIONS = [
    pytest.param([{'observed': 8, 'snapshot': {}}], id='unlinked-allocation'),
    pytest.param([{'observed': 8, 'snapshot': {'local_position_id': 2,
                                              'entry_client_order_id': REQUEST}}],
                 id='foreign-entry-allocation'),
    pytest.param([{'observed': 8, 'snapshot': {'local_position_id': 1,
                                              'entry_client_order_id': 'OTHER-ENTRY'}}],
                 id='foreign-request-allocation'),
    pytest.param([{'observed': 8, 'accounted': 6}], id='not-fully-accounted'),
    pytest.param([{'observed': 6}, {'observed': 6}], id='double-counted'),
    pytest.param([{'observed': 25}], id='more-than-original'),
    pytest.param([], id='no-allocation'),
]


@pytest.mark.asyncio
@pytest.mark.parametrize('closes', RESIDUAL_REJECTIONS)
async def test_unprovable_residual_is_not_used_as_identity(tmp_path, monkeypatch, closes):
    engine, sessions = await fixture(tmp_path, meta=stage_meta(), closes=closes)
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        proven = await rt._entry_provenance(FIGI, ('sandbox', ACCOUNT), side='LONG', units=RESIDUAL,
                                            entry_price=PRICE, lot=LOT)
        assert proven == (None, 'unknown', None, None)
        full = await rt._entry_provenance(FIGI, ('sandbox', ACCOUNT), side='LONG', units=QTY,
                                          entry_price=PRICE, lot=LOT)
        assert full == (ENTRY, 'broker_history', 1, REQUEST)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_closed_units_are_summed_from_linked_allocations(tmp_path, monkeypatch):
    engine, sessions = await fixture(tmp_path, meta=stage_meta(),
                                     closes=[{'observed': 5}, {'observed': 3}])
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        covered = await rt._entry_closed_units(row_id=1, request_id=REQUEST, original_units=QTY,
                                               mode='sandbox', account=ACCOUNT, figi=FIGI)
        assert covered == 8
    finally:
        await engine.dispose()


EXIT_POLICY = {'policy': 'fixed_sl_tp', 'version': 1,
               'params': {'stop_loss_pct': 0.01, 'take_profit_pct': 0.02}}
ENTRY_PLAN = {'requested_units': QTY, 'lot_size': LOT, 'entry_units': QTY,
              'exit_policy': EXIT_POLICY, 'entry_plan_version': 1}


def policy_meta():
    return {**stage_meta(), 'entry_exit_policy': EXIT_POLICY, 'entry_plan': ENTRY_PLAN}


@pytest.mark.asyncio
async def test_policy_and_plan_metadata_do_not_block_provenance(tmp_path, monkeypatch):
    """A record that also carries the exit policy and entry plan still proves this cycle."""
    engine, sessions = await fixture(tmp_path, meta=policy_meta())
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        outcome = await rt._submit_close_reconciled(FIGI, 'TEST', QTY, 99.0, 'stop', paper_filled=False)
        assert outcome['status'] in ('pending_unknown', 'reconciled_closed')
        intents, trades = await read_state(sessions)
        (intent,) = intents
        assert trades == []
        snapshot = intent.position_snapshot
        assert snapshot['local_position_id'] == 1
        assert snapshot['entry_client_order_id'] == REQUEST
        assert datetime.fromisoformat(snapshot['entry_time']) == ENTRY
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_provenance_does_not_rewrite_entry_metadata(tmp_path, monkeypatch):
    """Reading provenance must leave the policy and plan the entry side persisted untouched."""
    engine, sessions = await fixture(tmp_path, meta=policy_meta())
    monkeypatch.setattr(runtime_module, 'SessionLocal', sessions)
    rt, broker = make_runtime(sessions)
    try:
        proven = await rt._entry_provenance(FIGI, ('sandbox', ACCOUNT), side='LONG', units=QTY,
                                            entry_price=PRICE, lot=LOT)
        assert proven == (ENTRY, 'broker_history', 1, REQUEST)
        async with sessions() as db:
            row = (await db.execute(select(SandboxTrade))).scalars().one()
            stored = json.loads(row.meta)
        assert stored['entry_exit_policy'] == EXIT_POLICY
        assert stored['entry_plan'] == ENTRY_PLAN
        assert stored['entry_fill_evidence'] == policy_meta()['entry_fill_evidence']
    finally:
        await engine.dispose()
