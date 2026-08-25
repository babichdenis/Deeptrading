Да, вы думаете в правильную сторону: стратегии, шумовые политики, выходы и риск нужно реализовывать как **отдельные Python-модули**, а связывать их через стабильные интерфейсы, registry и API. Тогда новая стратегия добавляется отдельным файлом, а основной движок, FastAPI и frontend не переписываются.

## Главная идея

Не строить систему так:

```text
MACD-код
  ├── SL/TP
  ├── VWAP
  ├── шумы
  ├── Lab
  └── frontend
```

А строить как конвейер:

```text
Candles
  ↓
Strategy plugin
  ↓
Signals in database
  ↓
Signal/noise policy plugin
  ↓
Order intents
  ↓
Canonical trade engine
  ↓
Execution + CostModel
  ↓
Trades / Metrics / Graph
```

MCP находится снаружи и управляет этим конвейером:

```text
AI
 ↓ MCP tools
FastAPI
 ↓
Research services
 ↓
plugins + engine
```

MCP не должен напрямую импортировать и менять Python-файлы. Он вызывает API, а API разрешает только зарегистрированные стратегии и параметры.

## Что является отдельным модулем

### Стратегия

Стратегия отвечает только на вопрос:

> Возник ли на закрытом баре сигнал BUY, SELL или ничего?

Файл:

```text
backend/app/research/strategies/rsi_reversal.py
```

Пример:

```python
class RsiReversalStrategy:
    strategy_id = "rsi_reversal"
    version = "1.0.0"

    def on_bar(self, context: StrategyContext) -> list[RawSignal]:
        ...
```

Стратегия не должна:

- открывать позицию;
- считать комиссию;
- ставить стоп;
- знать о FastAPI;
- писать в PostgreSQL;
- рисовать на frontend;
- выбирать лучший результат.

Она только создаёт сигнал и features.

### Signal/noise policy

Отдельный файл:

```text
backend/app/research/policies/confirmed_flip.py
```

Он отвечает:

> Что делать с raw signal с учётом текущего состояния позиции и предыдущих сигналов?

Примеры решений:

```text
ACCEPT
IGNORE_SAME_SIDE
PENDING_OPPOSITE
REJECT_MIN_HOLD
REJECT_COOLDOWN
REJECT_SESSION_CUTOFF
```

Policy не должна самостоятельно считать P&L или выполнять заявки.

### Exit policy

Отдельный файл:

```text
backend/app/research/exits/atr_trailing.py
```

Она отвечает:

> Нужно ли закрыть уже открытую позицию на текущем баре и по какой причине?

Примеры:

```text
HOLD
STOP
TARGET
TRAIL
SIGNAL_EXIT
SESSION_CLOSE
```

### Risk policy

Отдельный файл:

```text
backend/app/research/risk/fixed_risk.py
```

Отвечает:

> Разрешён ли вход и каким должен быть размер позиции?

Проверяет:

- дневной лимит убытка;
- максимальное количество позиций;
- максимальный риск;
- размер лота;
- доступность short;
- portfolio limits.

### Strategy engine / position engine

Это центральный модуль:

```text
backend/app/research/engine/position_engine.py
```

Он:

- хранит state `FLAT/LONG/SHORT`;
- принимает `OrderIntent`;
- применяет execution model;
- вызывает exit policy;
- пишет ledger;
- считает costs.

Его нельзя менять при добавлении обычной стратегии.

## Как модули соединяются

Нужны стабильные типы данных.

```python
@dataclass(frozen=True)
class Candle:
    figi: str
    ts: datetime
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
```

```python
@dataclass(frozen=True)
class RawSignal:
    strategy_id: str
    strategy_version: str
    figi: str
    signal_ts: datetime
    side: Literal["BUY", "SELL"]
    reason: str
    features: dict[str, Any]
```

