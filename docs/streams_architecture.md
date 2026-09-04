# Stream Architecture — T-Investments API

## Проблема

Polling (GetPositions / GetPortfolio каждые N сек) — **ненадёжно**:
- Bot «думает» что позиция SHORT, а сервер уже закрыл её
- PostOrder/SandboxOrder — синхронный, блокирует, не даёт feedback
- Состояние в БД/памяти расходится с реальным состоянием на сервере
- Баг SBER: бот видел SHORT с SL=279, intrabar_exit мгновенно срабатывал

## Решение — все ключевые данные через стримы

### Доступные стримы SDK (`t_tech.invest`)

| Стрим | Сервис | Метод | Что даёт |
|-------|--------|-------|----------|
| **PositionsStream** | `operations_stream` | `positions_stream(accounts, with_initial_positions=True)` | Реальные позиции: qty, figi, side, blocked, money |
| **TradesStream** | `orders_stream` | `trades_stream(accounts)` | Факт исполнения: order_id, figi, price, qty, datetime |
| **OrderStateStream** | `orders_stream` | `order_state_stream(request)` | Жизненный цикл: placed → filled / rejected / cancelled |
| **OperationsStream** | `operations_stream` | `operations_stream(request)` | Операции: комиссии, dividend, ввод/вывод |
| **MarketDataStream** | `market_data_stream` | `market_data_stream(request_iterator())` | Сделки, стаканы, свечи в реальном времени |

### Схема архитектуры

```
┌─────────────────────────────────────────────────┐
│                   StreamManager                  │
│                                                  │
│  ┌──────────────┐  ┌──────────────┐             │
│  │ PositionsStream│  │ TradesStream │             │
│  │ (background)   │  │ (background)  │             │
│  └──────┬───────┘  └──────┬──────┘             │
│         │                  │                     │
│  ┌──────▼───────┐  ┌──────▼──────┐             │
│  │ OrderStateStream│  │ OperationsStream│         │
│  │ (background)   │  │ (background)    │         │
│  └──────┬───────┘  └──────┬──────┘             │
│         │                  │                     │
│         └────────┬─────────┘                     │
│                  ▼                               │
│         Server State (in-memory)                 │
│         - _server_positions[figi]                │
│         - _server_trades[order_id]               │
│         - _server_orders[order_id]               │
└─────────────────────────────────────────────────┘
         ▲                    │
         │                    ▼
         │           ┌───────────────┐
         │           │   Bot Logic    │
         │           │ (reads state)  │
         │           └───────┬───────┘
         │                   │
    PostOrderAsync    OrderStateStream
    (non-blocking)    (confirmation)
```

### Ключевые принципы

1. **Single Source of Truth** — PositionsStream = единственное хранилище позиций
2. **PostOrderAsync** вместо PostOrder — non-blocking, возвращает order_request_id
3. **OrderStateStream** — подтверждение/отказ ордера
4. **TradesStream** — факт исполнения сделки (цена, количество, время)
5. **Reconnect** — автоматический reconnect при обрыве с keepalive ping

### Данные из стримов

#### PositionsStreamResponse → PositionData
```python
securities: List[PositionsSecurities]
    figi: str
    balance: int        # количество лотов
    blocked: int        # заблокированные (стоп-ордера)
    ticker: str
    instrument_uid: str

money: List[PositionsMoney]
    available_value: MoneyValue
    blocked_value: MoneyValue
```

#### TradesStreamResponse → OrderTrades
```python
order_id: str
direction: OrderDirection  # BUY/SELL
figi: str
trades: List[OrderTrade]
    date_time: datetime
    price: Quotation
    quantity: int
    trade_id: str
```

#### OrderStateStreamResponse → OrderState
```python
order_id: str
execution_report_status: OrderExecutionReportStatus
    EXECUTION_REPORT_STATUS_NEW
    EXECUTION_REPORT_STATUS_FILLED
    EXECUTION_REPORT_STATUS_CANCELLED
    EXECUTION_REPORT_STATUS_REJECTED
    ...
```

### План реализации

#### Этап 1: StreamManager (ядро)
- Класс `StreamManager` в `app/bot/stream_manager.py`
- Запуск PositionsStream + TradesStream + OrderStateStream
- In-memory state: `_positions`, `_trades`, `_orders`
- Reconnect logic с exponential backoff
- Единый entry point: `await manager.get_position(figi)`

#### Этап 2: Bot → Stream
- Bot читает позиции из `_server_positions` (не poll GetPositions)
- `_execute_pending` вызывает `PostOrderAsync` (не PostSandboxOrder)
- Ждёт подтверждения через `await manager.wait_order_filled(order_id, timeout=30)`
- Если timeout → cancel + retry

#### Этап 3: Reconciliation
- Периодическая сверка `_server_positions` vs `sandbox_trades` в БД
- Если расхождение → логируем + корректируем
- Daily reconciliation report

#### Этап 4: UI streams
- SSE/WebSocket из StreamManager → frontend
- Live positions table обновляется из стрима
- Live trades feed (deal tape)
- Order status indicators (pending → filled → P&L)

### Покрытие стримами

| Компонент | Сейчас | Stream |
|-----------|--------|--------|
| Позиции | poll GetPositions 5s | PositionsStream ✅ |
| Сделки | sandbox_trades в БД | TradesStream ✅ |
| Ордера | PostSandboxOrder sync | PostOrderAsync + OrderStateStream ✅ |
| Свечи | REST poll | MarketDataStream (awaiting_close) ✅ |
| Комиссии | ручной расчёт | OperationsStream ✅ |
| Cash/Balance | GetPortfolio poll | PositionsStream (money) ✅ |

### Постримовые данные (всё ещё poll, но безопасно)

- `GetMaxLots` — одноразово перед входом (margin cap)
- `GetOrders` / `GetOrderState` — fallback для проверки старых ордеров
- `GetOperations` — история для reconciliation

### Reconnect strategy

```python
async def _reconnect(self):
    self._reconnect_count += 1
    delay = min(2 ** self._reconnect_count, 300)  # max 5 min
    logger.warning(f"Stream disconnected, reconnect in {delay}s (attempt {self._reconnect_count})")
    await asyncio.sleep(delay)
    await self.start()

# При успехе:
self._reconnect_count = 0
```

### Ping keepalive

```python
# SDK поддерживает ping:
positions_stream(accounts=accounts, ping_delay_ms=10_000)
trades_stream(accounts=accounts, ping_delay_ms=10_000)
order_state_stream(request=OrderStateStreamRequest(ping_delay_millis=10_000))
```
