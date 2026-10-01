"""L2.2 parity-гейт индикаторов: incremental == batch.

- RsiState ↔ entry_gates.rsi_map, AtrState ↔ конвейер ensemble.py:1091-1097,
  EmaState ↔ indicators.ema.
- Ключевой тест: состояние двигается только по closed-барам DataContext,
  forming оценивается через snapshot — совпадение с batch на каждом префиксе.
- Плоский участок (30 одинаковых close) бьёт в ветку avg_loss<=0.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle
from app.services.data_context import DataContext
from app.services.entry_gates import rsi_map
from app.services.indicator_state import AtrState, EmaState, RsiState
from app.services.indicators import ema

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _make_bars(n=1200, seed=11):
    import random
    rng = random.Random(seed)
    out = []
    price = 100.0
    for i in range(n):
        ts = T0 + timedelta(minutes=i)
        if 500 <= i < 530:
            pass  # плоский участок: цена не меняется
        else:
            price = max(price + rng.gauss(0, 0.0012) * price, 1.0)
        o = price
        h = price * (1 + abs(rng.gauss(0, 0.0005)))
        l = price * (1 - abs(rng.gauss(0, 0.0005)))
        c = price
        out.append(Candle(ts=ts, open=o, high=h, low=l, close=c,
                          volume=int(rng.uniform(500, 2000))))
    return out


def _atr_ref(bars, period=14):
    """Эталон: дословно математика ensemble.py:1091-1097."""
    out = [None] * len(bars)
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        if len(trs) > period:
            trs.pop(0)
        if i >= period:
            out[i] = sum(trs) / period
    return out


def test_rsi_matches_batch_on_prefixes():
    bars = _make_bars()
    st = RsiState(14)
    for i, b in enumerate(bars):
        st.update(b)
        if i % 11 == 0 or i == len(bars) - 1:
            m = rsi_map(bars[:i + 1], 14)
            if not m:
                assert st.value is None
            else:
                ts = bars[i].ts
                assert ts in m, f"prefix={i}"
                assert st.value == m[ts][0], f"prefix={i}"
                assert st.prev_value == m[ts][1], f"prefix={i}"


def test_atr_matches_pipeline_on_prefixes():
    bars = _make_bars()
    st = AtrState(14)
    for i, b in enumerate(bars):
        st.update(b)
        if i % 11 == 0 or i == len(bars) - 1:
            ref = _atr_ref(bars[:i + 1])
            assert st.value == ref[i], f"prefix={i} {st.value} vs {ref[i]}"


def test_ema_matches_batch():
    bars = _make_bars()
    closes = [b.close for b in bars]
    st = EmaState(20)
    ref = ema(closes, 20)
    # Инкрементальный EMA и batch-EMA идут по разным формулам накопления
    # (рекуррентная vs взвешенная сумма), поэтому float совпадает не точно:
    # расхождение порядка 1e-14 — это арифметика порядка, не регресс.
    for i, c in enumerate(closes):
        assert st.update(c) == pytest.approx(ref[i], rel=1e-9, abs=1e-12), f"i={i}"


def test_closed_forming_composition_via_data_context():
    """Связка L2.1+L2.2: closed двигает состояние, forming — только snapshot."""
    m1 = _make_bars(n=1500)
    ctx = DataContext((300,))
    rsi, atr = RsiState(14), AtrState(14)
    checked = 0
    for c in m1:
        ev = ctx.append(c)
        if ev[300] == "closed":
            for b in ctx.closed(300)[-1:]:
                rsi.update(b)
                atr.update(b)
        bars5 = ctx.bars(300)
        m = rsi_map(bars5, 14)
        if m:
            last_ts = bars5[-1].ts
            assert last_ts in m
            # forming через snapshot == batch на полном префиксе
            assert rsi.evaluate(ctx.forming(300)) == m[last_ts][0]
            checked += 1
        ref5 = _atr_ref(bars5)
        assert atr.evaluate(ctx.forming(300)) == ref5[-1]
        # персистентное состояние forming не загрязняет: value == batch(closed)
        mc = rsi_map(ctx.closed(300), 14)
        if mc:
            assert rsi.value == mc[ctx.closed(300)[-1].ts][0]
    assert checked > 200


def test_snapshot_does_not_pollute():
    st = RsiState(14)
    bars = _make_bars(n=100)
    for b in bars:
        st.update(b)
    before = st.snapshot()
    st.evaluate(bars[-1])
    st.evaluate(bars[-1])
    assert st.snapshot() == before
