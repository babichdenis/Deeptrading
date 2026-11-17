from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.entry_pause import EntryPause
from app.bot.runtime import PaperBotRuntime


def test_releasing_one_recovery_cannot_clear_manual_or_other_account():
    state=EntryPause(manual=True)
    state.set_blocker('close_reconciliation',True,'live','FIRST','REQUEST')
    state.set_blocker('close_reconciliation',True,'live','SECOND','REQUEST')
    state.set_blocker('close_reconciliation',False,'live','FIRST','REQUEST')
    assert state.paused and state.reasons==['close_reconciliation','manual']
    state.manual=False
    assert state.paused
    state.set_blocker('close_reconciliation',False,'live','SECOND','REQUEST')
    assert not state.paused


@pytest.mark.asyncio
async def test_manual_resume_does_not_override_recovery_or_risk(monkeypatch):
    save=AsyncMock()
    monkeypatch.setattr('app.bot.runtime.save_bot_flags',save)
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    rt.events=SimpleNamespace(log=Mock())
    rt.config=SimpleNamespace(daily_loss_limit=100)
    rt.refresh_daily_pnl=AsyncMock(return_value=-101)
    rt._pause_close(('live','OFFLINE'),'REQUEST')
    rt._entry_pause().set_blocker('daily_loss_limit',True)
    await rt.set_entries_paused(True)
    result=await rt.set_entries_paused(False)
    assert result['entries_paused']
    assert result['entry_pause_reasons']==['close_reconciliation','daily_loss_limit']
    save.assert_awaited_with({'entries_paused':False})
    rt._pause_close(('live','OFFLINE'),'REQUEST',active=False)
    assert rt.entries_paused


@pytest.mark.asyncio
async def test_risk_is_registered_even_while_manually_paused():
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    rt.entries_paused=True
    rt.config=SimpleNamespace(daily_loss_limit=100)
    rt.events=SimpleNamespace(log=Mock())
    rt.refresh_daily_pnl=AsyncMock(return_value=-101)
    await rt._check_circuit_breaker()
    rt.entries_paused=False
    assert rt.entries_paused and rt._entry_pause().reasons==['daily_loss_limit']
    rt.refresh_daily_pnl.return_value=0
    await rt._check_circuit_breaker()
    assert not rt.entries_paused


@pytest.mark.asyncio
async def test_unknown_risk_cannot_resume_until_successful_refresh():
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config=SimpleNamespace(daily_loss_limit=100)
    rt.events=SimpleNamespace(log=Mock())
    rt.refresh_daily_pnl=AsyncMock(side_effect=ValueError('unknown'))
    with pytest.raises(ValueError):await rt._check_circuit_breaker()
    assert rt.entries_paused and rt._entry_pause().reasons==['risk_evidence_unavailable']
    rt.refresh_daily_pnl=AsyncMock(return_value=0)
    await rt._check_circuit_breaker()
    assert not rt.entries_paused


@pytest.mark.asyncio
async def test_manual_resume_rechecks_previously_unknown_risk(monkeypatch):
    monkeypatch.setattr('app.bot.runtime.save_bot_flags',AsyncMock())
    rt=PaperBotRuntime.__new__(PaperBotRuntime)
    rt.entries_paused=True
    rt.config=SimpleNamespace(daily_loss_limit=100)
    rt.events=SimpleNamespace(log=Mock())
    rt.refresh_daily_pnl=AsyncMock(side_effect=ValueError('unknown'))
    assert (await rt.set_entries_paused(False))['entries_paused']
    rt.refresh_daily_pnl=AsyncMock(return_value=0)
    assert not (await rt.set_entries_paused(False))['entries_paused']
