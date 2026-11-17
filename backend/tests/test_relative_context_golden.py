from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from dataclasses import replace

from app.engine.models import Candle
from app.engine.relative_context import build_relative_context


MSK=ZoneInfo('Europe/Moscow')
AT=datetime(2026,9,4,10,tzinfo=MSK)


def bars():
    return [Candle(datetime(2026,9,day,10,minute,tzinfo=MSK),100,102,99,101,10)
            for day in (1,2,3) for minute in (0,5)]


def test_previous_three_dates_and_exact_endpoints():
    market=bars();stock=[replace(b,open=100,close=102) for b in market]
    row=build_relative_context({'A':stock},market,[AT],minimum_common_bars=2)[0]
    assert row['prior_dates']==['2026-09-01','2026-09-02','2026-09-03']
    feature=row['features']['A'];assert feature['valid']
    assert feature['relative_return']==102/100-101/100
    assert feature['common_bars']==6


def test_missing_one_prior_date_is_unknown_not_shorter_lookback():
    market=bars();stock=[b for b in market if b.ts.day!=2]
    row=build_relative_context({'A':stock},market,[AT],minimum_common_bars=2)[0]
    assert row['features']['A']['valid'] is False


def test_future_bars_cannot_change_prior_context():
    market=bars();future=replace(market[-1],ts=AT+timedelta(days=1),close=999,high=1000)
    old=build_relative_context({'A':market},market,[AT],minimum_common_bars=2)
    new=build_relative_context({'A':market+[future]},market+[future],[AT],minimum_common_bars=2)
    assert old==new
