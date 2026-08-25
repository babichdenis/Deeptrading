Ниже — подробная спецификация механизма выставления сделки. Её можно использовать как отдельное ТЗ для разработки.

# Механизм выставления сделки

## 1. Главный принцип

Сигнал стратегии **не является заявкой** и тем более не является исполненной сделкой.

Полная цепочка:

```text
Свеча закрылась
  ↓
Стратегия создала сигнал
  ↓
Signal policy разрешила сигнал
  ↓
Position engine создал Order Intent
  ↓
Risk engine проверил намерение
  ↓
Order manager создал заявку
  ↓
Broker adapter отправил заявку брокеру
  ↓
Брокер подтвердил или отклонил заявку
  ↓
Произошло полное/частичное исполнение
  ↓
Position engine обновил позицию
  ↓
Trade ledger записал результат
```

Нельзя после появления `BUY` сразу считать, что позиция открыта. Позиция открывается только после подтверждённого fill.

Жизненный цикл заявки должен отдельно учитывать `New`, `Partially Filled`, `Filled`, `Canceled`, `Rejected` и промежуточные состояния; подтверждение заявки не равно её исполнению. [1108][1109]

***

# 2. Участники механизма

## 2.1. Strategy runtime

Создаёт raw signal:

```json
{
  "signal_id": "sig_123",
  "figi": "BBG004730N88",
  "side": "BUY",
  "signal_time": "2026-08-24T10:00:00Z",
  "strategy_id": "rsi_reversal",
  "strategy_version": "1.0.0",
  "reason": "rsi_turn_up",
  "features": {
    "rsi": 31.2
  }
}
```

Стратегия не создаёт заявку.

## 2.2. Signal policy

Решает, что делать с raw signal:

```text
ACCEPT
IGNORE_SAME_SIDE
PENDING_OPPOSITE
REJECT_MIN_HOLD
REJECT_COOLDOWN
REJECT_SESSION_CUTOFF
REJECT_NO_NEXT_BAR
```

Пример:

```text
BUY signal
position = LONG
decision = IGNORE_SAME_SIDE
```

Другой пример:

```text
SELL signal
position = LONG
decision = PENDING_OPPOSITE
```

## 2.3. Position engine

Хранит фактическое логическое состояние:

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

Для backtest можно использовать упрощённые:

```text
FLAT
LONG
SHORT
```

Но в paper/live нужны операционные состояния, потому что между созданием заявки и её исполнением проходит время.

## 2.4. Risk engine

Проверяет, можно ли отправлять заявку.

## 2.5. Order manager

Создаёт заявку, отправляет брокеру, отслеживает статус, обрабатывает partial fill, cancel, reject и timeout.

## 2.6. Broker adapter

Единый интерфейс для:

```text
Historical adapter
Paper adapter
Sandbox broker adapter
Live broker adapter
```

Position engine не должен знать детали API конкретного брокера.

***

# 3. Order Intent

Перед настоящей заявкой создаётся внутреннее намерение:

```json
{
  "intent_id": "intent_123",
  "figi": "BBG004730N88",
  "action": "OPEN",
  "side": "BUY",
  "requested_qty": 1,
  "source_signal_id": "sig_123",
  "position_state": "FLAT",
  "created_at": "2026-08-24T10:00:00Z",
  "execution_rule": "NEXT_AVAILABLE_OPEN",
  "config_version": "cfg_001",
  "idempotency_key": "cfg_001:SBER:10:00:OPEN:BUY"
}
```

Типы intent:

```text
OPEN
CLOSE
FLIP
MODIFY_STOP
MODIFY_TARGET
CANCEL_ORDER
```

### Почему нужен Intent

Он отделяет:

```text
стратегия решила войти
```

от:

```text
риск разрешил
```

и от:

```text
брокер исполнил
```

Если intent уже создан, повторное событие не должно создать дубликат заявки.

***

# 4. Начало сделки

## 4.1. Условия для открытия

До создания заявки проверить:

```text
position_state == FLAT
signal accepted
FIGI eligible
session open
data fresh
price valid
next bar available
risk limits pass
no unresolved order
qty valid
buying power sufficient
spread acceptable
```

Если хотя бы одно условие не выполнено:

```text
OPEN REJECTED
```

с точным кодом причины.

## 4.2. Пример long

```text
10:00 — RSI создал BUY
10:00 — quorum 2/3
10:00 — position = FLAT
10:00 — risk checks PASS
10:00 — create OPEN BUY intent
10:01 — submit order
10:01 — broker ACK
10:01 — FILLED
10:01 — position = LONG
```

## 4.3. Вход по следующему open

Если сигнал сформирован на закрытии свечи в 10:00:

```text
signal_time = 10:00
entry_time = next available open
```

Для 5m + 1m:

```text
signal on 5m close at 10:00
entry on first available 1m open after 10:00
```

Нельзя использовать цену close сигнальной свечи, если это не предусмотрено отдельной execution policy.

***

# 5. Проверки перед заявкой

Risk gate должен быть отдельным обязательным этапом.

## 5.1. Instrument checks

```text
FIGI exists
instrument active
trading status allowed
market open
short available, если SELL opening
```

## 5.2. Market data checks

```text
last price fresh
last candle timestamp within tolerance
no data gap
spread below max
price not zero/negative
```

## 5.3. Position checks

```text
no existing position
no pending entry
no pending exit
no unknown broker state
no duplicate intent
```

## 5.4. Portfolio checks

```text
max open positions
max notional
max exposure per FIGI
max sector exposure
max long exposure
max short exposure
buying power
margin
daily loss limit
drawdown limit
```

## 5.5. Order checks

```text
qty > 0
qty matches lot size
price matches tick size
notional within limit
slippage estimate acceptable
order type allowed
price collar passed
```

Результат:

```json
{
  "risk_decision": "PASS",
  "checks": {
    "instrument": "PASS",
    "data_fresh": "PASS",
    "position_limit": "PASS",
    "daily_loss": "PASS",
    "spread": "PASS"
  }
}
```

При отказе:

```json
{
  "risk_decision": "REJECT",
  "reason_code": "SPREAD_TOO_WIDE",
  "details": {
    "current_spread_bps": 18.4,
    "max_spread_bps": 10
  }
}
```

***

# 6. Формирование брокерской заявки

После risk PASS создаётся объект Order.

```json
{
  "order_id": "ord_123",
  "intent_id": "intent_123",
  "client_order_id": "bot-cfg001-sber-20260824-1001-buy-001",
  "broker_order_id": null,
  "figi": "BBG004730N88",
  "side": "BUY",
  "purpose": "OPEN",
  "quantity": 1,
  "filled_quantity": 0,
  "remaining_quantity": 1,
  "order_type": "MARKET",
  "limit_price": null,
  "status": "CREATED",
  "created_at": "2026-08-24T10:00:05Z"
}
```

Для limit order:

```json
{
  "order_type": "LIMIT",
  "limit_price": 324.30,
  "time_in_force": "DAY"
}
```

Для market order:

```json
{
  "order_type": "MARKET"
}
```

Тип заявки должен быть частью конфигурации, а не скрытой логикой adapter-а.

***

# 7. Order lifecycle

## Основные состояния

```text
CREATED
  ↓
RISK_CHECKED
  ↓
SUBMITTING
  ↓
SUBMITTED
  ↓
ACKNOWLEDGED / NEW
  ↓
PARTIALLY_FILLED
  ↓
FILLED
```

Terminal states:

```text
FILLED
CANCELED
REJECTED
EXPIRED
```

Дополнительные:

```text
PENDING_CANCEL
PENDING_REPLACE
UNKNOWN
STALE
```

### Важно

`ACKNOWLEDGED` означает:

> брокер принял заявку в обработку.

Это не означает:

> сделка уже совершена.

Заявка может быть отклонена после предварительного подтверждения, поэтому состояние нельзя считать окончательным до terminal event. [1108][1114]

