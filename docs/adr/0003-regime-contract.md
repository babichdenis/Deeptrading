# ADR 0003 — Regime Contract: измерения, оси, forward-валидация

- **Статус:** принято (решения владельца 2026-10-02, все открытые вопросы закрыты); Stage C запущен отдельным пакетом
- **Факты:** Stage A — `docs/results/REGIME_CALIBRATION_2026-10-02.md` (+ `backend/reports/regime_calibration/regime_calibration_report.md`); Stage B — `docs/results/REGIME_CALIBRATION_FORWARD_2026-10-02.md` (+ forward report)
- **Опирается на:** ADR-0002 (видимость бара строго по закрытию: `bar.ts + TF <= as_of`)
- **Субъект кода:** `app/services/regime.py` (`RegimeDetector`), `app/services/regime_state.py` (`RegimeState`), диагностика `app/services/regime_calibration.py`
- **Production-код этим ADR не меняется.** Пороги `adx_threshold=19`, `drift_pct=0.5%`, `cons_pct=0.71`, `atr_percentile_threshold=78` заморожены решением владельца 2026-10-02 и здесь не калибруются.

## Контекст

**Stage A** (T, SBER, LKOH, ROSN, NVTK, SMLT; 93 дня; 5m и 1h; read-only боевая БД):

- NEUTRAL/mixed 2.6% (5m) / 10.8% (1h); RANGE 70.0% / 53.7%; HIGH_VOLATILITY 24.8% / 22.3%; TREND_UP+TREND_DOWN ≈ 2.7% / 13.2%. Без выходных: RANGE 65.0% / 42.7%, HV 27.9% / 29.3%.
- ADX текущей реализации (не Wilder, без сглаживания) имеет p50 ≈ 71 → порог 19 пройден почти всегда; ветка `flat_ema_low_adx` (RANGE) почти мертва.
- NEUTRAL/mixed блокируется в основном consistency-почти-попаданием `[0.66, 0.71)`: up 96.7%, down 89.2% (5m); и конфликтом slope/drift (399+317 на 5m, 407 на 1h).
- Один и тот же детектор применяется на разных ТФ: live передаёт только `tf=hour`, `compute_ensemble` по умолчанию 5min; `drift_bars=6` даёт окно 30 мин на 5m и 6 ч на 1h.
- ~30% наблюдений — выходные (круглосуточные свечи, объём ~10× ниже дневного); runtime в выходные не торгует.
- Singleton runs 40.1% (5m) / 47.6% (1h), median run = 2 бара — flicker без hysteresis.

**Stage B** (forward h=12/24/48, строго бары t+1..t+h):

- **Текущие TREND_UP/DOWN не валидны как forward-directional метки:** p50 ret/ATR (h=24) после TREND_UP −0.43 (5m) / −0.18 (1h), после TREND_DOWN +0.26 / +0.29; P(+1 ATR) после TREND_UP 29.0% < 38.5% после TREND_DOWN (5m). В дневной сессии будней эффект сохраняется (−0.50 vs +0.06 на 5m). Это утверждение про **метки**, не доказательство «детектор предсказывает разворот»: нужен отдельный тест persistence/exhaustion/mean-reversion.
- По forward ER состояния не разделяются: p50 0.14–0.22 у всех.
- HIGH_VOL не предсказывает повышенный будущий диапазон: forward range/ATR p50 3.68 (HV) против 4.90 (RANGE), P(range≥3 ATR) 65.1% vs 78.7% (5m) — текущий HIGH_VOL descriptive («в моменте было высоко»), с mean reversion волатильности.
- Признаки почти не предсказывают направление (|ρ| ≤ 0.08); лучшие связи: ADX→forward ER +0.12 (5m); atr_percentile→forward range −0.31 (5m) / −0.43 (1h).
- Forward-классы `FUT_*` — временная диагностика (range/√h, direction efficiency, ER), truth-label не являются.

**Вывод.** Четырёхклассовая mutually-exclusive метка — derived summary текущих измерений, а не первичное физическое состояние. Детектор не разделяет оси (направление, сила тренда, волатильность, структура), не несёт confidence и не имеет hysteresis. Потребители (гейты `trade_regimes`, `bias_by_state`, `regime_setups_filter`, метаданные сделок, heatmap, UI) читают эту метку как контракт и не должны меняться молча.

## Решение

### 0. Модель данных

```
MarketMeasurements → RegimeObservation (оси + confidence) → Stateful Classifier (hysteresis) → Legacy adapter → trade_regimes
```

