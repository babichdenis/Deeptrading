from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json


def probe_source_window(figi, start, end, fetch=None):
    now = datetime.now(timezone.utc)
    if not figi or any(t.utcoffset() is None for t in (start,end)):
        raise ValueError('Instrument and aware bounds required')
    if not start < end <= now or end-start > timedelta(hours=1):
        raise ValueError('Require historical window of at most one hour')
    if any(t.second or t.microsecond for t in (start,end)):
        raise ValueError('Minute aligned bounds required')
    receipt = {'provider':'t_tech.invest.market_data.get_candles','figi':figi,
               'start':start.isoformat(),'end':end.isoformat(),'interval':'1min',
               'requested_at':now.isoformat(),'writes_database':False}
    try:
        response = (fetch or _fetch)(figi,start,end)
    except Exception as error:
        return {**receipt,'status':'source_error','error_type':type(error).__name__,
                'absence_proven':False,'candles':[]}
    raw = asdict(response)
    encoded = json.dumps(raw,sort_keys=True,separators=(',',':'),default=str)
    rows = []
    for candle in response.candles:
        rows.append({'ts':candle.time.isoformat(),'is_complete':candle.is_complete,
                     **{field:float(getattr(candle,field).units)+float(getattr(candle,field).nano)/1e9
                        for field in ('open','high','low','close')},'volume':candle.volume})
    return {**receipt,'status':'response_received','raw_response':raw,
            'response_sha256':sha256(encoded.encode()).hexdigest(),'candles':rows,
            'absence_proven':False,'pagination':'single_bounded_request_no_continuation_token'}


def _fetch(figi,start,end):
    from t_tech.invest import Client, CandleInterval
    from app.config import get_settings
    with Client(get_settings().feed_token) as client:
        return client.market_data.get_candles(figi=figi,interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
                                             from_=start,to=end)