## Пример переходов

```text
CREATED
→ RISK_CHECKED
→ SUBMITTED
→ ACKNOWLEDGED
→ FILLED
```

Отказ:

```text
CREATED
→ RISK_CHECKED
→ SUBMITTED
→ REJECTED
```

Частичное исполнение:

```text
SUBMITTED
→ PARTIALLY_FILLED
→ PARTIALLY_FILLED
→ FILLED
```

Отмена:

```text
NEW
→ PENDING_CANCEL
→ CANCELED
```

В период `PENDING_CANCEL` заявка ещё может получить fill. Нельзя считать её отменённой сразу после отправки cancel request. [1116]

***

# 8. Partial fill

Даже если на первом этапе используется `qty=1`, механизм должен поддерживать partial fill.

Пример:

```text
requested_qty = 100
fill 1: 30
fill 2: 40
fill 3: 30
```

Состояние:

```text
filled_quantity = 70
remaining_quantity = 30
status = PARTIALLY_FILLED
```

После каждого fill:

1. записать `Fill`;
2. обновить среднюю цену;
3. обновить позицию;
4. пересчитать остаточный риск;
5. обновить стоп/target для фактически исполненной позиции;
6. проверить, разрешён ли остаток;
7. обработать отмену остатка при необходимости.

### Частичный вход

Если вход исполнен частично:

```text
позиция существует в размере filled_quantity
```

Нельзя считать, что исполнился весь размер.

### Частичный выход

Если закрытие исполнилось частично:

```text
позиция остаётся открытой на remaining_quantity
```

Позиция не переходит в FLAT до полного закрытия.

***

# 9. Обновление позиции после fill

Каждый fill должен быть отдельным объектом:

```json
{
  "fill_id": "fill_001",
  "order_id": "ord_123",
  "figi": "BBG004730N88",
  "side": "BUY",
  "quantity": 1,
  "price": 324.30,
  "commission": 0.16,
  "slippage": 0.02,
  "fill_time": "2026-08-24T10:01:02Z"
}
```

После fill:

```text
position.quantity += fill.quantity
position.avg_entry_price = weighted_average(...)
position.status = LONG
position.opened_at = first_fill_time
```

Для выхода:

```text
realized_gross_pnl = exit_value - entry_value
realized_net_pnl = gross - commission - slippage - fees
```

Для short — зеркальная формула.

***

# 10. Выставление стопа и target

После подтверждения входа бот должен создать protective levels.

## Вариант A: стоп внутри движка

Бот получает live price stream и сам принимает решение о выходе.

```text
position LONG
stop_level = 323.70
last_price <= 323.70
→ create CLOSE SELL intent
```

## Вариант B: защитная заявка у брокера

После fill бот выставляет broker-side stop/stop-limit order.

Преимущество:

- защита может сработать при проблеме приложения.

Недостатки:

- нужно отслеживать статус;
- возможны stop rejection;
- нужно отменять/обновлять связанные заявки;
- требуется OCO/bracket logic, если поддерживается.

На первом этапе лучше явно выбрать одну модель. Нельзя молча использовать разные правила в Lab, paper и live.

## Stop/target lifecycle

```text
ENTRY FILLED
  ↓
CREATE PROTECTIVE STOP
  ↓
STOP ACKNOWLEDGED
  ↓
CREATE TARGET, если используется
  ↓
POSITION PROTECTED
```

Если stop не выставился:

```text
POSITION_UNPROTECTED
```

Действие:

```text
pause new entries
retry/correct
or close position immediately
```

# 11. Exit и закрытие позиции

Закрытие может быть вызвано:

```text
PROTECTIVE_STOP
TAKE_PROFIT
TRAILING_STOP
OPPOSITE_SIGNAL
CONFIRMED_FLIP
SESSION_CLOSE
TIME_STOP
RISK_EXIT
EMERGENCY_EXIT
DATA_GAP_EXIT
```

## Последовательность

```text
Exit decision
  ↓
Create CLOSE intent
  ↓
Risk validation for close
  ↓
Create closing order
  ↓
Submit to broker
  ↓
Track fill
  ↓
Update remaining position
  ↓
Cancel linked stop/target
  ↓
Position FLAT after full close
  ↓
Write completed trade
```

## Приоритет закрытия

Утвердить один порядок:

```text
1. Emergency exit
2. Protective stop
3. Broker/market risk exit
4. Session close
5. Target
6. Trailing
7. Confirmed signal exit
8. Time stop
```

Но порядок должен совпадать с Lab или быть явно описан как live-specific execution policy.

***

# 12. Flip

Flip — это не одна магическая заявка.

Правильная цепочка:

```text
LONG
  ↓ confirmed SELL
CLOSE LONG intent
  ↓
close order FILLED
  ↓
risk check for SHORT
  ↓
OPEN SHORT intent
  ↓
short order FILLED
  ↓
SHORT
```

Нельзя открыть short, пока long ещё не закрыт, если конфигурация не поддерживает hedge/netting.

Если закрытие long исполнилось частично:

```text
не открывать полный short
```

Сначала:

- завершить close;
- определить фактически свободный capital;
- пересчитать size short;
- пройти risk checks;
- только потом открыть short.

Если close failed:

```text
state = LONG
flip = BLOCKED
alert
```

***

# 13. Что делать при проблемах

## Timeout при отправке

Ситуация:

```text
бот отправил заявку
ответ не пришёл
```

Нельзя автоматически повторять заявку.

Правильно:

```text
order = UNKNOWN
pause duplicate submissions
query broker by client_order_id
query open orders
query recent fills
reconcile
```

## Rejected

```text
order status = REJECTED
```

Действия:

- записать broker reason;
- перевести intent в failed;
- не считать trade;
- решить, можно ли повторить;
- повтор разрешать только для retryable errors;
- ограничить число retries.

## Partially filled

- обновить позицию на фактический размер;
- отменить остаток, если policy требует;
- не отправлять новую заявку без учёта уже исполненной части.

## Broker disconnected

```text
pause new entries
keep monitoring local positions if safe
reconnect
query broker positions/orders/fills
reconcile
```

## Local restart

После старта:

```text
load local state
fetch broker state
compare
resolve mismatch
set READY only after reconciliation
```

# 14. Reconciliation

Reconciliation — обязательный процесс.

Сравнивать:

```text
local positions vs broker positions
local orders vs broker open orders
local fills vs broker operations
local cash vs broker balance
```

Результаты:

```text
RECONCILED
MISMATCH_POSITION
MISMATCH_ORDER
MISSING_FILL
UNKNOWN_STATE
```

При mismatch:

```text
бот не открывает новые сделки
```

Пользователь видит:

```text
RECONCILIATION_REQUIRED
```

# 15. Idempotency

Все операции должны быть идемпотентными.

## Для signal

```text
signal_key =
config_version + figi + timeframe + candle_close_time + strategy_version
```

## Для intent

```text
intent_key =
position_id + action + decision_time + reason
```

## Для order

```text
client_order_id =
bot_id + config_version + intent_id
```

Если сеть повторила запрос, система должна вернуть существующий order, а не создать новый.

# 16. Пример полной сделки

```text
10:00
5m candle closed

10:00:00
Strategy:
RSI BUY

10:00:01
Signal policy:
ACCEPT

10:00:01
Position:
FLAT

10:00:02
Risk:
PASS

10:00:02
Intent:
OPEN LONG qty=1

10:00:03
Order:
SUBMITTED

10:00:04
Broker:
ACKNOWLEDGED

10:01:02
Fill:
BUY 1 @ 324.30

10:01:03
Position:
LONG qty=1

10:01:04
Protective stop:
323.70

11:05:00
Target touched

11:05:01
Intent:
CLOSE LONG

11:05:02
Order:
SUBMITTED

11:05:05
Fill:
SELL 1 @ 325.50

11:05:06
Position:
FLAT

11:05:07
Trade:
net +0.84 ₽
```

