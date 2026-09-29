"""Канон ATR: engine.indicators.atr и indicatorhub._atr обязаны совпадать.

В проекте ATR исторически жил в двух местах: в движке
(app.engine.indicators.atr) и в индикаторном хабе
(app.engine.indicatorhub._atr). Для ADX и RSI дубликаты уже выкинули в
пользу хаба (ENG-010, audit 2026-09-29) — значения расходились. ATR пока
был в том же состоянии, поэтому здесь фиксируем равенство побитово: если
одна из реализаций когда-нибудь поменяет формулу или порядок операций,
тест упадёт, а не тихо разъедется с ранжированием Universe.

Проверяется полная серия, не только последнее значение: ATR рекуррентен,
поэтому расхождение может спрятаться в середине.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.engine.indicatorhub import _atr as hub_atr
from app.engine.indicators import atr as engine_atr
from app.engine.models import Candle

PERIOD = 14
WINDOW = 44


def _series(seed: int, n: int, *, start: float = 100.0) -> list[Candle]:
    """Детерминированный псевдослучайный ценовой путь."""
    rnd = random.Random(seed)
    out: list[Candle] = []
    price = start
    t0 = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
    for i in range(n):
        price = max(1.0, price + rnd.uniform(-2.0, 2.0))
        hi = price + abs(rnd.uniform(0.0, 1.5))
        lo = price - abs(rnd.uniform(0.0, 1.5))
        out.append(
            Candle(
                ts=t0 + timedelta(minutes=i),
                open=price,
                high=hi,
                low=lo,
                close=price,
                volume=rnd.uniform(10.0, 1000.0),
            )
        )
    return out


def _flat(n: int, price: float = 50.0) -> list[Candle]:
    t0 = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
    return [
        Candle(ts=t0 + timedelta(minutes=i), open=price, high=price, low=price,
               close=price, volume=1.0)
        for i in range(n)
    ]


def _spiky(n: int, every: int = 17) -> list[Candle]:
    base = 100.0
    t0 = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)
    out = []
    for i in range(n):
        price = base + (40.0 if i % every == 0 else 0.0)
        out.append(
            Candle(ts=t0 + timedelta(minutes=i), open=price, high=price + 2.0,
                   low=price - 2.0, close=price, volume=100.0)
        )
    return out


def _assert_identical(candles: list[Candle], period: int = PERIOD) -> None:
    got = hub_atr(candles, period)
    want = engine_atr(candles, period)
    assert len(got) == len(want)
    for i, (a, b) in enumerate(zip(got, want)):
        assert a == b, f"расхождение в индексе {i}: hub={a!r} engine={b!r}"


def test_random_series_match_bit_for_bit():
    for seed in range(25):
        _assert_identical(_series(seed, 200))


def test_match_around_period_boundaries():
    for n in (0, 1, 12, 13, 14, 15, 16, 43, 44, 45, 200, 1000):
        _assert_identical(_series(7, n))


def test_match_on_other_periods():
    for period in (2, 5, 9, 14, 20, 50):
        _assert_identical(_series(11, 300), period)


def test_match_on_degenerate_series():
    _assert_identical(_flat(120))
    _assert_identical(_spiky(200))
    _assert_identical(_spiky(200, every=3))
    _assert_identical([])


def test_universe_window_value_is_identical():
    """Ровно тот срез, который читает Universe: последние 44 бакета, period 14."""
    for seed in (1, 2, 3, 99):
        candles = _series(seed, 400)
        seg = candles[-WINDOW:]
        hub_last = next(v for v in reversed(hub_atr(seg, PERIOD)) if v is not None)
        engine_last = next(
            v for v in reversed(engine_atr(seg, PERIOD)) if v is not None
        )
        assert hub_last == engine_last
