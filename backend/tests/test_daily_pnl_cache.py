import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.runtime import PaperBotRuntime, DAILY_PNL_TTL
from app.bot.risk import RiskSnapshot
from app.bot.gates import TimeContext, gate_risk_day

NOW = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)


def runtime():
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker_mode = 'live'
    rt.broker = SimpleNamespace(mode='live', _account='OFFLINE')
    rt.config = SimpleNamespace(daily_loss_limit=100)
    rt._bot_now = lambda: NOW
    rt._daily_pnl_cache = (NOW, -150)
    rt._daily_pnl_cache_scope = rt._daily_pnl_scope()
    rt._turnover_cache = {'FIGI': 12}
    return rt


@pytest.mark.parametrize('elapsed', [timedelta(seconds=-1), DAILY_PNL_TTL, timedelta(minutes=1)])
def test_expired_or_future_value_is_unknown_without_overwriting_loss(elapsed):
    rt = runtime()
    rt._bot_now = lambda: NOW + elapsed
    assert rt.daily_pnl_cached() is None
    assert rt._daily_pnl_cache == (NOW, -150)
    assert rt._turnover_cache == {'FIGI': 12}
    assert not rt.risk_snapshot().entries_allowed()


def test_moscow_day_boundary_invalidates_even_fresh_cache():
    rt = runtime()
    saved = NOW.replace(hour=20, minute=59, second=59)
    rt._daily_pnl_cache = (saved, -150)
    rt._bot_now = lambda: saved + timedelta(seconds=2)
    assert rt.daily_pnl_cached() is None


@pytest.mark.parametrize('change', ['account', 'mode', 'broker'])
def test_scope_change_cannot_reuse_value(change):
    rt = runtime()
    if change == 'account': rt.broker._account = 'SECOND'
    elif change == 'mode': rt.broker_mode = 'sandbox'
    else: rt.broker = SimpleNamespace(mode='live', _account='OFFLINE')
    assert rt.daily_pnl_cached() is None


@pytest.mark.parametrize('pnl', [None, float('nan'), float('inf'), -float('inf')])
def test_unknown_risk_blocks_even_with_disabled_limit(pnl):
    risk = RiskSnapshot(pnl, 0, False)
    assert risk.state == 'UNKNOWN'
    assert not risk.entries_allowed()


@pytest.mark.asyncio
async def test_concurrent_refresh_runs_once_and_retains_known_loss():
    rt = runtime()
    rt._daily_pnl_cache = None
    async def refresh():
        await asyncio.sleep(0)
        rt._daily_pnl_cache = (NOW, -150)
    rt._check_circuit_breaker = AsyncMock(side_effect=refresh)
    assert all(await asyncio.gather(*(rt._ensure_risk_fresh() for _ in range(5))))
    rt._check_circuit_breaker.assert_awaited_once()
    assert not rt.risk_snapshot().entries_allowed()


@pytest.mark.asyncio
async def test_refresh_failure_cannot_authorize_entry():
    rt = runtime()
    rt._daily_pnl_cache = None
    rt.refresh_daily_pnl = AsyncMock(side_effect=RuntimeError('offline'))
    assert not await rt._ensure_risk_fresh()
    assert rt.entries_paused
    assert rt.risk_snapshot().state == 'UNKNOWN'


def test_unknown_risk_gate_formats_without_inventing_zero():
    result = gate_risk_day(TimeContext(cfg=SimpleNamespace(), side='BUY',
        risk_allowed=False, risk_state='UNKNOWN', daily_pnl=None))
    assert not result.passed


def test_paper_run_identity_invalidates_cached_risk():
    rt = runtime()
    rt.broker_mode = 'paper'
    rt.config.test_name = 'FIRST'
    rt._daily_pnl_cache_scope = rt._daily_pnl_scope()
    assert rt.daily_pnl_cached() == -150
    rt.config.test_name = 'SECOND'
    assert rt.daily_pnl_cached() is None