# 17. Механизм для интерфейса

## Live order timeline

Для каждой заявки показывать:

```text
Signal created
Decision accepted
Risk passed
Intent created
Order submitted
Broker acknowledged
Partial fill
Full fill
Position updated
Stop placed
Exit triggered
Closed
```

## Список текущих заявок

| ID | FIGI | Purpose | Side | Qty | Filled | Price | Status |
|---|---|---|---|---:|---:|---:|---|
| 123 | SBER | OPEN | BUY | 1 | 1 | 324.30 | FILLED |
| 124 | SBER | STOP | SELL | 1 | 0 | 323.70 | NEW |

## Позиции

| Ticker | Side | Qty | Avg entry | Current | Unrealized | Stop | Target | Status |
|---|---|---:|---:|---:|---:|---:|---:|---|
| SBER | LONG | 1 | 324.30 | 325.10 | +0.80 | 323.70 | 325.50 | PROTECTED |

## Ошибки

```text
10:12 GAZP
Order rejected
reason: INSUFFICIENT_BUYING_POWER
action: no retry
```

# 18. API

## Intents

```http
GET /api/v1/bot/intents
GET /api/v1/bot/intents/{id}
```

## Orders

```http
GET /api/v1/bot/orders
GET /api/v1/bot/orders/{id}
POST /api/v1/bot/orders/{id}/cancel
```

## Fills

```http
GET /api/v1/bot/fills
GET /api/v1/bot/fills/{id}
```

## Positions

```http
GET /api/v1/bot/positions
GET /api/v1/bot/positions/{figi}
POST /api/v1/bot/positions/{figi}/close
POST /api/v1/bot/positions/close-all
```

## Risk

```http
GET /api/v1/bot/risk/checks
GET /api/v1/bot/risk/limits
POST /api/v1/bot/risk/pause
```

## Reconciliation

```http
POST /api/v1/bot/reconciliation/run
GET /api/v1/bot/reconciliation/status
POST /api/v1/bot/reconciliation/{id}/resolve
```

# 19. Минимальная база данных

## `order_intents`

```text
id
bot_config_id
figi
action
side
qty
source_signal_id
position_id
status
reason
idempotency_key
created_at
```

## `orders`

```text
id
intent_id
client_order_id
broker_order_id
figi
side
purpose
order_type
requested_qty
filled_qty
remaining_qty
limit_price
status
submitted_at
updated_at
error_code
error_message
```

## `fills`

```text
id
order_id
broker_fill_id
figi
side
qty
price
commission
slippage
fill_time
```

## `positions`

```text
id
bot_config_id
figi
side
qty
avg_entry_price
realized_pnl
unrealized_pnl
stop_level
target_level
status
opened_at
updated_at
```

## `reconciliation_events`

```text
id
time
category
local_value
broker_value
resolution
status
details
```

# 20. Что одинаково с Lab

Должно быть одинаковым:

- signal timing;
- signal policy;
- position state;
- entry intent;
- exit reasons;
- stop/target/trailing formulas;
- session rules;
- P&L formula;
- quantity rules;
- config version;
- engine version.

Различаться может:

- historical fill;
- paper fill;
- real broker fill;
- partial fills;
- latency;
- rejected orders;
- actual slippage.

# 21. Порядок реализации

## Этап 1. Paper order lifecycle

- signal;
- intent;
- risk;
- order;
- fill;
- position;
- ledger;
- UI timeline.

## Этап 2. Broker read-only

- positions;
- orders;
- fills;
- balances;
- reconciliation.

## Этап 3. Sandbox

- submit order;
- cancel;
- partial fills;
- reject;
- reconnect;
- kill switch.

## Этап 4. Live small size

- frozen config;
- one strategy;
- limited universe;
- fixed qty;
- strict daily loss;
- manual monitoring.

# Главная формула

```text
Signal ≠ Intent
Intent ≠ Order
Order ≠ Fill
Fill ≠ Completed Trade
```

Именно это должно быть центральным принципом механизма выставления сделок.

Полная цепочка:

```text
Signal
→ Decision
→ Intent
→ Risk PASS
→ Order
→ Broker ACK
→ Fill
→ Position
→ Exit Order
→ Exit Fill
→ Trade Ledger
→ P&L
```

В Lab одна сделка должна быть не просто строкой `entry → exit`, а **полным воспроизводимым объектом**, который связывает сигнал, конфигурацию, решения, исполнение, выход и финансовый результат.

Главный принцип:

```text
Lab получает конфигурацию и данные
→ принимает решение только по доступной информации
→ моделирует заявку и fill
→ ведёт состояние позиции
→ закрывает позицию
→ считает costs и P&L
→ выдаёт полный trade record и объяснение
```

Для воспроизводимости нужно сохранять не только дату, сторону, цену и P&L, но и версии данных, конфигурации, execution assumptions, costs, risk constraints и все промежуточные решения. Это соответствует практике audit-ready backtesting. [1123][1127][1128]

# 1. Что Lab должен получить до сделки

Есть два уровня входных данных:

1. **Вход эксперимента** — конфигурация всего запуска.
2. **Вход конкретной сделки** — состояние рынка, сигнал и параметры на момент решения.

Нельзя смешивать эти уровни.

# 2. Вход эксперимента

До начала Lab должен получить immutable experiment snapshot.

## 2.1. Идентификация эксперимента

```json
{
  "experiment_id": "exp_001",
  "experiment_version": "1.0.0",
  "purpose": "DESIGN",
  "created_at": "2026-08-24T04:00:00Z",
  "parent_configuration_id": "cfg_001",
  "parent_configuration_version": "1.0.0"
}
```

## 2.2. Данные

```json
{
  "dataset_id": "warehouse_design_v1",
  "dataset_version": "v1",
  "data_hash": "sha256:...",
  "source": "warehouse",
  "from_ts": "2026-06-15T00:00:00Z",
  "to_ts": "2026-07-14T23:59:59Z",
  "timezone": "Europe/Moscow",
  "figis": [
    "BBG004730N88",
    "BBG004730RP0"
  ],
  "signal_timeframes": {
    "bias": "1h",
    "setup": "5m",
    "entry": "1m"
  },
  "execution_timeframe": "1m"
}
```

Нужно сохранить:

- dataset id;
- version;
- hash;
- период;
- timezone;
- FIGI;
- timeframe;
- availability;
- правила очистки;
- правила gaps;
- данные, использованные для warmup.

Если свечи позже изменятся, старый experiment не должен тихо пересчитаться на новых данных.

## 2.3. Стратегия и signals

Lab получает ссылки на Warehouse runs:

```json
{
  "signal_sources": [
    {
      "run_id": "run_bias_001",
      "role": "BIAS",
      "strategy_id": "ema_trend",
      "strategy_version": "1.0.0",
      "timeframe": "1h",
      "params_hash": "a91f22d8"
    },
    {
      "run_id": "run_setup_001",
      "role": "SETUP",
      "strategy_id": "pullback_ema",
      "strategy_version": "1.0.0",
      "timeframe": "5m",
      "params_hash": "c7221d11"
    },
    {
      "run_id": "run_entry_001",
      "role": "ENTRY",
      "strategy_id": "micro_breakout",
      "strategy_version": "1.0.0",
      "timeframe": "1m",
      "params_hash": "f121ab80"
    }
  ]
}
```

Lab не должен пересчитывать стратегию, если run уже существует. Он читает сохранённые signals и применяет к ним торговые правила.

## 2.4. Signal policy

```json
{
  "signal_policy": {
    "id": "confirmed_flip",
    "version": "1.0.0",
    "params": {
      "same_side": "IGNORE",
      "opposite": "PENDING_THEN_FLIP",
      "opposite_confirm_bars": 2,
      "min_hold_bars": 3,
      "cooldown_bars": 1,
      "setup_expiry_bars": 6
    }
  }
}
```

## 2.5. Exit policy

