from bisect import bisect_left
from collections import Counter
from datetime import timedelta, time
from zoneinfo import ZoneInfo


MSK = ZoneInfo('Europe/Moscow')
FIVE_MINUTES = timedelta(minutes=5)


def _main_session(at):
    local = at.astimezone(MSK)
    return local.weekday() < 5 and time(10) <= local.time() < time(18, 45)


def _ordered(bars):
    values = tuple(bars)
    previous = None
    for bar in values:
        if bar.ts.tzinfo is None or bar.ts.utcoffset() is None or (previous is not None and bar.ts <= previous):
            raise ValueError('Strictly ordered aware context bars required')
        previous = bar.ts
    return values


def build_relative_context(stock_bars, complete_market_bars, decision_times, *,
                           lookback_dates=3, minimum_common_bars=95):
    """Previous observed main-session dates; source coverage is not a trading calendar.

    Market input must contain independently identified complete closed 5m buckets.
    Stock bars match the frozen research rule, including observed sparse buckets.
    """
    if type(lookback_dates) is not int or lookback_dates <= 0 or type(minimum_common_bars) is not int or minimum_common_bars <= 0:
        raise ValueError('Positive integer context coverage parameters required')
    market = {bar.ts:bar for bar in _ordered(complete_market_bars) if _main_session(bar.ts)}
    dates = sorted({at.astimezone(MSK).date() for at in market})
    stocks = {ticker:_ordered(bars) for ticker,bars in stock_bars.items()}
    result = []
    previous = None
    for at in decision_times:
        if at.tzinfo is None or at.utcoffset() is None or (previous is not None and at <= previous):
            raise ValueError('Strictly ordered aware decision times required')
        previous = at
        index = bisect_left(dates, at.astimezone(MSK).date())
        prior = dates[max(0,index-lookback_dates):index]
        snapshot = {'as_of':at.isoformat(),'prior_dates':[day.isoformat() for day in prior],'features':{}}
        for ticker,bars in stocks.items():
            selected = [bar for bar in bars if bar.ts.astimezone(MSK).date() in prior
                        and bar.ts in market and bar.ts+FIVE_MINUTES <= at]
            counts = Counter(bar.ts.astimezone(MSK).date() for bar in selected)
            if len(prior)!=lookback_dates or any(counts[day]<minimum_common_bars for day in prior):
                feature={'valid':False,'reason':'insufficient_common_session_coverage','common_bars':len(selected)}
            else:
                first,last=selected[0],selected[-1]
                stock_return=last.close/first.open-1
                market_return=market[last.ts].close/market[first.ts].open-1
                feature={'valid':True,'stock_return':stock_return,'moex_return':market_return,
                         'relative_return':stock_return-market_return,'common_bars':len(selected),
                         'from':first.ts.isoformat(),'through':(last.ts+FIVE_MINUTES).isoformat()}
            snapshot['features'][ticker]=feature
        result.append(snapshot)
    return result
