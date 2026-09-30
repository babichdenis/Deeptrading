"""Паритет хвостовых индикаторов портов (owner-кэш) с полным расчётом — бит-в-бит."""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.engine.ose import indicators as I

T0 = datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)


def _series(n: int, seed: int = 42, flat_from: int | None = None):
    rnd = random.Random(seed)
    out = []
    px = 100.0
    for i in range(n):
        if flat_from is not None and i >= flat_from:
            px = 100.0
        else:
            px = max(5.0, px + rnd.uniform(-1.5, 1.5))
        hi = px + rnd.uniform(0.0, 1.0)
        lo = px - rnd.uniform(0.0, 1.0)
        out.append(Candle(ts=T0 + timedelta(minutes=10 * i), open=px,
                          high=hi, low=lo, close=px, volume=1.0 + i % 7))
    return out


def _eq(a, b) -> bool:
    if isinstance(a, dict):
        return set(a) == set(b) and all(_eq(a[k], b[k]) for k in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(
            (x is None and y is None) or x == y for x, y in zip(a, b))
    return a == b


# (имя, позиционные args, kwargs)
CASES = [
    ("sma", (21,), {}),
    ("rsi", (14,), {}),
    ("stochastic", (), {}),
    ("bollinger", (), {}),
    ("envelops", (), {}),
    ("price_channel", (), {}),
    ("atr", (), {}),
    ("atr", (), {"mode": "percent"}),
    ("cci", (), {}),
    ("macd", (), {}),
    ("rvi", (), {}),
]


def test_incremental_bit_parity():
    bars = _series(160)
    for name, args, kwargs in CASES:
        fn = getattr(I, name)
        owner = type("O", (), {})()
        for n in range(1, len(bars) + 1):
            tail = fn(bars[:n], *args, owner=owner, **kwargs)
            batch = fn(bars[:n], *args, **kwargs)
            assert _eq(tail, batch), f"{name}{args}{kwargs} расходится на n={n}"


def test_incremental_flat_parity():
    bars = _series(140, seed=7, flat_from=80)
    for name, args, kwargs in CASES:
        fn = getattr(I, name)
        owner = type("O", (), {})()
        for n in range(1, len(bars) + 1):
            assert _eq(fn(bars[:n], *args, owner=owner, **kwargs),
                       fn(bars[:n], *args, **kwargs)), f"{name} flat n={n}"


def test_power_and_cache_reset():
    bars = _series(120)
    owner = type("O", (), {})()
    for n in range(1, len(bars) + 1):
        assert _eq(I.bulls_power(bars[:n], owner=owner), I.bulls_power(bars[:n]))
        assert _eq(I.bears_power(bars[:n], owner=owner), I.bears_power(bars[:n]))
    # Смена серии на том же owner: кэш обязан пересчитаться
    other = _series(120, seed=77)
    assert _eq(I.sma(other, 21, owner=owner), I.sma(other, 21))
    assert _eq(I.rsi(other, owner=owner), I.rsi(other))
    # Уменьшение окна
    assert _eq(I.macd(bars[:40], owner=owner), I.macd(bars[:40]))
