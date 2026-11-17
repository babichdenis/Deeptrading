from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.bot.universe.session_trend import SessionWindow, completed_session_trend

START = datetime(2026, 11, 2, 7, tzinfo=timezone.utc)


def fixture():
    sessions = [SessionWindow(str(i), START+timedelta(days=i), START+timedelta(days=i,hours=10), 'test_schedule') for i in range(5)]
    bars = [SimpleNamespace(ts=s.closes_at-timedelta(minutes=1), close=p) for s,p in zip(sessions,[100,102,101,106,1])]
    return sessions,bars,sessions[3].closes_at


def test_three_returns_need_four_closes_and_ignore_future():
    sessions,bars,at=fixture()
    result=completed_session_trend(bars,sessions,at)
    assert result['sessions']==['0','1','2','3']
    assert result['return_pct']==6
    assert result['efficiency']==0.75
    assert result['direction']=='up'
    assert result==completed_session_trend(bars[:-1],sessions,at)


def test_missing_session_is_not_replaced_by_older_trading_day():
    sessions,bars,at=fixture()
    assert completed_session_trend([bars[0],bars[2],bars[3]],sessions,at)['reason']=='missing_session_close'


def test_unfinished_session_not_used():
    sessions,bars,at=fixture()
    assert completed_session_trend(bars,sessions,at-timedelta(seconds=1))['reason']=='insufficient_completed_sessions'


def test_stale_close_remains_unknown():
    sessions,bars,at=fixture()
    bars[1].ts-=timedelta(minutes=6)
    assert completed_session_trend(bars,sessions,at)['state']=='unknown'


def test_down_direction_and_duplicate_close():
    sessions,bars,at=fixture()
    bars[3].close=90
    assert completed_session_trend(bars,sessions,at)['direction']=='down'
    assert completed_session_trend(bars+[bars[3]],sessions,at)['reason']=='duplicate_close_bar'
