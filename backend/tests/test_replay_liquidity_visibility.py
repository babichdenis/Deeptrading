from types import SimpleNamespace
import pytest
from app.bot.runtime import PaperBotRuntime
from app.bot.gates import MarketContext, gate_liquidity


@pytest.mark.asyncio
async def test_replay_turnover_never_reads_live_cache_or_source():
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(feed='replay')
    rt._turnover_cache = {'SBER': 1e12}
    rt._turnover_ts = 1e12
    assert await rt._daily_turnover('SBER') is None


def test_replay_candidate_cannot_use_today_turnover():
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(feed='replay', queue_history_veto=False)
    rt.buffers = {}
    rt._sector_meta = {}
    rt._regime_cache = None
    rt._hist_cache = {}
    rt._turnover_cache = {'SBER': 1e12}
    rt._fit_score = lambda *args: 1.0
    rt._min_turnover = lambda *args: 100
    result = rt._strength_for('FIGI', 'SBER', 'BUY')
    assert result['veto'] == ['liquidity_unknown']


@pytest.mark.parametrize('threshold,passed', [(0, True), (100, False)])
def test_missing_historical_turnover_obeys_configured_gate(threshold, passed):
    result = gate_liquidity(MarketContext(cfg=SimpleNamespace(entry_min_turnover=threshold), side='BUY'))
    assert result.passed == passed
