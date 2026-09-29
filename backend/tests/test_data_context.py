"""L2.1 parity-гейт DataContext: bars(tf) == batch resample на каждом префиксе.

closed — append-only (стабильность объектов), forming заменяется (не мутируется),
события 'closed' стреляют ровно на границах бакетов.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.data_context import DataContext
from app.services.ensemble import resample

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)
TFS = (60, 300, 600, 1800, 3600)


def _make_candles(n=1500, seed=7):
    import random
    rng = random.Random(seed)
    out = []
    price = 100.0
    for i in range(n):
        ts = T0 + timedelta(minutes=i)
        price = max(price + rng.gauss(0, 0.001) * price, 1.0)
        o = price
        h = price * (1 + abs(rng.gauss(0, 0.0005)))
        l = price * (1 - abs(rng.gauss(0, 0.0005)))
        c = price + rng.gauss(0, 0.0003) * price
        out.append(Candle(ts=ts, open=o, high=h, low=l, close=c,
                          volume=int(rng.uniform(500, 2000))))
    return out


def _key(b):
    return (b.ts.isoformat(), b.open, b.high, b.low, b.close, b.volume)


def test_bars_equal_batch_on_prefixes():
    candles = _make_candles()
    ctx = DataContext()
    for i, c in enumerate(candles):
        ctx.append(c)
        if i % 7 == 0 or i == len(candles) - 1:
            fed = candles[:i + 1]
            for tf in TFS:
                assert [_key(b) for b in ctx.bars(tf)] == \
                       [_key(b) for b in resample(list(fed), tf)], \
                       f"prefix={i} tf={tf}"


def test_closed_is_batch_minus_forming():
    candles = _make_candles(n=500)
    ctx = DataContext()
    for c in candles:
        ctx.append(c)
    for tf in TFS:
        batch = resample(list(candles), tf)
        assert [_key(b) for b in ctx.closed(tf)] == [_key(b) for b in batch[:-1]]
        assert _key(ctx.forming(tf)) == _key(batch[-1])


def test_closed_append_only_and_stable():
    candles = _make_candles(n=600)
    ctx = DataContext()
    prev_len = 0
    for c in candles[:500]:
        ctx.append(c)
        cur = ctx.closed(300)
        assert len(cur) >= prev_len
        prev_len = len(cur)
    snap = list(ctx.closed(300))
    for c in candles[500:]:
        ctx.append(c)
    # объекты closed стабильны (identity), список только растёт
    assert all(a is b for a, b in zip(snap, ctx.closed(300)))


def test_close_events_fire_on_boundaries():
    candles = _make_candles(n=500)
    ctx = DataContext()
    closes = 0
    for c in candles:
        before = len(ctx.closed(300))
        ev = ctx.append(c)
        after = len(ctx.closed(300))
        # событие 'closed' ровно тогда, когда closed вырос на этом шаге
        assert (ev[300] == "closed") == (after == before + 1)
        closes += ev[300] == "closed"
    assert closes == len(ctx.closed(300))


def test_forming_replaced_not_mutated():
    candles = _make_candles(n=20)
    ctx = DataContext()
    for c in candles[:6]:
        ctx.append(c)
    old = ctx.forming(3600)
    old_key = _key(old)
    ctx.append(candles[6])  # та же часовая корзина (минуты :50-:59)
    assert _key(old) == old_key  # старый объект не изменился
    assert ctx.forming(3600) is not old  # замена, не мутация
