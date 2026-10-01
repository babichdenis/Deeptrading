"""Заморозка инвентаря агрегаторов ТФ (REF-001b).

Канон — START (доказано против T-Invest 102/102). Тест:
1) START-лагерь (Resampler, universe.bars, ml_ensemble_filter) обязан давать ИДЕНТИЧНЫЙ ряд;
2) END-лагерь (services.ensemble.resample, candlehub.build_tf) — xfail(strict) до конвергенции:
   когда схлопнется на START — тест станет XPASS и strict=True потребует обновить его.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.models import Candle as EC

T0 = datetime(2026, 9, 24, 3, 50, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 24, 21, 0, tzinfo=timezone.utc)


def _rows():
    """Синтетические 1m: пропуски минут, клиринг-гэп 18:45–19:05, разные значения."""
    out = []
    t = T0
    i = 0
    while t < T1:
        m = t.hour * 60 + t.minute
        if not (18 * 60 + 45 <= m < 19 * 60 + 5) and (m % 7) != 3:
            px = 100 + (i % 13) * 0.7
            out.append((t, round(px, 2), round(px + 0.5, 2), round(px - 0.5, 2),
                        round(px + 0.1, 2), 10.0 + i))
        t += timedelta(minutes=1)
        i += 1
    return out


def _engine_candles(rows):
    return [EC(ts=ts, open=o, high=h, low=low, close=c, volume=v)
            for ts, o, h, low, c, v in rows]


class _RB:
    """Бар с figi для канонического Resampler."""
    __slots__ = ("figi", "ts", "open", "high", "low", "close", "volume")

    def __init__(self, figi=None, ts=None, open=None, high=None, low=None, close=None,
                 volume=None, **_kw):
        self.figi = figi
        self.ts = ts
        self.open = open
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume


def _series(emitted):
    return [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in emitted]


def _start_camp(rows):
    from app.marketdata.resampler import Resampler
    rs = Resampler("5min")
    out = []
    for ts, o, h, low, c, v in rows:
        e = rs.feed(_RB(figi="X", ts=ts, open=o, high=h, low=low, close=c, volume=v))
        if e is not None:
            out.append((e.ts, e.open, e.high, e.low, e.close, e.volume))
    for e in (rs.flush() or []):
        out.append((e.ts, e.open, e.high, e.low, e.close, e.volume))
    return out


def test_start_camp_identical():
    rows = _rows()
    ecs = _engine_candles(rows)
    from app.bot.universe.bars import resample_1m_to_5m
    from app.services.ml_ensemble_filter import resample_to_5m
    canon = _start_camp(rows)
    uni = _series(resample_1m_to_5m(ecs, "X"))
    ml = _series(resample_to_5m(ecs))
    assert canon == uni == ml, "START-лагерь агрегаторов разошёлся (заморозка нарушена)"


def test_candlehub_build_tf_converged_to_start():
    """candlehub переведён на START (REF-001b): build_tf == канон."""
    rows = _rows()
    ecs = _engine_candles(rows)
    from app.engine.candlehub import build_tf
    assert _series(build_tf(ecs, 300, include_partial=True)) == _start_camp(rows)


@pytest.mark.xfail(reason="services.ensemble.resample ещё END (ceil) — к конвергенции на START",
                   strict=True)
def test_ensemble_resample_converged_to_start():
    rows = _rows()
    ecs = _engine_candles(rows)
    from app.services.ensemble import resample
    assert _series(resample(ecs, 300)) == _start_camp(rows)
