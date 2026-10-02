"""TrendMeasure: есть ли и насколько сильно направленное движение (ИЗМЕРЕНИЕ).

Часть параллельного слоя Universe 2.0: legacy-слой НЕ менялся. TrendMeasure
только измеряет направление/силу тренда на as_of и НИЧЕГО не выбирает:
direction=UP не означает BUY, и инструмент не попадает ни в какой
«Trend Universe» из-за измерения.

Первая реализация намеренно простая и объяснимая:
    slope             = наклон линейной регрессии по close за окно
    normalized_slope  = slope / ATR   (канонический ATR из indicatorhub)
    direction         = UP/DOWN/FLAT по знаку normalized_slope
    strength          = min(|normalized_slope|, 1.0) — детерминированная
                        нормализация, масштабно-независимая и ограниченная [0,1]

FLAT-классификация: явный порог FLAT_THRESHOLD в нормализованной шкале — единый
для UP/DOWN и равный 0.0 по умолчанию («знак нуля -> FLAT»). Произвольных
оптимизированных порогов нет: решение «какой strength считать трендом» оставлено
конкретной стратегии (TrendStrategyScreener), а не этому модулю.

Принципы:
    * только ЗАКРЫТЫЕ на as_of бары: ts + TF <= as_of, т.е. видимость по факту
      закрытия, а не по метке (метка = начало бакета). Решение владельца,
      контракт в bars.bar_is_visible и test_universe_v2_as_of_boundary.py;
    * детерминированность: одинаковые входы -> одинаковый результат;
    * недостаточная история (меньше 2 баров для наклона или нет ATR для
      нормировки) -> valid=False с причиной;
    * внутри measure нет обращения к DB/Broker/HTTP и скрытых настроек.
"""
from __future__ import annotations

from datetime import datetime
from typing import Sequence

from app.bot.universe.domain import InstrumentRef, TrendDirection, TrendFeatures
from app.bot.universe.features import ATR_PERIOD, _visible
from app.bot.universe.volatility import _last_non_null
from app.engine.indicatorhub import _atr as _atr_canonical

FLAT_THRESHOLD = 0.0


def _linear_regression_slope(prices: Sequence[float]) -> float | None:
    """Наклон OLS по индексам [0..n-1] -> ценам close.

    Меньше 2 точек или нулевая дисперсия x — наклона нет (возвращаем None for
    insufficient, 0.0 для плоского ряда).
    """
    n = len(prices)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(prices) / n
    num = 0.0
    den = 0.0
    for x, y in enumerate(prices):
        num += (x - mean_x) * (y - mean_y)
        den += (x - mean_x) * (x - mean_x)
    if den == 0.0:
        return 0.0
    return num / den


def compute_trend_features(
    instrument: InstrumentRef,
    bars: Sequence,
    *,
    as_of: datetime,
    window: int,
    atr_period: int = ATR_PERIOD,
) -> TrendFeatures:
    """TrendFeatures на as_of. Чистая детерминированная функция.

    Наклон регрессии считается по close последних window visible-баров;
    нормализация — канонический ATR по тому же окну. direction — по знаку
    normalized_slope с порогом FLAT_THRESHOLD.
    """
    visible = _visible(bars, as_of)
    if not visible:
        return TrendFeatures(instrument, as_of, valid=False, reason="no_data")

    prices = [float(b.close) for b in visible[-window:]]
    slope = _linear_regression_slope(prices)
    if slope is None:
        return TrendFeatures(
            instrument,
            as_of,
            bars_used=len(visible),
            valid=False,
            reason="insufficient_bars",
        )

    atr = _last_non_null(_atr_canonical(visible[-window:], atr_period))
    if atr is None or atr <= 0:
        return TrendFeatures(
            instrument,
            as_of,
            slope=slope,
            bars_used=len(visible),
            valid=False,
            reason="no_atr_for_normalization",
        )

    normalized = slope / float(atr)
    flat_eps = FLAT_THRESHOLD
    if abs(normalized) <= flat_eps:
        direction = TrendDirection.FLAT
    elif normalized > 0:
        direction = TrendDirection.UP
    else:
        direction = TrendDirection.DOWN

    return TrendFeatures(
        instrument,
        as_of,
        direction=direction,
        strength=min(abs(normalized), 1.0),
        slope=slope,
        normalized_slope=normalized,
        bars_used=len(visible),
        valid=True,
    )


def compute_trend_features_many(
    instruments: Sequence[InstrumentRef],
    bars_by_instrument: dict,
    *,
    as_of: datetime,
    window: int,
    atr_period: int = ATR_PERIOD,
    key=lambda ref: ref.figi,
) -> tuple[TrendFeatures, ...]:
    """TrendFeatures для набора инструментов одним проходом.

    Инструмент без ключа получает valid=False (no_data), а не исключается.
    """
    return tuple(
        compute_trend_features(
            ref,
            bars_by_instrument.get(key(ref), ()),
            as_of=as_of,
            window=window,
            atr_period=atr_period,
        )
        for ref in instruments
    )