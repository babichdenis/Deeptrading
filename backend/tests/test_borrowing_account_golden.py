from datetime import timedelta

from app.engine.account_replay import run_shared_account
from app.engine.broker_tariffs import equity_trade_fee
from app.engine.models import Side
from app.engine.research_account import ResearchAccount
from tests.test_research_account_golden import T, fill
from tests.test_account_replay_golden import spec


def test_long_cash_borrow_includes_entry_commission():
    account = ResearchAccount(1000, max_mark_age=timedelta(minutes=1))
    account.apply_fill(fill('long', qty=15))
    account.mark('A', 100, available_at=T)
    state = account.snapshot(at=T)
    assert state.cash == 999
    assert state.settlement_cash == -501
    assert state.uncovered_cash == state.uncovered_value == 501
    assert state.short_market_value == 0


def test_short_is_borrowing_even_below_one_gross_equity():
    account = ResearchAccount(10000, max_mark_age=timedelta(minutes=1))
    account.mark('A', 100, available_at=T)
    proposal = fill('short', side='SELL')
    assert account.can_increase(proposal, gross_equity_limit=1).allowed
    assert account.can_increase(proposal, gross_equity_limit=1, allow_borrowing=False).reason == 'BORROWING_NOT_APPROVED'
    account.apply_fill(proposal)
    state = account.snapshot(at=T)
    assert state.settlement_cash == 10999
    assert state.uncovered_cash == 0
    assert state.uncovered_value == state.short_market_value == 1000


def test_unknown_borrowing_approval_allows_funded_long_but_not_cash_deficit():
    account = ResearchAccount(1000, max_mark_age=timedelta(minutes=1))
    account.mark('A', 100, available_at=T)
    assert account.can_increase(fill('small', qty=9), gross_equity_limit=6, allow_borrowing=None).allowed
    denied = account.can_increase(fill('large', qty=10), gross_equity_limit=6, allow_borrowing=None)
    assert denied.reason == 'BORROWING_NOT_APPROVED'
    assert denied.projected_uncovered_value == 1


def test_missing_short_mark_keeps_uncovered_total_unknown():
    account = ResearchAccount(10000, max_mark_age=timedelta(minutes=1))
    account.apply_fill(fill('short', side='SELL'))
    state = account.snapshot(at=T)
    assert state.settlement_cash == 10999
    assert state.short_market_value is None and state.uncovered_value is None


def test_cash_principal_conservation_through_partial_close_and_funding_charge():
    account = ResearchAccount(1000, max_mark_age=timedelta(minutes=1))
    account.apply_fill(fill('long', qty=15))
    account.apply_fill(fill('partial', side='SELL', qty=5, price=110, intent='reduce'))
    account.charge('carry', 40, at=T)
    account.mark('A', 110, available_at=T)
    state = account.snapshot(at=T)
    assert state.settlement_cash == 8
    assert state.uncovered_value == 0
    assert state.equity == 1108


def test_replay_borrowing_gate_can_deny_short_without_denying_funded_long():
    long, short = spec('A'), spec('B', side=Side.SELL)
    long.borrowing_gate = short.borrowing_gate = lambda at: None
    result = run_shared_account([long, short], initial_capital=10000, gross_equity_limit=6)
    assert len(result.ledgers['A'].trades) == 1
    assert not result.ledgers['B'].trades
    assert result.rejections[0]['reason'] == 'BORROWING_NOT_APPROVED'


def test_dynamic_sizing_respects_no_new_borrowing_despite_high_gross_limit():
    item = spec(fee=.001)
    item.equity_fraction = 1
    item.borrowing_gate = lambda at: False
    result = run_shared_account([item], initial_capital=1000, gross_equity_limit=6)
    assert result.ledgers['A'].trades[0].qty == 9
    assert result.sizing_decisions[0]['borrowing_approved'] is False


def test_unknown_policy_never_blocks_confirmed_cover():
    item = spec(side=Side.SELL)
    item.borrowing_gate = lambda at: at == item.candles[1].ts
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=6)
    assert len(result.ledgers['A'].trades) == 1
    assert result.final_equity == 10000


def test_both_leg_commissions_apply_to_full_sixfold_notional_long_and_short():
    for side, cover in (('BUY', 'SELL'), ('SELL', 'BUY')):
        account = ResearchAccount(10000, max_mark_age=timedelta(minutes=1))
        fee = equity_trade_fee(600 * 100, plan='trader')
        assert fee == 30
        account.apply_fill(fill('entry', side=side, qty=600, fee=fee))
        account.apply_fill(fill('exit', side=cover, qty=600, fee=fee, intent='reduce'))
        assert account.snapshot(at=T).equity == 9940
