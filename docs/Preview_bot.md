Торгующий бот лучше проектировать не как «ещё один Lab», а как **live-исполнитель той же конфигурации и той же логики позиции**, с отдельными real-time-механизмами: отбором акций, проверкой данных, риском, заявками, подтверждением исполнения и восстановлением после ошибок.

Главное правило:

```text
Warehouse собирает конфигурацию
Lab проверяет её на истории
Paper повторяет её в реальном времени без денег
Bot исполняет её через брокера
```

При этом стратегия, signal policy, position engine и exit logic должны быть максимально одинаковыми во всех режимах. Различаться должны в основном источник данных и execution adapter.

# 1. Полная схема бота

```text
Market Data Stream
        ↓
Data Quality / Normalizer
        ↓
Universe Selector
        ↓
Strategy Runtime
        ↓
Signal Policy
        ↓
Portfolio / Risk Engine
        ↓
Order Intent
        ↓
Pre-trade Risk Gate
        ↓
Order Manager
        ↓
Broker Adapter
        ↓
Execution Events / Fills
        ↓
Position Reconciliation
        ↓
P&L / Monitoring / UI
```

В отдельной ветке:

```text
Research configuration
        ↓
Paper Bot
        ↓
Live Bot
```

# 2. Что бот получает из Warehouse и Lab

Бот не должен получать набор разрозненных параметров. Он должен получать **замороженную конфигурацию**.

Пример:

```json
{
  "bot_config_id": "botcfg_001",
  "source_configuration_id": "cfg_001",
  "source_lab_experiment_id": "exp_014",
  "strategy_version": "rsi_reversal@1.0.0",
  "signal_policy_version": "confirmed_flip@1.0.0",
  "exit_policy_version": "atr_trailing@1.0.0",
  "engine_version": "trade_engine_v1",
  "cost_model_version": "canonical_v1",
  "universe_policy_id": "volatility_liquid_v1",
  "risk_policy_id": "risk_fixed_v1",
  "execution_mode": "PAPER",
  "status": "FROZEN"
}
```

После запуска paper нельзя молча менять:

- RSI period;
- quorum;
- timeframe;
- stop;
- trailing;
- position sizing;
- universe rules.

Для изменения создаётся новая версия:

```text
botcfg_001 → botcfg_002
```

# 3. Первоначальный подбор акций

Это отдельный механизм, не часть стратегии входа.

Нужно разделить:

```text
Universe selection = какие акции можно торговать
Strategy = когда входить в выбранной акции
```

## 3.1. Источники universe

Бот может брать акции из:

1. фиксированного списка;
2. Warehouse universe;
3. динамического volatility/liquidity scanner;
4. списка, утверждённого пользователем;
5. комбинации этих источников.

## 3.2. Фильтры отбора

Минимальные фильтры:

- акция доступна для торговли;
- FIGI корректен;
- рынок открыт или скоро откроется;
- trading status разрешает операции;
- достаточно свежих свечей;
- нет data gap;
- есть следующий бар/поток;
- достаточная ликвидность;
- допустимый спред;
- допустимый средний оборот;
- достаточная волатильность;
- short доступен, если стратегия требует short;
- акция не находится в blacklist;
- нет активной технической ошибки.

## 3.3. Волатильность

Выбирать только «самые волатильные» бумаги опасно: высокая волатильность может сопровождаться плохой ликвидностью и большим проскальзыванием.

Лучше считать итоговый score:

```text
universe_score =
  volatility_score
  + liquidity_score
  - spread_penalty
  - data_quality_penalty
  - concentration_penalty
```

Пример:

```json
{
  "ticker": "SBER",
  "atr_percent": 1.8,
  "avg_turnover": 28000000,
  "spread_bps": 4.2,
  "data_quality": "PASS",
  "short_available": true,
  "universe_score": 82,
  "status": "ELIGIBLE"
}
```

## 3.4. Два режима universe

### Static universe

Список задаётся заранее:

```text
SBER, GAZP, ROSN, MTSS, MGNT
```

Преимущество:

- воспроизводимость;
- проще Lab и paper;
- меньше риска, что состав изменится задним числом.

### Dynamic universe

Список пересчитывается перед сессией или по расписанию:

```text
top 10 по volatility_score
при условии liquidity_score > threshold
```

Преимущество:

- бот работает с актуальными движущимися бумагами.

Но dynamic universe должен сохранять snapshot:

