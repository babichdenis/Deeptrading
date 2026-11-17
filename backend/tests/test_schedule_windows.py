from datetime import datetime, timezone

import pytest

from app.bot.universe.schedule_windows import schedule_windows

NOW=datetime(2026,11,16,12,tzinfo=timezone.utc)


def receipt():
    interval={'start_ts':'2026-11-16T04:00:00+00:00','end_ts':'2026-11-16T05:59:59+00:00'}
    rows=[{'type':kind,'interval':interval} for kind in ['regular_trading_session','regular_trading_session_morning','opening_auction']]
    return {'requested_at':NOW.isoformat(),'provider':'test','sha256':'hash',
            'raw_response':{'exchanges':[{'exchange':'MOEX','days':[{'date':'2026-11-16','is_trading_day':True,'intervals':rows}]}]}}


def test_deduplicates_regular_alias_and_ignores_auction_without_rounding():
    windows=schedule_windows(receipt(),'moex',NOW)
    assert len(windows)==1
    assert windows[0].closes_at.second==59
    assert 'hash' in windows[0].schedule_source


def test_future_observation_rejected():
    with pytest.raises(ValueError,match='not yet observed'):
        schedule_windows(receipt(),'MOEX',NOW.replace(hour=11))


def test_ambiguous_case_match_rejected():
    data=receipt()
    data['raw_response']['exchanges'].append({'exchange':'moex','days':[]})
    with pytest.raises(ValueError,match='ambiguous'):
        schedule_windows(data,'MOEX',NOW)


def test_nontrading_day_is_not_invented():
    data=receipt()
    data['raw_response']['exchanges'][0]['days'][0]['is_trading_day']=False
    assert schedule_windows(data,'MOEX',NOW)==[]


def test_day_groups_sessions_but_waits_for_last_minute_close():
    from app.bot.universe.schedule_windows import trading_day_windows
    data=receipt()
    intervals=data['raw_response']['exchanges'][0]['days'][0]['intervals']
    intervals.append({'type':'regular_trading_session_evening','interval':{
        'start_ts':'2026-11-16T16:00:01+00:00','end_ts':'2026-11-16T20:49:59+00:00'}})
    days=trading_day_windows(data,'moex',NOW)
    assert len(days)==1
    assert days[0].market_end==NOW.replace(hour=20,minute=49,second=59)
    assert days[0].closes_at==NOW.replace(hour=20,minute=50)


def test_four_trading_days_produce_three_returns_only_when_last_bar_closed():
    from app.bot.universe.schedule_windows import trading_day_windows
    from app.bot.universe.session_trend import completed_session_trend
    from datetime import timedelta
    from types import SimpleNamespace
    data=receipt()
    data['requested_at']='2026-11-15T12:00:00+00:00'
    days=[]
    bars=[]
    for i,price in enumerate([100,102,101,106]):
        day=16+i
        days.append({'date':f'2026-11-{day}','is_trading_day':True,'intervals':[
            {'type':'regular_trading_session','interval':{
                'start_ts':f'2026-11-{day}T04:00:00+00:00','end_ts':f'2026-11-{day}T20:49:59+00:00'}}]})
        bars.append(SimpleNamespace(ts=NOW.replace(day=day,hour=20,minute=49),close=price))
    data['raw_response']['exchanges'][0]['days']=days
    at=NOW.replace(day=19,hour=20,minute=50)
    windows=trading_day_windows(data,'MOEX',at)
    assert completed_session_trend(bars,windows,at)['return_pct']==6
    assert completed_session_trend(bars,windows,at-timedelta(seconds=1))['state']=='unknown'
