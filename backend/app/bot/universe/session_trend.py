from dataclasses import dataclass
from datetime import datetime, timedelta
import math


@dataclass(frozen=True)
class SessionWindow:
    session_id: str
    opens_at: datetime
    closes_at: datetime
    schedule_source: str
    market_end: datetime | None = None


def completed_session_trend(bars, sessions, as_of, *, count=3, max_close_age=timedelta(minutes=5)):
    if as_of.utcoffset() is None or count < 1 or max_close_age < timedelta(0):
        raise ValueError('Aware clock, positive count and nonnegative close age required')
    ordered = sorted(sessions, key=lambda s: s.opens_at)
    for index, session in enumerate(ordered):
        if (session.opens_at.utcoffset() is None or session.closes_at.utcoffset() is None
                or session.opens_at >= session.closes_at or not session.schedule_source):
            raise ValueError('Explicit aware session and schedule provenance required')
        if session.market_end is not None and (session.market_end.utcoffset() is None
                or not session.opens_at < session.market_end <= session.closes_at):
            raise ValueError('Invalid market end or availability time')
        if index and ordered[index-1].closes_at > session.opens_at:
            raise ValueError('Overlapping sessions')
    completed = [s for s in ordered if s.closes_at <= as_of]
    selected = completed[-(count+1):]
    receipt = {'state':'unknown', 'count':count, 'as_of':as_of.isoformat(),
               'sessions':[s.session_id for s in selected], 'closes':[],
               'return_pct':None, 'direction':None, 'efficiency':None}
    if len(selected) < count+1:
        return {**receipt, 'reason':'insufficient_completed_sessions'}
    visible = []
    for bar in bars:
        if bar.ts.utcoffset() is None:
            raise ValueError('Aware bar timestamp required')
        if bar.ts + timedelta(minutes=1) <= as_of:
            visible.append(bar)
    for session in selected:
        eligible = [b for b in visible if session.opens_at <= b.ts
                    and b.ts + timedelta(minutes=1) <= session.closes_at
                    and (session.market_end is None or b.ts < session.market_end)]
        if not eligible:
            return {**receipt, 'reason':'missing_session_close'}
        bar = max(eligible, key=lambda b:b.ts)
        if sum(b.ts == bar.ts for b in eligible) != 1:
            return {**receipt, 'reason':'duplicate_close_bar'}
        age = session.closes_at - (bar.ts + timedelta(minutes=1))
        if age > max_close_age or not math.isfinite(bar.close) or bar.close <= 0:
            return {**receipt, 'reason':'stale_or_invalid_session_close'}
        receipt['closes'].append({'session_id':session.session_id, 'price':bar.close,
                                  'bar_ts':bar.ts.isoformat(), 'age_seconds':age.total_seconds(),
                                  'schedule_source':session.schedule_source})
    prices = [row['price'] for row in receipt['closes']]
    change = prices[-1]-prices[0]
    path = sum(abs(b-a) for a,b in zip(prices,prices[1:]))
    return {**receipt, 'state':'known', 'reason':None, 'return_pct':100*change/prices[0],
            'direction':'up' if change > 0 else 'down' if change < 0 else 'flat',
            'efficiency':abs(change)/path if path else 0.0}
