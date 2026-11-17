from datetime import date, datetime, timedelta, timezone

import pytest

from app.engine.account_replay import run_shared_account
from app.engine.models import Candle
from app.engine.overnight_funding import FundingClose, FundingSession, OvernightFundingPolicy
from app.engine.research_account import ResearchAccount
from tests.test_research_account_golden import fill
from tests.test_account_replay_golden import spec


MIDNIGHT = datetime(2026, 9, 4, 21, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def policy(**kwargs):
    return OvernightFundingPolicy(plan='trader', valuation_minute_msk=0, small_balance_exempt=True, **kwargs)


def test_aggregate_cash_borrowing_charged_once_not_per_position():
    account = ResearchAccount(10000, max_mark_age=M)
    account.apply_fill(fill('a', qty=150, at=MIDNIGHT - M))
    account.apply_fill(fill('b', figi='B', qty=150, at=MIDNIGHT - M))
    assessment = policy().evaluate(MIDNIGHT, account)
    assert assessment.uncovered_value == 20002
    assert assessment.amount == 40
    assert policy().evaluate(MIDNIGHT + M, account) is None


def test_short_uses_explicit_previous_close_not_current_trade_mark():
    account = ResearchAccount(10000, max_mark_age=M)
    account.apply_fill(fill('short', side='SELL', qty=100, at=MIDNIGHT - M))
    account.mark('A', 999, available_at=MIDNIGHT)
    funding = policy(previous_session=lambda day: FundingSession(date(2026, 9, 4), 'calendar-fixture'),
        closing_quote=lambda figi, day: FundingClose(100, day, MIDNIGHT - M, 'official-close-fixture'))
    result = funding.evaluate(MIDNIGHT, account)
    assert result.uncovered_value == 10000
    assert result.amount == 40
    assert account.snapshot(at=MIDNIGHT).short_market_value == 99900


def test_missing_and_future_close_evidence_never_become_zero_fee():
    account = ResearchAccount(10000, max_mark_age=M)
    account.apply_fill(fill('short', side='SELL', qty=100, at=MIDNIGHT - M))
    assert policy().evaluate(MIDNIGHT, account).amount is None
    funding = policy(previous_session=lambda day: FundingSession(date(2026, 9, 4), 'calendar-fixture'),
        closing_quote=lambda figi, day: FundingClose(100, day, MIDNIGHT + M, 'future-close'))
    assert funding.evaluate(MIDNIGHT, account).reason == 'CLOSING_QUOTE_INVALID_OR_FUTURE'


def test_weekend_calendar_days_are_charged_and_final_cash_reconciles():
    start = MIDNIGHT - 2 * M
    monday = MIDNIGHT + timedelta(days=2, hours=10)
    candles = [Candle(at, 1000, 1000, 1000, 1000) for at in (start, start + M, monday)]
    item = spec(candles=candles)
    item.config.qty = 20
    result = run_shared_account([item], initial_capital=10000, gross_equity_limit=3, funding_policy=policy())
    paid = [charge for charge in result.funding_charges if charge.amount]
    assert [charge.billing_date for charge in paid] == [date(2026, 9, day) for day in (5, 6, 7)]
    assert [charge.amount for charge in paid] == [40, 40, 40]
    assert result.final_equity == 9880


def test_midnight_fee_changes_next_admission_instead_of_only_final_report():
    times = [MIDNIGHT + (i - 2) * M for i in range(4)]
    a = spec('A', candles=[Candle(at, 1000, 1000, 1000, 1000) for at in times])
    a.config.qty = 20
    b = spec('B', candles=[Candle(at, 100, 100, 100, 100) for at in times], signal_indices=(1,))
    free = run_shared_account([a, b], initial_capital=10000, gross_equity_limit=2.1)
    charged = run_shared_account([a, b], initial_capital=10000, gross_equity_limit=2.1, funding_policy=policy())
    assert len(free.ledgers['B'].trades) == 1
    assert not charged.ledgers['B'].trades
    assert charged.rejections[0]['at'] == MIDNIGHT
    assert charged.final_equity == 9960


def test_unresolved_short_funding_aborts_replay_explicitly():
    from app.engine.models import Side
    times = [MIDNIGHT + (i - 2) * M for i in range(4)]
    item = spec(candles=[Candle(at, 100, 100, 100, 100) for at in times], side=Side.SELL)
    with pytest.raises(ValueError, match='Unknown funding'):
        run_shared_account([item], initial_capital=10000, gross_equity_limit=2, funding_policy=policy())


def test_unknown_small_balance_exemption_is_not_assumed_free():
    account = ResearchAccount(1000, max_mark_age=M)
    account.apply_fill(fill('long', qty=15, at=MIDNIGHT - M))
    funding = OvernightFundingPolicy(plan='trader', valuation_minute_msk=0, small_balance_exempt=None)
    result = funding.evaluate(MIDNIGHT, account)
    assert result.amount is None
    assert result.reason == 'SMALL_BALANCE_EXEMPTION_UNKNOWN'
