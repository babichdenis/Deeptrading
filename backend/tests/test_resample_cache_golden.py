from dataclasses import replace
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.engine.models import Candle
from app.services import ensemble


@pytest.fixture(autouse=True)
def clean_cache():
    ensemble.clear_resample_cache()
    yield
    ensemble.clear_resample_cache()


def candles(price=100.0):
    start = datetime(2026, 9, 1, 7, tzinfo=timezone.utc)
    return [Candle(start + timedelta(minutes=i), price, price + 2, price - 1, price + 1, 10) for i in range(12)]


@pytest.mark.parametrize('field,value', [('open', 90), ('high', 120), ('low', 80), ('close', 110), ('volume', 900)])
def test_revised_same_window_matches_uncached(field, value):
    bars = candles()
    ensemble.cached_resample(bars, 300)
    index = 0 if field == 'open' else 4
    bars[index] = replace(bars[index], **{field: value})
    assert ensemble.cached_resample(bars, 300) == ensemble.resample(bars, 300)


def test_cache_retention_is_bounded():
    for price in range(100):
        ensemble.cached_resample(candles(price), 300)
    assert ensemble._cached_resample_content.cache_info().currsize <= 8


def test_parallel_instruments_match_canonical():
    inputs = [candles(100 + i) for i in range(20)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        actual = list(pool.map(lambda bars: ensemble.cached_resample(bars, 300), inputs))
    assert actual == [ensemble.resample(bars, 300) for bars in inputs]


def test_equal_content_reuses_calculation():
    bars = candles()
    first = ensemble.cached_resample(bars, 300)
    assert ensemble.cached_resample(list(bars), 300) == first
    assert ensemble._cached_resample_content.cache_info().hits == 1


def test_returned_list_cannot_poison_cache():
    bars = candles()
    ensemble.cached_resample(bars, 300).clear()
    assert ensemble.cached_resample(bars, 300) == ensemble.resample(bars, 300)
