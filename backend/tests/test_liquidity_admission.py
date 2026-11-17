from types import SimpleNamespace
import pytest
from app.bot.portfolio import turnover_veto, candidate_score
from app.bot.gates import gate_liquidity


@pytest.mark.parametrize('turnover,reason', [
    (None, 'liquidity_unknown'), (float('nan'), 'liquidity_unknown'),
    (float('inf'), 'liquidity_unknown'), (-1, 'liquidity_unknown'),
    (0, 'illiquid'), (99, 'illiquid'), (100, None), (101, None)])
def test_queue_and_entry_use_same_liquidity_admission(turnover, reason):
    assert turnover_veto(turnover, 100) == reason
    scored = candidate_score(ret_ticker=0, ret_index=0, turnover=turnover, min_turnover=100)
    assert scored['veto'] == ([] if reason is None else [reason])
    gate = gate_liquidity(SimpleNamespace(cfg=SimpleNamespace(entry_min_turnover=100), turnover=turnover))
    assert gate.passed == (reason is None)


def test_disabled_liquidity_gate_does_not_require_data():
    assert turnover_veto(None, 0) is None
    gate = gate_liquidity(SimpleNamespace(cfg=SimpleNamespace(entry_min_turnover=0), turnover=None))
    assert gate.passed
