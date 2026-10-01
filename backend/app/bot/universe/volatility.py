"""VolatilityMeasure: насколько инструмент волатилен (ИЗМЕРЕНИЕ, не решение).

Часть параллельного слоя Universe 2.0: legacy-слой (features.py/selection.py)
НЕ менялся. Этот модуль отвечает на ровно один вопрос — «какая волатильность
у инструмента на as_of» — и не ранжирует, не фильтрует и не создаёт ордеров.

ATR — КАНОНИЧЕСКИЙ (app.engine.indicatorhub._atr), второго atr() в проекте нет.
Паритет с legacy-слоем гарантирован тестами (см. test_atr_canon_parity.py и
parity-блок в tests/test_universe_v2_volatility.py).

Принципы:
    * только бары с ts <= as_of (граница P1.3 зафиксирована в
      test_universe_v2_as_of_boundary.py; строгая граница ts + TF <= as_of
      готова в bars.bar_is_visible, но смена семантики — решение владельца);
    * детерминированность: одинаковые входы -> одинаковый результат;
    * недостаток/невалидность данных -> valid=False с понятной причиной;
    * внутри measure нет обращения к DB/Broker/HTTP и скрытых настроек —
      все параметры явные аргументы.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Sequence

from app.bot.universe.domain import InstrumentRef, VolatilityFeatures
from app.bot.universe.features import ATR_PERIOD, FEATURE_WINDOW
from app.engine.indicatorhub import _atr as _atr_canonical


def _last_non_null(values: Sequence) -> float | None:
    return next((v for v in reversed(values) if v is not None), None)


def _realized_volatility(bars: Sequence, window: int) -> float | None:
    """Std декартовых лог-доходностей visible-баров. Плоский ряд -> 0.0."""
    closes = [float(b.close) for b in bars[-window:]]
    if len(closes) < 2:
        return None
    logs: list[float] = []
    for prev, cur in zip(closes, closes[1:]):
        if prev <= 0 or cur <= 0:
            return None
        logs.append(math.log(cur / prev))
    if not logs:
        return None
    mean = sum(logs) / len(logs)
    var = sum((x - mean) ** 2 for x in logs) / len(logs)
    return math.sqrt(var)


def _range_pct(bars: Sequence, window: int, close: float) -> float | None:
    """(max_high - min_low)/close*100 по окну. Не требует трендовых допущений."""
    seg = bars[-window:]
    if not seg or close <= 0:
        return None
    hi = max(float(b.high) for b in seg)
    lo = min(float(b.low) for b in seg)
    return round((hi - lo) / close * 100, 3)


def compute_volatility_features(
    instrument: InstrumentRef,
    bars: Sequence,
    *,
    as_of: datetime,
    window: int = FEATURE_WINDOW,
    period: int = ATR_PERIOD,
) -> VolatilityFeatures:
    """VolatilityFeatures на as_of. Чистая детерминированная функция.

    visible = бары с ts <= as_of; ATR считается по последним window из них
    каноническим атрибутом indicatorhub. Единственное место принятия решения
    о valid: хватило истории и цены не нулевые/отрицательные.
    """
    visible = [b for b in bars if b.ts <= as_of]
    if not visible:
        return VolatilityFeatures(instrument, as_of, valid=False, reason="no_data")

    last_atr = _last_non_null(_atr_canonical(visible[-window:], period))
    close = float(visible[-1].close)
    if last_atr is None:
        base = VolatilityFeatures(
            instrument,
            as_of,
            bars_used=len(visible),
            valid=False,
            reason="insufficient_bars",
        )
        return base
    if close <= 0:
        return VolatilityFeatures(
            instrument,
            as_of,
            atr=float(last_atr),
            bars_used=len(visible),
            valid=False,
            reason="invalid_close",
        )

    return VolatilityFeatures(
        instrument,
        as_of,
        atr=float(last_atr),
        atr_pct=round(last_atr / close * 100, 3),
        realized_volatility=_realized_volatility(visible, window),
        range_pct=_range_pct(visible, window, close),
        bars_used=len(visible),
        valid=True,
    )


def compute_volatility_features_many(
    instruments: Sequence[InstrumentRef],
    bars_by_instrument: dict,
    *,
    as_of: datetime,
    window: int = FEATURE_WINDOW,
    period: int = ATR_PERIOD,
    key=lambda ref: ref.figi,
) -> tuple[VolatilityFeatures, ...]:
    """VolatilityFeatures для набора инструментов одним проходом.

    Инструмент без ключа в bars_by_instrument получает valid=False (no_data),
    а не исключается: полноту отбора решает StrategyScreener/Selection.
    """
    return tuple(
        compute_volatility_features(
            ref,
            bars_by_instrument.get(key(ref), ()),
            as_of=as_of,
            window=window,
            period=period,
        )
        for ref in instruments
    )