- `RegimeObservation` — **descriptive**, не predictive. Не вероятностью прибыли и не торговым сигналом.
- Legacy label ∈ {`TREND_UP`, `TREND_DOWN`, `RANGE`, `HIGH_VOLATILITY`, `NEUTRAL`} — производная для существующих потребителей.
- Каждое наблюдение несёт явный `tf` (секунды) и `version`; скрытых defaults нет. Единого глобального TF нет (решение владельца): production default — H1 (live), heatmap 30m — отдельная observation, 5m — отдельная для backtest/strategy; смешивать состояния разных ТФ запрещено. Формирующийся бар не входит в состояние (только `clone/evaluate` для отображения) — как в `RegimeState` сейчас.
- Видимость — только закрытые бары, `bar.ts + TF <= as_of` (ADR-0002).

Поля `RegimeObservation`:

| Поле | Домен | Смысл |
|---|---|---|
| `direction` | UP / DOWN / FLAT | знак текущего смещения close, нормированного на волатильность |
| `direction_strength` | [0, 1] | насколько однозначно/сильно направление |
| `trend_strength` | [0, 1] | беззнаковая эффективность движения (persistence), отдельно от направления |
| `volatility` | LOW / NORMAL / HIGH / EXTREME | текущий уровень волатильности |
| `volatility_percentile` | [0, 100] | место текущего уровня в истории (ATR-percentile) |
| `structure` | TRENDING / RANGE / TRANSITION | форма движения (см. §4) |
| `confidence` | [0, 1] | уверенность наблюдения (см. §8) |
| `reason` | строка | код причины/ветки (warmup, low_confidence, transition, hv_override, …) |
| `features` | dict | нормализованные измерения (§5), без абсолютных процентов |

Имена: `RegimeState` уже занят incremental-классом `app/services/regime_state.py`; в Stage C observation/classifier не переиспользуют это имя (например, `RegimeObservation`, `RegimeClassifierState`), runtime-словарь `self._regimes[figi]` остаётся legacy-хранилищем метки.

### 1. Direction

Смысл: **куда смещена цена на окне измерения, после нормировки на волатильность и ТФ**. Это не «предсказание доходности»; это измерение текущего смещения.
- Домен: UP / DOWN / FLAT; `direction_strength = clip(|drift_atr| / k_dir, 0, 1)` (или эквивалент через согласованность; конкретная форма — Stage C).
- Обязательно нормировать: `drift_atr = (close_t − close_{t−W}) / ATR_t` (в цене), `slope_atr` — наклон, делённый на ATR. Абсолютный `drift_pct=0.5%` в новой модели не используется (разный смысл для разных инструментов и ТФ); он остаётся только в legacy adapter.
- FLAT — не «нет движения», а «смещение неотличимо от шума на данном окне» (порог объявляется и версионируется в Stage C).

### 2. Trend Strength

Смысл: **сколько движения идёт в одну сторону без учёта знака** — насколько рынок «эффективен», а не насколько он прибылен.
- Домен [0, 1] (непрерывный), без категорий. Не путать с Direction: сильный тренд возможен при любом знаке; направление — отдельная ось.
- Измерения: ER (Kaufman) на окне W, directional consistency (доля баров в одну сторону), |slope|/ATR, DI spread/ADX как вспомогательная ordinal-мера.
- ADX: текущая реализация не-Wilder и даёт завышенную шкалу (p50≈71). В новой модели ADX не является пороговым классификатором; порог 19 живёт только в legacy adapter. Порт Wilder ADX — отдельное решение (см. открытые вопросы), т.к. меняет смысл порога.

### 3. Volatility

Смысл: **текущий уровень колебаний** (описательный), bucket + percentile.
- Измерения: ATR%, `atr_percentile` (rank в rolling-окне), `realized_vol / ATR`, range expansion (`bar range / ATR`, `range_W / ATR`).
- Границы bucket'ов — версионируемые параметры Stage C; в этом ADR числа не калибруются. Legacy adapter использует замороженные `atr_percentile=78` и `range_mult=2.75` для `HIGH_VOLATILITY`.
- Факт Stage B: HIGH не обязан предсказывать HIGH (mean reversion), поэтому bucket — состояние, а не прогноз; валидация — persistence (§6), не направление.

### 4. Structure

