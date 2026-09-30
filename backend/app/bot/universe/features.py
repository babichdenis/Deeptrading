"""Feature-слой Universe: производные признаки инструмента.

ATR и ATR% считаются здесь, а НЕ в Screener: Screener получает только
готовый вердикт валидности (features_valid) и решает, допускать ли
инструмент. Так сохраняется разделение из
docs/osengine/screener_universe.txt и остаётся место для полноценного
Feature Engine без правки Screener'а.

Окно в 44 бара — историческое: столько нужно для периода 14 с
Wilder-сглаживанием плюс запас после ресемпла 1m -> 5m.

Канон ATR — индикаторный хаб, как и для ADX/RSI (ENG-010): engine.
indicators.atr и indicatorhub._atr совпадают побитово, и равенство
держит tests/test_atr_canon_parity.py. Если одну из двух реализаций
поменяют, упадёт тест, а не тихо разъедется ранжирование Universe.

Phase 4: compute_feature_set — чистый расчёт без БД, без wall-clock и без
мутабельного состояния. Look-ahead закрыт конструктивно: бары с ts > as_of
отбрасываются ДО расчёта, поэтому добавление будущей свечи не может изменить
признаки для as_of. Единственный ATR считается в _measure, его используют и
новый контракт, и legacy-функция atr_pct — второй реализации нет.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, Mapping, Sequence

from app.bot.universe.domain import FeatureSet, InstrumentRef, MarketFeatures
from app.engine.indicatorhub import _atr as _atr_canonical

ATR_PERIOD = 14
FEATURE_WINDOW = 44


def _measure(bars: Sequence, window: int, period: int) -> tuple[float | None, float | None, bool]:
    """Единственное место, где считается ATR. Возвращает (atr, atr_pct, valid).

    Арифметика — ровно та, что была в legacy: round(atr / close * 100, 3).
    close берётся как есть, без приведения к float, чтобы значения совпали
    с прежним кодом побитово.
    """
    if not bars:
        return None, None, False
    values = _atr_canonical(bars[-window:], period)
    last_atr = next((v for v in reversed(values) if v is not None), None)
    close = bars[-1].close
    if not last_atr or not close:
        return None, None, False
    return float(last_atr), round(last_atr / close * 100, 3), True


def compute_feature_set(
    instrument: InstrumentRef,
    bars: Sequence,
    *,
    as_of: datetime,
    window: int = FEATURE_WINDOW,
    period: int = ATR_PERIOD,
) -> FeatureSet:
    """Признаки инструмента на момент as_of. Детерминированная чистая функция.

    Единственная точка отсечения — фильтр по ts: на as_of видны только бары с
    bars.timestamp <= as_of. Всё остальное зависит только от аргументов: ни БД,
    ни системных часов, ни глобального состояния.
    """
    visible = [b for b in bars if b.ts <= as_of]
    if not visible:
        return FeatureSet(instrument=instrument, as_of=as_of, valid=False)

    atr, pct, ok = _measure(visible, window, period)
    close = visible[-1].close
    return FeatureSet(
        instrument=instrument,
        as_of=as_of,
        atr=atr,
        atr_pct=pct,
        close=float(close) if close else 0.0,
        bars_used=len(visible),
        valid=ok,
    )


def compute_feature_sets(
    instruments: Iterable[InstrumentRef],
    bars_by_instrument: Mapping[str, Sequence],
    *,
    as_of: datetime,
    key: Callable[[InstrumentRef], str] = lambda ref: ref.figi,
    window: int = FEATURE_WINDOW,
    period: int = ATR_PERIOD,
) -> tuple[FeatureSet, ...]:
    """Считает FeatureSet для набора инструментов одним проходом.

    bars_by_instrument отображает ref -> бары (по возрастанию ts). Инструмент без
    ключа получает невалидный FeatureSet, а не исключается: полноту отбора
    решает Screener/Selection, этот слой молча ничего не теряет.
    """
    return tuple(
        compute_feature_set(
            ref, bars_by_instrument.get(key(ref), ()), as_of=as_of, window=window, period=period
        )
        for ref in instruments
    )


def atr_pct(bars: list, window: int, period: int = ATR_PERIOD) -> tuple[float, bool]:
    """Возвращает (atr_pct, metrics_valid).

    metrics_valid=False, если ATR не удалось посчитать (мало баров) либо
    последний close нулевой — данные непригодны для оценки волатильности.

    Семантика намеренно повторяет legacy: при невалидных данных atr_pct = 0,
    а решение «пропустить инструмент или оставить с нулевым ATR%» принимает
    профиль Screener'а, а не эта функция. Это thin-обёртка над _measure,
    оставленная для compatibility-пути; новый код зовёт compute_feature_set.
    """
    _, pct, ok = _measure(bars, window, period)
    return (0.0, False) if pct is None else (pct, ok)


def compute_market_features(
    instrument: InstrumentRef,
    bars: Sequence,
    *,
    as_of: datetime,
    window: int = FEATURE_WINDOW,
    period: int = ATR_PERIOD,
) -> MarketFeatures:
    """Композиция VolatilityMeasure + TrendMeasure на as_of.

    Параллельный слой Universe 2.0: MarketFeatures объединяет измерения, но
    НИЧЕГО не решает (не фильтрует, не ранжирует, не торгует). Sector сюда
    НЕ входит — это metadata из Universe/SectorMembership.
    """
    from .trend import compute_trend_features
    from .volatility import compute_volatility_features

    vol = compute_volatility_features(
        instrument, bars, as_of=as_of, window=window, period=period
    )
    trend = compute_trend_features(
        instrument, bars, as_of=as_of, window=window, atr_period=period
    )
    return MarketFeatures(
        instrument=instrument,
        as_of=as_of,
        volatility=vol,
        trend=trend,
    )


def compute_market_features_many(
    instruments: Iterable[InstrumentRef],
    bars_by_instrument: Mapping[str, Sequence],
    *,
    as_of: datetime,
    key: Callable[[InstrumentRef], str] = lambda ref: ref.figi,
    window: int = FEATURE_WINDOW,
    period: int = ATR_PERIOD,
) -> tuple[MarketFeatures, ...]:
    """MarketFeatures для набора инструментов. Инструмент без баров не исключается."""
    return tuple(
        compute_market_features(
            ref,
            bars_by_instrument.get(key(ref), ()),
            as_of=as_of,
            window=window,
            period=period,
        )
        for ref in instruments
    )


def average_turnover(bars: list, window: int = 10) -> float:
    """Средний оборот (close × volume) за последние window баров.

    Делим на window, а не на фактическое число баров, — как в legacy: при
    нехватке баров значение занижено, но это стабильная метрика.

    ВНИМАНИЕ: значение НЕ используется в ranking. Оборот из таблицы universe
    устаревает (runtime.py это признаёт), а канонический источник оборота
    (MOEX ISS) пока не определён, поэтому liquidity-aware ranking запрещён
    до решения по единому источнику.
    """
    return sum(float(b.close) * float(b.volume) for b in bars[-window:]) / window