```json
{
  "exit_policy": {
    "id": "atr_trailing",
    "version": "1.0.0",
    "params": {
      "atr_period": 14,
      "initial_stop_atr": 2.0,
      "trail_activation_atr": 1.0,
      "trail_distance_atr": 2.0,
      "take_profit": null,
      "session_close": true
    }
  }
}
```

## 2.6. Execution model

```json
{
  "execution": {
    "engine_version": "trade_engine_v1",
    "signal_rule": "CLOSED_BAR_ONLY",
    "entry_rule": "NEXT_AVAILABLE_OPEN",
    "exit_resolution": "1M_INTRABAR",
    "same_bar_conflict_rule": "STOP_LOSS_FIRST",
    "missing_next_bar": "REJECT",
    "gap_rule": "CONSERVATIVE_OPEN_FILL"
  }
}
```

## 2.7. Costs

```json
{
  "cost_model": {
    "id": "canonical_v1",
    "version": "1.0.0",
    "commission_model": "percentage",
    "commission_rate": 0.0005,
    "slippage_model": "fixed_bps",
    "slippage_bps": 2,
    "tick_size": 0.01,
    "lot_size": 1,
    "minimum_commission": 0
  }
}
```

Вместо абстрактной mid-price модели можно использовать bid/ask или отдельный spread/slippage model, если эти данные доступны. Разница между ожидаемой и фактической ценой — часть slippage; spread должен быть виден в отчёте отдельно, если моделируется. [1134][1135]

## 2.8. Position sizing

```json
{
  "position_sizing": {
    "mode": "FIXED_QTY",
    "qty": 1,
    "max_qty": 1,
    "risk_per_trade": null,
    "max_notional": 50000
  }
}
```

## 2.9. Risk and session rules

```json
{
  "risk_policy": {
    "id": "fixed_risk_v1",
    "max_open_positions": 5,
    "max_daily_loss": 1000,
    "max_portfolio_exposure": 0.5,
    "max_figi_exposure": 0.1,
    "allow_short": true
  },
  "session_policy": {
    "id": "moex_intraday_v1",
    "timezone": "Europe/Moscow",
    "force_close": true,
    "entry_cutoff_bars": 6,
    "overnight": false
  }
}
```

# 3. Что Lab получает в момент потенциального входа

Для каждой возможной сделки Lab должен создать `decision snapshot`.

Это снимок состояния системы **до предполагаемого входа**.

## 3.1. Market context

```json
{
  "market_context": {
    "figi": "BBG004730N88",
    "ticker": "SBER",
    "decision_time": "2026-08-24T10:00:00Z",
    "signal_bar_time": "2026-08-24T10:00:00Z",
    "last_closed_bar": {
      "open": 324.10,
      "high": 324.45,
      "low": 323.90,
      "close": 324.30,
      "volume": 125000
    },
    "next_execution_bar": {
      "time": "2026-08-24T10:01:00Z",
      "open": 324.35,
      "available": true
    },
    "session_state": "TRADING",
    "data_quality": "PASS"
  }
}
```

Важно:

- signal time;
- последняя закрытая свеча;
- следующая свеча;
- доступность следующего open;
- session state;
- gaps;
- data quality.

## 3.2. MTF context

Если конфигурация MTF:

```json
{
  "mtf_context": {
    "bias": {
      "timeframe": "1h",
      "source_bar_close": "2026-08-24T10:00:00Z",
      "state": "LONG_ALLOWED",
      "features": {
        "ema20": 323.80,
        "ema50": 322.90,
        "ema50_slope": 0.04
      }
    },
    "setup": {
      "timeframe": "5m",
      "source_bar_close": "2026-08-24T10:00:00Z",
      "state": "SETUP_LONG",
      "setup_age_bars": 2,
      "expires_at": "2026-08-24T10:30:00Z"
    },
    "entry": {
      "timeframe": "1m",
      "state": "ENTRY_LONG",
      "reason": "micro_breakout"
    },
    "alignment": "PASS"
  }
}
```

Это нужно, чтобы после теста можно было ответить:

> Почему сделка была разрешена именно здесь?

## 3.3. Raw signals

```json
{
  "raw_signals": [
    {
      "signal_id": "sig_rsi_1001",
      "strategy_id": "rsi_reversal",
      "side": "BUY",
      "signal_time": "2026-08-24T10:00:00Z",
      "reason": "rsi_turn_up",
      "features": {
        "rsi": 31.2,
        "previous_rsi": 29.8
      }
    },
    {
      "signal_id": "sig_vwap_1002",
      "strategy_id": "vwap_reclaim",
      "side": "BUY",
      "signal_time": "2026-08-24T10:00:00Z",
      "reason": "vwap_reclaim"
    }
  ]
}
```

## 3.4. Quorum

```json
{
  "quorum": {
    "enabled": true,
    "rule": "2_OF_3",
    "window_bars": 0,
    "votes": {
      "BUY": 2,
      "SELL": 0
    },
    "members": [
      "rsi_reversal",
      "vwap_reclaim"
    ],
    "result": "BUY"
  }
}
```

## 3.5. Current position

```json
{
  "position_before_decision": {
    "state": "FLAT",
    "qty": 0,
    "avg_entry_price": null,
    "opened_at": null,
    "bars_held": 0,
    "cooldown_until": null,
    "pending_opposite": null
  }
}
```

Если позиция существует:

```json
{
  "position_before_decision": {
    "state": "LONG",
    "qty": 1,
    "avg_entry_price": 324.30,
    "opened_at": "2026-08-24T09:35:00Z",
    "bars_held": 5,
    "minutes_held": 25,
    "initial_stop": 323.70,
    "target": 325.50,
    "active_trail": null,
    "pending_opposite": null
  }
}
```

## 3.6. Policy decision

```json
{
  "policy_decision": {
    "action": "ACCEPT",
    "reason_code": "QUORUM_PASS",
    "checks": {
      "bias_allowed": true,
      "setup_active": true,
      "entry_confirmed": true,
      "same_side": false,
      "min_hold": true,
      "cooldown": true,
      "session_cutoff": true,
      "next_bar": true
    }
  }
}
```

# 4. Формирование Order Intent

После принятия сигнала создаётся intent.

```json
{
  "order_intent": {
    "intent_id": "intent_001",
    "action": "OPEN",
    "side": "BUY",
    "figi": "BBG004730N88",
    "requested_qty": 1,
    "signal_id": "sig_rsi_1001",
    "decision_time": "2026-08-24T10:00:00Z",
    "execution_time": "2026-08-24T10:01:00Z",
    "execution_price_source": "NEXT_BAR_OPEN",
    "status": "CREATED"
  }
}
```

Если сигнал не принят, intent не создаётся. Но decision всё равно сохраняется.

# 5. Расчёт входа

Lab должен сохранить не только факт входа, но и расчёт параметров.

## 5.1. Fill assumptions

```json
{
  "entry_execution": {
    "bar_time": "2026-08-24T10:01:00Z",
    "bar_open": 324.35,
    "assumed_fill_price": 324.35,
    "price_rule": "NEXT_OPEN",
    "slippage": 0.02,
    "commission": 0.16,
    "effective_entry_price": 324.37,
    "qty": 1,
    "notional": 324.35
  }
}
```

## 5.2. Stop/target calculation

```json
{
  "risk_levels_at_entry": {
    "atr_period": 14,
    "atr_value": 0.40,
    "stop_distance": 0.60,
    "initial_stop": 323.75,
    "risk_per_unit": 0.60,
    "target_mode": "2R",
    "target_distance": 1.20,
    "target": 325.55,
    "trail_enabled": false
  }
}
```

Если вход short — уровни зеркальные.

# 6. Данные во время удержания сделки

Lab должен сохранять не только финал, но и события внутри позиции.

## 6.1. Position events

