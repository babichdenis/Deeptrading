from dataclasses import replace

import pytest

from app.engine.portfolio_margin import RiskRate, MarginExposure, PortfolioMarginPolicy
from app.engine.research_account import ResearchAccount
from app.engine.account_replay import run_shared_account
from tests.test_account_replay_golden import T, M, spec
from tests.test_research_account_golden import fill


def rate(figi='A', side='BUY', value=.2, **changes):
    return replace(RiskRate(figi, side, value, True, T, T, T + 10 * M, 'fixture'), **changes)


def policy(*rates, fraction=.8):
    return PortfolioMarginPolicy(rates, max_use_fraction=fraction)


def test_mixed_portfolio_consumes_sum_of_directional_margin_without_netting():
    p = policy(rate(), rate('B', 'SELL', .5))
    decision = p.evaluate(at=T, positions=[MarginExposure('A', 'BUY', 20000)],
                          proposal=MarginExposure('B', 'SELL', 10000), projected_liquid=10000)
    assert not decision.allowed
    assert decision.projected_margin == 9000 and decision.limit == 8000


def test_fee_and_adverse_fill_reduce_budget_before_entry_without_mutation():
    account = ResearchAccount(1000, max_mark_age=M, margin_policy=policy(rate(value=.8)))
    account.mark('A', 100, available_at=T)
    assert account.can_increase(fill('free', at=T, fee=0), gross_equity_limit=6).allowed
    for price, fee in [(100, 1), (101, 0)]:
        decision = account.can_increase(fill('cost', at=T, price=price, fee=fee), gross_equity_limit=6)
        assert decision.reason == 'MARGIN_BUDGET_EXCEEDED'
        assert decision.projected_margin == 800
        assert decision.margin_limit < 800
    assert not account.positions


def test_existing_positions_use_current_mark_not_entry_price():
    account = ResearchAccount(1000, max_mark_age=M,
                              margin_policy=policy(rate(), rate('B', value=.5)))
    account.apply_fill(fill('held', at=T, qty=10, fee=0))
    account.mark('A', 200, available_at=T)
    account.mark('B', 100, available_at=T)
    decision = account.can_increase(fill('new', figi='B', qty=20, fee=0, at=T), gross_equity_limit=6)
    assert decision.allowed and decision.projected_margin == 1400
    assert decision.margin_limit == 1600


@pytest.mark.parametrize('changes,reason', [({'available_at':T+M}, 'MARGIN_RATE_UNKNOWN'),
    ({'valid_until':T}, 'INVALID'), ({'collateral_eligible':False}, 'MARGIN_COLLATERAL_SCOPE_UNSUPPORTED')])
def test_rate_evidence_and_collateral_scope(changes, reason):
    if reason == 'INVALID':
        with pytest.raises(ValueError): policy(rate(**changes))
        return
    decision = policy(rate(**changes)).evaluate(at=T, positions=[],
        proposal=MarginExposure('A', 'BUY', 100), projected_liquid=1000)
    assert decision.reason == reason


def test_conflicting_rates_fail_closed_and_expiry_is_exclusive():
    p = policy(rate(), rate(value=.3))
    args = dict(positions=[], proposal=MarginExposure('A','BUY',100), projected_liquid=1000)
    assert p.evaluate(at=T, **args).reason == 'MARGIN_RATE_CONFLICT'
    assert policy(rate()).evaluate(at=T+10*M, **args).reason == 'MARGIN_RATE_UNKNOWN'


def test_unknown_held_rate_blocks_entry_even_when_new_rate_known():
    account = ResearchAccount(1000, max_mark_age=M, margin_policy=policy(rate('B')))
    account.apply_fill(fill('held', at=T, qty=1))
    for figi in ('A','B'): account.mark(figi,100,available_at=T)
    assert account.can_increase(fill('new',figi='B',at=T),gross_equity_limit=6).reason == 'MARGIN_RATE_UNKNOWN'


def test_replay_wholelot_sizing_uses_same_account_margin_budget():
    item = spec()
    item.equity_fraction = 1
    result = run_shared_account([item], initial_capital=1000, gross_equity_limit=6,
                                 margin_policy=policy(rate(value=1)))
    assert result.ledgers['A'].trades[0].qty == 8
    assert result.final_equity == 1000


def test_expired_rate_does_not_prevent_confirmed_exit():
    result = run_shared_account([spec()], initial_capital=10000, gross_equity_limit=6,
                                 margin_policy=policy(rate(valid_until=T+2*M)))
    assert len(result.ledgers['A'].trades)==1
    assert result.final_equity == 10000


def test_eligible_rate_does_not_override_borrowing_denial():
    account = ResearchAccount(1000,max_mark_age=M,margin_policy=policy(rate('A','SELL')))
    account.mark('A',100,available_at=T)
    decision=account.can_increase(fill('short',at=T,side='SELL'),gross_equity_limit=6,allow_borrowing=False)
    assert decision.reason == 'BORROWING_NOT_APPROVED'
