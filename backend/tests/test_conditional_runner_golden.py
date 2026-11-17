from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

from app.engine import EngineConfig, EngineRunner, FixedSlTpPolicy, ScriptedStrategy, Side, Signal
from app.engine.conditional_runner import ConditionalEntryRunner
from app.engine.costs import CostModel
from app.engine.models import Candle


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
TF = timedelta(minutes=5)


def bars(prices):
    return [Candle(ts=T + i * TF, open=p, high=p, low=p, close=p) for i, p in enumerate(prices)]


def runner(side=Side.BUY, fraction=.01, lifetime=5 * TF, approve=lambda _: True):
    return ConditionalEntryRunner(ScriptedStrategy({0: Signal('test', side, T)}),
        FixedSlTpPolicy(stop_pct=.01, target_pct=.02),
        EngineConfig(figi='TEST', qty=10, cost_model=CostModel(slippage_bps=0, tick_size=0)),
        confirmation_fraction=fraction, lifetime=lifetime, timeframe=TF, lot_size=10,
        approve_and_reserve=approve)


def test_disabled_adapter_equals_canonical_full_trade_dictionaries():
    candles = bars([100, 100, 103, 102])
    adapted = runner(fraction=None)
    baseline = EngineRunner(ScriptedStrategy({0: Signal('test', Side.BUY, T)}),
        adapted.exit_policy, adapted.cfg)
    expected = baseline.run(candles)
    actual = adapted.run(candles)
    assert expected.trades
    assert [asdict(t) for t in actual.trades] == [asdict(t) for t in expected.trades]


@pytest.mark.parametrize('side, prices, expected_entry, expected_exit', [
    (Side.BUY, [100, 101, 103, 99, 99], 103, 99),
    (Side.SELL, [100, 99, 97, 101, 101], 97, 101),
])
def test_confirmation_uses_next_open_and_canonical_gap_stop(side, prices, expected_entry, expected_exit):
    r = runner(side=side)
    ledger = r.run(bars(prices))
    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.entry_time == T + 2 * TF
    assert trade.entry_price == expected_entry
    assert trade.exit_price == expected_exit
    assert trade.exit_reason == 'stop_loss'
    assert trade.qty == 10
    assert trade.net_pnl == pytest.approx(-40 - (expected_entry + expected_exit) * 10 * .0005)


def test_no_confirmation_or_reservation_means_no_trade():
    assert not runner().run(bars([100, 100, 100, 100])).trades
    assert not runner(approve=lambda _: False).run(bars([100, 101, 102, 102])).trades


def test_gap_after_confirmation_expires_before_execution():
    candles = bars([100, 101, 102, 102])
    candles[2] = Candle(ts=T + 20 * TF, open=102, high=102, low=102, close=102)
    candles[3] = Candle(ts=T + 21 * TF, open=102, high=102, low=102, close=102)
    r = runner(lifetime=3 * TF)
    assert not r.run(candles).trades
    assert r.intents.get(r.intent_ids[0]).status == 'expired'


def test_repeated_run_resets_intents_and_strategy():
    r = runner()
    candles = bars([100, 101, 103, 99, 99])
    first = [asdict(t) for t in r.run(candles).trades]
    assert first
    assert [asdict(t) for t in r.run(candles).trades] == first


def test_entry_policy_rechecked_at_confirmation_time():
    from app.engine.models import DecisionAction
    class Reject:
        def decide(self, signal, state, bars_held):
            if signal is not None:
                assert signal.time == T + TF
            return DecisionAction.NO_SIGNAL, 'test_reject'
    r = runner()
    r.policy = Reject()
    assert not r.run(bars([100, 101, 103, 104])).trades
    assert r.intents.get(r.intent_ids[0]).status == 'cancelled'


def test_opposite_signal_invalidates_old_intent_before_price_trigger():
    r = runner()
    r.strategy = ScriptedStrategy({0: Signal('test', Side.BUY, T),
                                   1: Signal('test', Side.SELL, T + TF)})
    assert not r.run(bars([100, 101, 102, 103])).trades
    assert r.intents.get(r.intent_ids[0]).reason == 'thesis_invalid'
