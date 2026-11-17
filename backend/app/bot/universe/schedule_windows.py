from datetime import datetime, timedelta

from app.bot.universe.session_trend import SessionWindow


def schedule_windows(receipt, exchange, as_of):
    observed = datetime.fromisoformat(receipt['requested_at'])
    if observed.utcoffset() is None or as_of.utcoffset() is None:
        raise ValueError('Aware schedule clocks required')
    if observed > as_of:
        raise ValueError('Schedule not yet observed at decision time')
    matches = [row for row in receipt['raw_response']['exchanges']
               if row['exchange'].casefold() == exchange.casefold()]
    if len(matches) != 1:
        raise ValueError('Schedule exchange missing or ambiguous')
    windows = []
    provenance = f"{receipt['provider']}:{receipt['sha256']}:{observed.isoformat()}"
    for day in matches[0]['days']:
        if not day['is_trading_day']:
            continue
        regular = [row for row in day.get('intervals', [])
                   if row['type'] == 'regular_trading_session'
                   or row['type'].startswith('regular_trading_session_')]
        unique = {}
        for row in regular:
            start = datetime.fromisoformat(row['interval']['start_ts'])
            end = datetime.fromisoformat(row['interval']['end_ts'])
            if (start.utcoffset() is None or end.utcoffset() is None
                    or start.year <= 1970 or start >= end):
                raise ValueError('Invalid regular trading interval')
            unique[(start,end)] = True
        if not unique:
            raise ValueError('Trading day has no explicit regular intervals')
        ordered = sorted(unique)
        if any(left[1] > right[0] for left,right in zip(ordered,ordered[1:])):
            raise ValueError('Conflicting regular intervals')
        for index, (start,end) in enumerate(ordered):
            windows.append(SessionWindow(f"{matches[0]['exchange']}:{day['date']}:{index}",start,end,provenance))
    return windows


def trading_day_windows(receipt, exchange, as_of):
    windows = schedule_windows(receipt, exchange, as_of)
    grouped = {}
    for window in windows:
        day_id = window.session_id.rsplit(':', 1)[0]
        grouped.setdefault(day_id, []).append(window)
    days = []
    for day_id, parts in grouped.items():
        opening = min(w.opens_at for w in parts)
        market_end = max(w.closes_at for w in parts)
        available_at = market_end.replace(second=0, microsecond=0)
        if available_at < market_end:
            available_at += timedelta(minutes=1)
        days.append(SessionWindow(day_id, opening, available_at,
                                  parts[0].schedule_source, market_end=market_end))
    return sorted(days, key=lambda day:day.opens_at)
