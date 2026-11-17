from types import SimpleNamespace
import pytest
from app.bot.runtime import PaperBotRuntime


@pytest.mark.asyncio
@pytest.mark.parametrize('broken', ['price', 'history', 'quorum'])
async def test_candidate_calculation_failure_remains_vetoed_in_actual_queue(broken):
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(feed='live', queue_history_veto=False)
    rt.buffers = {}
    rt._sector_meta = {}
    rt._regime_cache = None
    rt._hist_cache = {}
    rt._turnover_cache = {'SBER': 1e9}
    rt._fit_score = lambda *args: 1.0
    rt._min_turnover = lambda *args: 100
    rt._cand_queue = {}
    rt.tcs_to_bbg = {}
    rt._log = lambda *args, **kwargs: None
    meta = None
    if broken == 'price':
        rt.buffers = {'FIGI': [SimpleNamespace(close='broken')] * 22}
    elif broken == 'history':
        rt._hist_cache = {'SBER': {'n': 'broken'}}
    else:
        meta = {'quorum_event': {'votes': 'broken'}}
    await rt._enqueue_candidate('FIGI', 'SBER', 'BUY', 'test', meta=meta)
    candidate = rt._cand_queue['FIGI']
    assert candidate['score'] == 0
    assert candidate['veto'] == ['candidate_inputs_unknown']
