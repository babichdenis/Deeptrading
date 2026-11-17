from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.account_replay import ReplayInstrument, run_shared_account
from app.engine.costs import CostModel
from app.engine.exits import FixedSlTpPolicy
from app.engine.models import Candle, Side, Signal
from app.engine.policies import ScriptedStrategy
from app.engine.runner import EngineConfig, EngineRunner


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def bars(prices):
    return [Candle(T + i * M, price, price, price, price) for i, price in enumerate(prices)]


def spec(figi='A', candles=None, signal_indices=(0,), side=Side.BUY, fee=0):
    candles = candles if candles is not None else bars([100] * 5)
    strategy = ScriptedStrategy({i: Signal('fixture', side, candles[i].ts) for i in signal_indices})
    return ReplayInstrument(candles, strategy, FixedSlTpPolicy(stop_pct=.01, target_pct=.02),
        EngineConfig(figi=figi, qty=10, cost_model=CostModel(commission_rate=fee, slippage_bps=0, tick_size=0)))


@pytest.mark.parametrize('side,prices', [(Side.BUY, [100, 100, 97, 99]), (Side.SELL, [100, 100, 104, 103])])
def test_unconstrained_trade_parity_and_account_cash(side, prices):
    item = spec(candles=bars(prices), side=side, fee=.0005)
    expected = EngineRunner(item.strategy, item.exit_policy, item.config).run(item.candles)
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    assert expected.trades
    assert [asdict(t) for t in result.ledgers['A'].trades] == [asdict(t) for t in expected.trades]
    assert result.final_equity == pytest.approx(10000 + sum(t.net_pnl for t in expected.trades))
    assert result.max_drawdown is not None and result.max_drawdown > 0
    assert not result.rejections


def test_intrabar_exit_proceeds_cannot_fund_other_ticker_same_open():
    a = bars([100, 100, 103, 103, 103])
    a[2] = Candle(T + 2 * M, 100, 103, 100, 103)
    result = run_shared_account([spec('B', signal_indices=(1, 2)), spec('A', candles=a)],
                                initial_capital=1500, gross_equity_limit=1)
    assert result.rejections == [{'figi': 'B', 'at': T + 2 * M, 'reason': 'GROSS_EQUITY_LIMIT'}]
    assert result.ledgers['A'].trades[0].exit_time == T + 2 * M
    assert result.ledgers['B'].trades[0].entry_time == T + 3 * M
    assert result.final_equity == 1520


def test_simultaneous_priority_deterministic_and_rejections_change_live_state():
    first = run_shared_account([spec('B'), spec('A')], initial_capital=1500, gross_equity_limit=1)
    second = run_shared_account([spec('A'), spec('B')], initial_capital=1500, gross_equity_limit=1)
    assert len(first.ledgers['A'].trades) == 1 and not first.ledgers['B'].trades
    assert first.rejections == second.rejections
    assert first.equity_curve == second.equity_curve


def test_unknown_mark_propagates_drawdown_and_blocks_entry_without_blocking_exit():
    a = bars([100, 100, 100, 100, 100, 100, 100])
    a = [a[0], a[1], a[-1]]
    b = spec('B', candles=bars([100] * 7), signal_indices=(4,))
    result = run_shared_account([spec('A', candles=a), b], initial_capital=10000, gross_equity_limit=1)
    assert result.unknown_equity_points > 0 and result.max_drawdown is None
    assert result.rejections == [{'figi': 'B', 'at': T + 5 * M, 'reason': 'UNKNOWN_MARK'}]
    assert len(result.ledgers['A'].trades) == 1
    assert result.final_equity == 10000


def test_future_other_ticker_high_cannot_change_current_entry_decision():
    outcomes = []
    for future_high in (101, 1000):
        a = bars([100] * 5)
        a[2] = Candle(T + 2 * M, 100, future_high, 100, 100)
        result = run_shared_account([spec('A', candles=a), spec('B', signal_indices=(1,))],
                                    initial_capital=1500, gross_equity_limit=1)
        outcomes.append(result.rejections)
    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0]['at'] == T + 2 * M


def test_repeat_run_resets_strategies_account_and_pending_fills():
    items = [spec('A'), spec('B')]
    a = run_shared_account(items, initial_capital=2500, gross_equity_limit=1)
    b = run_shared_account(items, initial_capital=2500, gross_equity_limit=1)
    assert a.equity_curve == b.equity_curve
    assert {k: [asdict(t) for t in v.trades] for k, v in a.ledgers.items()} == {
        k: [asdict(t) for t in v.trades] for k, v in b.ledgers.items()}


def test_unsupported_partial_exit_is_explicit_not_silently_unaccounted():
    item = spec()
    item.config.partial_r = 1
    with pytest.raises(ValueError, match='Partial'):
        run_shared_account([item], initial_capital=10000, gross_equity_limit=1)


def test_fixed_budget_and_slot_limit_apply_to_simultaneous_entries():
    a, b = spec('A'), spec('B')
    for instrument in (a, b):
        instrument.entry_budget = 550
        instrument.lot_size = 2
    result = run_shared_account([b, a], initial_capital=10000,
                                gross_equity_limit=1, max_positions=1)
    assert result.sizing_decisions[0]['accepted_shares'] == 4
    assert result.rejections == [{'figi': 'B', 'at': T + M, 'reason': 'MAX_POSITIONS'}]
    assert len(result.ledgers['A'].trades) == 1
    assert not result.ledgers['B'].trades


def test_carried_marks_report_preserves_admission_and_strict_risk():
    a = bars([100] * 7)
    items = [spec('A', candles=[a[0], a[1], a[-1]]),
             spec('B', candles=a, signal_indices=(4,))]
    strict = run_shared_account(items, initial_capital=10000, gross_equity_limit=1)
    reported = run_shared_account(items, initial_capital=10000, gross_equity_limit=1,
                                  report_carried_marks=True)
    assert reported.equity_curve == strict.equity_curve
    assert reported.rejections == strict.rejections
    assert reported.max_drawdown is None
    assert reported.final_equity == strict.final_equity
    assert reported.carried_marks[0] == {
        'at': T + 4 * M, 'equity': 10000, 'ages_seconds': {'A': 120},
    }


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
def test_stop_risk_budget_includes_both_commissions_and_rounds_lots(side):
    item = spec(side=side, fee=.01)
    item.entry_budget = 1000
    item.stop_risk_budget = 15
    item.lot_size = 2
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    assert result.ledgers['A'].trades[0].qty == 4


def test_short_entry_gate_does_not_block_long_signal_exit():
    item = spec(signal_indices=())
    item.strategy = ScriptedStrategy({
        0: Signal('fixture', Side.BUY, T),
        1: Signal('fixture', Side.SELL, T + M),
        2: Signal('fixture', Side.SELL, T + 2 * M),
    })
    item.short_entry_gate = lambda at: 'SHORT_CONTEXT_REJECT'
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    trade, = result.ledgers['A'].trades
    assert trade.exit_reason == 'signal_exit'
    assert trade.exit_time == T + 2 * M
    assert result.rejections == [{'figi': 'A', 'at': T + 3 * M, 'reason': 'SHORT_CONTEXT_REJECT'}]