```json
{
  "date": "2026-08-22",
  "selected_figis": [...],
  "selection_time": "...",
  "selection_rules": {...},
  "scores": {...}
}
```

Иначе исторический backtest невозможно воспроизвести.

# 4. Предторговая проверка акции

Перед каждой стратегией бот должен проверять, можно ли вообще принимать сигнал.

```text
is_eligible(figi, now, context)
```

Проверки:

- instrument exists;
- data fresh;
- price valid;
- session valid;
- trading status valid;
- not halted;
- spread under limit;
- volatility not in emergency mode;
- not blacklisted;
- position state known;
- no unresolved broker order;
- risk capacity available.

Если проверка не пройдена:

```text
signal = REJECTED
reason = DATA_STALE / SPREAD_TOO_WIDE / NOT_TRADABLE / RISK_LIMIT
```

# 5. Market data runtime

Бот должен получать поток данных, но не передавать сырые события напрямую стратегии.

Нужен pipeline:

```text
Broker stream
  ↓
Normalizer
  ↓
Timestamp validator
  ↓
Candle builder
  ↓
Closed candle event
  ↓
Indicator cache
  ↓
Strategy runtime
```

## Важное правило

Стратегия получает событие только после закрытия свечи:

```text
CANDLE_CLOSED
```

а не каждое промежуточное изменение цены, если стратегия рассчитана на свечи.

Для MTF:

```text
1h candle closed
5m candle closed
1m candle closed
```

Каждая стратегия получает только те бары, которые уже завершены.

# 6. Runtime strategy

Стратегия в live должна быть тем же plugin, что и в Warehouse.

Она получает:

```python
StrategyContext(
    figi=...,
    timeframe=...,
    closed_candles=...,
    indicators=...,
    current_position=...,
    session=...,
    market_state=...
)
```

Возвращает:

```json
{
  "signal_id": "sig_live_001",
  "figi": "SBER",
  "side": "BUY",
  "signal_time": "...",
  "strategy_id": "rsi_reversal",
  "strategy_version": "1.0.0",
  "features": {...},
  "reason": "rsi_turn_up"
}
```

Live signal должен иметь idempotency key:

```text
(figi, strategy_version, timeframe, candle_close_time, config_version)
```

Если поток прислал одну свечу дважды, второй сигнал не должен создать вторую заявку.

# 7. Signal policy в live

После raw signal применяется та же policy, что в Lab:

```text
raw signal
→ same-side check
→ pending opposite
→ min hold
→ cooldown
→ session cutoff
→ risk check
→ order intent
```

Пример:

```text
Raw: BUY
Current position: LONG
Decision: IGNORE_SAME_SIDE
No order
```

Или:

```text
Raw: SELL
Current position: LONG
Decision: PENDING_OPPOSITE
No order yet
```

В UI это должно быть видно в реальном времени.

# 8. Position engine

Position engine должен быть единым с Lab.

Состояния:

```text
FLAT
LONG
SHORT
```

Но в live нужно добавить операционные состояния:

```text
FLAT
ENTRY_PENDING
LONG
EXIT_PENDING
SHORT
FLIP_PENDING
RECONCILIATION_REQUIRED
UNKNOWN
```

`UNKNOWN` — критическое состояние. Если локальная система не знает, есть ли позиция у брокера, бот не должен отправлять новые заявки.

## Жизненный цикл

```text
FLAT
  ↓ signal accepted
ENTRY_PENDING
  ↓ broker fill
LONG
  ↓ exit decision
EXIT_PENDING
  ↓ broker fill
FLAT
```

Для flip:

```text
LONG
  ↓ confirmed opposite
EXIT_PENDING
  ↓ long closed
FLIP_PENDING
  ↓ risk check
ENTRY_PENDING
  ↓ short filled
SHORT
```

Нельзя считать flip завершённым только после отправки заявки. Он завершён только после подтверждённого fill.

# 9. Order Manager

Нужен отдельный Order Management System.

Он хранит:

```text
order intent
broker order
broker status
fills
cancel/replace
retries
errors
```

Статусы заявки:

```text
CREATED
RISK_CHECKED
SUBMITTED
ACKNOWLEDGED
PARTIALLY_FILLED
FILLED
CANCEL_REQUESTED
CANCELLED
REJECTED
STALE
UNKNOWN
```

## Важные правила

- каждая заявка имеет client_order_id;
- повторная отправка использует idempotency key;
- частичное исполнение учитывается;
- rejected order не считается исполненной сделкой;
- network timeout не означает, что заявки нет;
- после timeout нужно запросить статус у брокера;
- нельзя отправлять новую заявку, пока неизвестен статус старой.