```json
[
  {
    "time": "10:01",
    "event": "POSITION_OPENED",
    "price": 324.35,
    "qty": 1
  },
  {
    "time": "10:06",
    "event": "SAME_SIDE_SIGNAL_IGNORED",
    "signal_id": "sig_1003"
  },
  {
    "time": "10:15",
    "event": "OPPOSITE_SIGNAL_PENDING",
    "signal_id": "sig_1004"
  },
  {
    "time": "10:20",
    "event": "OPPOSITE_PENDING_CANCELLED",
    "reason": "CURRENT_SIDE_SIGNAL"
  },
  {
    "time": "10:25",
    "event": "TRAIL_ACTIVATED",
    "price": 324.80,
    "trail_level": 324.20
  }
]
```

## 6.2. Intrabar audit

Если execution resolution 1m:

```json
{
  "intrabar_audit": {
    "timeframe": "1m",
    "bars_checked": 12,
    "high_watermark": 325.10,
    "low_watermark": 324.00,
    "stop_touched": false,
    "target_touched": true,
    "trail_touched": false,
    "ambiguous_bar": false,
    "fallback_used": false
  }
}
```

Если одновременно достигнуты stop и target:

```json
{
  "ambiguous_bar": true,
  "conflict_rule": "STOP_LOSS_FIRST",
  "selected_exit": "STOP_LOSS"
}
```

# 7. Данные на выходе

Когда Lab решает закрыть позицию, сохраняется `exit decision`.

```json
{
  "exit_decision": {
    "time": "2026-08-24T11:05:00Z",
    "reason": "TAKE_PROFIT",
    "trigger": {
      "target": 325.55,
      "bar_high": 325.60
    },
    "position_state": "LONG",
    "qty_to_close": 1
  }
}
```

Возможные причины:

```text
PROTECTIVE_STOP
TAKE_PROFIT
ATR_TRAIL_STOP
OPPOSITE_SIGNAL_EXIT
CONFIRMED_OPPOSITE_FLIP
SESSION_CLOSE
TIME_STOP
RISK_EXIT
DATA_GAP_EXIT
END_OF_DATA
```

# 8. Полный Trade Record

Это основная выдача Lab по одной завершённой сделке.

```json
{
  "trade_id": "trade_001",
  "experiment_id": "exp_001",
  "configuration_id": "cfg_001",

  "instrument": {
    "figi": "BBG004730N88",
    "ticker": "SBER",
    "asset_type": "SHARE",
    "currency": "RUB"
  },

  "side": "LONG",
  "qty": 1,

  "signal": {
    "signal_ids": [
      "sig_rsi_1001",
      "sig_vwap_1002"
    ],
    "primary_strategy": "quorum_2of3",
    "signal_time": "2026-08-24T10:00:00Z",
    "signal_bar_timeframe": "5m",
    "reason": "QUORUM_PASS",
    "features": {
      "rsi": 31.2,
      "vwap_distance": -0.8
    }
  },

  "mtf_context": {
    "bias": "LONG_ALLOWED",
    "setup": "SETUP_LONG",
    "entry": "ENTRY_LONG",
    "bias_timeframe": "1h",
    "setup_timeframe": "5m",
    "entry_timeframe": "1m"
  },

  "entry": {
    "signal_time": "2026-08-24T10:00:00Z",
    "entry_time": "2026-08-24T10:01:00Z",
    "signal_bar_close": 324.30,
    "execution_bar_open": 324.35,
    "fill_price": 324.37,
    "qty": 1,
    "notional": 324.35,
    "execution_rule": "NEXT_AVAILABLE_OPEN",
    "execution_resolution": "1m"
  },

  "initial_risk": {
    "atr_period": 14,
    "atr_value": 0.40,
    "initial_stop": 323.75,
    "target": 325.55,
    "risk_per_unit": 0.60,
    "risk_total": 0.60
  },

  "exit": {
    "exit_time": "2026-08-24T11:05:00Z",
    "exit_price": 325.55,
    "exit_reason": "TAKE_PROFIT",
    "exit_trigger": "TARGET_TOUCHED",
    "exit_timeframe": "1m"
  },

  "duration": {
    "minutes_held": 64,
    "bars_held_signal": 13,
    "bars_held_execution": 64
  },

  "pnl": {
    "gross_pnl": 1.20,
    "commission_entry": 0.16,
    "commission_exit": 0.16,
    "slippage_entry": 0.02,
    "slippage_exit": 0.02,
    "other_fees": 0.00,
    "total_costs": 0.36,
    "net_pnl": 0.84,
    "r_multiple": 1.40,
    "return_percent": 0.37
  },

  "position_events": [
    {
      "time": "10:06",
      "type": "SAME_SIDE_IGNORED"
    },
    {
      "time": "10:15",
      "type": "OPPOSITE_PENDING"
    }
  ],

  "quality": {
    "data_gaps": 0,
    "ambiguous_bars": 0,
    "fallback_used": false,
    "lookahead_check": "PASS"
  },

  "versions": {
    "engine": "trade_engine_v1",
    "cost_model": "canonical_v1",
    "strategy": "quorum_2of3@1.0.0",
    "signal_policy": "confirmed_flip@1.0.0",
    "exit_policy": "atr_stop@1.0.0"
  }
}
```

# 9. Что выдать по каждой сделке

Минимально:

- trade_id;
- experiment_id;
- FIGI;
- ticker;
- side;
- qty;
- signal time;
- entry time;
- entry price;
- exit time;
- exit price;
- exit reason;
- gross P&L;
- costs;
- net P&L;
- duration;
- stop/target/trailing;
- engine/version;
- data quality.

Расширенно:

- все raw signals;
- quorum votes;
- policy decisions;
- MTF context;
- ignored signals;
- pending;
- cooldown;
- intrabar audit;
- risk checks;
- execution assumptions;
- fill details.

# 10. Отдельно выдавать rejected signals

Lab должен выдавать не только trades.

Пример:

```json
{
  "signal_id": "sig_1003",
  "figi": "SBER",
  "time": "2026-08-24T10:06:00Z",
  "side": "BUY",
  "decision": "IGNORED",
  "reason_code": "IGNORE_SAME_SIDE",
  "position_state": "LONG",
  "counterfactual": {
    "enabled": true,
    "estimated_result": 0.42
  }
}
```

Counterfactual нельзя смешивать с фактическим P&L.

# 11. Итоговые метрики Lab

После всех сделок выдавать:

## Trade metrics

- total trades;
- completed trades;
- open positions at end;
- long/short count;
- wins/losses;
- win rate;
- average win;
- average loss;
- median win/loss;
- largest win;
- largest loss;
- average duration;
- median duration;
- average R;
- median R;
- expectancy.

## P&L

- gross P&L;
- commissions;
- slippage;
- other fees;
- net P&L;
- return %;
- cumulative equity;
- realized/unrealized;
- turnover.

## Risk

- max drawdown;
- max drawdown duration;
- max consecutive losses;
- max daily loss;
- volatility of daily P&L;
- exposure;
- max position size;
- max concurrent positions;
- risk per trade.

## Stability

- per FIGI;
- per day;
- H1/H2;
- long/short;
- session segments;
- regime;
- net without top1;
- positive FIGI count;
- positive day count.

## Technical QC

- missing bars;
- duplicated bars;
- unavailable FIGI;
- fallback executions;
- ambiguous bars;
- no-next-bar rejections;
- signal/entry timing violations;
- deterministic replay check;
- configuration hash;
- output hash.

# 12. Equity and drawdown

Lab должен выдать time series:

```json
{
  "time": "2026-08-24T11:05:00Z",
  "realized_pnl": 0.84,
  "cumulative_pnl": 0.84,
  "equity": 100000.84,
  "high_watermark": 100000.84,
  "drawdown": 0
}
```

Для каждого дня:

```json
{
  "date": "2026-08-24",
  "trades": 12,
  "gross_pnl": 4.20,
  "costs": 2.10,
  "net_pnl": 2.10,
  "equity_close": 100002.10,
  "drawdown_close": -0.40
}
```

