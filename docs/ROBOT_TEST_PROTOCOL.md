# ROBOT_TEST_PROTOCOL — машинный протокол Robot Lab

Дата: 2026-09-28. Статус: v1 (нормативный).

Назначение: единая онтология + правила, чтобы агент (нейронка) не могла
трактовать этапы по-своему. Полная человеческая версия (52 пункта) — в
обсуждении 2026-09-28; этот файл — нормативная выжимка с привязкой к коду
Deeptrading.

Связанные документы:
- `docs/osengine/PORTING_MAP.md` — что портируем и как ложится на движок;
- `docs/osengine/PORT_NOTES_EXITS.md` — механика выходов OsEngine (слоты);
- `docs/osengine/robots_registry.tsv` — реестр 201 робота-источника.

---

## 0. Онтология: чем робот отличается от стратегии

**Главное правило: мы никогда не тестируем «робота». Мы тестируем
стратегию, прогоняем эксперимент и храним карточку.**

Цепочка порождения:

```
РОБОТ (.cs из ~/OsEngine/Robots, строка robots_registry.tsv)
   │ STEP A: декомпозиция ENTRY / EXIT / FILTERS / POSITION-MGMT
   ▼
ENTRY-модель (сигнальная часть; родной выход робота выброшен)
   │ × ExitPolicy этапа × фильтры (сначала NONE)
   ▼
СТРАТЕГИЯ (strategy_id + params + direction + universe)
   │ × TF из матрицы совместимости (§4)
   ▼
ВАРИАНТ (variant_id = {strategy_id}@{tf}-xNN)
   │ × прогон на фиксированном стенде (§3)
   ▼
ЭКСПЕРИМЕНТ (exp_id) → метрики + сделки
   │ × гейты этапа (§1)
   ▼
КАНДИДАТ / REJECT → STRATEGY CARD (паспорт)
```

### Определения и привязка к коду

| Термин | Определение | Где живёт |
|---|---|---|
| Робот | исходник OsEngine: вход+выход+управление позицией слиты в одном классе | `~/OsEngine/Robots/**.cs`; строка в robots_registry.tsv |
| ENTRY-модель | сигнальная часть робота без его выхода | `app/engine/ose/robots.py` (PriceChannelTrade, …) или Strategy в `app/engine/strategies.py` |
| Стратегия | минимальная тестируемая единица: ENTRY + ExitPolicy + фильтры + TF | strategy_id — ключ `STRATEGY_REGISTRY`; витрина — `STRATEGY_CATALOG` |
| Вариант | стратегия, прибитая к TF и коду выхода | конфиг эксперимента; variant_id = {strategy_id}@{tf}-xNN |
| Эксперимент | один прогон варианта на стенде; к нему приложимы все метрики | `app/services/experiments.py` → Experiment/ExperimentTrade; config_hash = `_config_hash` (sha256-16) |
| Кандидат | вариант, прошедший гейты текущего этапа | поле status карточки/результата |
| Strategy Card | паспорт кандидата по §39 человеческой версии | план: `lab/cards/*.json` |

### Жёсткие отношения (не обсуждаемые)

1. **1 робот → N стратегий** (комбинации ExitPolicy × фильтры). Робот без назначенного выхода — не тестируемая сущность, а источник ENTRY.
2. **1 стратегия → M вариантов** (TF матрицы, где робот логически применим; иначе `NOT_APPLICABLE_TF`).
3. **1 вариант → K экспериментов** (скрининг, OOS-окна, walk-forward, sensitivity). Повторный прогон = новый exp_id; перезапись и удаление запрещены (§8).
4. **Метрики принадлежат эксперименту.** Запрещено «робот X имеет PF 1.3» — только «EXP-000127, вариант macd_cross@15m-x02, стенд S1: PF 1.31».
5. **Три TF**: signal / filter / execution. Движок сейчас работает на одном TF; до появления MTF-контекста (§9) в конфиге явно пишем `filter_tf=none`, `execution_tf=same` — не подразумеваем.

### Naming

- `strategy_id` — ключи STRATEGY_REGISTRY (ose_price_channel, macd_cross, …); новые порты регистрируются там же и в STRATEGY_CATALOG.
- `variant_id` — `{strategy_id}@{tf}`; при смене выхода в серии — суффикс `-xNN` (§5).
- `exp_id` — EXP-NNNNNN, сквозной, не переиспользуется.
- `stand_id` — S1, S2, …; смена execution/комиссии/universe = новый стенд.
- `spec_id` — `{source_robot}__{entry_strategy_id}__{xcode}`.

---

## 1. Статусы и переходы

Прогрессивные:

```
DISCOVERED → IMPLEMENTED → SANITY_CHECK → SCREENED → CANDIDATE
→ PARAM_TESTED → VALIDATED → FROZEN → FORWARD_TEST
```