Для production-бота важны подтверждение исполнения и reconciliation после reconnect/restart; локальное состояние нельзя считать истинным без сверки с брокером. [1098][1103]

# 10. Execution adapter

Должны быть разные адаптеры:

```text
HistoricalExecutionAdapter
PaperExecutionAdapter
BrokerExecutionAdapter
```

Они реализуют один интерфейс:

```python
class ExecutionAdapter(Protocol):
    async def submit_order(...)
    async def cancel_order(...)
    async def get_order_status(...)
    async def get_fills(...)
    async def get_positions(...)
```

Различаться должны:

- источник fill;
- задержки;
- частичные исполнения;
- реальные ошибки;
- broker API.

Не переписывать position logic для paper и live.

# 11. Pre-trade risk engine

Риск проверяется до отправки заявки брокеру. Системы algorithmic trading обычно используют лимиты капитала, максимальный размер заявки/позиции, price collars, лимиты частоты и защиту от повторных заявок. [1093][1097]

Проверки:

```text
1. Стратегия активна?
2. FIGI разрешён?
3. Session разрешена?
4. Данные свежие?
5. Цена корректна?
6. Qty допустим?
7. Notional не превышен?
8. Position limit не превышен?
9. Portfolio exposure не превышен?
10. Daily loss limit не превышен?
11. Spread допустим?
12. Order rate не превышен?
13. Нет duplicate order?
14. Buying power достаточна?
15. Short разрешён?
16. Price collar пройден?
```

При отказе:

```json
{
  "decision": "REJECT",
  "reason_code": "MAX_POSITION_NOTIONAL",
  "details": {
    "requested": 100000,
    "allowed": 50000
  }
}
```

# 12. Position sizing

Размер позиции — отдельный модуль.

Варианты:

### Fixed quantity

```text
qty = 1
```

### Fixed cash

```text
notional = 10000 ₽
```

### Risk-based

```text
risk_per_trade = 0.5% equity
qty = risk_amount / distance_to_stop
```

### Volatility-adjusted

```text
qty decreases when ATR increases
```

На первом этапе для paper лучше:

```text
fixed qty
```

Пока не подтверждены стратегия и execution, не нужно усложнять sizing.

# 13. Exit manager в live

Exit manager должен работать независимо от появления нового сигнала.

Он проверяет:

- protective stop;
- target;
- trailing;
- opposite confirmation;
- session close;
- max hold;
- risk exit;
- emergency exit.

Приоритеты должны быть определены заранее.

Пример:

```text
1. emergency/system exit
2. protective stop
3. session close
4. target
5. trailing
6. confirmed signal exit
```

Но точный порядок должен совпадать с Lab и быть записан в engine policy.

# 14. Paper mode

Paper — это не просто Lab на сегодняшнем дне.

Paper должен работать в реальном времени:

```text
live market data
→ same strategy runtime
→ same signal policy
→ same risk engine
→ paper execution adapter
→ paper ledger
```

Paper должен моделировать:

- задержку;
- next open или доступную цену;
- spread;
- slippage;
- partial fills;
- rejected orders;
- broker response delay;
- stale data;
- reconnect.

Сравнивать:

```text
paper expected fill
vs
broker observed market prices
```

# 15. Reconciliation

После запуска:

1. Получить broker positions.
2. Получить open orders.
3. Сравнить с локальным state.
4. Если расхождение:
   - остановить новые заявки;
   - выставить `RECONCILIATION_REQUIRED`;
   - показать alert;
   - предложить исправление;
   - только после подтверждения продолжить.

После restart:

```text
local state restored
→ broker state fetched
→ positions matched
→ orders matched
→ state marked RECONCILED
```

Нельзя автоматически предполагать, что локальная БД правильная.

# 16. Circuit breakers

Нужны уровни остановки.

## Strategy-level

- слишком много сигналов;
- слишком много rejected;
- слишком частые flips;
- anomalous indicator;
- volatility spike.

## Instrument-level

- spread too wide;
- stale data;
- trading halt;
- price jump;
- too many stop-outs;
- duplicate signals.

## Portfolio-level

- daily loss limit;
- intraday drawdown;
- max gross exposure;
- max number positions;
- concentration limit;
- too many correlated positions.

## System-level

