import pytest

from app.bot.paper_cashflow import settle_paper_exit


@pytest.mark.parametrize("gross,entry,exit_fee,credit,net", [
    (100.0, 0.5, 0.55, 99.45, 98.95),
    (-100.0, 0.5, 0.45, -100.45, -100.95),
    (100.0, 0.5, 1.1, 98.9, 98.4),
    (0.0, 0.5, 0.5, -0.5, -1.0),
])
def test_cash_and_trade_net_reconcile(gross, entry, exit_fee, credit, net):
    result = settle_paper_exit(gross, exit_fee, entry)
    assert result.cash_credit == pytest.approx(credit)
    assert result.total_net == pytest.approx(net)
    assert 10000.0 - entry + result.cash_credit == pytest.approx(10000.0 + result.total_net)
    assert result.total_commission == pytest.approx(entry + exit_fee)


def test_legacy_entry_fee_remains_unknown_without_blocking_cash_settlement():
    result = settle_paper_exit(100.0, 0.55, None)
    assert result.cash_credit == pytest.approx(99.45)
    assert result.total_net is None
    assert result.total_commission is None


@pytest.mark.parametrize("gross,exit_fee,entry", [(float("nan"), 1, 1), (1, -1, 1), (1, 1, float("inf"))])
def test_invalid_money_rejected(gross, exit_fee, entry):
    with pytest.raises(ValueError):
        settle_paper_exit(gross, exit_fee, entry)
