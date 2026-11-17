"""Паритет batch и stream на границах M5/M30/H1 (ARCH-06, задача B).

Проверяется контракт, на котором держится исполнение:

* старший ТФ, собранный batch-ресемплом движка, совпадает с рядом живого хаба
  по OHLCV и по набору закрытых меток;
* префикс ленты не переписывает уже закрытые бакеты (нет future-mutation);
* пропуск в ленте не создаёт пустой бакет;
* индикатор из хаба на каждом шаге равен полному расчёту по тем же барам;
* незакрытый бар не виден решению, а прогрев означает not-ready → ready.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.domain import InstrumentRef
from app.bot.universe.features import compute_feature_set
from app.engine.candlehub import CandleHub, build_tf
from app.engine.indicatorhub import INDICATORS, IndicatorHub
from app.engine.models import Candle
from app.services.ensemble import resample

FIGI = "BBGTFPARITY"
REF = InstrumentRef(ticker="PARI", figi=FIGI)
T0 = datetime(2026, 9, 28, 7, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)
TFS = (300, 1800, 3600)
FROM = 40
TO = 140


def _tape(*, gap: bool = True, n: int = 96) -> list[Candle]:
    """Детерминированная 5m-лента; при gap=True выпадает окно 10:10..10:55."""
    bars: list[Candle] = []
    for i in range(n):
        ts = T0 + STEP * i
        if gap and 26 <= i < 36:
            continue
        px = 100.0 + 3.0 * math.sin(i / 6.0)
        bars.append(Candle(ts=ts, open=px, high=px + 0.4, low=px - 0.4,
                           close=px + 0.1 * math.cos(i / 3.0), volume=10.0 + i % 5))
    return bars


def _same(a: Candle, b: Candle) -> bool:
    return (
        a.ts == b.ts
        and a.open == pytest.approx(b.open)
        and a.high == pytest.approx(b.high)
        and a.low == pytest.approx(b.low)
        and a.close == pytest.approx(b.close)
        and a.volume == pytest.approx(b.volume)
    )


@pytest.mark.parametrize("tf", TFS)
def test_batch_resample_matches_build_tf(tf: int) -> None:
    tape = _tape()
    batch = resample(tape, tf)
    stream = build_tf(tape, tf, include_partial=True)
    assert len(batch) == len(stream)
    for left, right in zip(batch, stream):
        assert _same(left, right), f"tf={tf}: {left.ts} расходится"
    closed_only = build_tf(tape, tf)
    assert closed_only == stream[:-1], "закрытые бакеты строятся иначе, чем полный ряд"


@pytest.mark.parametrize("tf", TFS)
def test_live_series_closed_matches_batch(tf: int) -> None:
    tape = _tape()
    batch = resample(tape, tf)
    hub = CandleHub()
    series = hub.series(FIGI, tf)
    for bar in tape:
        hub.ingest_1m(FIGI, bar)
    closed = series.snapshot()
    assert closed, "ряд хаба пуст"
    for got, expected in zip(closed, batch):
        assert _same(got, expected), f"tf={tf}: закрытый {got.ts} расходится"


@pytest.mark.parametrize("tf", TFS)
def test_prefix_does_not_rewrite_closed_buckets(tf: int) -> None:
    tape = _tape()
    full = resample(tape, tf)
    for i in range(FROM, TO):
        part = resample(tape[:i], tf)
        closed_part = part[:-1] if part else []
        for got in closed_part:
            expected = next((b for b in full if b.ts == got.ts), None)
            assert expected is not None, f"tf={tf} n={i}: лишний бакет {got.ts}"
            assert _same(got, expected), f"tf={tf} n={i}: бакет {got.ts} изменился"


@pytest.mark.parametrize("tf", TFS)
def test_gap_creates_no_empty_bucket(tf: int) -> None:
    tape = _tape(gap=True)
    buckets = resample(tape, tf)
    assert buckets
    assert all(b.volume > 0 for b in buckets), "появился бакет без объёма"
    stamps = [b.ts for b in buckets]
    assert stamps == sorted(stamps), "бакеты не по возрастанию метки"
    assert len(set(stamps)) == len(stamps), "бакеты повторяются"


@pytest.mark.parametrize("tf", TFS)
def test_hub_indicator_matches_batch_on_every_step(tf: int) -> None:
    tape = _tape()
    hub = IndicatorHub()
    hub.subscribe(FIGI, tf, "atr", {"length": 14})
    definition = INDICATORS["atr"]
    params = definition.resolve({"length": 14})
    checked = 0
    for i in range(1, len(tape) + 1):
        prefix = tape[:i]
        hub.commit(FIGI, tf, prefix)
        streamed = hub.get_at(FIGI, tf, "atr")
        batch = definition.calculate(prefix, params)["value"][-1]
        if batch is None:
            assert streamed is None, f"tf={tf} n={i}: хаб дал значение без истории"
            continue
        assert streamed == pytest.approx(batch, rel=1e-12, abs=1e-12), (
            f"tf={tf} n={i}: {streamed} != {batch}")
        checked += 1
    assert checked > 10, "слишком мало сверенных точек"


def test_open_bar_is_invisible_to_decision() -> None:
    bar = _tape(gap=False, n=1)[0]
    before = compute_feature_set(REF, [bar], as_of=bar.ts)
    assert before.bars_used == 0
    assert before.valid is False
    at_close = compute_feature_set(REF, [bar], as_of=bar.ts + STEP)
    assert at_close.bars_used == 1


def test_warmup_switches_validity() -> None:
    tape = _tape(gap=False, n=60)
    short = compute_feature_set(REF, tape[:10], as_of=tape[9].ts + STEP)
    assert short.valid is False
    full = compute_feature_set(REF, tape, as_of=tape[-1].ts + STEP)
    assert full.valid is True
    assert full.bars_used == len(tape)
    assert full.atr is not None and full.atr_pct is not None


def test_future_bars_do_not_change_past_features() -> None:
    tape = _tape(gap=False, n=70)
    as_of = tape[59].ts + STEP
    base = compute_feature_set(REF, tape[:60], as_of=as_of)
    with_future = compute_feature_set(REF, tape, as_of=as_of)
    assert base.bars_used == with_future.bars_used == 60
    assert base.atr == pytest.approx(with_future.atr)
    assert base.atr_pct == pytest.approx(with_future.atr_pct)
    assert base.close == pytest.approx(with_future.close)