- broker disconnected;
- market data disconnected;
- order status unknown;
- event loop failure;
- database unavailable;
- clock drift;
- repeated API errors.

Действия:

```text
PAUSE_NEW_ENTRIES
CANCEL_PENDING
CLOSE_POSITIONS
SWITCH_TO_PAPER
FULL_KILL_SWITCH
```

# 17. Session lifecycle

Бот должен иметь явные состояния сессии:

```text
PRE_MARKET
OPENING
TRADING
CLOSING_SOON
SESSION_CLOSE
POST_MARKET
ERROR
```

Для каждой сессии:

```text
allowed entries
allowed exits
force close
universe refresh
reconciliation
```

Пример:

```text
PRE_MARKET:
  load universe
  validate data
  reconcile broker
  prepare strategies

OPENING:
  optional entry restriction

TRADING:
  normal operation

CLOSING_SOON:
  no new entries
  manage exits

SESSION_CLOSE:
  close intraday positions
  cancel orders
  save report

POST_MARKET:
  calculate daily metrics
```

# 18. Первичный подбор акций перед сессией

Предлагаемый процесс:

```text
1. Load allowed universe
2. Query instrument metadata
3. Check trading status
4. Load latest candles
5. Validate freshness
6. Calculate volatility/liquidity score
7. Apply blacklist/whitelist
8. Select top-N
9. Save universe snapshot
10. Subscribe to market data
11. Warm up strategies
12. Mark bot READY
```

Snapshot:

```json
{
  "selection_id": "universe_20260822",
  "created_at": "...",
  "method": "vol_liquidity_v1",
  "figis": [
    {
      "figi": "BBG004730N88",
      "ticker": "SBER",
      "score": 82,
      "volatility": 1.8,
      "liquidity": 91,
      "status": "ELIGIBLE"
    }
  ]
}
```

Если universe обновляется во время сессии, не надо мгновенно закрывать позиции по исключённым акциям. Отдельно определить:

```text
new entries disabled
existing positions managed normally
```

# 19. Интерфейс бота

Интерфейс должен быть не просто «Start/Stop».

## Главный dashboard

Верхняя строка:

```text
BOT STATUS: RUNNING
MODE: PAPER
SESSION: TRADING
DATA: HEALTHY
BROKER: CONNECTED
RECONCILIATION: PASS
RISK: NORMAL
```

Цвета:

- green — healthy;
- yellow — warning;
- red — blocked/error;
- gray — inactive.

## Карточка конфигурации

```text
Bot config: cfg_001
Strategy: RSI + Bollinger + VWAP
Engine: v1
Cost model: canonical_v1
Universe: 8/14 selected
```

Read-only после запуска.

## Universe panel

Таблица:

| Ticker | Score | ATR | Liquidity | Spread | Status | Position |
|---|---:|---:|---:|---:|---|---|
| SBER | 82 | 1.8% | high | 4 bps | ELIGIBLE | LONG |
| GAZP | 76 | 1.4% | high | 5 bps | ELIGIBLE | FLAT |
| CHMF | 35 | 2.8% | low | 18 bps | BLOCKED | FLAT |

## Live positions

| Ticker | Side | Qty | Entry | Current | Unrealized | Stop | Target | State |
|---|---|---:|---:|---:|---:|---:|---:|---|
| SBER | LONG | 1 | 324.30 | 325.10 | +0.80 | 323.70 | 325.50 | OPEN |

## Orders

Показывать полный lifecycle:

```text
CREATED
RISK_CHECKED
SUBMITTED
ACKNOWLEDGED
PARTIALLY_FILLED
FILLED
```

Отдельно отображать rejected/cancelled/stale.

## Signal stream

```text
10:00 SBER BUY RSI
10:00 quorum 2/3 ACCEPT
10:00 risk PASS
10:01 order submitted
10:01 filled
```

Для rejected:

```text
10:15 GAZP BUY
REJECTED: SPREAD_TOO_WIDE
```

## P&L

Показывать:

- realized P&L;
- unrealized P&L;
- total equity;
- today P&L;
- drawdown;
- commissions;
- slippage;
- exposure;
- margin/buying power.

## Risk panel

```text
Daily loss limit: 1000 ₽
Used: 220 ₽
Remaining: 780 ₽

Max exposure:
Used 28%
Max 50%

Open positions:
4 / 8

Status:
NORMAL
```

## Kill switch

Кнопки:

```text
[Pause new entries]
[Cancel pending orders]
[Close all positions]
[Emergency stop]
```