Отрицательные (терминальные, журнал не стирают):

```
REJECTED, INVALID, UNSTABLE, OVERFIT, INSUFFICIENT_SAMPLE,
COST_SENSITIVE, EXECUTION_FRAGILE, REDUNDANT, NOT_APPLICABLE_TF
```

Коды причин (machine enum): NEGATIVE_EXPECTANCY, PF_BELOW_1, FRAGILE_TOP1,
UNIVERSE_DEPENDENT, REJECT_BY_COSTS, REJECT_BY_EXEC, REJECT_BY_OOS,
CORRELATED_WITH_EXISTING.

### Гейты переходов (машинно проверяемы по результатам)

| Переход | Гейт |
|---|---|
| DISCOVERED → IMPLEMENTED | strategy_id в STRATEGY_REGISTRY; `build_strategy(id, params)` не бросает; карточка в STRATEGY_CATALOG |
| IMPLEMENTED → SANITY_CHECK | ≥1 тест с ручными числами (как tests/test_ose_robots.py) зелёный на Mac и .8 |
| SANITY_CHECK → SCREENED | E0+E1 выполнены; execution_errors == 0; сделки внутри диапазона бара |
| SCREENED → CANDIDATE | sample ≥ PRELIMINARY (≥20 сделок); expectancy > 0; PF > 1; top1_contribution_pct < 50; net после costs > 0 |
| CANDIDATE → PARAM_TESTED | E5: parameter plateau — соседи ±10–20% сохраняют знак net |
| PARAM_TESTED → VALIDATED | OOS: net_val > 0 и net_test > 0; PF_val ≥ 0.7 × PF_train (пороги менять = ревизия протокола, не подгонка) |
| VALIDATED → FROZEN | E6: robustness ±20% → net > 0; costs ×2 → net > 0; worst-case exec → net > 0 |
| FROZEN → FORWARD_TEST | paper/forward на новых данных, параметры заморожены |

Примечание: вердикт прогона INSUFFICIENT_DATA / NEGATIVE / WEAK_POSITIVE /
POSITIVE уже считает `experiments.py::_verdict` (min_trades=15, PF ≥ 1.15,
DD < 25%). Это вердикт эксперимента, а не статус кандидата — статус
назначается только по таблице выше.

## 2. Серии экспериментов (порядок фиксированный)

| Серия | Что проверяем | Гейт-результат |
|---|---|---|
| E0_SANITY | прогон на fixture-данных | execution_errors == 0; fills внутри диапазона бара |
| E1_ENTRY_SCREEN | ENTRY без фильтров × выход этапа x01–x03 | ≥20 сделок; expectancy > 0 |
| E2_EXIT_SWEEP | тот же ENTRY × x01–x11 | лучший выход по net; delta vs baseline обязательна |
| E3_TF_MATRIX | вариант на TF матрицы §4 | знак net стабилен минимум на 2 TF |
| E4_FILTER_ABLATION | + фильтры по одному | каждый фильтр: оставлен (net↑) или выключен |
| E5_PARAM_OOS | plateau ±10–20% + train/val/test | plateau; net_val>0; net_test>0; PF_val ≥ 0.7×PF_train |
| E6_ROBUSTNESS | costs ×2, slippage ×3, сдвиг входа на бар | net > 0 во всех стрессах |
| E7_WALKFORWARD | фолды train 12m / test 3m, ≥4 фолдов | ≥60% фолдов net>0; агрегат net>0 |

Пропуск серии разрешён только с формулировкой причины в карточке
(например, NOT_APPLICABLE_TF для E3 на конкретном TF).

## 3. Стенд (stand_id)

S1 = {universe: ликвидный список MOEX (фиксируется в конфиге), execution:
next_open, commission/slippage: из конфига, capital: 100 000}. Любое
изменение исполнения, издержек или universe = новый stand_id и повторные
контрольные прогоны. Прямое сравнение метрик между стендами запрещено —
только через delta_vs_baseline в рамках одного стенда.

## 4. Матрица TF

Ряд: 1m, 5m, 10m, 15m, 30m, 1h. signal_tf берём из природы робота
(robots_registry.tsv: индикаторы и логика подсказывают рабочий диапазон);
полная матрица = все осмысленные TF ряда. Роботы микроструктуры
(спред/стакан/тики) — NOT_APPLICABLE_TF ниже 5m с записью причины.
До MTF-гэпа (§9) filter_tf/execution_tf в конфиге пишем явно как
none/same.

## 5. Коды выходов (xNN)

