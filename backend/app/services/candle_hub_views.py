from dataclasses import replace
from datetime import timedelta

from app.engine.candlehub import tf_to_seconds
from app.services.candle_hub_history import aggregate_history


def historical_timeframe_views(minute_history, figi, timeframes, *, start, end, as_of,
                               require_full_minutes=True):
    if any(t.utcoffset() is None for t in (start,end,as_of)) or start>=end or not figi or not isinstance(require_full_minutes, bool):
        raise ValueError('Aware ordered history range required')
    if minute_history.source_rows != len(minute_history.candles):
        raise ValueError('Unaggregated minute snapshot required')
    for candle in minute_history.candles:
        if not start <= candle.ts < end or candle.ts + timedelta(minutes=1) > min(end, as_of):
            raise ValueError('Snapshot outside requested range or visibility')
    views={}
    for tf in timeframes:
        seconds=tf_to_seconds(tf)
        if seconds%60 or start.timestamp()%seconds or end.timestamp()%seconds:
            raise ValueError('Range must align with each timeframe')
        if min(end,as_of)<start+timedelta(seconds=seconds):
            views[tf]=replace(minute_history,candles=(),status='not_visible',coverage_known=False,source_rows=0,missing_minutes=(),unexpected_minutes=(),incomplete_buckets=(),sparse_buckets=(),error_type=None,require_full_minutes=require_full_minutes)
            continue
        if minute_history.error_type is not None or minute_history.status=='source_error':
            views[tf]=replace(minute_history,require_full_minutes=require_full_minutes)
            continue
        aggregated=aggregate_history(minute_history.candles,figi,tf,as_of=min(end,as_of),require_full_minutes=require_full_minutes)
        if not minute_history.candles:
            status=minute_history.status
        else:
            status='partial' if not minute_history.coverage_known or minute_history.missing_minutes or minute_history.unexpected_minutes or aggregated.incomplete_buckets else 'ok'
        views[tf]=replace(minute_history,candles=aggregated.candles,status=status,incomplete_buckets=aggregated.incomplete_buckets,sparse_buckets=aggregated.sparse_buckets,require_full_minutes=require_full_minutes)
    return views
