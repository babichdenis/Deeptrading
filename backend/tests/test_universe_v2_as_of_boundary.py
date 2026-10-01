"""AUDIT P1.3: граница as_of — контракт зафиксирован тестом, решение по смене — за владельцем.

Факт из аудита: после перехода на START-метку старший бар с ts=T описывает
[T, T+TF). Условие `ts <= as_of` включает бар, который на as_of ещё не закрыт.

Здесь зафиксировано ТЕКУЩЕЕ поведение слоя (ts <= as_of, граница включительная)
и явно помечен риск. Менять семантику мер на `ts + TF <= as_of` — решение с
последствиями для бэктестов и ранжирования Universe, поэтому оно не сделано
молча: смена ломает 7 существующих тестов, фиксирующих старое поведение.

Хелперы bar_is_visible/bar_close_ts в bars.py — готовая реализация «строгой»
границы: их достаточно применить в _visible, когда владелец утвердит смену.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.bars import bar_close_ts, bar_is_visible
from app.bot.universe.domain import InstrumentRef
from app.bot.universe.features import compute_feature_set, compute_market_features
from app.bot.universe.trend import compute_trend_features
from app.bot.universe.volatility import compute_volatility_features

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)

SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")


def _bars(n=60):
    from app.engine.models import Candle as EngineCandle

    out = []
    for i in range(n):
        base = 100 + 0.7 * i
        out.append(
            EngineCandle(
                ts=T0 + STEP * i, open=base, high=base + 0.5, low=base - 0.5,
                close=base + 0.1, volume=1000.0,
            )
        )
    return out


# ── Метание START подтверждено ───────────────────────────────────────────


def test_bar_label_is_bucket_start_not_close():
    """Метка = начало бакета (канон проекта, как в T-Invest и в resampler).

    Бар [T0, T0+5) закрывается и эмитится с меткой T0 — то есть метка указывает
    на НАЧАЛО интервала, а не на момент закрытия.
    """
    from dataclasses import dataclass

    from app.marketdata.resampler import Resampler

    @dataclass
    class _B:
        figi: str
        ts: datetime
        open: float = 1.0
        high: float = 1.0
        low: float = 1.0
        close: float = 1.0
        volume: float = 1.0

    r = Resampler("5min")
    for i in range(5):
        assert r.feed(_B("F", T0 + timedelta(minutes=i))) is None
    emitted = r.feed(_B("F", T0 + timedelta(minutes=5)))

    assert emitted is not None
    assert emitted.ts == T0
    assert bar_close_ts(emitted, STEP) == T0 + STEP


# ── Хелперы строгой границы готовы и корректны ──────────────────────────


def test_strict_boundary_helpers_behave_as_documented():
    bar = _bars(1)[0]
    assert bar_is_visible(bar, as_of=bar.ts, interval=STEP) is False
    assert bar_is_visible(bar, as_of=bar.ts + STEP, interval=STEP) is True
    assert bar_is_visible(
        bar, as_of=bar.ts + STEP - timedelta(microseconds=1), interval=STEP
    ) is False
    assert bar_is_visible(bar, as_of=bar.ts + STEP + timedelta(hours=1), interval=STEP) is True


def test_strict_boundary_would_exclude_exactly_one_bar():
    """Сколько баров теряет строгая граница: ровно один — тот, что на as_of."""
    bars = _bars(60)
    as_of = bars[-1].ts
    loose = [b for b in bars if b.ts <= as_of]
    strict = [b for b in bars if bar_is_visible(b, as_of=as_of, interval=STEP)]
    assert len(loose) - len(strict) == 1


# ── ТЕКУЩИЙ контракт слоя: ts <= as_of ──────────────────────────────────


def test_current_contract_includes_bar_labelled_as_of():
    """Зафиксировано: слой СЕЙЧАС включает бар с ts == as_of (граница <=)."""
    bars = _bars(60)
    fs = compute_feature_set(SBER, bars, as_of=bars[-1].ts)
    assert fs.valid
    assert fs.bars_used == 60
    assert fs.close == pytest.approx(bars[-1].close)


def test_current_contract_excludes_bar_after_as_of():
    bars = _bars(60)
    fs = compute_feature_set(SBER, bars, as_of=bars[-1].ts - timedelta(seconds=1))
    assert fs.valid
    assert fs.bars_used == 59
    assert fs.close == pytest.approx(bars[-2].close)


def test_all_measures_share_the_same_boundary():
    """Volatility/trend/MarketFeatures не разъезжаются по границе."""
    bars = _bars(60)
    as_of = bars[-1].ts

    vol = compute_volatility_features(SBER, bars, as_of=as_of)
    trend = compute_trend_features(SBER, bars, as_of=as_of, window=44)
    mkt = compute_market_features(SBER, bars, as_of=as_of)

    assert vol.bars_used == trend.bars_used == 60
    assert mkt.volatility.bars_used == mkt.trend.bars_used == 60
    assert vol.valid and trend.valid and mkt.valid


def test_future_bar_never_leaks_into_features():
    """Регресс-тест, который обязан остаться зелёным при ЛЮБОЙ границе.

    Бар строго после as_of не влияет на признаки — это уже работает.
    """
    from app.engine.models import Candle as EngineCandle

    bars = _bars(60)
    as_of = bars[-1].ts
    baseline = compute_market_features(SBER, bars, as_of=as_of)

    poison = bars + [
        EngineCandle(
            ts=as_of + STEP, open=1.0, high=9999.0, low=0.01, close=8888.0, volume=10**9
        )
    ]
    got = compute_market_features(SBER, poison, as_of=as_of)

    assert got.volatility.atr == pytest.approx(baseline.volatility.atr, rel=1e-12)
    assert got.volatility.atr_pct == baseline.volatility.atr_pct
    assert got.trend.direction == baseline.trend.direction
    assert got.trend.strength == pytest.approx(baseline.trend.strength, rel=1e-12)
    assert got.volatility.bars_used == baseline.volatility.bars_used


def test_no_bars_visible_is_invalid():
    fs = compute_feature_set(SBER, _bars(1), as_of=T0 - timedelta(minutes=1))
    assert fs.valid is False
    assert fs.bars_used == 0
    assert fs.atr is None