# 13. Как это отображать во фронтенде

## Вкладка Trade detail

Секции:

```text
1. Instrument
2. Signal
3. MTF context
4. Entry
5. Risk levels
6. Position events
7. Exit
8. Costs
9. P&L
10. Data quality
11. Versions
```

## Визуальная timeline

```text
10:00 Signal BUY
10:00 Quorum PASS
10:00 Policy ACCEPT
10:01 Entry FILLED
10:06 Same-side ignored
10:15 Opposite pending
10:25 Pending cancelled
11:05 Target hit
11:05 Exit FILLED
11:05 Trade completed
```

## График

Показывать:

- signal bar;
- entry;
- stop;
- target;
- trail;
- exit;
- position zone;
- signal context;
- MTF layers.

При клике на позицию открывается полный Trade Record.

# 14. API выдачи

```http
GET /api/v1/lab/experiments/{experiment_id}/trades
GET /api/v1/lab/experiments/{experiment_id}/trades/{trade_id}
GET /api/v1/lab/experiments/{experiment_id}/signals
GET /api/v1/lab/experiments/{experiment_id}/decisions
GET /api/v1/lab/experiments/{experiment_id}/equity
GET /api/v1/lab/experiments/{experiment_id}/drawdown
GET /api/v1/lab/experiments/{experiment_id}/metrics
GET /api/v1/lab/experiments/{experiment_id}/qc
GET /api/v1/lab/experiments/{experiment_id}/report
```

# 15. Финальная логика одной сделки

```text
1. Lab читает закрытый бар.
2. Читает доступные signals из Warehouse.
3. Собирает MTF context.
4. Проверяет filters/quorum.
5. Читает текущую position state.
6. Применяет signal policy.
7. Создаёт decision snapshot.
8. Если ACCEPT — создаёт Order Intent.
9. Проверяет risk.
10. Определяет next execution bar.
11. Моделирует fill.
12. Открывает position.
13. Рассчитывает initial stop/target/trailing.
14. Последовательно проверяет следующие бары.
15. Записывает ignored/pending/cooldown events.
16. Находит exit trigger.
17. Моделирует exit fill.
18. Считает gross/costs/net.
19. Закрывает position.
20. Записывает полный Trade Record.
21. Обновляет equity/drawdown.
```

# Главное требование

Одна завершённая сделка в Lab должна позволять ответить на все вопросы:

- какой сигнал возник;
- какие стратегии проголосовали;
- какой был старший/средний/младший контекст;
- почему вход разрешили;
- по какой цене вошли;
- какие были stop/target/trailing;
- какие сигналы проигнорировали;
- почему закрыли;
- какой был gross;
- сколько стоили комиссии и slippage;
- какой получился net;
- не было ли gaps/ambiguous bars/look-ahead;
- какими версиями движка и конфигурации всё считалось.

Короткая формула:

```text
Trade = Signal context
      + Decision audit
      + Entry execution
      + Position lifecycle
      + Exit execution
      + Costs
      + P&L
      + Data quality
      + Version lineage
```

Именно такой объект позволит одинаково отображать сделку в Lab, экспортировать её в CSV/JSON, сравнивать с paper-исполнением и позже проверять расхождения с реальным брокерским fill.

Да, эти механизмы нужно сделать **единым торговым ядром** для Lab, paper/live-бота и частично Warehouse. При этом frontend не должен ждать завершения расчётов или постоянно опрашивать backend: тяжёлые задания выполняются worker-ами, а прогресс, сигналы, сделки, статусы и ошибки доставляются через WebSocket. Такой подход позволяет использовать общую семантику backtest/paper/live и отдельно менять только адаптер исполнения. [1138][1141][1139]

# 1. Единое торговое ядро

Общая цепочка должна быть такой:

```text
Market/Candle Event
        ↓
Strategy
        ↓
Raw Signal
        ↓
Signal Policy / Quorum / Filter
        ↓
Decision
        ↓
Order Intent
        ↓
Risk Engine
        ↓
Execution Adapter
        ↓
Fill
        ↓
Position Engine
        ↓
Trade Ledger
        ↓
Metrics / Events / UI
```

Эта цепочка используется в трёх режимах:

```text
Lab:
исторические свечи → исторический execution adapter

Paper:
живые свечи → paper execution adapter

Live:
живые свечи → broker execution adapter
```

## Что должно быть общим

Общими должны быть:

- Strategy interface;
- signal format;
- signal policy;
- quorum;
- MTF logic;
- position state machine;
- entry/exit semantics;
- stop/target/trailing calculation;
- session rules;
- risk decisions;
- P&L formula;
- trade ledger format;
- event types;
- reason codes.

## Что может различаться

Различаться должны только адаптеры среды:

| Режим | Источник свечей | Исполнение |
|---|---|---|
| Lab | исторический dataset | simulated fill |
| Paper | live market stream | paper fill |
| Live | live broker stream | real broker fill |

Нельзя делать отдельную торговую логику для Lab и отдельную для бота. Иначе Lab будет проверять не то, что реально торгует бот. Единые semantics для backtest и live — нормальная архитектурная цель подобных систем. [1138][1141]

# 2. Слои единого ядра

## 2.1. Data/Event layer

Получает:

```text
CANDLE_CLOSED
TICK
QUOTE_UPDATED
SESSION_OPEN
SESSION_CLOSE
DATA_GAP
```

Для Lab события извлекаются из истории. Для paper/live приходят из stream.

Стратегия должна получать одинаковый объект:

```python
MarketEvent(
    event_type="CANDLE_CLOSED",
    figi="BBG004730N88",
    timeframe="5m",
    ts=...,
    candle=...
)
```

## 2.2. Strategy layer

Возвращает:

```text
RawSignal
```

Например:

```json
{
  "signal_id": "sig_001",
  "figi": "SBER",
  "side": "BUY",
  "signal_ts": "2026-08-24T10:00:00Z",
  "strategy_id": "rsi_reversal",
  "strategy_version": "1.0.0",
  "reason": "rsi_turn_up",
  "features": {
    "rsi": 31.2
  }
}
```

Strategy не знает:

- реальный ли это брокер;
- Lab это или live;
- какая комиссия;
- как выставляется заявка;
- сколько денег на счёте.

## 2.3. Decision/policy layer

Получает raw signal и состояние позиции:

```text
RawSignal + PositionState + MarketContext
```

Возвращает:

```text
Decision
```

Пример:

```json
{
  "decision_id": "dec_001",
  "action": "ACCEPT",
  "reason_code": "QUORUM_PASS",
  "source_signal_id": "sig_001",
  "figi": "SBER"
}
```

Или:

```json
{
  "action": "REJECT",
  "reason_code": "IGNORE_SAME_SIDE"
}
```

## 2.4. Order intent layer

Создаёт внутреннее торговое намерение:

```text
OPEN
CLOSE
FLIP
CANCEL
MODIFY_PROTECTION
```

Пример:

```json
{
  "intent_id": "intent_001",
  "action": "OPEN",
  "side": "BUY",
  "figi": "SBER",
  "qty": 1,
  "source_decision_id": "dec_001",
  "execution_rule": "NEXT_AVAILABLE_OPEN"
}
```

## 2.5. Risk layer

Решает:

```text
разрешать ли intent
```

Результаты:

```text
RISK_PASS
RISK_REJECT
RISK_PAUSE
```

Risk layer должна быть общей по смыслу, но в Lab и live иметь разные источники данных:

```text
Lab:
initial_cash = configured

Live:
buying_power = broker account
```

## 2.6. Execution adapter

Общий интерфейс:

```python
class ExecutionAdapter(Protocol):
    async def submit_order(self, order: OrderRequest) -> OrderAck:
        ...

    async def cancel_order(self, order_id: str) -> CancelResult:
        ...

    async def get_order(self, order_id: str) -> OrderStatus:
        ...

    async def get_open_orders(self) -> list[OrderStatus]:
        ...

    async def get_positions(self) -> list[BrokerPosition]:
        ...
```

