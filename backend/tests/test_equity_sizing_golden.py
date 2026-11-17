import pytest

from app.engine.account_replay import run_shared_account
from tests.test_account_replay_golden import bars, spec


def test_equity_fraction_sizes_whole_lots_and_restores_original_config():
    item = spec()
    item.equity_fraction = .5
    item.lot_size = 10
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    assert result.ledgers['A'].trades[0].qty == 50
    assert item.config.qty == 10
    assert result.sizing_decisions[0]['equity_before'] == 10000
    assert result.sizing_decisions[0]['accepted_shares'] == 50


def test_fee_prevents_spending_exact_equity_and_sizes_down_instead_of_rejecting():
    item = spec(fee=.001)
    item.equity_fraction = 1
    result = run_shared_account([item], initial_capital=1000, gross_equity_limit=1)
    assert result.ledgers['A'].trades[0].qty == 9
    assert result.final_equity == pytest.approx(998.2)
    assert result.sizing_decisions[0]['desired_shares'] == 10


def test_no_fractional_lot_when_one_lot_plus_fee_exceeds_capacity():
    item = spec(fee=.001)
    item.equity_fraction = 1
    item.lot_size = 10
    result = run_shared_account([item], initial_capital=1000, gross_equity_limit=1)
    assert not result.ledgers['A'].trades
    assert result.rejections[0]['reason'] == 'NO_WHOLE_LOT_CAPACITY'
    assert result.final_equity == 1000


def test_second_entry_uses_remaining_shared_capacity_in_whole_shares():
    items = [spec('A'), spec('B')]
    for item in items:
        item.equity_fraction = .6
    result = run_shared_account(items, initial_capital=1500, gross_equity_limit=1)
    assert result.ledgers['A'].trades[0].qty == 9
    assert result.ledgers['B'].trades[0].qty == 6
    assert result.sizing_decisions[1]['gross_before'] == 900


def test_return_scales_with_capital_when_lots_and_fees_allow_it():
    outcomes = []
    for capital in (10000, 100000):
        item = spec(candles=bars([100, 100, 103, 103]))
        item.equity_fraction = .2
        item.lot_size = 10
        result = run_shared_account([item], initial_capital=capital, gross_equity_limit=1)
        outcomes.append((result.final_equity / capital - 1, result.ledgers['A'].trades[0].qty))
    assert outcomes[0][0] == pytest.approx(outcomes[1][0])
    assert outcomes[1][1] == 10 * outcomes[0][1]


def test_repeat_run_does_not_reuse_prior_dynamic_quantity():
    item = spec()
    item.equity_fraction = .2
    a = run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
    b = run_shared_account([item], initial_capital=20000, gross_equity_limit=1)
    assert a.ledgers['A'].trades[0].qty == 20
    assert b.ledgers['A'].trades[0].qty == 40
    assert item.config.qty == 10


@pytest.mark.parametrize('fraction', [0, -1, 1.1, True, float('nan')])
def test_invalid_equity_fraction_cannot_disable_sizing(fraction):
    item = spec()
    item.equity_fraction = fraction
    with pytest.raises(ValueError):
        run_shared_account([item], initial_capital=10000, gross_equity_limit=1)
