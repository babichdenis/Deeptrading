from datetime import datetime, timedelta, timezone
import pytest
from app.bot.universe.move_select import MoveSelectConfig, _move_score

def row(day, opening, close):
    return (datetime(2026,11,day,tzinfo=timezone.utc),opening,max(opening,close),min(opening,close),close)

@pytest.mark.parametrize('days,expected,bars', [(1,0.1,1),(2,-0.12,2),(3,-0.208,3)])
def test_window_excludes_padding_and_uses_last_observed_dates(days,expected,bars):
    history=[row(1,1000,500),row(10,100,90),row(12,90,72),row(13,72,79.2)]
    result=_move_score(history,MoveSelectConfig(lookback_days=days))
    assert result['cum_ret'] == pytest.approx(expected)
    assert result['bars'] == bars
    assert result['first_open'] == history[-days][1]


def test_hourly_window_keeps_all_closed_rows_of_selected_date():
    previous=row(10,1000,500)
    first=row(12,100,90)
    second=(first[0]+timedelta(hours=1),90,95,88,95)
    result=_move_score([previous,first,second],MoveSelectConfig(tf='hour',lookback_days=1))
    assert result['bars'] == 2
    assert result['cum_ret'] == pytest.approx(-0.05)


def test_insufficient_observed_dates_do_not_produce_score():
    result=_move_score([row(12,100,110)],MoveSelectConfig(lookback_days=3))
    assert result['cum_ret'] is None
    assert result['range'] is None
    assert result['rsi'] is None
