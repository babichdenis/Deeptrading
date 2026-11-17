from decimal import Decimal

import pytest
from sqlalchemy import event

from app.models.sandbox_trade import SandboxTrade
from tests.test_broker_order_journal_postgres import isolated_postgres, pg_engine
from tests.test_close_after_open_handoff_postgres import ready


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['valid', 'duplicate', 'scope', 'mode', 'nan', 'zero'])
async def test_scoped_protection_update_never_selects_arbitrary_position(pg_engine, case):
    journal, original, rt = await ready(pg_engine)
    row_id = (await journal.load(original)).entry_accounting['local_position_id']
    if case == 'duplicate':
        async with journal.sessions() as db:
            source = await db.get(SandboxTrade, row_id)
            db.add(SandboxTrade(mode=source.mode, broker_account_id=source.broker_account_id,
                figi=source.figi, ticker=source.ticker, side=source.side, qty=source.qty,
                entry_time=source.entry_time, entry_price=source.entry_price, stop_loss=source.stop_loss))
            await db.commit()
    def change_scope(conn, cursor, statement, parameters, context, executemany):
        if 'FOR UPDATE' in statement:
            setattr(rt.broker, 'mode' if case == 'mode' else '_account', 'sandbox' if case == 'mode' else 'OTHER')
    if case in ('scope', 'mode'): event.listen(pg_engine.sync_engine, 'before_cursor_execute', change_scope)
    try:
        await rt._st_update_sl('FIGI', float('nan') if case == 'nan' else 0 if case == 'zero' else 101, True)
    finally:
        if case in ('scope', 'mode'): event.remove(pg_engine.sync_engine, 'before_cursor_execute', change_scope)
    async with journal.sessions() as db:
        row = await db.get(SandboxTrade, row_id)
        assert row.stop_loss == (Decimal('101') if case == 'valid' else original.stop_loss)
        if case == 'valid': assert row.trailing_active and row.take_profit is None
    assert rt.entries_paused == (case != 'valid')


@pytest.mark.asyncio
async def test_success_clears_only_matching_persistence_pause(pg_engine):
    journal, original, rt = await ready(pg_engine)
    await rt._st_update_sl('FIGI', float('nan'), True)
    rt.entries_paused = True
    await rt._st_update_sl('FIGI', 101, True)
    assert rt._entry_pause().manual
    assert not any(key[0] == 'protection_persistence_failed' for key in rt._entry_pause().blockers)


@pytest.mark.asyncio
async def test_paper_protection_is_scoped_to_test_name(pg_engine, monkeypatch):
    from types import SimpleNamespace

    journal, original, rt = await ready(pg_engine)
    row_id = (await journal.load(original)).entry_accounting['local_position_id']
    async with journal.sessions() as db:
        first = await db.get(SandboxTrade, row_id)
        first.mode, first.broker_account_id, first.test_name = 'paper', None, 'FIRST'
        other = SandboxTrade(mode='paper', broker_account_id=None, test_name='SECOND', figi='FIGI',
            ticker='OFFLINE', side='BUY', qty=20, entry_time=first.entry_time,
            entry_price=105, stop_loss=original.stop_loss)
        db.add(other)
        await db.commit()
        other_id = other.id
    rt.broker = SimpleNamespace()
    rt.broker_mode = 'paper'
    rt.config.test_name = 'FIRST'
    monkeypatch.setattr('app.bot.runtime.SessionLocal', journal.sessions)
    await rt._st_update_sl('FIGI', 101, True)
    async with journal.sessions() as db:
        assert (await db.get(SandboxTrade, row_id)).stop_loss == 101
        assert (await db.get(SandboxTrade, other_id)).stop_loss == original.stop_loss
