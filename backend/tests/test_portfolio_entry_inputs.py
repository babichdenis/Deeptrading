import pytest

from app.bot.portfolio import PortfolioLimits, check_order


@pytest.mark.parametrize('equity', [None, 0, -1, float('nan'), float('inf'), -float('inf'), 'broken'])
def test_invalid_equity_blocks_new_entry_even_with_disabled_caps(equity):
    limits = PortfolioLimits(0, 0, 0, 0)
    assert check_order({'equity': equity}, 'BUY', 100, 'TEST', limits=limits) == (False, 'invalid_equity')


def test_missing_equity_is_not_permission_to_trade():
    assert check_order({}, 'SELL', 100, 'TEST') == (False, 'invalid_equity')


@pytest.mark.parametrize('notional', [None, 0, -1, float('nan'), float('inf'), -float('inf'), 'broken'])
def test_invalid_order_value_blocks_new_entry(notional):
    assert check_order({'equity': 10000}, 'BUY', notional, 'TEST') == (False, 'invalid_notional')


@pytest.mark.parametrize('side', ['', 'HOLD', None])
def test_unknown_side_is_not_short(side):
    assert check_order({'equity': 10000}, side, 100, 'TEST') == (False, 'invalid_side')


@pytest.mark.parametrize('side', ['BUY', 'SELL', 'LONG', 'SHORT', 'buy', 'sell'])
def test_valid_numeric_inputs_preserve_disabled_caps(side):
    assert check_order({'equity': '10000'}, side, '100', 'TEST',
                       limits=PortfolioLimits(0, 0, 0, 0)) == (True, 'ok')
