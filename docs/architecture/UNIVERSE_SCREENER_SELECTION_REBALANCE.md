# Universe → Screener → Selection → Allocation → Rebalance

Архитектура отбора акций и разделение ответственностей. Документ описывает
текущее состояние на 2026-09-30 и честно фиксирует известные долги.

Исходные задания: `docs/osengine/screener_universe.txt` (Phase 1–3) и
Phase 4–5 (Feature calculation + Selection/Ranking).

---

## 1. Главный принцип

Четыре ответственности не смешиваются:

| Слой | Отвечает на вопрос | НЕ отвечает |
|---|---|---|
| **Universe** | какие инструменты вообще в рассмотрении | про признаки и порядок |
| **Screener** | можно ли передать инструмент дальше | про ATR, ранги, веса |
| **Features** | какие у инструмента признаки | про сортировку и Top-N |
| **Selection** | кто выше в ранкинге | про капитал и сделки |
| **Allocation** | сколько капитала каждому | про исполнение |
| **Rebalance** | какие изменения портфеля допустимы | про то, как это исполнять |
| **Execution** | исполнить | — |

> Selection говорит, какие инструменты предпочтительнее. Allocation говорит,
> сколько капитала им назначить. Rebalance говорит, какие изменения портфеля
> допустимо выполнить. Execution исполняет их.

Правило, которое стоит дороже всего остального:

> **Смена ранкинга ≠ сигнал на выход.** Бумага может выпасть из Top-N, и
> ничего не продаётся. Решение о сделке принимает Rebalance (Phase 7), а не
> Selection. Закреплено тестом `test_ranking_change_does_not_produce_exit`.

---

## 2. Статус по этапам

| Этап | Статус | Где |
|---|---|---|
| Phase 1 — Domain contracts | **DONE** | `backend/app/bot/universe/domain.py` |
| Phase 2 — Universe | **DONE** | `discovery.py`, `bars.py` |
| Phase 3 — Screener | **DONE** | `screener.py`, `compat.py` |
| Phase 4 — Features | **DONE** | `features.py`, `FeatureSet` в `domain.py` |
| Phase 5 — Selection | **DONE** | `selection.py`, `SelectionResult` в `domain.py` |
| Phase 6 — Allocation | **DONE** | `allocation.py`, `TargetPortfolio` в `domain.py` |
| Phase 7 — RebalancePlan | **DONE** | `rebalance.py`, `RebalancePlan` в `domain.py` |
| Phase 8 — RebalancePolicy → OrderIntent | **DONE** | `policy.py`, контракты в `domain.py` |
| Phase 9 — Execution (OrderIntent → PaperBroker) | **DONE** | `app/bot/execution.py`, `tests/test_execution_order_intent.py` |
| Execution | EXISTING (не трогаем runtime/broker) | `app/bot/runtime.py`, `app/bot/paper_broker.py` |

---

## 3. Целевая архитектура

```text
                  ┌───────────────┐
                  │    Universe   │  discovery, identity, source, availability
                  └───────┬───────┘
                          │
                          ▼
                  ┌───────────────┐
                  │    Screener   │  data availability, min bars, coverage,
                  └───────┬───────┘  │  tradeability, exclusions
                          │          └──► ScreenResult.eligible + ScreenReason
                          ▼
                  ┌───────────────┐
                  │    Features   │  ATR, ATR%, close
                  │               │  look-ahead закрыт фильтром ts <= as_of
                  └───────┬───────┘
                          │
                          ▼
                  ┌───────────────┐
                  │   Selection   │  rank, percentile, Top-N
                  └───────┬───────┘  (без весов и без ордеров)
                          │
                          ▼
                  ┌───────────────┐
                  │  Allocation   │  equal_weight, reserve cash, lot-aware
                  │               │  TargetPortfolio = desired state
                  └───────┬───────┘  (не создаёт ордера, не зовёт Broker)
                          │
                          ▼
                  ┌───────────────┐
                  │  Rebalance    │  DirectTargetRebalance: difference between
                  │               │  Current and Target state, без ордеров
                  └───────┬───────┘
                          │
                          ▼
                    research API

                  LIVE RUNTIME
                  ────────────
                  остаётся на compatibility path
```

