from datetime import datetime, timedelta, timezone

from app.engine.models import PositionState, Side, Signal, DecisionAction
from scripts.research.architect_rotation_panel import select_at, RotationPolicy


AT = datetime(2026, 9, 15, tzinfo=timezone.utc)


def trade(exit_time, net=1, reason='signal_exit'):
    return {'exit_time': exit_time.isoformat(), 'net_pnl': net, 'entry_price': 100, 'qty': 1, 'exit_reason': reason}


def test_unknown_and_future_exits_cannot_change_selection():
    known = {'A': [trade(AT-timedelta(days=1)) for _ in range(5)]}
    expected = select_at(known, AT)
    assert expected and expected[0]['ticker'] == 'A'
    changed = {**known, 'B': [trade(AT, 100000) for _ in range(20)]}
    changed['A'] = known['A'] + [trade(AT+timedelta(days=1), -100000)]
    assert select_at(changed, AT) == expected
    assert select_at({'A': [trade(AT-timedelta(days=1), reason='end_of_data') for _ in range(5)]}, AT) == []


def test_exit_bar_must_be_available_before_selection():
    rows = {'A': [trade(AT-timedelta(minutes=5)) for _ in range(5)]}
    assert select_at(rows, AT) == []
    assert select_at(rows, AT+timedelta(seconds=1))


def test_exclusion_does_not_block_exit_and_allows_reentry_on_return():
    signal = Signal(strategy_id='test', side=Side.SELL, time=AT)
    p = RotationPolicy('A', {})
    assert p.decide(signal, PositionState.LONG, 5)[0] is DecisionAction.ACCEPT_EXIT
    assert p.decide(signal, PositionState.FLAT, 0)[0] is DecisionAction.NO_SIGNAL
    p.daily['2026-09-15'] = {'A'}
    assert p.decide(signal, PositionState.FLAT, 0)[0] is DecisionAction.ACCEPT_ENTRY
