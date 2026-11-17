from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services.candle_hub_history import read_history_range
from app.services.candle_hub_history import aggregate_history
from app.engine.models import Candle


T = datetime(2026, 9, 1, tzinfo=timezone.utc)
M = timedelta(minutes=1)


def db(rows):
    return SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))


def row(i, price=100):
    return (T + i * M, price, price + 1, price - 1, price, 1)


@pytest.mark.asyncio
async def test_more_than_5000_minutes_are_not_silently_truncated():
    result = await read_history_range(db([row(i) for i in range(6001)]), 'TEST', '1min',
        T, T + 6001 * M, as_of=T + 6001 * M,
        expected_minutes=(T + i * M for i in range(6001)), calendar_evidence='synthetic-continuous-session')
    assert result.status == 'ok'
    assert len(result.candles) == result.source_rows == 6001
    assert result.candles[0].ts == T and result.candles[-1].ts == T + 6000 * M


@pytest.mark.asyncio
async def test_incomplete_bucket_not_returned_and_unknown_calendar_not_complete():
    result = await read_history_range(db([row(i) for i in range(10) if i != 2]), 'TEST', '5min',
        T, T + 10 * M, as_of=T + 10 * M)
    assert result.status == 'partial' and not result.coverage_known
    assert [c.ts for c in result.candles] == [T + 5 * M]
    assert result.incomplete_buckets == (T,)


@pytest.mark.asyncio
async def test_expected_missing_and_empty_no_trading_are_distinct():
    unknown = await read_history_range(db([]), 'TEST', '1min', T, T + M, as_of=T + M)
    closed = await read_history_range(db([]), 'TEST', '1min', T, T + M, as_of=T + M,
        expected_minutes=[], calendar_evidence='calendar-closed')
    missing = await read_history_range(db([]), 'TEST', '1min', T, T + M, as_of=T + M,
        expected_minutes=[T], calendar_evidence='calendar-open')
    assert unknown.status == missing.status == 'missing'
    assert closed.status == 'no_trading'
    assert missing.missing_minutes == (T,)


@pytest.mark.asyncio
async def test_db_error_not_no_trading():
    database = db([])
    database.execute.side_effect = SQLAlchemyError('private connection detail')
    result = await read_history_range(database, 'TEST', '1min', T, T + M, as_of=T + M)
    assert result.status == 'source_error'
    assert result.error_type == 'SQLAlchemyError'
    assert 'private' not in repr(result)


@pytest.mark.asyncio
async def test_revision_is_read_fresh_and_reaggregated():
    first = await read_history_range(db([row(i) for i in range(5)]), 'TEST', '5min', T, T + 5 * M, as_of=T + 5 * M)
    second = await read_history_range(db([row(i, 120 if i == 4 else 100) for i in range(5)]),
        'TEST', '5min', T, T + 5 * M, as_of=T + 5 * M)
    assert first.candles[0].close == 100
    assert second.candles[0].close == 120


@pytest.mark.asyncio
async def test_unclosed_or_invalid_rows_raise_instead_of_leaking():
    future = await read_history_range(db([row(0)]), 'TEST', '1min', T, T + M, as_of=T)
    assert future.status == 'not_visible' and not future.candles
    with pytest.raises(ValueError, match='visibility'):
        await read_history_range(db([row(1)]), 'TEST', '1min', T, T + 2 * M, as_of=T + M)
    with pytest.raises(ValueError, match='OHLCV'):
        await read_history_range(db([row(0, float('nan'))]), 'TEST', '1min', T, T + M, as_of=T + M)


@pytest.mark.asyncio
async def test_expected_calendar_cannot_be_implicit():
    with pytest.raises(ValueError, match='evidence'):
        await read_history_range(db([]), 'TEST', '1min', T, T + M, as_of=T + M, expected_minutes=[])


@pytest.mark.asyncio
async def test_sparse_output_preserves_unknown_coverage_and_missing_evidence():
    result = await read_history_range(db([row(0), row(4, 120)]), 'TEST', '5min',
        T, T + 5 * M, as_of=T + 5 * M, require_full_minutes=False,
        expected_minutes=[T + i * M for i in range(5)], calendar_evidence='synthetic')
    assert result.status == 'partial'
    assert result.missing_minutes == (T + M, T + 2 * M, T + 3 * M)
    assert result.sparse_buckets == ((T, 2),)
    assert result.incomplete_buckets == (T,)
    assert not result.require_full_minutes
    assert result.candles == (Candle(T, 100, 121, 99, 120, 2),)


def test_sparse_aggregation_never_fills_empty_or_unclosed_buckets():
    candles = [Candle(*row(i)) for i in (0, 4, 15)]
    output = aggregate_history(candles, 'TEST', '5min', as_of=T + 16 * M,
                               require_full_minutes=False)
    assert [c.ts for c in output.candles] == [T]
    assert output.sparse_buckets == ((T, 2),)
    assert output.incomplete_buckets == (T, T + 15 * M)


@pytest.mark.parametrize('indices,as_of', [([1, 0], 5), ([0, 0], 5), ([5], 5)])
def test_artifact_aggregation_validates_minute_sequence(indices, as_of):
    with pytest.raises(ValueError):
        aggregate_history([Candle(*row(i)) for i in indices], 'TEST', '5min',
                          as_of=T + as_of * M, require_full_minutes=False)


@pytest.mark.asyncio
async def test_unknown_sparse_and_error_do_not_become_complete():
    result = await read_history_range(db([row(0)]), 'TEST', '5min', T, T + 5 * M,
                                     as_of=T + 5 * M, require_full_minutes=False)
    assert result.status == 'partial' and not result.coverage_known
    database = db([])
    database.execute.side_effect = SQLAlchemyError('private')
    error = await read_history_range(database, 'TEST', '5min', T, T + 5 * M,
                                    as_of=T + 5 * M, require_full_minutes=False)
    assert error.status == 'source_error' and not error.require_full_minutes