Реализации:

```text
HistoricalExecutionAdapter
PaperExecutionAdapter
BrokerExecutionAdapter
```

## 2.7. Position engine

Position engine получает только fills и position events.

Он не должен считать позицию открытой только потому, что order был submitted.

Состояния:

```text
FLAT
ENTRY_PENDING
LONG
EXIT_PENDING
SHORT
FLIP_PENDING
UNKNOWN
RECONCILIATION_REQUIRED
```

После fill:

```text
position.quantity
position.avg_entry_price
position.stop
position.target
position.unrealized_pnl
```

## 2.8. Ledger

Каждое событие и каждая завершённая сделка сохраняются одинаково:

```text
Signal
Decision
Intent
Order
Fill
PositionEvent
Trade
```

Lab и бот должны выдавать один формат trade ledger, чтобы можно было сравнивать:

```text
Lab trade
vs
Paper trade
vs
Live trade
```

# 3. Частичное использование ядра в Warehouse

Warehouse не должен запускать полный Lab на каждой операции, но должен использовать общие contracts и часть engine.

## Warehouse использует

- Candle model;
- Signal model;
- MTF context;
- signal policies;
- quorum;
- preview position logic;
- preview entry/exit semantics;
- preliminary P&L calculator;
- reason codes.

## Warehouse не использует полностью

- broker order lifecycle;
- live reconciliation;
- production risk gate;
- real account buying power;
- actual broker fills.

Схема:

```text
Warehouse:
signals + preview engine

Lab:
signals + full historical engine

Bot:
signals + live/paper engine
```

Важно: preview-режим Warehouse должен явно иметь:

```text
execution_mode = PREVIEW
```

и его P&L не должен называться final или live-equivalent.

# 4. Где находятся тяжёлые процессы

FastAPI не должен сам считать полный Lab run в HTTP handler.

Правильная схема:

```text
Frontend
   ↓ HTTP
FastAPI creates job
   ↓
Redis/queue
   ↓
Lab worker
   ↓
PostgreSQL / artifact storage
   ↓
WebSocket events
   ↓
Frontend
```

Worker выполняет:

- загрузку свечей;
- чтение signals;
- MTF alignment;
- policy;
- execution;
- trades;
- metrics;
- report generation.

FastAPI только:

- принимает запрос;
- валидирует конфигурацию;
- создаёт job;
- возвращает job_id;
- отдаёт snapshots/results;
- публикует WebSocket updates.

Для долгих задач можно использовать Redis + ARQ/Celery/RQ-подобную очередь. В типовой схеме клиент создаёт job, worker выполняет его в отдельном процессе, а результат передаётся через channel layer/WebSocket. [1139]

# 5. WebSocket для frontend

WebSocket нужен для событий, а не для передачи всех больших результатов одним потоком.

## Что передавать через WebSocket

### Job progress

```json
{
  "type": "JOB_PROGRESS",
  "job_id": "job_001",
  "stage": "EXECUTION",
  "progress": 42.5,
  "current_figi": "SBER",
  "processed_figis": 6,
  "total_figis": 14,
  "processed_bars": 120000,
  "total_bars": 300000
}
```

### Signal event

```json
{
  "type": "SIGNAL_CREATED",
  "run_id": "run_001",
  "figi": "SBER",
  "time": "2026-08-24T10:00:00Z",
  "side": "BUY",
  "strategy_id": "rsi_reversal"
}
```

### Decision event

```json
{
  "type": "SIGNAL_DECISION",
  "experiment_id": "exp_001",
  "figi": "SBER",
  "time": "2026-08-24T10:00:00Z",
  "decision": "ACCEPTED",
  "reason_code": "QUORUM_PASS"
}
```

### Order event

```json
{
  "type": "ORDER_STATUS_CHANGED",
  "order_id": "ord_001",
  "status": "SUBMITTED",
  "figi": "SBER",
  "side": "BUY",
  "qty": 1
}
```

### Fill event

```json
{
  "type": "FILL_RECEIVED",
  "order_id": "ord_001",
  "figi": "SBER",
  "side": "BUY",
  "qty": 1,
  "price": 324.35,
  "time": "2026-08-24T10:01:02Z"
}
```

### Position event

```json
{
  "type": "POSITION_UPDATED",
  "figi": "SBER",
  "state": "LONG",
  "qty": 1,
  "avg_entry_price": 324.35,
  "unrealized_pnl": 0.80
}
```

### Trade completed

```json
{
  "type": "TRADE_COMPLETED",
  "trade_id": "trade_001",
  "figi": "SBER",
  "side": "LONG",
  "net_pnl": 0.84,
  "exit_reason": "TAKE_PROFIT"
}
```

### Risk event

```json
{
  "type": "RISK_EVENT",
  "severity": "WARNING",
  "figi": "SBER",
  "reason_code": "SPREAD_TOO_WIDE",
  "action": "REJECT_NEW_ENTRY"
}
```

### Error/alert

```json
{
  "type": "SYSTEM_ALERT",
  "severity": "CRITICAL",
  "code": "RECONCILIATION_REQUIRED",
  "message": "Local position differs from broker position"
}
```

## Что не передавать постоянно через WebSocket

Не нужно отправлять:

- полный dataset свечей на каждый progress event;
- полный ledger на каждый trade;
- большие CSV;
- полный report;
- весь JSON эксперимента в каждом сообщении.

Вместо этого отправлять event:

```json
{
  "type": "TRADE_COMPLETED",
  "trade_id": "trade_001"
}
```

А frontend при необходимости делает:

```http
GET /api/v1/experiments/exp_001/trades/trade_001
```

или получает результат из локального cache.

# 6. WebSocket protocol

## Подключение

```text
/ws
```

После подключения frontend отправляет subscription:

```json
{
  "type": "SUBSCRIBE",
  "channels": [
    "job:job_001",
    "experiment:exp_001",
    "bot:bot_001",
    "figi:BBG004730N88"
  ]
}
```

Backend отвечает:

```json
{
  "type": "SUBSCRIBED",
  "channels": [...]
}
```

## Snapshot после подключения

WebSocket не должен рассчитывать, что frontend знает историю событий.

При подписке backend должен отправить:

```json
{
  "type": "SNAPSHOT",
  "channel": "experiment:exp_001",
  "payload": {
    "status": "RUNNING",
    "progress": 42.5,
    "trades_count": 124,
    "positions_count": 3
  }
}
```

После snapshot идут только новые events.

Это защищает от потери данных, если frontend был временно отключён.

## Event envelope

Все события должны иметь единый конверт:

```json
{
  "event_id": "evt_001",
  "event_type": "TRADE_COMPLETED",
  "event_version": 1,
  "sequence": 152,
  "occurred_at": "2026-08-24T10:05:00Z",
  "source": "lab_worker",
  "entity_type": "trade",
  "entity_id": "trade_001",
  "channel": "experiment:exp_001",
  "payload": {}
}
```

`sequence` нужен, чтобы frontend мог заметить пропущенное событие.

## Reconnect

При reconnect frontend отправляет:

```json
{
  "type": "RESUME",
  "channel": "experiment:exp_001",
  "last_sequence": 149
}
```

Backend:

- отправляет пропущенные события 150–152;
- если события уже удалены, отправляет новый snapshot;
- frontend перестраивает состояние.

# 7. HTTP и WebSocket: кто за что отвечает

## HTTP

Используется для:

- создания configuration;
- запуска job;
- получения исторических candles;
- получения полного ledger;
- загрузки report;
- отмены job;
- ручных действий;
- snapshots.

## WebSocket

Используется для:

- progress;
- live market updates;
- signal events;
- order statuses;
- fills;
- positions;
- alerts;
- completed trade events.

Коротко:

```text
HTTP = запросить состояние / выполнить команду
WebSocket = получить изменение состояния
```