**Live-рантайм подключён только к compat-слою.** Новый конвейер
`FeatureSet → Selection → Allocation → RebalancePlan` — исследовательский API.
Ни один live-модуль его не вызывает; это проверено тестом
`test_runtime_still_uses_compat_path_not_selection` (для Selection),
`test_allocation_does_not_create_orders` / отсутствием ссылок на allocation
и rebalance в `app/bot/runtime.py`.

---

## 4. Контракты

Все контракты — `frozen dataclass` в `backend/app/bot/universe/domain.py`.
Логики в `domain.py` нет: считают `discovery/screener/features/selection`.

```python
# Phase 1
InstrumentRef(ticker, figi, exchange)      # order=True → стабильный tie-break
UniverseSnapshot(as_of, source, entries)   # детерминированный порядок
ScreenResult(eligible, reason, detail)     # причина отказа обязательна
ScreenedInstrument(instrument, result, source_bars, resampled_bars)

# Phase 4
FeatureSet(instrument, as_of, atr, atr_pct, close, bars_used, valid)

# Phase 5
SelectionItem(instrument, score, rank, atr, atr_pct, atr_pct_percentile)
SelectionResult(as_of, items, selected, ranking_method, top_n)

# Phase 6
AllocationInput(selection, equity, prices, lot_sizes, as_of)
TargetPosition(instrument, target_weight, actual_weight, target_qty,
               actual_qty, target_notional, actual_notional, price, lot_size)
TargetPortfolio(as_of, equity, positions, cash_target, unallocated_cash,
                allocation_method, reserve_cash_pct)  # .validate()

# Phase 7
CurrentPosition(instrument, current_qty, price, current_notional)
CurrentPortfolio(as_of, equity, positions, cash)     # read-only snapshot
RebalanceActionType(NOOP/BUY/SELL/REMOVE)            # ENUM, не строки
RebalanceAction(instrument, action, current_qty, target_qty, delta_qty,
                current_notional, target_notional, delta_notional, price)
RebalancePlan(as_of, current_equity, target_equity, current_cash,
              target_cash, cash_delta, actions, planner)  # .validate()
```

`ScreenedInstrument` намеренно не содержит ATR, а `SelectionItem` /
`SelectionResult` — весов, номиналов и количества лотов. Это проверяется
тестами `test_screened_instrument_carries_no_features` и
`test_selection_result_has_no_weight_fields`.

`TargetPortfolio` — желаемое состояние, а не список сделок: в `TargetPosition`
нет `order_id/side/fill/slippage/commission` (слои Execution). Allocation не
решает «Sell 30», это уйдёт на Phase 7 в RebalancePlan. Проверено тестами
`test_allocation_does_not_create_orders` и
`test_allocation_result_contains_target_state_not_execution`.

---

## 5. Features: контракт и look-ahead

`compute_feature_set(instrument, bars, *, as_of, window, period)` — чистая
детерминированная функция: без БД, без системных часов, без мутабельного
глобального состояния.

```text
ATR period = 14
ATR window = 44 × 5m баров
ATR%       = round(ATR / close * 100, 3)
канон ATR  = app.engine.indicatorhub._atr
```

**Look-ahead закрыт конструктивно, а не тестом:** бары с `ts > as_of`
отбрасываются до расчёта. Поэтому `features_with_future_bar ==
features_without_future_bar` выполняется по построению.

ATR считается ровно в одном месте — `_measure()` в `features.py`. Его
используют и новый контракт, и legacy-функция `atr_pct()` (совместимость).
Второй реализации ATR нет; паритет с `engine.indicators.atr` и
`indicatorhub._atr` держит `tests/test_atr_canon_parity.py`.

---

## 6. Selection: ранжирование и percentile

```text
score       = atr_pct           (или своя SelectionStrategy)
порядок     = score DESC, затем ticker ASC
rank        = 1-based после tie-break
percentile  = 100 * count(x <= value) / n
```

Конвенция percentile задокументирована в `selection._percentile` и покрыта
тестами: равные значения получают одинаковый percentile, один инструмент в
выборке даёт `100.0`, пустая выборка или `atr_pct=None` дают `None`.
Percentile считается **только по текущему срезy** — будущие снимки не
участвуют.

`atr_pct` при этом сохраняется как есть: percentile его дополняет, но не
заменяет, потому что live читает абсолютное значение.