| Код | Policy | Дефолт |
|---|---|---|
| x01 | fixed_sl_tp | stop 1%, target 2% |
| x02 | atr_stop | period 14, mult 2.0, rr 2.0 |
| x03 | atr_trailing | period 14, init 2.0, act 1.0, dist 2.0 |
| x04 | fixed_sl_tp wide | stop 2%, target 4% |
| x05 | atr_stop + BE-активация | trail_activation_r 1.0 |
| x06 | time exit | max_bars 20 |
| x07 | signal reversal | выход по обратному сигналу ENTRY |
| x08 | session close | закрытие на границе сессии |
| x09 | native | родной выход робота; только exec_mode=native |
| x10 | combo | x02 + трейлинг после 1R |
| x11 | TBD | назначается в E2, фиксируется в конфиге |

x01–x03 маппятся на EXIT_POLICY_SPECS из app/services/experiments.py
1:1. Новый код = правка этого файла, не молчаливое расширение.

## 6. Параметры: registry и дисциплина

Каждый порт публикует параметр-спеку (имя, тип, default, min/max, шаг,
группа entry/exit/filter/position) — источник для Optuna и UI. Сейчас
источник правды — Params-дата классы портов в `app/engine/ose/robots.py`
и `_PARAM_TYPES`/карточки в `strategies.py`/`catalog.py`; до отдельного
registry новые параметры добавляются только там и там.

Правила:
- параметр без min/max не оптимизируется (только фиксированное значение);
- диапазоны берём из оригинального .cs (обычно 10–300 для периодов), не выдумываем;
- изменение набора параметров стратегии после старта серии = новый xcode/ветка, не правка истории;
- OOS-пороги (§1) не подкручиваются по результатам — только ревизией протокола с датой и причиной.

## 7. Фильтры и подтверждения

Порядок добавления фиксированный, по одному за раз (E4_FILTER_ABLATION):
trend (EMA на signal_tf) → volatility (ATR-режим) → volume → time
(TradingSchedule) → mtf_direction (после MTF-гэпа §9). Фильтр оставляем
только если net вырос у кандидата и не упал на соседних параметрах.
Подтверждения второго индикатора (RSI+ADX и т.п.) оформляются как
отдельная ENTRY-модель с собственным strategy_id, а не фильтр.

## 8. Журнал экспериментов

- Experiment/ExperimentTrade — append-only: перезапись и удаление запрещены.
- Повторный прогон того же конфига = новый exp_id; идентичность конфига проверяется `_config_hash` (sha256-16, `app/services/experiments.py`).
- Каждый эксперимент после первого обязан иметь `deltas_vs_baseline` относительно parent_exp (Δ net, pf, dd, expectancy, trades, win_rate, commission, slippage).
- Прогон с правкой кода движка = новый stand_id (или новый exp в той же серии с пометкой code_change) — сравнивать с прогонами до правки нельзя.

## 9. Гэпы движка (осознанные, не имитируем)

До закрытия этих гэпов протокол их явно декларирует в конфиге:

| Гэп | Сейчас | Целевое (после порта) |
|---|---|---|
| MTF DataFeed | один TF в EngineRunner | `ctx.candles(tf)` для filter_tf/execution_tf |
| ExecutionSpec | next_open на закрытии бара | MARKET_WITH_SLIPPAGE / LIMIT / LIMIT_WITH_TIMEOUT отдельным объектом |
| TradingSchedule | нет | сессионные allow/deny-окна как фильтр |
| Parameter Registry | Params-классы портов | единая спека для Optuna/UI |
| Portfolio-стенд | single-strategy TesterTab | N стратегий, общий капитал/риск |

Серию нельзя засчитать, если она опирается на закрытый гэп, которого ещё нет:
E3 на filter_tf ≠ signal_tf до реализации MTF не назначается (NOT_APPLICABLE_TF
с причиной `mtf_gap`).

## 10. Где что лежит (реализация на 2026-09-28)

| Компонент | Файл |
|---|---|
| ENTRY-порты (12 роботов) | `app/engine/ose/robots.py`, регистрация `strategies.py` STRATEGY_REGISTRY |
| Индикаторы (sma/rsi/stoch/bollinger/envelops/price_channel/atr/cci/macd/rvi/power) | `app/engine/ose/indicators.py` |
| Исполнение (слоты стоп/тейк, OCO, трейлинг) | `TesterTab` в `app/engine/ose/robots.py`; семантика — `docs/osengine/PORT_NOTES_EXITS.md` |
| Адаптеры голосования | `app/engine/ose/strategy.py` |
| Метрики прогона (FIFO-сделки, эквити, DD) | `app/engine/ose/metrics.py` |
| Журнал экспериментов | `app/services/experiments.py`, таблицы Experiment/ExperimentTrade |
| Реестр роботов-источников | `docs/osengine/robots_registry.tsv` (201 строк) |

Прогонный скрипт `backtest_ose.py` (на .8) постепенно выносится в
`ose/metrics.py` + runner; новые сценарии — только через тесты с ручными
числами, не через правку прогонного скрипта.
