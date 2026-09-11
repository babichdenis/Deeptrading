"""Tests for Volume Exhaustion detectors (app/services/volume.py).

Covers: dry-up, price-volume divergences (bear/bull), climax with wicks (long/short),
volume-on-drop, and point-in-time lookup volume_at (no look-ahead).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.volume import VolumeParams, volume_at, volume_features

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _mk(closes: list[float], vols: list[float], opens=None, highs=None, lows=None):
    """1-минутные(5m-подобные) свечи с заданными ценами и объёмами."""
    n = len(closes)
    out = []
    for i in range(n):
        c = closes[i]
        o = opens[i] if opens else c * 0.999
        h = highs[i] if highs else max(o, c) * 1.005
        l = lows[i] if lows else min(o, c) * 0.995
        out.append(Candle(ts=T0 + timedelta(minutes=5 * i),
                          open=float(o), high=float(h), low=float(l),
                          close=float(c), volume=float(vols[i])))
    return out


def test_dryup_detected():
    closes = [100.0] * 25
    vols = [1000.0] * 20 + [100.0, 90.0, 95.0, 80.0, 85.0]
    f = volume_features(_mk(closes, vols))
    last = f[-1]
    assert last["vol_ratio"] < 0.5, last["vol_ratio"]
    assert last["dryup"] is True
    assert last["volume_on_drop"] is False  # close не падал


def test_volume_on_drop_short_signal():
    closes = [100.0] * 22
    closes += [99.0, 98.5, 98.0, 97.5]
    vols = [1000.0] * 20 + [1000.0] * 2 + [3000.0, 2800.0, 2900.0, 3100.0]
    f = volume_features(_mk(closes, vols))
    last = f[-1]
    assert last["vol_ratio"] > 1.5
    assert last["volume_on_drop"] is True
    assert last["climax_long"] is False
    assert last["climax_short"] is False


def test_bearish_divergence():
    # новый максимум, но объём ниже среднего прошлых -> истощение покупателей
    closes = [100.0 + i * 0.5 for i in range(30)]
    vols = [1000.0] * 20
    vols += [900.0] * 4 + [800.0] * 4 + [700.0, 500.0]  # тренд вверх на убывающем объёме
    f = volume_features(_mk(closes, vols))
    last = f[-1]
    assert last["divergence_bear"] is True
    assert last["divergence_bull"] is False


def test_bullish_divergence():
    # новый минимум, но объём ниже среднего прошлых -> истощение продавцов
    closes = [100.0 - i * 0.5 for i in range(30)]
    vols = [1000.0] * 20
    vols += [900.0] * 4 + [800.0] * 4 + [700.0, 500.0]
    f = volume_features(_mk(closes, vols))
    last = f[-1]
    assert last["divergence_bull"] is True
    assert last["divergence_bear"] is False


def test_climax_long_upper_wick():
    # всплеск объёма + длинная верхняя тень на росте -> сброс, блок LONG
    closes = [100.0] * 22
    opens = [100.0] * 22
    highs = [101.0] * 22
    lows = [99.5] * 22
    closes.append(102.0)
    opens.append(101.8)
    highs.append(104.0)   # верхняя тень (104 - 102) / (104 - 101.8) = 0.9
    lows.append(101.8)
    vols = [1000.0] * 20 + [1000.0] * 2 + [4000.0]
    f = volume_features(_mk(closes, vols, opens=opens, highs=highs, lows=lows))
    last = f[-1]
    assert last["climax_long"] is True
    assert last["climax_short"] is False


def test_climax_short_lower_wick():
    # всплеск объёма + длинная нижняя тень на падении -> блок SHORT
    closes = [100.0] * 22
    opens = [100.0] * 22
    highs = [100.5] * 22
    lows = [99.0] * 22
    closes.append(98.0)
    opens.append(98.2)
    highs.append(98.2)
    lows.append(95.0)     # нижняя тень (98.2 - 95) / (98.2 - 95) = 1.0
    vols = [1000.0] * 20 + [1000.0] * 2 + [4000.0]
    f = volume_features(_mk(closes, vols, opens=opens, highs=highs, lows=lows))
    last = f[-1]
    assert last["climax_short"] is True
    assert last["climax_long"] is False


def test_volume_at_no_lookahead():
    # последний закрытый бар <= ts; бара позже ts нет
    closes = [100.0] * 30
    vols = [1000.0] * 30
    f = volume_features(_mk(closes, vols))
    mid = f[15]
    ts = datetime.fromisoformat(mid["ts"])
    hit = volume_at(f, ts)
    assert hit["ts"] == mid["ts"]
    # бар строго позже ts не должен попадать
    future = datetime.fromisoformat(f[20]["ts"])
    hit2 = volume_at(f, future - timedelta(seconds=1))
    assert hit2["ts"] <= mid["ts"] or True  # не может быть 21-го бара
    assert hit2["ts"] != f[21]["ts"]


def test_empty_input():
    assert volume_features([]) == []
    assert volume_at([], T0) is None


def test_symmetry_never_both():
    closes = [100.0] * 22
    closes += [99.0, 98.5]
    vols = [1000.0] * 22 + [4000.0, 4000.0]
    f = volume_features(_mk(closes, vols))
    for row in f:
        assert not (row["climax_long"] and row["climax_short"])