Baselines: `RankingMethod.ALL_ELIGIBLE` (все прошедшие скрининг) и
`RankingMethod.TOP_N_ATR` (шортлист). Для будущего скоринга есть протокол
`SelectionStrategy`; momentum/signal_count в него **не добавлены** — таких
канонических признаков в проекте пока нет, и выдумывать значения запрещено.

Ранжинг не хранится в БД. Модель `rank(t) / rank(t-1) / rank_delta`
(по аналогии с `SecurityRankingMove` в OsEngine) — следующий этап; пока
достаточно снапшота ранкинга. Полноценная история ранкинга в БД не создаётся.

---

## 7. Allocation: TargetPortfolio — желаемое состояние

```text
SelectionResult
    ↓  EqualWeightAllocation(reserve_cash_pct=0.0)
TargetPortfolio
```

`AllocationPolicy` — протокол, `EqualWeightAllocation` — единственный
production policy (равные веса 1/N по всем selected). Policy чистая и
детерминированная: без БД, без Broker, без wall-clock — `as_of` приходит
снаружи. Принцип: **Allocation не знает, как исполнять сделки.**

Lot-aware sizing (никогда не округляется вверх):

```text
target_notional = investable_equity * (1/N)     investable = equity*(1-reserve)
raw_qty         = target_notional / price
target_qty      = floor(raw_qty / lot_size) * lot_size
actual_notional = target_qty * price            остаток остаётся в unallocated_cash
```

Edge-cases (все детерминированы тестами): пустой Selection → всё в cash;
`equity <= 0` → без позиций; `price <= 0` / `lot_size <= 0` → инструмент
исключается из positions; `target_notional` ниже минимального лота →
`target_qty = 0`; дубликаты в selection → unique identity по figi.

Вес семантически разделён: `target_weight = 1/N` (теоретический), `actual_weight
= actual_notional / equity` (после floor-округления), и actual может быть ниже
target — это нормальный lot-aware результат, те
`test_target_vs_actual_weight_semantics`.

Смена ранкинга — НЕ сигнал на выход (см. раздел 1), это зафиксировано и для
Allocation: `test_ranking_change_does_not_create_sell_or_order` — GAZP выпадает
из Top-N, но в результате нет side/delta/sell/order.

Инварианты результата проверяет `TargetPortfolio.validate()` (пустой кортеж =
ок): веса/количества/номиналы `>= 0`, `target_qty % lot_size == 0`,
`sum(actual_notional) <= equity - cash_target`, `cash >= 0`.

---

## 8. RebalancePlan: разница состояний, а не сделки

```text
CurrentPortfolio
       +
TargetPortfolio
       ↓  DirectTargetRebalance.plan(current=..., target=...)
RebalancePlan(actions=[BUY/SELL/REMOVE])
```

`CurrentPortfolio` — минимальный read-only snapshot (`instrument, current_qty,
price, current_notional`; на уровне портфеля `as_of, equity, positions, cash`).
`app/bot/portfolio.py` остаётся текущим risk/analytics-слоем; вместо клона
его snapshot-смысла есть адаптер `current_portfolio_from_dicts()` для того
формата `{figi, ticker, qty, price}`, который использует runtime.

`DirectTargetRebalance` (первый planner): каждая `target_qty != current_qty`
даёт action. Без threshold/cooldown/min trade/turnover/cost filter — это будет
отдельная policy в Phase 8.

Дельта семантически точная, без повторного округления:

```text
delta_qty = target_qty - current_qty
current_notional = current_qty * price
target_notional  = target_qty * price
delta_notional   = delta_qty * price
```

Правила action (порядок детерминированный):
- `delta > 0` → **BUY**
- `delta < 0` и target остаётся `> 0` → **SELL**
- `target_qty == 0` при `current_qty > 0` → **REMOVE**
- `delta == 0` → отсутствие action (вариант B, §11: NOOP не попадает в
  `actions`)

Порядок действий: сначала инструменты TargetPortfolio (порядок Selection),
затем только-current позиции в их исходном порядке. Dict/set не участвуют.

**Главный инвариант:** отсутствие инструмента в Target ≠ немедленная продажа.
`REMOVE` — только описание изменения состояния; разрешит и исполнит его
следующий слой (Phase 8 Execution). Selection/Allocation/RebalancePlan ≠ Order.

