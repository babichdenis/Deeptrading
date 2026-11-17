from dataclasses import replace
from datetime import timedelta

import pytest

from app.engine.borrowing_eligibility import BorrowingEligibility, BorrowingPermission
from app.engine.account_replay import run_shared_account
from app.engine.models import Side
from app.engine.research_account import ResearchAccount
from tests.test_account_replay_golden import spec, T, M
from tests.test_research_account_golden import fill


def permission(**changes):
    return replace(BorrowingPermission('A', 'SELL', True, T, T, T + 3 * M, 'fixture'), **changes)


def test_permission_is_directional_and_expires_at_boundary():
    policy = BorrowingEligibility([permission()])
    assert policy.evaluate(figi='A', side='SELL', at=T).allowed
    for figi, side, at in [('B', 'SELL', T), ('A', 'BUY', T), ('A', 'SELL', T + 3 * M)]:
        assert policy.evaluate(figi=figi, side=side, at=at).reason == 'BORROWING_PERMISSION_UNKNOWN'


def test_future_publication_never_changes_past_permission():
    policy = BorrowingEligibility([permission(available_at=T + M)])
    assert not policy.evaluate(figi='A', side='SELL', at=T).allowed
    assert policy.evaluate(figi='A', side='SELL', at=T + M).allowed


def test_conflicting_evidence_fails_closed_independent_of_order():
    records = [permission(), permission(allowed=False, evidence_id='other')]
    for values in (records, reversed(records)):
        assert BorrowingEligibility(values).evaluate(figi='A', side='SELL', at=T).reason == 'BORROWING_PERMISSION_CONFLICT'


@pytest.mark.parametrize('changes', [dict(allowed=1), dict(valid_until=T), dict(evidence_id=''),
                                    dict(available_at=T.replace(tzinfo=None))])
def test_invalid_permission_rejected(changes):
    with pytest.raises(ValueError):
        BorrowingEligibility([permission(**changes)])


def test_unknown_permission_only_blocks_new_borrowing_without_mutation():
    account = ResearchAccount(1000, max_mark_age=timedelta(minutes=1))
    account.mark('A', 100, available_at=T)
    policy = BorrowingEligibility([])
    assert account.can_increase(fill('cash', qty=9, at=T), gross_equity_limit=6,
                                borrowing_policy=policy).allowed
    for side in ('BUY', 'SELL'):
        decision = account.can_increase(fill(side, side=side, qty=10, at=T), gross_equity_limit=6,
                                        borrowing_policy=policy)
        assert decision.reason == 'BORROWING_PERMISSION_UNKNOWN'
    assert account.snapshot(at=T).equity == 1000
    assert not account.positions


def test_permission_does_not_override_risk_gate_or_gross_limit():
    account = ResearchAccount(1000, max_mark_age=M)
    account.mark('A', 100, available_at=T)
    policy = BorrowingEligibility([permission()])
    proposal = fill('short', side='SELL', qty=20, at=T)
    assert account.can_increase(proposal, gross_equity_limit=6, borrowing_policy=policy,
                                allow_borrowing=False).reason == 'BORROWING_NOT_APPROVED'
    assert account.can_increase(proposal, gross_equity_limit=1,
                                borrowing_policy=policy).reason == 'GROSS_EQUITY_LIMIT'


def test_replay_permission_expiry_does_not_prevent_cover():
    item = spec(side=Side.SELL)
    item.borrowing_policy = BorrowingEligibility([permission(valid_until=T + 2 * M)])
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=6)
    assert len(result.ledgers['A'].trades) == 1
    assert result.ledgers['A'].trades[0].exit_time >= T + 2 * M
    assert result.final_equity == 10000


def test_replay_sizer_and_final_admission_share_permission_policy():
    item = spec(fee=.001)
    item.equity_fraction = 1
    item.borrowing_policy = BorrowingEligibility([])
    result = run_shared_account([item], initial_capital=1000, gross_equity_limit=6)
    assert result.ledgers['A'].trades[0].qty == 9
    short = spec(side=Side.SELL)
    short.borrowing_policy = BorrowingEligibility([])
    rejected = run_shared_account([short], initial_capital=10000, gross_equity_limit=6)
    assert not rejected.ledgers['A'].trades
    assert rejected.rejections[0]['reason'] == 'BORROWING_PERMISSION_UNKNOWN'
