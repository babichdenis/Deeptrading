from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.research_account import ResearchAccount
from app.engine.staged_position import ConfirmedFill


T = datetime(2026, 9, 1, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def fill(key, figi='A', side='BUY', qty=10, price=100, fee=1, at=T, intent='increase'):
    return ConfirmedFill(key, figi, side, qty, price, fee, at, intent)


def test_two_instruments_marked_equity_and_fee_conservation():
    account = ResearchAccount(10000, max_mark_age=M)
    account.apply_fill(fill('long'))
    account.apply_fill(fill('short', 'B', 'SELL', qty=5, price=200, fee=2))
    account.mark('A', 110, available_at=T + M)
    account.mark('B', 180, available_at=T + M)
    snapshot = account.snapshot(at=T + M)
    assert snapshot.cash == 9997
    assert snapshot.equity == 10197
    assert snapshot.gross_exposure == 2000
    account.apply_fill(fill('exit-long', side='SELL', price=110, at=T + M, intent='reduce'))
    account.apply_fill(fill('exit-short', 'B', 'BUY', qty=5, price=180, fee=2, at=T + M, intent='reduce'))
    flat = account.snapshot(at=T + 10 * M)
    assert flat.cash == flat.equity == 10194
    assert flat.gross_exposure == 0


def test_marked_loss_blocks_second_entry_despite_positive_realized_cash():
    account = ResearchAccount(2000, max_mark_age=M)
    account.apply_fill(fill('first'))
    account.mark('A', 50, available_at=T + M)
    account.mark('B', 100, available_at=T + M)
    proposal = fill('second', 'B', qty=11, at=T + M)
    decision = account.can_increase(proposal, gross_equity_limit=1)
    assert account.snapshot(at=T + M).cash == 1999
    assert decision.projected_equity == 1498
    assert decision.projected_exposure == 1600
    assert not decision.allowed and decision.reason == 'GROSS_EQUITY_LIMIT'


def test_initial_fee_and_fill_slippage_affect_admission_before_mutation():
    account = ResearchAccount(1000, max_mark_age=M)
    account.mark('A', 100, available_at=T)
    decision = account.can_increase(fill('entry', price=101), gross_equity_limit=1)
    assert not decision.allowed
    assert decision.projected_equity == 989
    assert account.snapshot(at=T).equity == 1000


def test_serial_entries_cannot_spend_same_equity_capacity_twice():
    account = ResearchAccount(1500, max_mark_age=M)
    for figi in ('A', 'B'):
        account.mark(figi, 100, available_at=T)
    first, second = fill('first'), fill('second', 'B')
    assert account.can_increase(first, gross_equity_limit=1).allowed
    account.apply_fill(first)
    assert not account.can_increase(second, gross_equity_limit=1).allowed
    assert account.can_increase(second, gross_equity_limit=2).allowed


def test_unknown_marks_block_entries_but_never_block_confirmed_exit():
    account = ResearchAccount(10000, max_mark_age=M)
    account.apply_fill(fill('first'))
    account.mark('B', 100, available_at=T + 2 * M)
    unknown = account.snapshot(at=T + 2 * M)
    assert unknown.equity is None and unknown.gross_exposure is None
    assert unknown.missing_marks == ('A',)
    assert account.can_increase(fill('second', 'B', at=T + 2 * M), gross_equity_limit=1).reason == 'UNKNOWN_MARK'
    account.apply_fill(fill('exit', side='SELL', price=90, at=T + 2 * M, intent='reduce'))
    assert account.snapshot(at=T + 2 * M).equity == 9898


def test_stale_mark_does_not_silently_become_current():
    account = ResearchAccount(10000, max_mark_age=M)
    account.mark('A', 100, available_at=T)
    account.apply_fill(fill('first'))
    assert account.snapshot(at=T + M).equity == 9999
    assert account.snapshot(at=T + 2 * M).equity is None
    account.mark('A', 90, available_at=T + 2 * M)
    assert account.snapshot(at=T + 2 * M).equity == 9899


def test_partial_exit_add_and_final_cash_match_confirmed_ledger():
    account = ResearchAccount(10000, max_mark_age=M)
    account.apply_fill(fill('first'))
    account.apply_fill(fill('partial', side='SELL', qty=5, price=110, at=T + M, intent='reduce'))
    account.apply_fill(fill('add', qty=5, price=120, at=T + 2 * M))
    account.apply_fill(fill('last', side='SELL', qty=10, price=130, at=T + 3 * M, intent='reduce'))
    assert account.snapshot(at=T + 3 * M).equity == 10246


def test_global_event_ids_idempotency_and_charges():
    account = ResearchAccount(10000, max_mark_age=M)
    first = fill('first')
    account.apply_fill(first)
    assert account.charge('funding', 40, at=T + M)
    assert not account.charge('funding', 40, at=T + M)
    assert not account.apply_fill(first).applied
    with pytest.raises(ValueError, match='Conflicting'):
        account.apply_fill(replace(first, figi='B'))
    with pytest.raises(ValueError, match='Conflicting'):
        account.charge('first', 1, at=T + M)
    assert account.snapshot(at=T + M).cash == 9959


def test_future_quote_or_event_cannot_value_past_account():
    account = ResearchAccount(1000, max_mark_age=M)
    account.mark('A', 100, available_at=T + M)
    with pytest.raises(ValueError, match='backwards'):
        account.snapshot(at=T)
    with pytest.raises(ValueError, match='backwards'):
        account.apply_fill(fill('past'))


@pytest.mark.parametrize('capital', [0, -1, float('nan'), float('inf'), True])
def test_invalid_capital_is_not_a_disabled_risk_gate(capital):
    with pytest.raises(ValueError):
        ResearchAccount(capital, max_mark_age=M)