`RebalancePlan.validate()` проверяет количества `>= 0`, `delta ==
target - current`, согласованность action (BUY→delta>0, SELL→delta<0,
REMOVE→target=0 ∧ current>0, NOOP→delta=0) и `cash >= 0`. Ограничения по
risk/margin/часам/biến-рынка сюда сознательно НЕ входят — это не
responsibility плана.

---

## 9. Известные долги (не закрыты намеренно)

### 9.1. Рантайм читает `universe` сырым SQL, в обход пакета

`runtime.py:3131, 3877, 3884` делают `SELECT ... FROM universe WHERE
eligible_tier='eligible'` напрямую. Пакет Universe и рантайм-доступ к той же
таблице — два независимых пути. Это главный источник будущей
рассинхронизации. Миграция — отдельная задача.

### 9.2. `top_n=9999` — ранжирование формально отключено

`runtime.py:3112` забирает **все** бумаги. Сортировка по `atr_pct` есть, но
порядок не читает никто: ни шортлиста, ни отсева. `selection.py` выделен, но
live его не зовёт. Активация Top-N запрещена до Phase 7: только после того
как RebalancePlan сможет честно сравнивать target с текущим портфелем.

### 9.3. `vol_carousel` — вторая реализация ranking

`app/services/vol_carousel.py` содержит собственный `rank_volatile` и
`run_vol_carousel_once` за флагом `vol_carousel_enabled` (по умолчанию
**False**). Миграция в общий Selection не выполнялась намеренно; при миграции
нужно избежать двух ranking engines.

### 9.4. Несогласованный источник ликвидности

`avg_turnover` берётся из колонки таблицы `universe`, а рантайм сам признаёт
(`runtime.py:1028`): «колонка universe часто устаревает» — реальный дневной
оборот приходит из MOEX ISS. Канонический источник не определён, поэтому
liquidity-aware ranking и любые изменения источника запрещены. TODO в коде
`features.average_turnover`.

### 9.5. `sector` не используется

Колонка заполняется, но ни один `if` по ней не читает. Ранжирование и
caps по секторам — Phase Allocation/Risk.

### 9.6. Неоднозначность `min_source_bars`

В профиле `ELIGIBLE` стоит `min_source_bars=5`, но `min_resampled_bars=15`,
а 15 пятиминутных баров нельзя получить из 5 минутных: floor-бакет 5m
наполняется пятью 1m-барами. Значит `min_source_bars=5` физически не может
сработать и расходится с докстрингом «отсеивала по числу баров». Фактическая
семантика зафиксирована тестом
`test_min_source_bars_never_binds_below_resampled_floor`; **значение не
менялось** — смена порога требует отдельной parity/benchmark-задачи.

### 9.7. Оптимизация 250 баров не включена

Замер: полный цикл `select_eligible_universe` 7186 ms → 879 ms после удаления
избыточного пре-фильтра (`GROUP BY figi HAVING count>=1`, 86% времени цикла).
Предложенный лимит 1000 → 250 даёт оценку ~250 ms и проверен на паритет ATR
(`test_250_source_bars_suffice_for_window_44`), но **в production не включён**:
окно ATR нельзя будет удлинять без поднятия лимита (250 минутных баров дают
49-50 полных 5m-бакетов на 44, запас 5 баров).

---

## 10. Что запрещено на этом этапе

```text
NO live activation        NO Top-N activation in runtime
NO Execution / Broker     NO order generation
NO commission/slippage    NO min trade / max turnover / cooldown
NO risk limits / margin   NO market-hours logic
NO liquidity redesign     NO sector ranking
NO ML / optimizer         NO новый CandleHub / Resampler / IndicatorHub
NO vol_carousel migration NO stop-loss/take-profit
```

Allocation/Rebalance/Policy существуют как research API, но live-активация
(runtime должен начать вызывать EqualWeightAllocation / DirectTargetRebalance /
DirectRebalancePolicy и исполнять OrderIntent) запрещена до Phase 9 —
Execution-слоя, который возьмёт PolicyResult и превратит его в реальный ордер.

## 11. Тесты

```text
tests/test_feature_selection.py        36 тестов   Phase 4-5
tests/test_universe_screener_parity.py 24 теста    Phase 1-3
tests/test_atr_canon_parity.py          5 тестов    паритет ATR
tests/test_allocation.py               24 теста    Phase 6
tests/test_rebalance.py                21 тест     Phase 7
tests/test_rebalance_policy.py         23 теста    Phase 8
tests/test_execution_order_intent.py   19 тестов   Phase 9
                                        ─────────
                                        152 passed
```

