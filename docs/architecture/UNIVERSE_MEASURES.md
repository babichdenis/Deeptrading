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

---

# ДОПОЛНЕНИЕ: рефайн Phase 9 (финальная модель, зафиксировано 30.09)

Владелец утвердил уточнённую модель — **реализуем её поверх Phase 1–8, не ломая их**. Ключевое:

## Финальная цепочка

```
Universe (identity / availability / eligibility / membership)
    → Eligible Instruments → Market Data
        → Volatility Measure + Trend Measure (ИЗМЕРЕНИЯ, не фильтры Universe)
            → MarketFeatures (контейнер наблюдений, НИЧЕГО не решает)
                → Strategy Screener (по семейству стратегии) → Selection(strategy score)
                    → Allocation (+ Sector constraints) → TargetPortfolio
                        → RebalancePlan → RebalancePolicy → OrderIntent → Execution
Sector/Group membership — отдельное измерение: используется и в Selection-контексте, и в Allocation/Risk.
```

## Ключевые решения (дельты к раннему промпту)

1. **Universe = только**: identity, availability, trade eligibility, membership. Без ATR-ranking,
   без trend-классификации, без Top-N, без strategy applicability.
   `select_volatile_universe()` → deprecated-compat; `select_eligible_universe()` остаётся
   совместимостью (Universe → Eligibility).
2. **Измерения отдельно**: `volatility.py` (VolatilityFeatures: atr, atr_pct, realized_volatility,
   range_pct, bars_used, valid) и `trend.py` (TrendFeatures: direction UP/DOWN/FLAT, strength,
   slope, **normalized_slope = slope/ATR**, ER, bars_used, valid). Единый ATR — только
   `indicatorhub._atr` (второй не создавать).
3. **`strength` первой версии** — допускается `abs(normalized_slope)` с нормализацией, но у нас
   уже есть канон ER (0..1, масштабно-инвариантный) — рекомендуем strength=ER, а slope/ATR — как
   отдельное поле и для direction. Пороги — у стратегии, НЕ «магические» в измерении.
4. **Screener двухуровневый**: `EligibilityScreener` (есть данные / достаточно истории / валидные
   цены / tradeable / не blacklist; причины NO_DATA…BLACKLISTED живут здесь) vs `StrategyScreener`
   (получает Universe-кандидата + MarketFeatures + параметры стратегии → accepted/score).
5. **Selection — strategy-owned**: `Selection(score)`, где score даёт стратегия (trend.strength,
   atr_pct, sector score, MR-score…). `TOP_N_ATR` — только compat-адаптер.
6. **Sector — данные, не вечное свойство**: `SectorMembership(instrument, sector, valid_from/to?)`,
   API: `get_sector / group_by_sector / get_sector_members`. Отсутствие сектора не удаляет
   инструмент из Universe. Сектор не фильтр Universe; используется Strategy Selection и
   Allocation/Risk. `SectorFeatures` (агрегация) — будущий слой, сейчас не строить.
7. **StrategyFamily** (metadata): TREND_FOLLOWING, MEAN_REVERSION, PATTERN, EVENT, SECTOR_ROTATION,
   CALENDAR, PORTFOLIO, MICROSTRUCTURE. Роботов под registry пока не переписывать.
8. **Не строить универсальный MarketRegime** — достаточно Volatility+Trend измерений.
9. **as_of — везде**: Universe/Features/Screening/Selection только по `ts <= as_of`; никакого
   future-бара; sector ranking — тоже as_of.
10. **Runtime/Execution/Broker/Portfolio — не трогать** на этом этапе; миграция runtime отдельно,
    после parity. Legacy удаляется только финальным этапом.
11. **Структура пакета** (сохранить рабочую): `eligibility.py`, `sectors.py`, `volatility.py`,
    `trend.py` (+ `features/` или единый `features.py` с логическим разделением), существующие
    selection/allocation/rebalance/policy/compat без смены семантики.
12. **Обязательные тесты**: Universe не фильтрует по trend/volatility; один Universe → разные
    StrategyScreener дают разные selection (SBER/GMKN для тренда, LKOH для возврата) — Universe
    snapshot не меняется; no look-ahead/детерминизм для обоих измерений; ATR-паритет с каноном.

## Связка с нашей работой по роботам (важно)

Следствие новой модели для исследований: **матрица «робот × режим/измерение»** — прогоняем
каждого робота по истории с as_of-метками измерений (Trend direction/strength, Volatility
stage/ATR%, Sector) и строим таблицу, где каждый робот себя чувствует лучше всего. Это
автоматизируется на базе Exit Lab + `trades_split` (сессии/ADX-режимы уже есть) и будущих
`trend.py`/`volatility.py` (как только появятся) — без ожидания полного Universe-рефактора.