Смысл: **форма движения** — качественная сводка того, как соотносятся оси: направленно-устойчивая (TRENDING), возвратная/пилообразная (RANGE), переходная/конфликтная (TRANSITION).
- Это наименее независимая ось: она агрегирует direction + trend_strength + consistency + slope/drift conflict. Отдельных «физических» измерений у неё нет — поэтому её метрики обязаны быть перечислены явно (ниже), а не подразумеваться.
- TRENDING: устойчивое направленное движение (ER высокий, доля меньшинства мала). RANGE: низкая направленность, высокая доля меньшинства/возвратности. TRANSITION: признаки конфликтуют (slope vs drift, ER vs ADX, смена знаков) — в legacy это частично «mixed».
- Волатильность в Structure не входит: расширение диапазона — ось Volatility.

### 5. Какие признаки измеряют каждую ось

| Ось | Признаки (правильно нормализованные measurements) | Не использовать |
|---|---|---|
| Direction | drift/ATR, slope/ATR, signed DI spread, directional consistency, distance from EMA/ATR (контекст растянутости) | абсолютный drift_pct как порог |
| Trend Strength | ER, directional consistency, \|slope\|/ATR, ADX (ordinal), DI spread | ADX как жёсткий порог |
| Volatility | ATR%, atr_percentile, realized_vol/ATR, bar range/ATR, range_W/ATR | абсолютный ATR% между инструментами/ТФ |
| Structure | ER + consistency + minority share + slope/drift conflict + смена знака slope | отдельные «ещё индикаторы» |
| Data quality / confidence | плотность баров (дыры), warmup, session/weekend-флаг, объём (volume_ratio) как подтверждение | — |

Правило: **список признаков закрыт на Stage C**; новый признак — через отдельное решение ADR, а не «добавим ещё 15 индикаторов».

### 6. Какие forward targets валидируют каждую ось

Цель обязана соответствовать смыслу state, а не PnL и не просто forward return. Запрещено оптимизировать детектор напрямую на forward return/PnL — иначе он превращается в directional predictor/стратегию.

| Ось / свойство | Forward target (t+1..t+h) | Метрики (диагностика) |
|---|---|---|
| Direction persistence | future normalized drift (`ret/ATR`), future directional efficiency (signed), future up/down excursion (MFE/MAE в ATR) | P(sign match), conditional p50/p25/p75, смещение подписанного target |
| Trend persistence | future ER, future directional consistency, future \|slope\|/ATR | условные p50 по бакетам, монотонность бакетов, Spearman(u, ER) |
| Volatility persistence | future realized_vol / current ATR, future range / current ATR, future ATR percentile | условные p50 по bucket, P(RV_ratio ≥ 1 \| HIGH), P(RV_ratio ≤ 1 \| LOW), монотонность |
| State persistence (structure/adapter) | same state через N баров; time-to-state-change; будущий ER/dir_eff при данном structure | transition matrix, median run, P(change), lag на смене |
| Confidence | не отдельная truth: проверяем, что рост confidence упорядочивает state persistence | reliability curve confidence → persistence (не probability of profit) |

Дисциплина: критерии приёмки каждой оси фиксируются **до** OOS; пороги калибруются на train и замораживаются; отчёты — per-ticker + обязательные срезы weekday-only и day-session (формат golden-отчётов, `docs/results/report_semi.txt`).

### 7. Горизонты для 5m / 1h

| Измерение (TF) | Окно измерения W | h=12 | h=24 (primary) | h=48 |
|---|---|---|---|---|
| 5m | 6 баров = 30 мин (legacy `drift_bars=6`; W новых осей объявляется в Stage C) | 1 ч | 2 ч | 4 ч |
| 1h | 6 баров = 6 ч | 12 ч | 24 ч | 48 ч |

- Исходы считаются строго по барам t+1..t+h (первый future-бар — следующий после бара наблюдения).
- State persistence N = 12 и 24 бара (совпадает с коротким и primary горизонтом).
- Обязательный срез day-session и weekday-only: исходы пересекают сессии/ночи/выходные; weekend-наблюдения (~30% строк, объём ~10x ниже) в валидации помечаются флагом и не смешиваются с торговыми.

### 8. Confidence

`confidence ∈ [0,1]` — уверенность наблюдения, не вероятность прибыли. Компоненты:
1. **Margin** — минимальный нормированный отступ от границ доменов/порогов.
2. **Agreement** — согласие признаков одной оси (drift vs slope; ER vs DI/ADX; range vs percentile).
3. **Data quality** — warmup пройден, нет дыр, достаточная плотность баров; weekend/несессионное — понижение.
4. **Tenure** — время в текущем состоянии (при stateful-модели) с насыщением.