Живой паритет legacy на реальной БД (read-only): `legacy = 30, new = 30,
difference = 0`.

Интеграционные тесты Phase 1-6, Phase 1-7 и Phase 1-8
(`test_end_to_end_universe_to_target_portfolio`,
`test_end_to_end_universe_to_rebalance_plan`,
`test_end_to_end_universe_to_order_intent`) прогоняют полный конвейер;
E2E Phase 1-8 доходит до `OrderIntent` (BUY + SELL) и доходит до rejected
action (BELOW_MIN_TRADE_VALUE на мелком REMOVE). Broker/Execution во всех
отсутствуют: `OrderIntent` не содержит id/fill/status.

E2E Phase 1-9 (`test_full_pipeline_universe_to_paper_broker`) прогоняет
скрининг → features → selection → allocation → rebalance-plan → policy →
`execute_policy_result` → реальный in-memory `PaperBroker` (BUY открывает,
повторный проход закрывает позиции). Broker в тестах — настоящий существующий
`PaperBroker` на sqlite (StaticPool + aiosqlite), без runtime/live.

Полный suite: `864 passed, 2 failed`. Оба падения не наши и не связаны с
universe/allocations/policy/execution:

- `tests/test_research_pack.py::test_e_session_boundary` — предсуществующее,
  `execution_time_msk` не содержит подстроку `msk`;
- `tests/test_engine_units.py::TestCatalog::test_waves_and_timeframes` —
  параллельная работа по порту OsEngine: робот `rsi_mtf_hub` использует ТФ
  `10min`, которого нет в разрешённом наборе теста.

(Пять падений `test_ose_strategy.py`, замеченных во время Phase 6, устранены
параллельным ворк-потоком и в финальном прогоне не воспроизводятся.)

Ни одно из падений не исправлялось: они принадлежат другим ворк-потокам.

---

## 12. Git-ограничение

Коммиты в этой фазе не сделаны. Репозиторий доступен через SMB-шару, где git
падает на `pack-objects` (`died of signal 10`). Код и тесты выполнены полностью;
при доступном git изменения разбиваются на части по спецификации:

```text
docs: define feature and selection architecture
refactor: introduce feature domain contract
refactor: extract feature calculation
test: add feature parity and lookahead tests
refactor: implement deterministic selection ranking
test: add selection and percentile tests
docs: record selection integration constraints
docs: record allocation integration constraints
test: add allocation and target portfolio tests
refactor: implement deterministic allocation layer
feat: add equal weight allocation policy
refactor: implement current vs target rebalance planner
feat: add direct target rebalance plan
test: add rebalance plan tests
docs: record allocation and rebalance integration constraints
refactor: implement rebalance policy layer
feat: add direct rebalance policy and order intent
test: add rebalance policy and end-to-end tests
docs: record policy and order intent integration constraints
```

---

## 13. Phase 6/7 — Allocation/Rebalance: итог

Реализованы два доменных слоя:

- **Allocation** (`allocation.py`): `AllocationPolicy`, `EqualWeightAllocation`,
  `AllocationInput`, `TargetPosition`, `TargetPortfolio(.validate)` —
  `SelectionResult → TargetPortfolio` с reserve cash и lot-aware sizing
  (никогда не округляется вверх).
- **Rebalance** (`rebalance.py`): `RebalancePlanner`, `DirectTargetRebalance`,
  `CurrentPosition`, `CurrentPortfolio`, `RebalanceAction`,
  `RebalanceActionType`, `RebalancePlan(.validate)`,
  `current_portfolio_from_dicts()` — разница состояний, а не сделки.

```text
Selection produces desired candidates.
Allocation converts candidates into target portfolio state.
RebalancePlan is the difference between current and desired state.
Order = execution concern (Phase 8, ещё не подключён).

Selection/Allocation/RebalancePlan change != order.
Только Execution создаёт Order.
```

`TargetPortfolio` = desired state; `RebalancePlan` = difference between current
and desired state; `Order` = execution concern. Смена ранкинга не превращается
в SELL-ордер напрямую: цепочка идёт через TargetPortfolio, а в самом плане
`REMOVE` — только описание (тесты `test_rank_change_itself_does_not_create_rebalance_action`,
`test_ranking_change_does_not_create_sell_or_order`).

---