# 8. Как не подвешивать frontend

Frontend не должен:

```text
POST /run
и ждать 10 минут ответа
```

Правильно:

```text
POST /jobs
→ 202 Accepted
→ job_id
→ WebSocket progress
→ final result available
```

Пример:

```http
POST /api/v1/lab/experiments/exp_001/run
```

Ответ:

```json
{
  "job_id": "job_001",
  "status": "PENDING",
  "websocket_channel": "job:job_001"
}
```

Frontend:

1. показывает progress;
2. не блокирует график;
3. позволяет смотреть другие данные;
4. получает `JOB_COMPLETED`;
5. делает GET summary/metrics;
6. открывает results.

# 9. Frontend state model

Frontend должен разделять состояния:

```text
connectionState
jobState
experimentState
marketState
botState
positionsState
ordersState
alertsState
```

Не хранить всё в одном огромном объекте.

## Job state

```typescript
type JobState = {
  id: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  stage?: string;
  progress: number;
  message?: string;
  error?: string;
};
```

## Position state

```typescript
type PositionState = {
  figi: string;
  state: "FLAT" | "ENTRY_PENDING" | "LONG" | "EXIT_PENDING" | "SHORT" | "UNKNOWN";
  qty: number;
  avgEntryPrice?: number;
  currentPrice?: number;
  unrealizedPnl?: number;
  stopLevel?: number;
  targetLevel?: number;
};
```

## Event reducer

Frontend должен обновлять state через reducer:

```text
SNAPSHOT → initial state
SIGNAL_CREATED → add signal
ORDER_STATUS_CHANGED → update order
FILL_RECEIVED → update fill
POSITION_UPDATED → update position
TRADE_COMPLETED → append trade / update P&L
```

Не полагаться только на локальные optimistic updates.

# 10. Backend event bus

Внутри backend нужен event bus:

```text
Domain event
  ↓
Database event log
  ↓
WebSocket publisher
  ↓
Frontend
```

События сначала должны быть зафиксированы в устойчивом хранилище или outbox, а потом отправлены через WebSocket.

Это защищает от ситуации:

```text
сделка записалась в БД
но WebSocket-сообщение потерялось
```

Пример:

```text
TradeCompleted
  ↓
trade_ledger insert
outbox_event insert
  ↓
publisher sends WebSocket event
  ↓
event marked delivered
```

Для live order/position streaming разумно разделять события lifecycle и состояние snapshot; WebSocket-потоки обычно используются именно для мгновенной передачи order/position status changes и устранения постоянного polling. [1137][1146]

# 11. Lab worker и события

Lab worker должен отправлять progress:

```text
JOB_STARTED
DATA_LOADED
SIGNALS_LOADED
FIGI_STARTED
BAR_PROGRESS
TRADE_CREATED
FIGI_COMPLETED
METRICS_STARTED
REPORT_CREATED
JOB_COMPLETED
```

Но не надо отправлять событие на каждый бар. Это создаст слишком большую нагрузку.

Правило:

```text
progress event:
не чаще одного раза в 250–500 ms
или после каждого 1–2% прогресса
```

Сделки и ошибки можно отправлять сразу.

# 12. Бот и WebSocket

Для live/paper бота WebSocket должен передавать:

```text
market candle events
signals
decisions
risk checks
orders
fills
positions
P&L
alerts
```

Но market data поток не обязательно полностью пересылать фронту, если frontend не показывает все ticks. Лучше иметь отдельные каналы:

```text
market:figi:SBER
bot:bot_001
orders:bot_001
positions:bot_001
alerts:bot_001
```

Frontend подписывается только на нужные каналы.

# 13. Безопасность WebSocket

Нужно предусмотреть:

- auth token;
- user permissions;
- channel access control;
- heartbeat/ping-pong;
- reconnect;
- rate limits;
- max subscriptions;
- event size limits;
- no secrets in payload;
- separate read/write permissions.

Критические команды не должны выполняться только из входящего WebSocket-сообщения без авторизации:

```text
CLOSE_ALL
KILL_SWITCH
ACTIVATE_LIVE
```

Для них нужен HTTP command endpoint и подтверждение, а WebSocket только сообщает результат.

# 14. Команды и события

Важно разделить:

```text
Command:
"запусти Lab"

Event:
"Lab запущен"

Command:
"поставь заявку"

Event:
"заявка подтверждена"

Event:
"заявка исполнена"
```

Frontend отправляет команды через HTTP:

```http
POST /api/v1/bot/orders
```

Backend публикует events:

```text
ORDER_CREATED
ORDER_SUBMITTED
ORDER_ACKNOWLEDGED
ORDER_FILLED
```

Не делать WebSocket единственным способом отправки команд.

# 15. API для общего runtime

## Общие snapshots

```http
GET /api/v1/runtime/{runtime_id}/state
GET /api/v1/runtime/{runtime_id}/positions
GET /api/v1/runtime/{runtime_id}/orders
GET /api/v1/runtime/{runtime_id}/events
```

## WebSocket

```text
/ws/runtime/{runtime_id}
```

Режим runtime:

```text
LAB
PREVIEW
PAPER
LIVE
```

Один runtime interface может иметь разные adapters.

# 16. Сравнение режимов

| Свойство | Warehouse preview | Lab | Paper | Live |
|---|---|---|---|---|
| Data | исторические | исторические | live stream | live broker |
| Position engine | preview | canonical | canonical | canonical |
| Costs | estimate | canonical | simulated/observed | actual |
| Orders | simulated | simulated | paper | broker |
| Partial fills | optional | model | model | real |
| WebSocket | progress/signals | progress/trades | events | events/orders |
| Reconciliation | нет | нет | internal | broker required |
| P&L | preliminary | full | real-time estimate | actual |

# 17. Минимальный MVP

## Общий engine

- CandleEvent;
- Signal;
- Decision;
- OrderIntent;
- Position;
- ExitDecision;
- Trade;
- P&L;
- reason codes.

## Lab

- historical adapter;
- worker;
- job status;
- trade ledger;
- metrics;
- WebSocket progress.

## Warehouse

- signal preview;
- configuration preview;
- position zones;
- WebSocket calculation progress.

## Paper

- live candle stream;
- paper adapter;
- real-time positions;
- real-time P&L.

## Live позже

- broker adapter;
- order lifecycle;
- fills;
- reconciliation;
- risk gates;
- kill switch.

# 18. Порядок реализации

1. Зафиксировать domain contracts.
2. Создать общий position/order/trade engine.
3. Написать golden tests.
4. Подключить historical execution adapter для Lab.
5. Подключить Paper execution adapter.
6. Создать job worker и progress events.
7. Добавить WebSocket snapshots/events.
8. Подключить frontend reducer.
9. Добавить broker read-only.
10. Добавить broker execution в sandbox.
11. Только затем live small-size.

# Финальная формулировка

```text
Lab, paper и live должны использовать один и тот же торговый runtime:
одни стратегии, одни signal policies, одни position states,
одни exits, одни reason codes и один формат ledger.

Меняется только execution adapter:
historical, paper или broker.

Warehouse использует тот же runtime частично:
для preview входов, выходов и предполагаемых позиций,
но не является источником финального P&L.

FastAPI отвечает за команды, snapshots, конфигурации и результаты.
Долгие расчёты выполняются отдельными workers.
WebSocket доставляет frontend события прогресса, сигналов, заявок,
fills, позиций, сделок и alerts, чтобы UI не блокировался и не зависел
от постоянного polling.

Frontend не считает торговую математику сам.
Он принимает snapshots и events, обновляет state через reducer
и при необходимости запрашивает детальные результаты обычным HTTP.
```

Самая важная граница:

```text
HTTP = команды и запрос полного состояния
WebSocket = поток изменений состояния
Worker = тяжёлый расчёт
Engine = единая торговая логика
Adapter = конкретная среда исполнения
```