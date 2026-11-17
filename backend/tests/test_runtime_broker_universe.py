from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.bot.runtime import PaperBotRuntime
from app.bot.universe.runtime_select import select_broker_candidates


def evidence():
    return dict(figi='A', ticker='A', uid='uid', class_code='TQBR', lot=1,
                api_trade_available_flag=True, buy_available_flag=True,
                sell_available_flag=True, short_enabled_flag=False)


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['unknown', 'stale', 'short_disabled', 'not_selected', 'wrong_mode'])
async def test_actual_submit_rejects_invalid_universe_before_sizing(case):
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._trace = None
    rt.pending_orders = {}
    rt.config = SimpleNamespace(mode='live', feed='live')
    rt._candidate_mode = 'sandbox' if case == 'wrong_mode' else 'live'
    rt._candidate_catalog = {} if case == 'unknown' else {'A': evidence()}
    rt._candidate_observed_at = datetime.now(timezone.utc) - timedelta(hours=2 if case == 'stale' else 0)
    rt._candidate_figis = set() if case == 'not_selected' else {'A'}
    rejections = []
    rt._trace_order_reject = lambda *args: rejections.append(args)
    await rt._submit_order('A', 'A', 'open', 'SELL' if case == 'short_disabled' else 'BUY')
    assert len(rejections) == 1
    assert rejections[0][4] == 'universe_admission'
    assert not rt.pending_orders
    if case == 'wrong_mode':
        assert rejections[0][5] == 'broker_evidence_mode_mismatch'


@pytest.mark.asyncio
async def test_shared_selector_uses_explicit_clock_hub_and_broker_lot(monkeypatch):
    import app.bot.universe.bars as bars
    import app.bot.universe.features as features

    now = datetime.now(timezone.utc)
    class Database:
        async def execute(self, query):
            assert 'eligible_tier' not in str(query)
            return SimpleNamespace(all=lambda: [('A', 'wrong', 99, 0, 0, '')])
    reader = SimpleNamespace(tail_bars=AsyncMock(return_value=[object()] * 44))
    monkeypatch.setattr(bars, '_reader', lambda: reader)
    monkeypatch.setattr(features, 'atr_pct', lambda data, window: (3.0, True))
    selected, decisions = await select_broker_candidates(Database(), catalog={'A': evidence()},
        observed_at=now, as_of=now, top_n=5)
    assert selected[0]['lot'] == selected[0]['lot_size'] == 1
    assert selected[0]['ticker'] == 'A'
    assert selected[0]['short_allowed'] is False
    assert reader.tail_bars.call_args.kwargs['as_of'] == now
    assert decisions[0]['reasons'] == []


@pytest.mark.asyncio
async def test_refresh_uses_broker_mode_and_reuses_catalog(monkeypatch):
    import app.bot.runtime as runtime
    import app.bot.universe.runtime_select as selection
    import app.services.tinvest as tinvest

    calls = []
    now = datetime.now(timezone.utc)
    def fetch(mode):
        calls.append(mode)
        return {'A': evidence()}, now
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(tinvest, 'fetch_share_catalog', fetch)
    monkeypatch.setattr(runtime, 'SessionLocal', Session)
    select = AsyncMock(return_value=([{'figi': 'A'}], []))
    monkeypatch.setattr(selection, 'select_broker_candidates', select)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(mode='sandbox', use_ensemble=False, top_n=5)
    await rt._refresh_broker_candidates()
    await rt._refresh_broker_candidates()
    assert calls == ['sandbox']
    assert rt._candidate_figis == {'A'}
    rt.config.mode = 'live'
    await rt._refresh_broker_candidates()
    assert calls == ['sandbox', 'live']


@pytest.mark.asyncio
async def test_hot_refresh_does_not_liquidate_removed_candidate(monkeypatch):
    import asyncio
    import app.bot.runtime as runtime

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    async def stop(*args):
        raise asyncio.CancelledError
    monkeypatch.setattr(runtime, 'SessionLocal', Session)
    monkeypatch.setattr(runtime.asyncio, 'sleep', stop)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.running = True
    rt.config = SimpleNamespace(mode='live', feed='live')
    rt.universe = [{'figi': 'A', 'ticker': 'A'}]
    rt.carousel_diag = {}
    rt._refresh_broker_candidates = AsyncMock(return_value=[])
    rt.deactivate_figi = AsyncMock()
    rt._log = lambda *args, **kwargs: None
    with pytest.raises(asyncio.CancelledError):
        await rt._hot_add_universe()
    rt.deactivate_figi.assert_not_awaited()
    assert rt.universe == [{'figi': 'A', 'ticker': 'A'}]


@pytest.mark.asyncio
async def test_failed_refresh_cannot_mix_new_catalog_with_old_candidates(monkeypatch):
    import app.bot.runtime as runtime
    import app.bot.universe.runtime_select as selection
    import app.services.tinvest as tinvest

    old_time = datetime.now(timezone.utc) - timedelta(hours=2)
    new_time = datetime.now(timezone.utc)
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(runtime, 'SessionLocal', Session)
    monkeypatch.setattr(tinvest, 'fetch_share_catalog', lambda mode: ({'A': evidence()}, new_time))
    monkeypatch.setattr(selection, 'select_broker_candidates', AsyncMock(side_effect=RuntimeError('Hub unavailable')))
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(mode='live', use_ensemble=False, top_n=5)
    rt._candidate_mode = 'sandbox'
    rt._candidate_observed_at = old_time
    rt._candidate_catalog = {'OLD': {}}
    rt._candidate_figis = {'OLD'}
    with pytest.raises(RuntimeError, match='Hub unavailable'):
        await rt._refresh_broker_candidates()
    assert rt._candidate_mode == 'sandbox'
    assert rt._candidate_observed_at == old_time
    assert rt._candidate_catalog == {'OLD': {}}
    assert rt._candidate_figis == {'OLD'}


@pytest.mark.asyncio
async def test_refresh_applies_manual_selection_after_shared_screen(monkeypatch):
    import app.bot.runtime as runtime
    import app.bot.universe.runtime_select as selection

    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
    monkeypatch.setattr(runtime, 'SessionLocal', Session)
    monkeypatch.setattr(runtime, 'load_universe_overrides', lambda: [dict(figi='B', side='long', lot=999)])
    select = AsyncMock(return_value=(
        [dict(figi='A', lot=1), dict(figi='B', lot=10)],
        [dict(figi='A', reasons=[]), dict(figi='B', reasons=[])]))
    monkeypatch.setattr(selection, 'select_broker_candidates', select)
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(mode='sandbox', use_ensemble=False, top_n=1)
    rt._candidate_mode = 'sandbox'
    rt._candidate_observed_at = datetime.now(timezone.utc)
    rt._candidate_catalog = {}
    result = await rt._refresh_broker_candidates()
    assert select.call_args.kwargs['top_n'] == 9999
    assert result == [dict(figi='B', side='long', lot=10)]
    assert rt._candidate_figis == {'B'}