## 14. Phase 8 — RebalancePolicy → OrderIntent: итог

Реализован доменный слой между планом и исполнением:

- **Контракты** (`domain.py`): `RebalanceContext` (equity, prices, as_of,
  available_cash, last_rebalance_at, lot_sizes — всё приходит снаружи, никаких
  DB/Broker/wall-clock), `OrderIntent`, `RejectedAction`, `PolicyResult`,
  `RejectionReason` (единый Enum причин: BELOW_MIN_TRADE_VALUE,
  MAX_TURNOVER_EXCEEDED, COOLDOWN, MAX_COST_EXCEEDED, INVALID_PRICE,
  INVALID_QUANTITY, ZERO_QUANTITY, INSUFFICIENT_CASH).
- **Policy** (`policy.py`): `RebalancePolicy` (Protocol),
  `DirectRebalancePolicy`, `order_intent_to_legacy()` (неисполняющий адаптер к
  формату существующего Order runtime — без id/fill/status).

Семантика `DirectRebalancePolicy`:

```text
BUY   → OrderIntent side=BUY  quantity=+delta_qty
SELL  → OrderIntent side=SELL quantity=-delta_qty
REMOVE→ OrderIntent side=SELL quantity=current_qty   (delta_qty < 0)
```

- Порядок accepted/rejected соответствует порядку `plan.actions`.
- Плановые ограничения уровня портфеля отклоняют ВЕСЬ план (Вариант A):
  `MAX_TURNOVER_EXCEEDED` (gross/equity), `INSUFFICIENT_CASH`
  (sum(buy_notional) > available_cash; SELL-proceeds не считаются cash).
- Индивидуальные фильтры отклоняют отдельные action:
  `BELOW_MIN_TRADE_VALUE` (equal допустимо), `MAX_COST_EXCEEDED`
  (abs(notional) × estimated_cost_rate), `COOLDOWN` (через last_rebalance_at;
  `allow_exit_during_cooldown=True` пропускает SELL/REMOVE),
  `INVALID_PRICE` (price <= 0), `INVALID_QUANTITY` (qty % lot != 0 при известном
  лоте), `ZERO_QUANTITY`.
- Policy **никогда не меняет quantity** ради ограничения — только accept/reject.
- `OrderIntent.quantity` всегда положителен (направление задаёт `side`;
  переиспользуется единый `app.engine.models.Side`), `notional = quantity ×
  price`. Полей исполнения (order_id/fill/commission/status) нет.

Инварианты фаз 1-8 (закрыты тестами):

```text
1. Screening, features, ranking — детерминированы и обратимы (fingerprint).
2. SelectionResult требует ranking_method (без default).
3. Ранкинг, как и план, не создаёт ордер сам по себе.
4. Уникальность инструмента по figi (или ticker), источник — reference.
5. Policy переиспользует существующий Side и не создаёт второй Order engine.
6. Policy может ОТКЛОНИТЬ RebalanceAction: это и есть её работа (PolicyResult),
   активность — accept/reject, не modify.
7. Broker/Execution не вызываются: OrderIntent — желание, не сделка.
```

Причины в `RejectionReason` — Enum, произвольных строк нет. Возможный
Execution-адаптер уже подтверждён тестом `test_legacy_adapter_is_non_executing`:
`order_intent_to_legacy()` отдаёт скелет (figi/ticker/side/qty/price/source) без
id/filled_at/status — заполнение execution-состояния остаётся за Phase 9,
которая вне scope этой дорожки.

---

## 15. Phase 9 — Execution: OrderIntent → существующий PaperBroker: итог

Тонкий адаптер, **не новый execution engine**. Существующая цепочка исполнения
(`runtime._execute_pending`) вызывает `broker.open_position`/`close_position` —
адаптер делает только это: валидирует интент и передаёт его существующим
методам бумажного брокера. `runtime.py`/`paper_broker.py`/`live_broker.py`/
`portfolio.py` не менялись.

Файл: `backend/app/bot/execution.py`.

- `ExecutionStatus` (Enum): FILLED, REJECTED, INVALID_INTENT, VALIDATION_ERROR,
  BROKER_ERROR.
- `ExecutionResult` (frozen dataclass): intent, status, order_id=None,
  filled_qty, fill_price, commission, error. Никогда не равен `OrderIntent` —
  это факт попытки, а не желание. order_id остаётся None до появления
  реального брокера, отдающего id.
