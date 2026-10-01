"""AUDIT P1.1 / P1.2: bounded bulk-загрузка баров и реальный lot_size.

Главное здесь не сам SQL, а инвариант, который его разрешает: меры Universe
читают только хвост окна (visible[-window:]) и последний close. Если этот
инвариант сломать, bounded-загрузка начнёт молча портить признаки — поэтому
эквивалентность «вся история == последние N баров» проверяется явно и падает
при любой правке volatility/trend/features.

Тесты чисто функциональные (без БД): group_tail_rows и фильтр окна — обычные
функции, SQL-корректность проверяется интеграционным тестом против живой БД.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.bars import group_tail_rows, load_bars_bulk_bounded
from app.bot.universe.domain import InstrumentRef
from app.bot.universe.features import (
    FEATURE_WINDOW,
    compute_feature_set,
    compute_market_features,
)
from app.bot.universe.trend import compute_trend_features
from app.bot.universe.volatility import compute_volatility_features

T0 = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)

SBER = InstrumentRef(ticker="SBER", figi="FIGI_SBER")
LKOH = InstrumentRef(ticker="LKOH", figi="FIGI_LKOH")


def _bars(closes, *, start=T0, step=STEP):
    """Свечи по возрастанию ts с осмысленным high/low (ATR должен считаться)."""
    from app.engine.models import Candle as EngineCandle

    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        hi = max(prev, c) + 0.5
        lo = min(prev, c) - 0.5
        out.append(
            EngineCandle(
                ts=start + step * i, open=prev, high=hi, low=lo, close=c, volume=1000.0
            )
        )
        prev = c
    return out


# ── group_tail_rows: чистая группировка ──────────────────────────────────


def test_group_tail_rows_groups_by_figi_and_sorts_by_ts():
    rows = [
        ("F1", T0 + STEP * 2, 2, 3, 1, 2.5, 10),
        ("F2", T0 + STEP * 1, 1, 2, 0.5, 1.5, 20),
        ("F1", T0 + STEP * 1, 1, 2, 0.5, 1.5, 10),
        ("F2", T0 + STEP * 2, 2, 3, 1, 2.5, 20),
    ]
    grouped = group_tail_rows(rows)

    assert sorted(grouped) == ["F1", "F2"]
    for bars in grouped.values():
        assert [b.ts for b in bars] == sorted(b.ts for b in bars)
    assert grouped["F1"][0].close == pytest.approx(1.5)
    assert grouped["F1"][1].close == pytest.approx(2.5)
    assert grouped["F2"][0].volume == pytest.approx(20)


def test_group_tail_rows_empty_input():
    assert group_tail_rows([]) == {}


def test_group_tail_rows_sorts_unsorted_input():
    rows = [
        ("F1", T0 + STEP * 3, 3, 4, 2, 3.5, 10),
        ("F1", T0 + STEP * 1, 1, 2, 0.5, 1.5, 10),
        ("F1", T0 + STEP * 2, 2, 3, 1, 2.5, 10),
    ]
    grouped = group_tail_rows(rows)
    assert [b.close for b in grouped["F1"]] == [1.5, 2.5, 3.5]


# ── Инвариант хвоста окна: bounded == вся история ─────────────────────────


@pytest.mark.parametrize("total", [FEATURE_WINDOW, FEATURE_WINDOW * 3, 200])
def test_bounded_tail_equals_full_history_for_all_measures(total):
    """Ядро P1.1: последние FEATURE_WINDOW баров дают тот же признак, что вся история.

    Если это перестанет выполняться, bounded-загрузка в runtime_select даст
    другие ранжирования, чем прежний load_all_bars — и это будет тихий регресс.
    """
    closes = [100 + 3.0 * ((i * 7) % 11) + 0.15 * i for i in range(total)]
    full = _bars(closes)
    as_of = full[-1].ts
    tail = full[-FEATURE_WINDOW:]

    assert len(tail) == FEATURE_WINDOW

    for fn in (
        lambda bars: compute_feature_set(SBER, bars, as_of=as_of),
        lambda bars: compute_volatility_features(SBER, bars, as_of=as_of),
        lambda bars: compute_trend_features(
            SBER, bars, as_of=as_of, window=FEATURE_WINDOW
        ),
        lambda bars: compute_market_features(SBER, bars, as_of=as_of),
    ):
        a, b = fn(full), fn(tail)
        assert a.valid == b.valid, fn
        if hasattr(a, "close"):
            assert a.close == pytest.approx(b.close), fn
        if hasattr(a, "atr_pct"):
            assert a.atr == pytest.approx(b.atr, rel=1e-12), fn
            assert a.atr_pct == b.atr_pct, fn
        if hasattr(a, "strength"):
            assert a.strength == pytest.approx(b.strength, rel=1e-12), fn
            assert a.direction == b.direction, fn


def test_bars_used_counts_visible_history_not_window():
    """bars_used — диагностика (сколько баров видно), НЕ окно расчёта.

    Поэтому 200 баров и хвост из 44 дают одинаковые atr/atr_pct, но разный
    bars_used. Если bars_used начнёт влиять на решения, этот теф reminder.
    """
    closes = [100 + 3.0 * ((i * 7) % 11) + 0.15 * i for i in range(200)]
    full = _bars(closes)
    as_of = full[-1].ts
    tail = full[-FEATURE_WINDOW:]

    a = compute_feature_set(SBER, full, as_of=as_of)
    b = compute_feature_set(SBER, tail, as_of=as_of)
    assert a.atr == pytest.approx(b.atr, rel=1e-12)
    assert a.atr_pct == b.atr_pct
    assert a.bars_used == 200
    assert b.bars_used == FEATURE_WINDOW


def test_market_features_volatility_and_trend_match_on_tail():
    full = _bars([100 + 3.0 * ((i * 7) % 11) + 0.15 * i for i in range(150)])
    as_of = full[-1].ts
    tail = full[-FEATURE_WINDOW:]

    a = compute_market_features(SBER, full, as_of=as_of)
    b = compute_market_features(SBER, tail, as_of=as_of)

    assert a.valid == b.valid
    assert a.volatility.valid == b.volatility.valid
    assert a.volatility.atr_pct == b.volatility.atr_pct
    assert a.volatility.atr == pytest.approx(b.volatility.atr, rel=1e-12)
    assert a.volatility.realized_volatility == pytest.approx(
        b.volatility.realized_volatility, rel=1e-12
    )
    assert a.volatility.range_pct == pytest.approx(b.volatility.range_pct, rel=1e-12)
    assert a.trend.direction == b.trend.direction
    assert a.trend.strength == pytest.approx(b.trend.strength, rel=1e-12)
    assert a.trend.slope == pytest.approx(b.trend.slope, rel=1e-12)


def test_trend_direction_identical_on_tail():
    up = _bars([100 + i for i in range(120)])
    as_of = up[-1].ts
    full_t = compute_trend_features(SBER, up, as_of=as_of, window=FEATURE_WINDOW)
    tail_t = compute_trend_features(
        SBER, up[-FEATURE_WINDOW:], as_of=as_of, window=FEATURE_WINDOW
    )
    assert full_t.valid and tail_t.valid
    assert full_t.direction == tail_t.direction
    assert full_t.strength == pytest.approx(tail_t.strength, rel=1e-12)


# ── as_of-дисциплина: хвост не может содержать бары после as_of ──────────


def test_tail_filter_drops_bars_after_as_of():
    """Bounded-загрузка отдаёт бары с ts <= as_of; лишние должны отсекаться."""
    closes = [100 + i for i in range(80)]
    full = _bars(closes)
    as_of = full[50].ts

    visible = [b for b in full if b.ts <= as_of]
    fs = compute_feature_set(SBER, visible, as_of=as_of)
    assert fs.valid
    assert fs.bars_used == 51
    assert fs.close == pytest.approx(full[50].close)


def test_as_of_boundary_bar_is_visible():
    """Бар ровно на as_of виден (ts <= as_of) — граница включительная."""
    closes = [100 + i for i in range(60)]
    full = _bars(closes)
    fs = compute_feature_set(SBER, full, as_of=full[-1].ts)
    assert fs.valid and fs.bars_used == 60

    fs_prev = compute_feature_set(SBER, full, as_of=full[-1].ts - timedelta(seconds=1))
    assert fs_prev.valid and fs_prev.bars_used == 59


def test_future_bar_cannot_change_past_features():
    """Добавление будущего бара не меняет признаки для as_of (look-ahead)."""
    closes = [100 + 2.0 * i for i in range(60)]
    full = _bars(closes)
    as_of = full[-1].ts
    baseline = compute_market_features(SBER, full, as_of=as_of)

    from app.engine.models import Candle as EngineCandle

    with_future = full + [
        EngineCandle(
            ts=full[-1].ts + STEP, open=100.0, high=500.0, low=1.0, close=499.0, volume=9e9
        )
    ]
    extended = compute_market_features(SBER, with_future, as_of=as_of)

    assert extended.volatility.atr == pytest.approx(baseline.volatility.atr, rel=1e-12)
    assert extended.trend.direction == baseline.trend.direction
    assert extended.volatility.bars_used == baseline.volatility.bars_used


# ── Контракт bounded-загрузки ────────────────────────────────────────────


def test_bulk_rejects_window_below_one():
    """window <= 0 — ошибка программиста, а не молчаливый пустой результат."""
    with pytest.raises(ValueError, match="window must be >= 1"):
        asyncio.run(load_bars_bulk_bounded(None, ["F1"], 5, window=0, as_of=T0))


def test_bulk_empty_figis_does_not_touch_db():
    """Пустой список FIGI не должен ходить в БД (db=None это доказывает)."""
    result = asyncio.run(load_bars_bulk_bounded(None, [], 5, window=FEATURE_WINDOW, as_of=T0))
    assert result == {}


def test_bulk_dedups_and_drops_blank_figis():
    """Повторы и пустые FIGI схлопываются: меньше работы для БД."""

    class _Recorder:
        def __init__(self):
            self.params = None

        async def execute(self, stmt, params=None):
            self.params = params

            class _R:
                def all(self):
                    return []

            return _R()

    rec = _Recorder()
    result = asyncio.run(
        load_bars_bulk_bounded(
            rec, ["F1", "F1", "  ", ""], 5, window=FEATURE_WINDOW, as_of=T0
        )
    )
    assert result == {}
    assert rec.params["figis"] == ["F1"]
    assert rec.params["window"] == FEATURE_WINDOW
    assert rec.params["as_of"] == T0


def test_tail_figis_reports_only_instruments_with_bars():
    from app.bot.universe.bars import tail_figis

    assert tail_figis(["F1", "F2"], {"F1": _bars([1.0, 2.0]), "F2": []}) == ["F1"]
    assert tail_figis([], {}) == []


def test_bulk_sql_has_as_of_bound_and_limit():
    """SQL-контракт: ts <= as_of и LIMIT присутствуют, порядок DESC внутри."""
    from app.bot.universe.bars import BULK_TAIL_SQL

    sql = " ".join(str(BULK_TAIL_SQL).split()).upper()
    assert "UNNEST(:FIGIS)" in sql
    assert "CANDLES.TS <= :AS_OF" in sql
    assert "LIMIT :WINDOW" in sql
    assert "ORDER BY CANDLES.TS DESC" in sql
    assert "INTERVAL = :INTERVAL" in sql


def test_bulk_sql_uses_named_array_param():
    """figi передаётся массивом: LATERAL на каждый элемент unnest, не на N строк."""
    from app.bot.universe.bars import BULK_TAIL_SQL

    sql = " ".join(str(BULK_TAIL_SQL).split()).upper()
    assert "CROSS JOIN LATERAL" in sql
    assert "ORDER BY F.FIGI, C.TS" in sql


@pytest.mark.integration
def test_bulk_matches_per_figi_on_live_db():
    """Главная гарантия P1.1 на живых данных: bulk == load_all_bars + хвост.

    Bounded-загрузка обязана давать ровно то же, что прежний load_all_bars с
    последующим отсечением мер. Сравниваем полные фичи, а не только длину.
    """
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.bot.universe.features import compute_feature_set

    url = "postgresql+asyncpg://deeptrading:deeptrading@192.168.1.7:5432/deeptrading"

    async def run():
        engine = create_async_engine(url, poolclass=None)
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(
                    text(
                        "SELECT max(ts) FROM candles WHERE interval=5"
                    )
                )).scalar()
                if row is None:
                    pytest.skip("нет 5m свечей в БД .7")
                as_of = row
                figis = [
                    r[0]
                    for r in (await conn.execute(
                        text(
                            "SELECT figi FROM candles WHERE interval=5 AND ts <= :a "
                            "GROUP BY figi HAVING count(*) >= 30 LIMIT 8"
                        ),
                        {"a": as_of},
                    )).all()
                ]
                if not figis:
                    pytest.skip("нет FIGI с достаточной 5m историей")

                bulk = await load_bars_bulk_bounded(
                    conn, figis, 5, window=FEATURE_WINDOW, as_of=as_of
                )
                for figi in figis:
                    # Эталон — сырой SQL, а не load_all_bars: тот ходит через
                    # ORM и на голом Connection отдаёт id вместо Candle.
                    ref_rows = (await conn.execute(
                        text(
                            "SELECT ts, open, high, low, close, volume FROM candles "
                            "WHERE figi = :f AND interval = 5 ORDER BY ts"
                        ),
                        {"f": figi},
                    )).all()
                    full = group_tail_rows(
                        [(figi,) + tuple(r) for r in ref_rows]
                    )[figi]
                    ref = InstrumentRef(ticker=figi[-6:], figi=figi)
                    a = compute_feature_set(ref, full, as_of=as_of)
                    b = compute_feature_set(ref, bulk.get(figi, []), as_of=as_of)
                    assert a.valid == b.valid, figi
                    if a.valid:
                        assert a.atr == pytest.approx(b.atr, rel=1e-9), figi
                        assert a.atr_pct == b.atr_pct, figi
                        assert a.close == pytest.approx(b.close), figi
                    assert len(bulk[figi]) <= FEATURE_WINDOW, figi
                    assert all(b.ts <= as_of for b in bulk[figi]), figi
        finally:
            await engine.dispose()

    asyncio.run(run())
