from datetime import datetime, timedelta, timezone

import pytest

from app.services.news import NewsItem, _parse_ts, blackout_reason


NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize('text', [
    '2026-09-01T12:00:00Z', '2026-09-01T15:00:00+03:00',
    '2026-09-01T14:00:00+02:00', '2026-09-01 15:00:00',
    'Tue, 01 Sep 2026 15:00:00 +0300',
])
def test_timestamp_offsets_preserved(text):
    assert _parse_ts(text) == NOW


@pytest.mark.parametrize('age_seconds,blocked', [(-1, False), (0, True), (3600, True), (3601, False)])
def test_blackout_only_known_published_window(age_seconds, blocked):
    item = NewsItem(source='fixture', title='SBER отмена дивидендов',
                    tickers=['SBER'], dt=NOW-timedelta(seconds=age_seconds))
    assert bool(blackout_reason([item], 'SBER', 60, as_of=NOW)) == blocked


def test_missing_or_naive_publication_not_treated_as_fresh():
    for timestamp in [None, NOW.replace(tzinfo=None)]:
        item = NewsItem(source='fixture', title='SBER отмена дивидендов', tickers=['SBER'], dt=timestamp)
        assert blackout_reason([item], 'SBER', as_of=NOW) is None


def test_naive_decision_time_rejected():
    with pytest.raises(ValueError, match='timezone aware'):
        blackout_reason([], 'SBER', as_of=NOW.replace(tzinfo=None))
