from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.engine.exits import intrabar_exit
from app.engine.models import Candle, PositionState, Side
from app.engine.protection import ProtectionState, advance_protection


@pytest.mark.parametrize(
    'side,position,old_stop,new_stop,target,low,high,next_open,expected',
    [
        (Side.BUY, PositionState.LONG, 95, 102, 110, 99, 105, 101, 101),
        (Side.SELL, PositionState.SHORT, 105, 98, 90, 95, 101, 99, 99),
    ],
)
def test_closed_bar_protection_applies_next_bar_golden(
    side, position, old_stop, new_stop, target, low, high, next_open, expected,
):
    bar = Candle(ts=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
                 open=100, high=high, low=low, close=new_stop)
    prior = ProtectionState(old_stop, target)
    assert intrabar_exit(bar, position, prior.stop, prior.target) == (None, '')
    calls = []

    def update_stop(*args, **kwargs):
        calls.append(('update', args[2], kwargs['commission'], kwargs['peak_price']))
        return new_stop

    def activate(*args):
        calls.append(('activate', args[3]))
        return True

    result = advance_protection(
        SimpleNamespace(update_stop=update_stop, trailing_activated=activate), prior,
        side=side, entry_price=100, qty=10, entry_commission=0.5,
        closed_bars=[bar], peak_price=high if side is Side.BUY else low,
    )
    assert result.state == ProtectionState(new_stop, None, True)
    assert prior == ProtectionState(old_stop, target, False)
    assert result.trailing_activated
    assert calls == [('update', old_stop, 0.5, high if side is Side.BUY else low), ('activate', 0.5)]
    next_bar = Candle(ts=datetime(2026, 11, 15, 12, 1, tzinfo=timezone.utc),
                      open=next_open, high=next_open, low=next_open, close=next_open)
    assert intrabar_exit(next_bar, position, result.state.stop, result.state.target) == (expected, 'stop_loss')


def test_breakeven_precedes_trailing_and_active_never_reactivates():
    bar = Candle(ts=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
                 open=100, high=107, low=100, close=106)
    seen = []

    def update_stop(side, entry, stop, bars, **kwargs):
        seen.append(stop)
        return stop + 1

    def activate(*args):
        pytest.fail('Already active trailing must not activate again')

    result = advance_protection(
        SimpleNamespace(update_stop=update_stop, trailing_activated=activate),
        ProtectionState(95, None, True), side=Side.BUY,
        entry_price=100, qty=10, entry_commission=0.5, closed_bars=[bar],
        entry_risk=5, be_trigger_r=1,
    )
    assert seen == [100]
    assert result.breakeven_changed
    assert result.breakeven_stop == 100
    assert result.state == ProtectionState(101, None, True)
    assert not result.trailing_activated


def test_policy_error_cannot_be_silently_reported_as_success():
    def update_stop(*args, **kwargs):
        raise ValueError('invalid policy data')

    with pytest.raises(ValueError, match='invalid policy data'):
        advance_protection(
            SimpleNamespace(update_stop=update_stop), ProtectionState(95, 110),
            side=Side.BUY, entry_price=100, qty=10, entry_commission=0.5,
            closed_bars=[],
        )


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
@pytest.mark.parametrize('scenario', ['conflict', 'abort', 'partial', 'already_partial'])
def test_protection_priority_golden(side, scenario):
    from app.engine.protection import ProtectionDecision, evaluate_protection

    long = side is Side.BUY
    stop, target = (95, 110) if long else (105, 90)
    low, high = (94, 111) if scenario == 'conflict' else (97, 103)
    bar = Candle(ts=datetime(2026, 11, 15, 12, tzinfo=timezone.utc),
                 open=100, high=high, low=low, close=100)
    decision = evaluate_protection(
        bar, ProtectionState(stop, target), side=side,
        entry_price=100, entry_risk=5, bars_held=0,
        abort_r=0.5 if scenario == 'abort' else 0,
        abort_max_bars=2, partial_r=0.5,
        partial_eligible=scenario != 'already_partial',
    )
    if scenario == 'conflict':
        assert decision == ProtectionDecision(stop, 'stop_loss')
    elif scenario == 'abort':
        assert decision.exit_price == (97.5 if long else 102.5)
        assert decision.exit_reason == 'early_abort'
        assert decision.partial_price is None
    elif scenario == 'partial':
        assert decision == ProtectionDecision(partial_price=102.5 if long else 97.5)
    else:
        assert decision == ProtectionDecision()


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
@pytest.mark.parametrize('close_based', [False, True])
def test_runner_trailing_trigger_policy_is_explicit(side, close_based):
    from datetime import timedelta
    from app.engine import CostModel, EngineConfig, EngineRunner, ScriptedStrategy, Signal
    from app.engine.models import ExitPlan

    stamp = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    long = side is Side.BUY
    rows = [(100, 100.2, 99.8, 100)] * 4
    rows += [(100, 100.5, 98.5, 100)] if long else [(100, 101.5, 99.5, 100)]
    gap = 98 if long else 102
    rows += [(gap, gap, gap, gap)]
    candles = [Candle(ts=stamp + timedelta(minutes=i), open=o, high=h, low=l, close=c)
               for i, (o, h, l, c) in enumerate(rows)]
    policy = SimpleNamespace(
        plan_entry=lambda *args: ExitPlan(stop_loss=99 if long else 101, take_profit=None),
        trailing_activated=lambda *args: True,
    )
    strategy = ScriptedStrategy({2: Signal(strategy_id='test', side=side, time=stamp)})
    ledger = EngineRunner(strategy, policy, EngineConfig(
        figi='OFFLINE', trailing_close_based=close_based,
        cost_model=CostModel(commission_rate=0, slippage_bps=0),
    )).run(candles)
    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.exit_reason == 'stop_loss'
    assert trade.exit_price == (gap if close_based else (99 if long else 101))
    assert trade.exit_time == candles[5 if close_based else 4].ts


def test_runner_rejects_string_boolean_trailing_policy():
    from app.engine import EngineConfig

    with pytest.raises(ValueError, match='trailing_close_based'):
        EngineConfig(trailing_close_based='false').validate()
