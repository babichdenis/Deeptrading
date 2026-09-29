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
"""

from __future__ import annotations

from app.engine.indicatorhub import _atr as _atr_canonical

ATR_PERIOD = 14
FEATURE_WINDOW = 44


def atr_pct(bars: list, window: int, period: int = ATR_PERIOD) -> tuple[float, bool]:
    """Возвращает (atr_pct, metrics_valid).

    metrics_valid=False, если ATR не удалось посчитать (мало баров) либо
    последний close нулевой — данные непригодны для оценки волатильности.

    Семантика намеренно повторяет legacy: при невалидных данных atr_pct = 0,
    а решение «пропустить инструмент или оставить с нулевым ATR%» принимает
    профиль Screener'а, а не эта функция.
    """
    if not bars:
        return 0.0, False

    values = _atr_canonical(bars[-window:], period)
    last_atr = next((v for v in reversed(values) if v is not None), None)
    close = bars[-1].close
    if not last_atr or not close:
        return 0.0, False
    return round(last_atr / close * 100, 3), True


def average_turnover(bars: list, window: int = 10) -> float:
    """Средний оборот (close × volume) за последние window баров.

    Делим на window, а не на фактическое число баров, — как в legacy: при
    нехватке баров значение занижено, но это стабильная метрика.
    """
    return sum(float(b.close) * float(b.volume) for b in bars[-window:]) / window
