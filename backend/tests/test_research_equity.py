from datetime import datetime, timedelta, timezone

import pytest

from app.engine.research_equity import EquityMark, EquityPoint, EquityPosition, mark_paper_equity, observed_max_drawdown

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def value(positions=(), marks=None, accrued_costs=0.0):
    return mark_paper_equity(as_of=NOW, cash=9990.0, positions=positions, marks=marks or {},
                             max_mark_age=timedelta(minutes=5), accrued_costs=accrued_costs)


def test_long_short_mark_to_market_after_paid_and_accrued_costs():
    result = value([EquityPosition('LONG', 'BUY', 20, 100.0), EquityPosition('SHORT', 'SELL', 10, 200.0)],
                   {'LONG': EquityMark(110.0, NOW), 'SHORT': EquityMark(190.0, NOW)}, accrued_costs=5.0)
    assert result.equity == 10285.0
    assert result.unrealized_pnl == 300.0
    assert result.gross_exposure == 4100.0
    assert result.net_exposure == 300.0


@pytest.mark.parametrize('mark, reason', [(None, 'missing_mark'),
    (EquityMark(101.0, NOW + timedelta(seconds=1)), 'future_mark'),
    (EquityMark(101.0, NOW - timedelta(minutes=6)), 'stale_mark')])
def test_unknown_price_is_not_entry_price_or_zero_profit(mark, reason):
    result = value([EquityPosition('A', 'BUY', 10, 100.0)], {'A': mark} if mark else {})
    assert result.equity is None
    assert result.unknown == ('A:' + reason,)


def test_unknown_financing_is_not_free():
    assert value(accrued_costs=None).equity is None
    assert value().equity == 9990.0


def test_duplicate_figi_rejected():
    with pytest.raises(ValueError, match='Duplicate'):
        value([EquityPosition('A', 'BUY', 1, 100.0)] * 2)


def test_open_position_drawdown_not_hidden_by_final_recovery():
    points = [EquityPoint(NOW + timedelta(minutes=i), equity, 0, 0, 0, ())
              for i, equity in enumerate([10000.0, 12000.0, 9000.0, 13000.0])]
    assert observed_max_drawdown(points) == 0.25
    points.insert(2, EquityPoint(NOW + timedelta(minutes=1), None, None, None, None, ('missing',)))
    assert observed_max_drawdown(points) is None