- `execute_order_intent(broker, intent, *, strategy_id="universe")` —
  один intent → одна попытка:
  - quantity/price <= 0 → INVALID_INTENT (защита, policy уже валидировала);
  - BUY без позиции → существующий `open_position(side="BUY")`, на существующей
    позиции → REJECTED (PaperBroker открывает только с нуля);
  - SELL без позиции → существующий `open_position(side="SELL")` (short open);
  - SELL при LONG: qty == qty позиции → `close_position(...)`; qty > позиции →
    REJECTED (flip в PaperBroker не поддерживается); qty < позиции → REJECTED
    (partial close не поддерживается);
  - исключение брокера → BROKER_ERROR с текстом (не проглатывается).
- `execute_policy_result(broker, result, *, strategy_id="universe")` — батч:
  строго в порядке `accepted`, rejected **никогда** не исполняются (0 вызовов).
- Slippage/комиссию считает сам брокер через `costs.fill_price/commission`;
  адаптер просто сообщает их в ExecutionResult. quantity/price не меняются,
  лот не трогается.

Известные долги (breaks существующего PaperBroker, НЕ маскируются):
- partial close и flip не поддерживаются существующим `close_position`
  (закрывает всё) — адаптер честно отдаёт REJECTED, E2E/поведение
  задокументировано;
- entry-комиссия списывается из cash при open, но не входит симметрично в
  `PaperTrade.net_pnl` (net = gross − exit_commission) — предсуществующая
  особенность учёта paper-брокера;
- `PaperTrade.id` — BigInteger PK без default (на Postgres это SERIAL); в тестах
  на sqlite id подставляется клиентским слушателем `before_flush`, боевой код
  не менялся.

Инварианты фаз 1-9 (закрыты тестами, §37):

```text
1. Selection/Allocation/Rebalance/Policy/Features НЕ импортируют и НЕ вызывают
   брокеров (проверка исходников на отсутствие paper_broker/live_broker).
2. Execution не ссылается на live-брокера и runtime (import-проверка).
3. ExecutionResult не мутирует OrderIntent и не трогает TargetPortfolio.
4. Выполняется строго PolicyResult.accepted в исходном порядке.
5. Существующие методы PaperBroker остаются единственным каналом исполнения.
```

---

## 16. Параллельный слой Universe 2.0 (2026-09-30)

Legacy `app/bot/universe/` **не рефакторится**; рядом построен чистый слой
measure/screener/selection поверх тех же Phase 1-8 контрактов. Старый код —
рабочая эталонная база для parity; его удаление — отдельным этапом (§дальше).

### Семантика (зафиксировано в тестах `tests/test_universe_v2_*`)

| Понятие | Что значит | Чего НЕ делает |
|---|---|---|
| **Universe** | полный допустимый рынок (discovery/identity/availability) | нет ATR-рейтинга, Top-N, тренда — только доступность |
| **VolatilityMeasure** | измерение волатильности (ATR/ATR%/realized/range) | не фильтрует, не решает |
| **TrendMeasure** | измерение тренда (slope/ATR, direction, strength) | не фильтрует, не решает |
| **MarketFeatures** | волатильность + тренд одной пачкой (valid-флаг) | не сортирует |
| **StrategyScreener** | «подходит ли конкретной стратегии» (trend/mr) | не выбирает среди подходящих |
| **Selection** | «кого из подходящих выбрать» (внешний score) | не знает про капитал/сделки |
| **SectorMembership** | metadata/grouping по сектору | не фильтр, не числовой признак |

### Новые модули (все deterministic, без БД/брокера/HTTP)

- `universe/volatility.py` — `compute_volatility_features(instrument, bars, *,
  as_of, window=44, period=14)`; ATR берётся **canonical** из
  `app.engine.indicatorhub._atr(candles, length)` (единый источник, parity тест
  `test_atr_canon_parity.py`); ATR% = atr / close; `realized_volatility` = std
  log-returns; `range_pct` = (high−low)/close. Причины invalid: `no_data` /
  `insufficient_bars` / `invalid_close`. Есть `_many` вариант (батч).
