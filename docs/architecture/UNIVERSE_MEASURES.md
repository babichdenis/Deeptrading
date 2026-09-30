# UNIVERSE_MEASURES — где живут измерители (компаньон к промпту Phase 9)

Вопрос владельца: «теоретически надо в signal hub [IndicatorHub] и так же в универсе, но как —
не понимаю реализацию». Ответ фиксируем здесь.

## Принцип: два уровня, а не один

```
IndicatorHub (app/engine/indicatorhub.py)        Universe (app/bot/universe/)
  ФОРМУЛЫ по одной серии бара:                    as_of-СНИМКИ измерений:
  - стриминговые/инкрементальные                  - собираются ИЗ hub-примитивов по истории
  - бит-в-бит, канон, тесты-эталоны               - кросс-секция (бумага vs индекс) тоже здесь
  - общие для стратегий, фич, labeling, гейтов    - не считают сами формулы заново
```

Правило: **если это «одна серия → ряд/значение» — в hub. Если это «срез рынка на as_of
(набор инструментов, композиция измерений, относительные метрики)» — в universe.**

## Маппинг

| Измеритель | Где | Статус |
|---|---|---|
| EfficiencyRatio (сила направленности, Кауфман) | **hub** | ✅ добавлен (`9fce673`), тесты |
| ADX (Wilder) | **hub** | ✅ есть (аудит ENG-010) |
| ATR / ATR% | **hub** (ATR) → `universe/features.py` уже строит FeatureSet через канон | ✅ (`features._measure`) |
| **VolatilityStagesAW** (спит/идёт/штормит: канал SMA над ATR% → стадии 0..3) | **hub** — добавить `volatility_stages` | план |
| **SuperTrend** (ATR-трейл + флип; направление) | **hub** — добавить `supertrend` (линия, ±1, бар флипа) | план |
| realized_volatility (std лог-доходностей), range_pct (ср. (H−L)/C) | **universe/features.py** — расширить FeatureSet (над канон-ATR/барами, без нового движка) | план |
| **TrendFeatures** (direction UP/DOWN/FLAT, slope, normalized_slope=slope/ATR, strength) | **universe/trend.py** (Phase 9 §12–15) | план |
| **vol(бумаги)/vol(индекса)** (бета по диапазону/ATR) | **universe/relative.py** — кросс-секция: IMOEX-ряд из БД + бумага; это НЕ hub (две серии) и НЕ VolatilityMeasure (не одна бумага) | план |
| `.Day`-баг группировки | не наш код; пометка для порта `PriceChannelScreenerOnIndexVolatility` | заметка |

## Контракты-эскизы

```python
# universe/trend.py
class TrendDirection(str, Enum): UP, DOWN, FLAT

@dataclass(frozen=True)
class TrendFeatures:
    instrument: InstrumentRef
    as_of: datetime
    direction: TrendDirection
    strength: float | None      # = EfficiencyRatio (0..1) — масштабно-инвариантна,
                                #   никаких оптимизированных порогов (Phase 9 §13)
    slope: float | None         # линейная регрессия close по окну
    normalized_slope: float | None  # slope / ATR (ATR канона, на бар)
    bars_used: int
    valid: bool

def compute_trend_features(instrument, bars, *, as_of, window, atr_period) -> TrendFeatures: ...
# FLAT: |normalized_slope| < domain-порога (явная константа) ИЛИ ER < константы — только
# фиксированные domain-пороги, не оптимизированные.
# direction_mode: "slope" (по умолчанию) | "supertrend" (направление из hub.supertrend).

# universe/features.py (расширение существующего FeatureSet)
#   + realized_volatility, range_pct, vol_stage (из hub.volatility_stages), bars_used

# universe/relative.py
def relative_volatility(instrument_bars, index_bars, *, as_of, window) -> RelativeVolatility:
    # vol_ratio_range = avg((maxHigh-minLow)/minLow%) бумаги / то же индекса (семантика OsEngine)
    # vol_ratio_atr   = ATR% бумаги / ATR% индекса (наша канон-база)
    # коридор (1.0..1.4) — НЕ в Universe: это решение StrategyScreener
```

Правила из Phase 9 §8–§16 соблюдаются: только `ts <= as_of`; детерминизм; canonical ATR
(`indicatorhub._atr` — единственный); measure не ходит в DB/Broker/HTTP (данные передаются
аргументом); invalid → `valid=False`, без NaN/исключений; пороги явные.

## Потребление (кто это ест)

- **TrendStrategyScreener**: `trend.strength (ER) >= порог` И `direction` совпадает со стороной.
- **MeanReversionScreener**: `direction == FLAT` или `trend.strength <= порог`.
- **Волатильностный скринер**: `vol_ratio_range` в коридоре 1.0–1.4 (как OsEngine), либо
  `atr_pct` порог — это уровень StrategyScreener, НЕ Universe (Phase 9 §10).
- **VolStages**: on/off гейт стратегии (как у них на индексе), стадия — поле MarketFeatures.
- **SuperTrend-направление**: альтернативный `direction_mode` для трендовых.
- **SuperTrend-выходы** (трейлинг по линии): в exit-политики — ПОСЛЕ Exit Lab v2
  (сначала докрутим выходы, потом подключаем).

## Порядок внедрения

1. hub: `volatility_stages` + `supertrend` (+ reference-тесты, как у ER).
2. universe: `trend.py`, расширение `features.py`, `relative.py` (+ тесты Phase 9 §25: no look-ahead,
   детерминизм, ATR-паритет, insufficient/invalid; E2E §26).
3. StrategyScreener-с: тренд (ER+направление), возврат (FLAT), волатильностный коридор.
4. Только после WF/E2E — использование в выборках; SuperTrend-выходы — отдельным заходом.

Legacy не трогаем; ранжирование/Top-N остаётся в Selection (Phase 9 §18–20), не в Universe.