Правило: ниже `c_unknown` (выбирается в Stage C, не в этом ADR) observation отдаётся как UNKNOWN и адаптером мапится в `NEUTRAL`. Confidence никогда не калибруется на forward return.

### 9. Hysteresis

- Вводится **только после** появления state model: `state(t) = f(features(t), state(t−1))` — и **только после того, как оси покажут устойчивую семантику** (решение владельца; критерии устойчивости фиксируются harness'ом осей до включения hysteresis).
- Применяется к дискретным категориям (structure/direction/legacy label), не к непрерывным силам.
- Механика: двойные пороги (вход в состояние строже выхода) + минимальная длительность (min dwell); параметры версионируются и фиксированы на прогон.
- Parity: batch/incremental совпадение обязательно **для каждого префикса** (новый класс тестов), а не только итогового массива; forming-бар — через `clone`, состояние не мутируется.
- Метрики качества: singleton/transition rate (сейчас 40.1%/47.6%), median run, lag обнаружения смены относительно no-hysteresis; flicker должен падать без неограниченного роста lag. Пороговые цели утверждаются в Stage C.
- Replay и live используют один и тот же state machine; порядок баров и граница закрытия — ADR-0002.

### 10. Backward compatibility: `trade_regimes` и маппинг

**Инвариант:** наружу (конфиг, API, БД, UI) по-прежнему отдаются ровно строки `NEUTRAL`, `TREND_UP`, `TREND_DOWN`, `HIGH_VOLATILITY`, `RANGE`. Новые поля — только аддитивно.

| Приоритет | Условие нового observation | Legacy label |
|---|---|---|
| 1 | warmup / нет данных / `confidence < c_unknown` | NEUTRAL |
| 2 | `volatility = EXTREME` | HIGH_VOLATILITY |
| 3 | `structure = TRENDING` и `direction = UP` | TREND_UP |
| 4 | `structure = TRENDING` и `direction = DOWN` | TREND_DOWN |
| 5 | `structure = RANGE` или `direction = FLAT` | RANGE |
| 6 | `structure = TRANSITION` (включая конфликт slope/drift) | NEUTRAL |
| 7 | иначе | NEUTRAL |

- **RANGE/HIGH_VOL конфликт (решение владельца):** `HIGH_VOLATILITY` в derived-метке даёт только `EXTREME`-волатильность; обычный HIGH при подтверждённом TREND отдаётся `TREND_UP/DOWN`. Волатильность — характеристика рынка (ось), а не взаимоисключающий direction label: Stage B не подтвердил persistence будущего диапазона у текущего HV. Замороженные `atr_percentile=78` / `range_mult=2.75` остаются только в legacy-провайдере (behavior-preserving) и не являются порогами v2.
- **mixed/UNKNOWN:** оба дают `NEUTRAL`, но `reason` сохраняет причину (`warmup`, `low_confidence`, `transition`), чтобы диагностика не потерялась.
- `reason`-коды legacy (`drift_up_cons0.83`, `mixed`, …) не являются контрактом API, но adapter на переходный период сохраняет максимально близкие коды для логов/разбора.
- На время миграции — провайдер метки: `regime_provider = legacy | v2` (default `legacy`). Cutover — только после golden-паритета и подписи владельца. БД-историю сделок не переписываем; `entry_regime` в meta остаётся строкой.

**Инвентарь потребителей (проверено по коду 2026-10-02):**

| Потребитель | Что читает | Контракт совместимости |
|---|---|---|
| Trade-gate + trend-alignment, `bot/runtime.py:5637-5654` | `self._regimes[figi].state` | ровно 5 строк; новые значения не появляются |
| `BotConfig.trade_regimes` default all (`runtime.py:285`); preset `regimes` (`runtime.py:519,548`); `bot_config.json` + `BotSetting('runtime_config')` (`runtime.py:362-412`) | list[str] | список из 5; round-trip без изменений |
| PATCH `/bot/config`, режим запуска `bot.py:202,254,400-407`; whitelist `_valid_reg` | 5 строк | валидация не расширяется молча |
| UI «Режимы» `main.ts:1875-1894`, `localStorage.bot_regimes`; тип `api.ts:510` | checkbox → PATCH | те же 5 чекбоксов |
| Быстрый режим-гейт `ensemble_strategy.py:132-148` (`compute_regime(...,3600)`) | state + `trade_regimes` | H1-семантика гейта; метка та же |
| `compute_ensemble`: `regime_setups_filter` (846-859), `bias_by_state` (1936-1947, 1230-1236), `adaptive` (1271-1276), `per_regime_quorum` (1257-1270), `regime_entry_policy` (924-949), `_signal_score` (694-699) | словари, ключ — state | ключи остаются 5 строками; неизвестное → NEUTRAL |
| `entry_gates._regime_bias_decision` (`entry_gates.py:84-104`) | TREND_STATES и 3 остальных | те же строки |
| `EngineRunner.neutral_mode` + `cfg.regime_bars` (`runner.py:33,230-239`) | `state == "NEUTRAL"` | формат `{ts,state}` и строки сохраняются |
| Replay `ReplayState`/`RegimeState` (`replay_pipeline.py:239-263`), parity `tests/test_regime_state.py` | state/reason/features по закрытым барам | batch==incremental; формирующий бар — clone |
| Метаданные сделок: `_entry_regime`/`entry_regime` (`runtime.py:902,2261-2296`), `_entry_card` | строка | 5 + `NO_REGIME`; записи не мигрируют |
| API сделок: `/bot/trades` (`bot.py:1780`), `/sandbox/trades` (`sandbox.py:716-731,900-917,1170-1176`), `_regime_entry` (`sandbox.py:933`) | meta.entry_regime + поля позиции | те же строки; `NO_REGIME` сохраняется |
| Replay Analytics: `analysis_replays.py:30-40,364,522` (dims `regime`, `regime_adx`) | meta.entry_regime | те же строки в бакетах |
| Frontend: чипы `main.ts:3435-3443,392,576-664`, сортировка `2787`, голоса `2862`, heatmap | state | неизвестная строка падает в fallback (уже есть); новые поля не обязательны |
| Heatmap meta: `bot.py:2482-2538` (`compute_regime(...,1800)`), правило владельца — канон 30m | state на 30m | TF heatmap не меняется |
| `preset_tags.REGIME_RU` (`preset_tags.py:22-25`) | state → RU | fallback на raw; новые значения не обязательны |
| `portfolio.regime_limits` (`portfolio.py:148`) + `runtime.market_regime` (`runtime.py:1110-1159`) | bear/bull/neutral/reversal по IMOEX | **НЕ этот детектор**; не трогать; коллизия имён задокументирована |
| `engine/regime_strategies.py` (`TRENDING/CHOPPY/TRANSITIONING`) + `engine/regime_ensembles.py`/`catalog.py:252-264` (params `regime`) | отдельный словарь | legacy-контур; строки TREND_UP/DOWN/HV принимаются как параметры; дедуп — открытый вопрос |
| `bot/gates.py:178` `_ALL_REGIMES`, REGIME_MODE toggle (`gates.py:215-223`) | 5 строк | показ/включение того же набора |

### 11. Границы Stage C: что менять и что не менять

**Менять:**
- Новый пакет измерений/`RegimeObservation`/stateful-классификатора/adapter'а (отдельно от legacy `regime.py`).
- Нормализацию признаков (drift/ATR, slope/ATR, …), confidence, hysteresis, версионирование параметров.
- Тестовый контур: ось-валидация, parity-every-prefix, adapter-golden, hysteresis.
- API/UI — только аддитивно (observation рядом с legacy state), за флагом.

**Не менять:**
- Пять legacy-строк и их смысл для потребителей; `trade_regimes` конфиг/валидацию/UI.
- Legacy-пороги 19 / 0.5% / 0.71 / 78 и `range_mult=2.75` заморожены как behavior-preserving для legacy-провайдера; ни один из них не является каноническим порогом v2 (решение владельца).
- Текущее поведение runtime/ensemble/аналитики до переключения `regime_provider` по подписи владельца.
- Heatmap-канон 30m; `market_regime`/`portfolio.regime_limits`; `engine/regime_strategies.py`.
- БД-историю (никаких перезаписей `entry_regime` в старых сделках).

**Запрещено:** тюнить новую модель на forward return/PnL; менять пороги в этом ADR; включать hysteresis в production без отчёта flicker/lag и parity.

## Альтернативы

- **Подкрутить пороги текущего детектора** (cons/drift/ADX) — отвергнуто: Stage B показывает, что дело не в порогах, а в отсутствии осей и confidence; смена `cons_pct` лишь передвинет near-miss.
- **Сразу обучаемый ML-классификатор** — отвергнуто на этом шаге: нет контракта осей и forward-таргетов, высок риск PnL-утечки и необъяснимости; контракт — предпосылка для любой модели.
- **Оставить как есть, зафиксировав только факты** — отвергнуто: flicker 40/48%, HV-перекрытие тренда, немые метки без confidence — потребители не могут отличить «уверенный RANGE» от «не знаю».
- **Сохранить 4 mutually-exclusive класса как первичное состояние** — отвергнуто: направление/сила/волатильность/структура не сводятся к одному ярлыку; HV и trend конкурируют за один слот.

## Следствия

- Stage C начинается с контракта, а не с кода: сначала harness forward-валидации по осям на Stage B датасете, затем stateful-модель, затем adapter и provider-flag.
- Появляется новый обязательный класс тестов: parity batch/incremental **для каждого префикса**, включая формирующийся бар.
- Любая будущая правка меток проходит golden-сравнение меток legacy↔v2 на одних и тех же барах; расхождения — отдельный разбор, не «молчаливое улучшение».
- Валидация осей даёт воспроизводимый отчёт (per-ticker, срезы weekday/day) до принятия решения о cutover; PnL в критериях не участвует.
- Потребители защищены: пять строк, конфиг, UI, meta и API не меняются, пока `regime_provider=legacy` (default).

## Решения владельца (2026-10-02) — открытые вопросы закрыты

1. **Канонический TF — per-consumer, production default = H1.** Единого глобального состояния нет: `RegimeObservation` всегда несёт `timeframe`; live canonical — H1 (как сейчас), heatmap 30m — отдельная observation, 5m — отдельная для backtest/strategy. Молчаливое смешивание состояний разных ТФ запрещено.
2. **HV и TREND — ортогональные оси.** Observation может одновременно иметь `direction=UP`, `trend_strength=STRONG`, `volatility=HIGH`, `structure=TREND`. Derived: `EXTREME → HIGH_VOLATILITY`, обычный `HIGH + подтверждённый TREND → TREND_UP/DOWN`.
3. **`trend_alignment`/heatmap — только informational.** `TREND_UP → BUY` / `TREND_DOWN → SELL` как торговый гейт не использовать до отдельной валидации Direction оси; допускается показывать `direction + strength + confidence`.
4. **Weekend не исключать из production measurements**, но не смешивать с основной calibration population: основной срез — weekday/day, weekend — отдельный robustness slice. `session`/`session_quality` в observation; произвольный confidence penalty не вводить до данных Stage C.
5. **ADX — ordinal/continuous, без порога 19.** Смысл `ADX ≥ 19` в v2-классификатор не переносится; canonical Wilder ADX — отдельная задача с parity/reference тестами, не смешивается с пересборкой regime.
6. **Словарь regime не переименовывать на Stage C** — только документировать: `services/regime.py` — canonical v2 market regime; `engine/regime_strategies.py` — legacy/strategy adapter; `market_regime` — consumer/storage terminology. Массовый rename — отдельная migration/deprecation задача.
7. **`range_mult=2.75` заморожен только как legacy-параметр** (behavior-preserving), не является v2 calibration constant. Как и `19 / 0.5% / 71% / 78`: legacy — frozen, v2 — ни один из них не канонический порог.

**Контракт Stage C (утверждён):**

```
RegimeObservation
├── timeframe
├── direction (DOWN/FLAT/UP) + strength
├── trend_strength
├── volatility (LOW/NORMAL/HIGH/EXTREME) + percentile
├── structure (TREND/RANGE/TRANSITION)
├── confidence
├── measurements (drift_atr, slope_atr, di_spread, ER, consistency, ATR percentile, RV/ATR, range/ATR)
└── reason_codes

RegimeProvider: legacy → текущие 5 states; v2 → RegimeObservation + derived state
regime_provider=legacy — default; БД и исторические trade_regimes не мигрируем.
```

**Порядок Stage C:** validation harness независимых осей → `RegimeObservation` → stateful classifier → legacy adapter; **hysteresis — только после того, как оси покажут устойчивую семантику**. Оптимизация под диагностический forward-класс Stage B запрещена.

## Связанное

- `docs/results/REGIME_CALIBRATION_2026-10-02.md`, `docs/results/REGIME_CALIBRATION_FORWARD_2026-10-02.md` — факты Stage A/B.
- `backend/reports/regime_calibration/` — отчёты и датасет (gitignored).
- `app/services/regime_calibration.py` — определения forward-исходов (диагностика; truth-label не является).
- ADR-0002 — граница видимости закрытого бара.
- `test_regime_state.py` — текущий parity (префиксы с шагом 13; в Stage C — каждый префикс).