```python
@dataclass(frozen=True)
class PolicyDecision:
    action: Literal[
        "ACCEPT",
        "IGNORE",
        "PENDING",
        "REJECT"
    ]
    reason_code: str
    signal: RawSignal
    metadata: dict[str, Any]
```

```python
@dataclass(frozen=True)
class OrderIntent:
    figi: str
    side: Literal["BUY", "SELL"]
    intent_type: Literal["OPEN", "CLOSE", "FLIP"]
    source_signal_id: int | None
    decision_ts: datetime
    execution_rule: str
```

```python
@dataclass(frozen=True)
class ExitDecision:
    action: Literal[
        "HOLD",
        "STOP",
        "TARGET",
        "TRAIL",
        "SIGNAL_EXIT",
        "SESSION_CLOSE"
    ]
    reason_code: str
    price_level: Decimal | None
    metadata: dict[str, Any]
```

Схема потока:

```python
raw_signal = strategy.on_bar(context)

decision = signal_policy.on_signal(
    signal=raw_signal,
    position_state=state,
    context=context,
)

if decision.action == "ACCEPT":
    intent = order_factory.from_decision(decision)
    engine.apply_intent(intent)

exit_decision = exit_policy.on_bar(
    position=engine.position,
    candle=candle,
    context=context,
)

if exit_decision.action != "HOLD":
    engine.close_position(exit_decision)
```

## Registry

Чтобы API и MCP знали доступные плагины, нужен registry.

```python
STRATEGY_REGISTRY = {
    "rsi_reversal": RsiReversalStrategy,
    "bollinger_reclaim": BollingerReclaimStrategy,
    "pullback_ema": PullbackEmaStrategy,
}
```

```python
POLICY_REGISTRY = {
    "none": NoopPolicy,
    "confirmed_flip": ConfirmedFlipPolicy,
    "stop_cooldown": StopCooldownPolicy,
}
```

```python
EXIT_REGISTRY = {
    "fixed_sl_tp": FixedSlTpExit,
    "atr_stop": AtrStopExit,
    "atr_trailing": AtrTrailingExit,
}
```

Frontend не знает Python-классы. Он получает карточки через API:

```http
GET /api/v1/strategies/catalog
GET /api/v1/policies/catalog
GET /api/v1/exits/catalog
```

Пример ответа:

```json
{
  "id": "rsi_reversal",
  "version": "1.0.0",
  "name": "RSI Reversal",
  "family": "reversal",
  "status": "AVAILABLE",
  "timeframes": ["5min", "15min", "hour"],
  "params_schema": {
    "period": {
      "type": "int",
      "default": 14,
      "min": 2,
      "max": 100
    },
    "oversold": {
      "type": "float",
      "default": 35,
      "min": 5,
      "max": 50
    }
  }
}
```

Frontend строит форму динамически из `params_schema`.

## Как добавляется новая стратегия

Допустим, нужна `opening_range_breakout`.

### 1. Создать файл

```text
strategies/opening_range_breakout.py
```

### 2. Реализовать интерфейс

```python
class OpeningRangeBreakout:
    strategy_id = "opening_range_breakout"
    version = "1.0.0"

    def metadata(self) -> StrategyMetadata:
        return ...

    def on_bar(self, context: StrategyContext) -> list[RawSignal]:
        ...
```

### 3. Добавить golden tests

Проверить:

- long breakout;
- short breakout;
- отсутствие сигнала внутри range;
- отсутствие look-ahead;
- session boundaries;
- недостаток history.

### 4. Зарегистрировать plugin

```python
register_strategy(OpeningRangeBreakout())
```

### 5. Добавить карточку через metadata

Frontend автоматически увидит:

- название;
- описание;
- timeframe;
- parameters;
- long/short rules;
- required features.

### 6. API запускает compute

```http
POST /api/v1/signals/compute
```

```json
{
  "figi": "BBG004730N88",
  "interval_name": "5min",
  "strategy_id": "opening_range_breakout",
  "params": {
    "opening_minutes": 30
  }
}
```

