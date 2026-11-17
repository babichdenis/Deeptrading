from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.engine import EngineConfig, EngineRunner, FixedSlTpPolicy
from app.engine.models import Candle
from app.engine.replay_runner import IncrementalRunner
from app.engine.wave1 import ReplayStrategy


def bars():
    t = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    return [Candle(t + timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(8)]


def runner(candles):
    return IncrementalRunner(ReplayStrategy([(candles[0].ts, 'BUY')]),
                             FixedSlTpPolicy(stop_pct=.05, target_pct=.1),
                             EngineConfig(figi='INTEGRITY', qty=1))


@pytest.mark.parametrize('field,value', [('open', 99.5), ('high', 102), ('low', 98), ('close', 100.5), ('volume', 2000)])
@pytest.mark.parametrize('extend_tail', [False, True])
def test_revised_history_is_rejected_without_mutating_state(field, value, extend_tail):
    candles = bars()
    r = runner(candles)
    r.extend(candles[:4])
    before = list(r.history)
    revised = list(candles[:5 if extend_tail else 4])
    revised[1] = replace(revised[1], **{field: value})
    with pytest.raises(ValueError, match='prefix'):
        r.extend(revised)
    assert r.history == before
    r.extend(candles)
    actual = r.finalize()
    ref = EngineRunner(ReplayStrategy([(candles[0].ts, 'BUY')]),
                       FixedSlTpPolicy(stop_pct=.05, target_pct=.1),
                       EngineConfig(figi='INTEGRITY', qty=1)).run(candles)
    assert actual.trades
    assert actual.fingerprint() == ref.fingerprint()


def test_unchanged_prefix_is_idempotent():
    candles = bars()
    r = runner(candles)
    r.extend(candles[:4])
    audit = list(r.st.ledger.audit)
    r.extend(list(candles[:4]))
    assert r.st.ledger.audit == audit
    assert len(r.history) == 4
