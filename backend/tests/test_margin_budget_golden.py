import pytest

from app.engine.margin_budget import check_margin_budget
from app.bot.portfolio import PortfolioLimits, snapshot, check_order


LIMITS = PortfolioLimits(0, 0, .8, 0)


def budget(**changes):
    values = dict(liquid_value=10000, committed_margin=7500, additional_notional=1000,
                  initial_risk_rate=.4, max_use_fraction=.8)
    return check_margin_budget(**(values | changes))


def test_directional_rates_change_admission():
    assert budget().allowed
    assert budget(initial_risk_rate=.6851).reason == 'MARGIN_BUDGET_EXCEEDED'


def test_full_risk_rate_is_not_replaced_by_existing_leverage():
    snap = snapshot(10000, [], margin={'liquid': 10000, 'starting_margin': 7500})
    snap['gross_leverage'] = 6
    assert not check_order(snap, 'BUY', 1000, 'A', {'A': {'dlong': 1}}, LIMITS)[0]


@pytest.mark.parametrize('value', [None, float('nan'), float('inf'), True, 'invalid', -1, 0])
def test_unknown_or_invalid_rate_never_uses_leverage_fallback(value):
    assert not budget(initial_risk_rate=value).allowed


def test_liquid_value_not_total_equity_controls_admission():
    snap = snapshot(10000, [], margin={'liquid': 5000, 'starting_margin': 3500})
    assert not check_order(snap, 'BUY', 2000, 'A', {'A': {'dlong': .4}}, LIMITS)[0]


def test_missing_margin_is_preserved_and_entry_denied():
    snap = snapshot(10000, [])
    assert snap['margin_used'] is None and snap['margin_free'] is None
    assert not check_order(snap, 'BUY', 100, 'A', {'A': {'dlong': .2}}, LIMITS)[0]


def test_corrected_margin_includes_committed_orders_without_rounding():
    snap = snapshot(10000, [], margin={'liquid': 10000, 'starting_margin': 0,
                                      'corrected_margin': 7999.999})
    assert not check_order(snap, 'BUY', 1, 'A', {'A': {'dlong': .2}}, LIMITS)[0]


def test_invalid_corrected_margin_does_not_fall_back_to_starting():
    snap = snapshot(10000, [], margin={'liquid': 10000, 'starting_margin': 0,
                                      'corrected_margin': None})
    assert not check_order(snap, 'BUY', 1, 'A', {'A': {'dlong': .2}}, LIMITS)[0]


def test_budget_boundary_and_cost_adjusted_liquid_value():
    assert budget(committed_margin=7600).allowed
    assert not budget(committed_margin=7600, liquid_value=9999).allowed
    assert not budget(committed_margin=9600, max_use_fraction=1).allowed


def test_margin_missing_does_not_enable_disabled_limit():
    assert check_order(snapshot(10000, []), 'BUY', 100, 'A', limits=PortfolioLimits(0, 0, 0, 0))[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('corrected', [8500, None])
async def test_live_adapter_preserves_corrected_margin_and_unknown(corrected):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from app.bot.live_broker import LiveBroker

    broker = LiveBroker.__new__(LiveBroker)
    broker._account = 'fixture'
    broker._q = lambda value: value
    broker._mark_ok = Mock()
    broker._mark_degraded = Mock()
    response = SimpleNamespace(liquid_portfolio=10000, starting_margin=1000,
                               minimal_margin=500, corrected_margin=corrected)
    broker._get_services = lambda: SimpleNamespace(users=SimpleNamespace(
        get_margin_attributes=lambda **kwargs: response))
    attributes = await broker.margin_attributes()
    assert attributes['corrected_margin'] == corrected
    snap = snapshot(10000, [], margin=attributes)
    assert not check_order(snap, 'BUY', 100, 'A', {'A': {'dlong': .2}}, LIMITS)[0]
    broker._mark_degraded.assert_not_called()