`Emergency stop` должен требовать подтверждение и показывать точное действие.

# 20. Audit log

Каждое решение сохраняется:

```text
timestamp
component
event_type
figi
config_id
strategy_version
position_state
order_id
decision
reason
payload
```

Примеры:

```text
SIGNAL_CREATED
SIGNAL_REJECTED
RISK_CHECK_PASSED
RISK_CHECK_FAILED
ORDER_SUBMITTED
ORDER_ACKNOWLEDGED
ORDER_PARTIALLY_FILLED
ORDER_FILLED
ORDER_REJECTED
POSITION_OPENED
POSITION_CLOSED
RECONCILIATION_STARTED
RECONCILIATION_FAILED
CIRCUIT_BREAKER_TRIGGERED
```

# 21. API бота

## Control

```http
GET  /api/v1/bot/status
POST /api/v1/bot/start
POST /api/v1/bot/pause
POST /api/v1/bot/stop
POST /api/v1/bot/kill
```

## Configuration

```http
GET  /api/v1/bot/config
POST /api/v1/bot/configure
POST /api/v1/bot/config/{id}/activate
POST /api/v1/bot/config/{id}/deactivate
```

## Universe

```http
GET  /api/v1/bot/universe
POST /api/v1/bot/universe/refresh
GET  /api/v1/bot/universe/snapshot
```

## Positions

```http
GET /api/v1/bot/positions
GET /api/v1/bot/positions/{figi}
POST /api/v1/bot/positions/{figi}/close
POST /api/v1/bot/positions/close-all
```

## Orders

```http
GET /api/v1/bot/orders
GET /api/v1/bot/orders/{id}
POST /api/v1/bot/orders/{id}/cancel
```

## Events/monitoring

```http
GET /api/v1/bot/events
GET /api/v1/bot/health
GET /api/v1/bot/risk
WS  /api/v1/ws/bot
```

# 22. Paper → live переход

Переход должен быть ступенчатым:

```text
RESEARCH
  ↓
DESIGN_PASS
  ↓
VAL_PASS
  ↓
PAPER_CONFIGURED
  ↓
PAPER_RUNNING
  ↓
PAPER_RECONCILIATION_PASS
  ↓
MANUAL_APPROVAL
  ↓
LIVE_SMALL_SIZE
  ↓
LIVE_LIMITED
  ↓
LIVE
```

Для live-small-size:

- qty минимальный;
- ограниченный universe;
- дневной loss cap;
- ручной мониторинг;
- автоматический kill switch.

# 23. Что считать одинаковым с Lab

Одинаковыми должны быть:

- strategy version;
- signal logic;
- MTF timing;
- signal policy;
- position state;
- exit policy;
- session rules;
- stop/target/trailing calculation;
- order intent semantics;
- P&L formula;
- reasons.

Различаться могут:

- historical fill vs broker fill;
- perfect candle availability vs live gaps;
- simulated slippage vs actual slippage;
- immediate historical result vs delayed live order;
- partial fills;
- broker rejects.

# 24. MVP для бота

Не нужно сразу строить весь live-сервис.

### Этап 1: paper replay

- реальный поток свечей;
- frozen configuration;
- одна позиция;
- paper fill;
- orders;
- ledger;
- dashboard.

### Этап 2: broker read-only

- account;
- positions;
- orders;
- balances;
- reconciliation;
- без отправки заявок.

### Этап 3: sandbox execution

- реальный broker adapter;
- минимальный размер;
- order lifecycle;
- kill switch;
- ручное подтверждение.

### Этап 4: live small size

- одна стратегия;
- ограниченный universe;
- минимальный qty;
- жёсткие risk limits.

# Итоговая структура

```text
Warehouse
  └── собирает конфигурации

Lab
  └── проверяет конфигурации на истории

Paper Bot
  └── выполняет конфигурацию в real-time без денег

Live Bot
  └── выполняет ту же конфигурацию через broker adapter
```

Бот должен быть не независимой новой системой, а последним исполнителем уже проверенной конфигурации. Его главные особенности — не новые индикаторы, а operational correctness:

- свежие данные;
- корректное состояние позиции;
- жизненный цикл заявки;
- reconciliation;
- pre-trade risk;
- circuit breakers;
- session control;
- audit;
- понятный интерфейс.

Именно эти механизмы защищают от ситуации, когда стратегия дала сигнал один раз, а система из-за reconnect, duplicate event или неизвестного order status отправила несколько заявок.