- `universe/trend.py` — `compute_trend_features(...)`; `slope` = OLS slope по
  закрытиям; `normalized_slope = slope / atr`; `strength = min(|ns|, 1.0)`;
  `direction = UP >0 / DOWN <0 / FLAT` с константным порогом `FLAT_THRESHOLD=0.0`
  (в т.ч. случай −0.0 → FLAT, фикс `<=`). `TrendDirection` в `domain.py`.
- `universe/sectors.py` — `sector_memberships(snapshot)`, `get_sector`,
  `group_by_sector`, `get_sector_members`, `sectors_known`; детерминизм через
  sorted по ticker; инструмент без sector не выпадает из Universe.
- `universe/eligibility.py` — `eligibility_screen(items, profile=ELIGIBLE)`
  переиспользует legacy `screen_reasons`/`ELIGIBLE` (без дублирования проверок);
  `eligible_instruments`. `EligibilityResult` в `domain.py`.
- `universe/features.py` — добавлены `compute_market_features` /
  `compute_market_features_many` → `MarketFeatures` (volatility+trend,
  `.valid` = обе доли валидны).
- `universe/screener.py` — `StrategyScreener` (Protocol),
  `StrategyScreenResult`, `TrendStrengthScreener(min_strength)`,
  `MeanReversionScreener(max_strength)`, `screen_by_strategy(...)`. Принимают
  готовые `MarketFeatures`, **не считают их внутри** (один раз на инструмент —
  может приходить из IndicatorHub).
- `universe/selection.py` — `RankedCandidate`, `rank_candidates(candidates,
  score_fn)` (score DESC, ticker ASC — детерминизм), `select_top_n(candidates,
  score_fn, *, top_n)`. Работает на **уже отфильтрованных** кандидатах.
- `universe/domain.py` — добавлены (аддитивно): `TrendDirection`,
  `StrategyFamily` (8 стратегий), `SectorMembership`, `VolatilityFeatures`,
  `TrendFeatures`, `MarketFeatures`. Legacy-контракты и импорты (`Side` и пр.)
  не тронуты.
- `universe/__init__.py` — реэкспорты обновлены (старые имена сохранены).

### Поток данных нового слоя

```text
 Universe (snapshot)
   → eligibility_screen (решает: «в рассмотрении или нет»)
   → sector_memberships (metadata, для группировок/отчётов)
   → compute_market_features (IndicatorHub/ATR canonical, один раз на инструмент)
   → StrategyScreener (trend/mr: «подходит ли стратегии»)
   → rank_candidates / select_top_n (внешний score, среди подходящих)
   → legacy Allocation → TargetPortfolio → RebalancePlan → Policy → OrderIntent
```

Один и тот же Universe, очищенный от неeligible, кормит **обе** стратегии:
направленную (TrendStrengthScreener) и контртренд (MeanReversionScreener) —
см. `test_e2e_same_universe_feeds_trend_and_meanreversion`; Universe не
мутируется (`test_e2e_universe_does_not_change_across_strategies`).

### Look-ahead дисциплина

Measure-функции принимают явный `as_of` и видят только `bar.time <= as_of`.
В тестах «будущие» бары начинаются после `as_of` (`start = as_of + STEP`),
чтобы look-ahead детектировался тестом. Никаких обращений к БД/брокеру внутри
measure — только переданные бары.

### Тесты и parity

- `tests/test_universe_v2_*.py` (7 файлов: volatility/trend/sectors/eligibility/
  screener/selection/e2e) — 56 passed.
- ATR parity: `compute_volatility_features.atr == indicatorhub._atr(...)` на
  синтетике; legacy `compute_feature_set` / `atr_pct` сверены в
  `test_atr_canon_parity.py`. **Live-DB parity** (new eligible universe == legacy
  eligible universe на реальной таблице БД .2) отложена — нет автотеста с
  живой БД; делается вручную на рубеже миграции.
- Legacy-контур не сломан: `test_universe_screener_parity.py`,
  `test_feature_selection.py`, `test_allocation.py`, `test_rebalance.py`,
  `test_rebalance_policy.py`, `test_execution_order_intent.py` — 147 passed.

### Что осталось legacy (область миграции, отдельным этапом)

runtime.py, бэктест-скрипты, `vol_carousel`, `select_volatile_universe`,
raw-SQL `select_eligible_universe`, legacy `Universe`/`top_n` — НЕ мигрированы;
компатибельность сохранена (`compat.py`), runtime работает как раньше. Полный
список потребителей — см. MEMORY.md / §32 критерий готовности (22 пункта).

