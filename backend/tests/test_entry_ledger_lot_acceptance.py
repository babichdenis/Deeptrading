import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.bot.live_broker import LiveBroker
from app.bot.runtime import PaperBotRuntime
from app.models.sandbox_trade import SandboxTrade


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['live', 'sandbox'])
@pytest.mark.parametrize('lot', [1, 10, 1000])
@pytest.mark.parametrize('commit_failure', [False, True])
async def test_entry_ledger_uses_confirmed_lot_and_account(monkeypatch, mode, lot, commit_failure):
    engine = create_engine('sqlite://')
    SandboxTrade.__table__.create(engine)
    session = Session(engine)

    class AsyncSession:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        async def execute(self, query): return session.execute(query)
        async def flush(self): session.flush()
        async def commit(self):
            if commit_failure:
                session.rollback()
                raise RuntimeError("offline commit failure")
            session.commit()
        def add(self, row):
            row.id = 1
            session.add(row)

    monkeypatch.setattr('app.bot.runtime.SessionLocal', AsyncSession)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker = LiveBroker.__new__(LiveBroker)
    rt.broker._account = 'OFFLINE_ACCOUNT'
    rt.broker_mode = mode
    rt.config = SimpleNamespace(test_name='')
    rt._regimes = {}
    rt._entry_regime = {}
    rt.strategies = {}
    rt._entry_card = lambda figi, meta, regime: {'entry': {'reason': 'offline'}, **meta}
    rt._bot_now = lambda: datetime(2026, 11, 16, tzinfo=timezone.utc)
    try:
        if commit_failure:
            with pytest.raises(RuntimeError, match='offline commit failure'):
                await rt._st_open('FIGI', 'TEST', 'BUY', 2, 100, 99, 102,
                                  meta={'lot_size': lot, 'requested_lots': 3, 'client_order_id': 'request'})
            assert session.scalars(select(SandboxTrade)).all() == []
            return
        await rt._st_open('FIGI', 'TEST', 'BUY', 2, 100, 99, 102,
                          meta={'lot_size': lot, 'requested_lots': 3, 'client_order_id': 'request'})
        rows = session.scalars(select(SandboxTrade)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.qty == 2 * lot
        assert row.mode == mode
        assert row.broker_account_id == 'OFFLINE_ACCOUNT'
        assert row.exit_time is None
        assert row.net_pnl is None
        assert json.loads(row.meta)['lot_size'] == lot
        assert float(row.stop_loss) == 99
        assert float(row.take_profit) == 102
        for cumulative in (2, 3, 3):
            await rt._st_open('FIGI', 'TEST', 'BUY', cumulative, 101, 98, 104,
                              meta={'lot_size': lot, 'client_order_id': 'request'})
            assert len(session.scalars(select(SandboxTrade)).all()) == 1
            assert row.qty == cumulative * lot
            assert row.exit_time is None
            assert float(row.stop_loss) == 99
        with pytest.raises(ValueError, match='entry_execution_conflict'):
            await rt._st_open('FIGI', 'TEST', 'BUY', 1, 100, 99, 102,
                              meta={'lot_size': lot, 'client_order_id': 'request'})
        with pytest.raises(ValueError, match='another_open_entry_exists'):
            await rt._st_open('FIGI', 'TEST', 'BUY', 1, 100, 99, 102,
                              meta={'lot_size': lot, 'client_order_id': 'other'})

    finally:
        session.close()
        engine.dispose()
