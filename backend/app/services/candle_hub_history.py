from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.engine.candlehub import CandleSeries, tf_to_seconds, validate_candle
from app.engine.models import Candle
from app.models.candle import Candle as StoredCandle


@dataclass(frozen=True)
class HistoricalRange:
    candles: tuple[Candle, ...]
    status: str
    coverage_known: bool
    source_rows: int
    missing_minutes: tuple[datetime, ...]
    unexpected_minutes: tuple[datetime, ...]
    incomplete_buckets: tuple[datetime, ...]
    calendar_evidence: str | None
    error_type: str | None = None
    source: str = 'postgresql:1m'
    require_full_minutes: bool = True
    sparse_buckets: tuple[tuple[datetime, int], ...] = ()


@dataclass(frozen=True)
class HistoricalAggregation:
    candles: tuple[Candle, ...]
    incomplete_buckets: tuple[datetime, ...]
    sparse_buckets: tuple[tuple[datetime, int], ...]


def aggregate_history(candles: Iterable[Candle], figi: str, tf: str | int, *,
                      as_of: datetime, require_full_minutes: bool = True) -> HistoricalAggregation:
    """Aggregate verified minute snapshots without inventing missing minutes or their cause."""
    as_of = _utc(as_of)
    seconds = tf_to_seconds(tf)
    if seconds % 60 or not isinstance(require_full_minutes, bool):
        raise ValueError('Invalid historical aggregation policy')
    values = tuple(candles)
    counts = {}
    previous = None
    for candle in values:
        at = _utc(candle.ts)
        if at.timestamp() % 60 or at + timedelta(minutes=1) > as_of:
            raise ValueError('Minute outside visibility or grid')
        if previous is not None and at <= previous:
            raise ValueError('Minute history must strictly increase')
        if validate_candle(candle) is not None:
            raise ValueError('Invalid minute OHLCV')
        previous = at
        bucket = datetime.fromtimestamp(int(at.timestamp()) // seconds * seconds, tz=timezone.utc)
        counts[bucket] = counts.get(bucket, 0) + 1
    closed = {ts for ts in counts if ts + timedelta(seconds=seconds) <= as_of}
    full = {ts for ts in closed if counts[ts] == seconds // 60}
    selected = full if require_full_minutes else closed
    series = CandleSeries(figi, seconds, maxlen=max(1, len(values)))
    series.seed(values)
    series.finalize()
    output = tuple(c for c in series.snapshot() if c.ts in selected)
    if len(output) != len(selected):
        raise ValueError('Canonical aggregation lost selected buckets')
    return HistoricalAggregation(output, tuple(sorted(set(counts) - full)),
                                 tuple((ts, counts[ts]) for ts in sorted(closed - full)))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('Aware range timestamps required')
    return value.astimezone(timezone.utc)


async def read_history_range(db, figi: str, tf: str | int, start: datetime, end: datetime,
                             *, as_of: datetime, expected_minutes: Iterable[datetime] | None = None,
                             calendar_evidence: str | None = None,
                             require_full_minutes: bool = True) -> HistoricalRange:
    """Read-only hub storage adapter, no memory-cache limit or automatic network fill.

    Expected minutes must come from independent calendar/coverage evidence, never
    from the returned rows. Unknown coverage remains partial even with valid bars.
    Sparse closed buckets are opt-in and never imply verified source coverage.
    """
    start, end, as_of = _utc(start), _utc(end), _utc(as_of)
    seconds = tf_to_seconds(tf)
    if seconds % 60 or not figi or start >= end or not isinstance(require_full_minutes, bool):
        raise ValueError('Invalid historical range')
    if start.timestamp() % seconds or end.timestamp() % seconds:
        raise ValueError('Range boundaries must align with requested timeframe')
    if expected_minutes is not None and not calendar_evidence:
        raise ValueError('Independent expected-minute evidence required')
    visible_end = min(end, as_of)
    if visible_end < start + timedelta(seconds=seconds):
        return HistoricalRange((), 'not_visible', False, 0, (), (), (), calendar_evidence,
                               require_full_minutes=require_full_minutes)
    minute = timedelta(minutes=1)
    expected = None
    if expected_minutes is not None:
        expected = set()
        for timestamp in expected_minutes:
            timestamp = _utc(timestamp)
            if timestamp.timestamp() % 60 or not start <= timestamp < end:
                raise ValueError('Expected minute outside aligned request')
            if timestamp + minute <= visible_end:
                expected.add(timestamp)
    query = select(StoredCandle.ts, StoredCandle.open, StoredCandle.high,
                   StoredCandle.low, StoredCandle.close, StoredCandle.volume).where(
        StoredCandle.figi == figi, StoredCandle.interval == 1,
        StoredCandle.ts >= start, StoredCandle.ts < end,
        StoredCandle.ts <= visible_end - minute).order_by(StoredCandle.ts)
    try:
        rows = (await db.execute(query)).all()
    except SQLAlchemyError as exc:
        return HistoricalRange((), 'source_error', expected is not None, 0, (), (), (),
                               calendar_evidence, type(exc).__name__, require_full_minutes=require_full_minutes)
    candles = []
    seen = set()
    for timestamp, opening, high, low, close, volume in rows:
        timestamp = _utc(timestamp)
        if timestamp.timestamp() % 60 or not start <= timestamp < end or timestamp + minute > visible_end:
            raise ValueError('Stored candle outside request/visibility or off minute grid')
        if timestamp in seen or (candles and timestamp <= candles[-1].ts):
            raise ValueError('Duplicate or unordered stored candle')
        candle = Candle(timestamp, float(opening), float(high), float(low), float(close), float(volume))
        if validate_candle(candle) is not None:
            raise ValueError('Invalid persisted OHLCV')
        seen.add(timestamp)
        candles.append(candle)
    aggregation = aggregate_history(candles, figi, seconds, as_of=as_of,
                                    require_full_minutes=require_full_minutes)
    output, incomplete = aggregation.candles, aggregation.incomplete_buckets
    missing = tuple(sorted(expected - seen)) if expected is not None else ()
    unexpected = tuple(sorted(seen - expected)) if expected is not None else ()
    if not candles:
        status = 'no_trading' if expected == set() else 'missing'
    elif expected is None or missing or unexpected or incomplete:
        status = 'partial'
    else:
        status = 'ok'
    return HistoricalRange(output, status, expected is not None, len(candles), missing,
                           unexpected, incomplete, calendar_evidence,
                           require_full_minutes=require_full_minutes,
                           sparse_buckets=aggregation.sparse_buckets)
