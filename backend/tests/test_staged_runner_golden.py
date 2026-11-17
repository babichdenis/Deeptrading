from datetime import datetime, timedelta, timezone

import pytest

from app.engine import EngineConfig, FixedSlTpPolicy, ScriptedStrategy, Side, Signal
from app.engine.costs import CostModel
from app.engine.models import Candle
from app.engine.staged_runner import StagedEntryRunner


T = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
TF = timedelta(minutes=5)


def run(prices, side=Side.BUY, add_lifetime=None, **config):
    r = StagedEntryRunner(ScriptedStrategy({0: Signal('test', side, T)}),
        FixedSlTpPolicy(stop_pct=.01, target_pct=.02),
        EngineConfig(figi='TEST', qty=100, cost_model=CostModel(slippage_bps=0, tick_size=0), **config),
        confirmation_fraction=None, lifetime=6 * TF, timeframe=TF, lot_size=10,
        approve_and_reserve=lambda _: True, add_confirmation_fraction=.001, add_lifetime=add_lifetime or 6 * TF)
    candles = [Candle(ts=T + i * TF, open=o, high=max(o, c), low=min(o, c), close=c)
               for i, (o, c) in enumerate(prices)]
    ledger = r.run(candles)
    return r, ledger


@pytest.mark.parametrize('side, prices', [
    (Side.BUY, [(100, 100), (100, 100.2), (100.3, 100.4), (99, 99)]),
    (Side.SELL, [(100, 100), (100, 99.8), (99.7, 99.6), (101, 101)]),
])
def test_addition_stays_within_original_risk_and_notional(side, prices):
    r, ledger = run(prices, side)
    adds = [row for row in r.add_events if row['result'] == 'filled']
    assert len(adds) == 1
    assert adds[0]['shares'] == 30
    assert adds[0]['planned_risk'] <= adds[0]['risk_budget']
    assert adds[0]['entry_notional'] <= adds[0]['notional_budget']
    assert ledger.trades[0].qty == 80
    assert ledger.trades[0].exit_reason == 'stop_loss'
    assert sum(t.net_pnl for t in ledger.trades) == pytest.approx(r.position_book.state.realized_net)
    assert r.position_book.state.cash_delta == pytest.approx(r.position_book.state.realized_net)
    assert r.position_book.state.shares == 0


def test_gap_through_stop_closes_first_tranche_without_adding():
    r, ledger = run([(100, 100), (100, 100.2), (98, 98), (98, 98)])
    assert not r.add_events
    assert ledger.trades[0].qty == 50
    assert ledger.trades[0].exit_price == 98
    assert len(r.position_book.fills) == 2


def test_first_tranche_can_close_without_confirmation():
    r, ledger = run([(100, 100), (100, 100), (99, 99), (99, 99)])
    assert not r.add_events
    assert ledger.trades[0].qty == 50
    assert r.position_book.state.shares == 0


def test_partial_exit_keeps_whole_lots_and_accounting_identity():
    r, ledger = run([(100, 100), (100, 100.2), (100.3, 100.4), (101, 101.5), (101, 101)],
                    partial_r=1, partial_fraction=.5, partial_to_be=False)
    assert any(t.exit_reason == 'partial_take' for t in ledger.trades)
    assert all(t.qty % 10 == 0 for t in ledger.trades)
    assert sum(t.net_pnl for t in ledger.trades) == pytest.approx(r.position_book.state.realized_net)
    assert r.position_book.state.shares == 0


@pytest.mark.parametrize('minutes, expect_add', [(1, True), (5, True), (15, True), (60, False)])
def test_fixed_thirty_minute_lifetime_across_timeframes(monkeypatch, minutes, expect_add):
    monkeypatch.setitem(globals(), 'TF', timedelta(minutes=minutes))
    r, ledger = run([(100, 100), (100, 100.2), (100.3, 100.4), (99, 99)],
                    add_lifetime=timedelta(minutes=30))
    assert bool([e for e in r.add_events if e['result'] == 'filled']) is expect_add
    assert ledger.trades
