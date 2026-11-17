"""Protective exit keeps one request identity and never retries an unresolved submission.

Submission is receipt-only: the adapter records a durable observation and returns it, so no
`PaperTrade` exists here and the legacy `_st_close` call must not run. Completing the close needs
the local projection, which is root's PostgreSQL acceptance, not this SQLite fixture.
"""
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import sqlite3
import pytest
from sqlalchemy import Integer, MetaData, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.live_broker import DEFAULT_ACCOUNT, LiveBroker, LivePosition
from app.bot.runtime import PaperBotRuntime
from app.engine.models import Candle
from app.models.broker_order_intent import BrokerOrderFill, BrokerOrderIntent
from app.models.paper import PaperAccount, PaperTrade
from t_tech.invest.schemas import OrderExecutionReportStatus as Status

FILL = Status.EXECUTION_REPORT_STATUS_FILL
PARTIAL = Status.EXECUTION_REPORT_STATUS_PARTIALLYFILL
CANCELLED = Status.EXECUTION_REPORT_STATUS_CANCELLED


def sqlite_trade_table():
    table = PaperTrade.__table__.to_metadata(MetaData())
    table.c.id.type = Integer()
    for constraint in list(table.foreign_key_constraints):
        table.constraints.discard(constraint)
    for column in table.columns:
        column.foreign_keys.clear()
    return table


async def journal_sessions(tmp_path):
    """The close path now needs the durable journal schema, so the fixture is a real sqlite db."""
    db_path = tmp_path / 'identity.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_path}')
    async with engine.begin() as connection:
        for table in (PaperAccount.__table__, BrokerOrderIntent.__table__,
                      BrokerOrderFill.__table__, sqlite_trade_table()):
            await connection.run_sync(lambda sync, t=table: t.create(sync, checkfirst=True))
        await connection.execute(PaperAccount.__table__.insert().values(
            id=1, name=DEFAULT_ACCOUNT, initial_cash=0, cash=0,
            created_at=datetime(2026, 11, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 11, 1, tzinfo=timezone.utc)))
    return engine, async_sessionmaker(engine, expire_on_commit=False), db_path


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome,executed,status', [
    ('full', 10, FILL),
    ('partial', 4, PARTIAL),
    ('zero', 0, CANCELLED),
    ('timeout', 0, None),
])
async def test_actual_protective_exit_tracks_same_request_and_never_retries_unknown(
        outcome, executed, status, tmp_path):
    figi = 'OFFLINE_FIGI'
    bar = Candle(ts=datetime(2026, 11, 15, 12, tzinfo=timezone.utc), open=94, high=94, low=93, close=94)
    pos = LivePosition(figi, 'TEST', 'LONG', 10, 100, bar.ts, None, None)
    # Provenance must come from the entry side, never from the position snapshot time.
    pos.entry_time_source = 'execution_ledger'
    pos.entry_time_confirmed = bar.ts
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._replay_cur = bar.ts
    rt._replay_from = None
    rt.config = SimpleNamespace(commission_rate=0.003, slippage_bps=0, params={})
    rt._in_trading_session = lambda: True
    rt._log = lambda *args, **kwargs: None
    rt.events = SimpleNamespace(log=lambda *args, **kwargs: None)
    rt.atr_now = lambda *args: 1.0
    rt._exit_plans = {figi: SimpleNamespace()}
    rt._exit_side = {figi: 'LONG'}
    rt._exit_entry_px = {figi: 100}
    rt._exit_qty = {figi: 10}
    rt._trail_stop = {figi: 95}
    rt._trail_active = {figi: True}
    rt._exit_target = {}
    rt._peak_pnl = {}
    rt.buffers = {figi: deque([bar])}
    rt._held = {figi}
    rt._entry_bar_index = {figi: 0}
    rt._bar_counter = 1
    rt._opposite_count = {}
    rt._last_exit_bar = {}
    rt._clear_exit_state = lambda *args: None
    rt._st_close = AsyncMock()
    rt._check_circuit_breaker = AsyncMock()
    broker = LiveBroker.__new__(LiveBroker)
    broker.mode = 'sandbox'
    broker._account = 'OFFLINE_ACCOUNT'
    broker._log = lambda *args: None
    broker.config = rt.config
    broker.get_position = AsyncMock(return_value=pos)
    broker._get_lot = AsyncMock(return_value=1)
    engine, sessions, db_path = await journal_sessions(tmp_path)
    submitted = []
    reserved_at_send = []

    def post(**request):
        assert rt.pending_orders[figi].created_at == bar.ts
        assert request['order_id'] == rt.pending_orders[figi].id
        with sqlite3.connect(db_path) as raw:
            reserved_at_send.append(raw.execute(
                'select count(*) from broker_order_intents').fetchone()[0])
        submitted.append(request)
        if outcome == 'timeout':
            raise TimeoutError('offline')
        return SimpleNamespace(order_id='BROKER_ID', order_request_id=request['order_id'],
                               figi=figi, direction=request['direction'],
                               lots_requested=request['quantity'],
                               lots_executed=executed,
                               execution_report_status=status,
                               executed_order_price=SimpleNamespace(units=94, nano=0))

    broker._api_post_order = post
    broker.sessions = sessions
    rt.broker = broker
    # No completion without the local projection: the exit must report unfinished, not closed.
    assert await rt._step_exit(figi, bar, pos) is False
    assert len(submitted) == 1
    assert submitted[0]['quantity'] == 10
    assert reserved_at_send == [1]
    assert figi in rt._held
    assert figi in rt._exit_plans
    rt._st_close.assert_not_awaited()
    async with sessions() as db:
        recorded = (await db.execute(select(PaperTrade))).scalars().all()
        (intent,) = (await db.execute(select(BrokerOrderIntent))).scalars().all()
    assert recorded == []
    assert intent.observed_units == executed
    assert intent.accounted_units == 0
    pending = rt.pending_orders[figi]
    assert pending.status == 'PENDING_RECONCILIATION'
    assert rt._exit_qty[figi] == 10
    if outcome == 'timeout':
        assert pending.meta.get('error_type') == 'TimeoutError'
        assert intent.observed_units == 0
    elif outcome == 'zero':
        assert pending.meta.get('observed_executed_units') == 0
    else:
        assert pending.meta.get('broker_order_id') == 'BROKER_ID'
        assert pending.meta.get('observed_executed_units') == executed
    # The unresolved request is never resent, whatever the outcome.
    assert await rt._step_exit(figi, bar, pos) is False
    assert len(submitted) == 1
    await engine.dispose()