### 7. Результат сохраняется в `strategy_runs` и `signals`

Frontend получает `run_id` и рисует сигналы. Lab потом может использовать этот `run_id`, не пересчитывая стратегию.

## Как добавляется новый шумовой фильтр

Допустим, нужен `min_hold_3_bars`.

Создаётся:

```text
policies/min_hold.py
```

Он не меняет signals в базе. Он применяет решение в Lab:

```text
raw signal
→ policy
→ accepted/rejected decision
```

Это важно: изменение `min_hold` не должно пересчитывать RSI, Bollinger или MACD.

## Как добавляется новый exit

Создаётся:

```text
exits/atr_trailing.py
```

Он подключается через experiment config:

```json
{
  "strategy_run_id": "...",
  "signal_policy": {
    "id": "confirmed_flip",
    "params": {
      "confirm_bars": 2
    }
  },
  "exit_policy": {
    "id": "atr_trailing",
    "params": {
      "period": 14,
      "initial_stop_atr": 2.0,
      "activation_atr": 1.0,
      "trail_distance_atr": 2.0
    }
  },
  "engine_version": "trade_engine_v1",
  "cost_model_version": "canonical_v1"
}
```

Сигналы останутся прежними. Пересчитается только Lab/P&L слой.

## Как FastAPI связывает всё

FastAPI должен быть orchestration layer.

### Сигналы

```text
POST /signals/compute
```

Что происходит:

1. Проверить strategy_id и params.
2. Рассчитать `params_hash`.
3. Найти exact cached run.
4. Если найден — вернуть его.
5. Если нет:
   - получить candles;
   - вызвать strategy plugin;
   - записать strategy_run;
   - записать signals;
   - вернуть run_id.

### Lab

```text
POST /experiments
```

Что происходит:

1. Проверить существующий `strategy_run_id`.
2. Проверить policy/exit/engine/cost versions.
3. Создать immutable experiment.
4. Отправить job worker-у.
5. Worker читает signals из БД.
6. Worker запускает position engine.
7. Результаты пишет в trades/metrics/artifacts.
8. API возвращает статус и результаты.

Ключевой момент:

```text
signals compute ≠ Lab execution
```

Пересчёт сигналов не должен происходить при изменении SL/TP, trailing или noise policy.

## Что делает frontend

Frontend не реализует торговую математику.

Он:

- получает каталог;
- показывает формы параметров;
- вызывает `/signals/compute`;
- получает `run_id`;
- запрашивает signals;
- показывает маркеры на графике;
- вызывает `/experiments`;
- показывает progress;
- показывает trades, metrics, reports;
- позволяет сравнивать несколько runs/experiments.

Frontend не должен:

- сам считать RSI;
- сам определять crossover;
- сам считать P&L;
- сам решать, был ли stop;
- самостоятельно фильтровать сигналы без API;
- дублировать Position Engine.

Иначе UI и Lab снова начнут показывать разные результаты.

## Что делает MCP

MCP — это интерфейс для AI, а не основная бизнес-логика.

```text
AI
 ↓ MCP tool
FastAPI endpoint
 ↓
Registry + Warehouse + Lab
```

MCP tools:

```text
list_strategy_catalog
get_strategy_schema
compute_signals
get_signal_run
create_experiment
run_experiment
get_job_status
get_metrics
get_trade_ledger
compare_experiments
```

AI не должен напрямую импортировать:

```python
from strategies.rsi_reversal import ...
```

Он вызывает:

```text
compute_signals(strategy_id="rsi_reversal", params={...})
```

Это даёт:

- контроль прав;
- audit trail;
- schema validation;
- versioning;
- ограничение параметров;
- повторяемость;
- возможность отключить AI, не ломая систему.

## Защита движка

Обычная новая стратегия не должна менять `trade_engine_v1`.

Если найдена ошибка:

