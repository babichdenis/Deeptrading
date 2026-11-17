import pytest

from app.engine.broker_tariffs import (
    EquityTariffCostModel, billing_period_service_fee, daily_uncovered_fee, equity_trade_fee,
)
from app.engine.costs import CostModel
from app.engine.models import Side


@pytest.mark.parametrize('plan, iceberg, notional, expected', [
    ('trader', False, 10000, 5), ('investor', False, 10000, 30),
    ('trader', True, 10000, 6), ('investor', True, 10000, 31),
    ('trader', False, 1, .01), ('trader', False, 50, .03),
    ('trader', False, 49.99, .02), ('trader', False, 50.01, .03),
])
def test_equity_transaction_fee(plan, iceberg, notional, expected):
    assert equity_trade_fee(notional, plan=plan, iceberg=iceberg) == expected


@pytest.mark.parametrize('amount, trader, investor', [
    (5000.01, 40, 40), (50000, 40, 40), (50000.01, 75, 80),
    (100000, 75, 80), (100000.01, 180, 190), (250000, 180, 190),
    (250000.01, 350, 375), (500000, 350, 375), (500000.01, 700, 750),
    (1000000, 700, 750), (1000000.01, 1750, 1850),
    (2500000, 1750, 1850), (2500000.01, 3500, 3700),
    (5000000, 3500, 3700), (5000000.01, 6900, 7200),
    (10000000, 6900, 7200), (10000001, 6800.00068, 7000.0007),
    (25000000, 17000, 17500), (25000001, 16250.00065, 16750.00067),
    (50000000, 32500, 33500), (50000001, 28500.00057, 29500.00059),
])
def test_document_tier_boundaries(amount, trader, investor):
    assert daily_uncovered_fee(amount, plan='trader', small_balance_exempt=None) == pytest.approx(trader)
    assert daily_uncovered_fee(amount, plan='investor', small_balance_exempt=None) == pytest.approx(investor)


def test_unknown_exposure_and_small_account_eligibility_are_not_free():
    fee = lambda value, exempt: daily_uncovered_fee(value, plan='trader', small_balance_exempt=exempt)
    assert fee(None, True) is None
    assert fee(0, None) == 0
    assert fee(5000, None) is None
    assert fee(5000, True) == 0
    assert fee(5000, False) == 5
    assert fee(6000, None) == 40


@pytest.mark.parametrize('trade, assets, turnover, expected', [
    (True, False, False, 390), (False, None, None, 0),
    (True, True, None, 0), (True, None, True, 0),
    (None, False, False, None), (True, None, False, None),
])
def test_service_eligibility_requires_evidence(trade, assets, turnover, expected):
    assert billing_period_service_fee(plan='trader', qualifying_trade=trade,
        prior_full_period_asset_exemption=assets,
        prior_full_period_turnover_exemption=turnover) == expected
    assert billing_period_service_fee(plan='investor', qualifying_trade=trade,
        prior_full_period_asset_exemption=assets,
        prior_full_period_turnover_exemption=turnover) == 0


@pytest.mark.parametrize('amount', [-1, float('nan'), float('inf')])
def test_invalid_money_rejected(amount):
    with pytest.raises(ValueError):
        equity_trade_fee(amount, plan='trader')
    with pytest.raises(ValueError):
        daily_uncovered_fee(amount, plan='trader', small_balance_exempt=True)


def test_explicit_model_preserves_execution_and_default_research_contract():
    original = CostModel()
    tariff = EquityTariffCostModel(plan='trader')
    assert original.commission(50) == .025
    assert tariff.commission(50) == .03
    assert tariff.version == 'Т-ТРЕЙД-260522'
    assert tariff.commission_rate == .0005
    for side in (Side.BUY, Side.SELL):
        assert tariff.fill_price(123.45, side) == original.fill_price(123.45, side)
    with pytest.raises(ValueError):
        EquityTariffCostModel(plan='unknown')


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
def test_runner_rounds_transaction_not_each_share(side):
    from datetime import datetime, timedelta, timezone
    from app.engine import EngineConfig, EngineRunner, FixedSlTpPolicy, ScriptedStrategy, Signal
    from app.engine.models import Candle

    start = datetime(2026, 9, 1, 10, tzinfo=timezone.utc)
    candles = [Candle(ts=start + timedelta(minutes=5 * i), open=5, high=5.01, low=4.99, close=5)
               for i in range(6)]
    model = EquityTariffCostModel(plan='trader', slippage_bps=0)
    runner = EngineRunner(
        strategy=ScriptedStrategy({2: Signal(strategy_id='scripted', side=side, time=candles[2].ts)}),
        exit_policy=FixedSlTpPolicy(stop_pct=.1, target_pct=.1),
        config=EngineConfig(figi='TEST', qty=10, cost_model=model))
    ledger = runner.run(candles)
    assert len(ledger.trades) == 1
    trade = ledger.trades[0]
    assert trade.commission == .06
    assert trade.gross_pnl == 0
    assert trade.net_pnl == -.06