1. Создать issue.
2. Добавить failing test.
3. Исправить engine.
4. Выпустить `trade_engine_v2`.
5. Старые experiments оставить на v1.
6. Новые experiments запускать на v2.
7. Не переписывать старые результаты молча.

Стратегия, policy и exit — плагины. Engine — защищённый versioned core.

## Рекомендуемая структура каталогов

```text
backend/app/
  api/
    routes/
      strategies.py
      signals.py
      experiments.py
      jobs.py
      results.py

  warehouse/
    models.py
    repository.py
    candle_store.py
    dataset_service.py
    validation.py

  research/
    contracts/
      candle.py
      signal.py
      decision.py
      order_intent.py
      exit_decision.py
      trade.py

    strategies/
      base.py
      registry.py
      metadata.py
      rsi_reversal.py
      bollinger_reclaim.py
      pullback_ema.py
      vwap_reclaim.py
      squeeze_breakout.py

    policies/
      base.py
      registry.py
      no_op.py
      confirmed_flip.py
      min_hold.py
      cooldown.py
      quorum.py

    exits/
      base.py
      registry.py
      fixed_sl_tp.py
      atr_stop.py
      atr_trailing.py
      signal_exit.py

    engine/
      position_engine.py
      execution_model.py
      cost_model.py
      session_rules.py
      risk_manager.py

    lab/
      experiment_service.py
      job_runner.py
      metrics.py
      reports.py

  db/
    models/
      datasets.py
      strategy_runs.py
      signals.py
      decisions.py
      experiments.py
      jobs.py
      trades.py
      metrics.py
```

## Схема взаимодействия на одном примере

Пользователь выбирает на фронте:

```text
SBER
5min
RSI Reversal
period=14
oversold=35
```

### Шаг 1: frontend

```http
POST /api/v1/signals/compute
```

### Шаг 2: FastAPI

- проверяет параметры;
- ищет cache;
- вызывает `RsiReversalStrategy`;
- сохраняет `strategy_run`;
- сохраняет raw signals.

### Шаг 3: frontend

```http
GET /api/v1/signals/{run_id}
```

Показывает BUY/SELL на графике.

### Шаг 4: пользователь выбирает Lab

```text
signal run: rsi_reversal run_id
signal policy: confirmed_flip
exit: atr_trailing
qty: 1
```

### Шаг 5: frontend

```http
POST /api/v1/experiments
```

### Шаг 6: Lab

- читает сохранённые signals;
- применяет policy;
- запускает engine;
- рассчитывает exits;
- записывает trades и metrics.

### Шаг 7: frontend

Получает:

```http
GET /api/v1/experiments/{id}/trades
GET /api/v1/experiments/{id}/metrics
```

И показывает реальные position zones.

## Самая важная граница

```text
Strategy = когда возник сигнал
Policy = разрешать ли сигнал
Engine = как открыть/держать/закрыть позицию
Frontend = как показать
MCP = как AI управляет API
```

Если эту границу сохранить, добавление новой стратегии будет выглядеть так:

```text
новый .py файл
→ registry
→ metadata/schema
→ golden tests
→ GET catalog
→ compute signals
→ визуальный QC
→ Lab
```

А не так:

```text
новая стратегия
→ переписать FastAPI
→ переписать график
→ переписать Lab
→ переписать P&L
→ вручную исправлять старые скрипты
```

## Практический порядок

Сейчас я бы делал так:

1. Зафиксировать contracts/dataclasses.
2. Реализовать registry.
3. Довести `POST /signals/compute` и cache.
4. Реализовать five wave-1 strategy plugins.
5. Добавить frontend strategy cards и signal markers.
6. Добавить `signal_decisions`.
7. Реализовать same-bar quorum `window_bars=0`.
8. Только после визуального QC подключить Lab.
9. Затем подключить policies и exits.
10. После стабилизации — MCP adapter.
11. Бот подключить последним.

Главное: **MCP не должен быть местом, где живёт стратегия или движок**. Он должен быть управляющим интерфейсом для уже хорошо разделённой платформы.