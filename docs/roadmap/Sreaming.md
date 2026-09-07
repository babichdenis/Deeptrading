Отлично, давай сделаем streaming-контур поверх движка и объединим его со стримами Т-Инвестиций. Это даст:

- Реальное время: свечи → сигналы → сделки → портфель.
- Возможность видеть «что происходит прямо сейчас» без бэктеста.
- Единую точку для аналитики (per-strategy, per-ticker, per-session).

***

## 1. Архитектура streaming-контура

### 1.1. Общая схема

```
┌─────────────────────────────────────────────────────────┐
│              T-Investments gRPC Streams                 │
│  - CandleStream (1m свечи)                              │
│  - TradesStream (сделки)                                │
│  - PositionsStream (позиции)                            │
│  - OrdersStream (ордера)                                │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│              Bot Streaming Layer                        │
│  - CandleFeed (уже есть)                                │
│  - TradeFeed (новый, сделки из стрима)                  │
│  - PositionFeed (новый, позиции из стрима)              │
│  - OrderFeed (новый, ордера из стрима)                  │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│           Streaming Compute Ensemble                    │
│  - EnsembleState (состояние на каждый бар)              │
│  - update(candle) → Signal | None                       │
│  - Кэш индикаторов, сигналов, кворума                   │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│              Bot Runtime (Paper/Live)                   │
│  - _process_candle(candle)                              │
│  - _process_trade(trade)                                │
│  - _process_position(position)                          │
│  - Логирование, аналитика                               │
└──────────────┬──────────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────────┐
│              Analytics Layer                            │
│  - Per-strategy stats (в реальном времени)              │
│  - Per-ticker stats                                     │
│  - Per-session stats                                    │
│  - Экспорт в БД / Prometheus / Grafana                  │
└─────────────────────────────────────────────────────────┘
```

***

## 2. Streaming Compute Ensemble

### 2.1. Проблема текущей версии

**Сейчас:**

```python
def compute_ensemble(candles_1m, req):
    # Пересчитывает ВСЁ с нуля:
    # 1. generate_signals() — цикл по всем барам × стратегии
    # 2. merge_quorum() — группировка по ts
    # 3. Gates — фильтрация
    # 4. EngineRunner.run() — бэктест
    ...
```

**Проблема:**

- На каждом новом баре пересчитывает всю историю.
- 100k баров × 7 стратегий = 700k вызовов стратегий.
- Медленно для реального времени.

***

### 2.2. Streaming-версия

**Идея:**

- Хранить состояние между барами.
- Обновлять только при новом баре.
- Возвращать сигнал (если есть) сразу.

**Структура:**

```python
@dataclass
class EnsembleState:
    figi: str
    req: dict
    
    # Кэш индикаторов (последние N значений)
    rsi_cache: dict[str, list[float]]  # strategy_id → [rsi_values]
    macd_cache: dict[str, tuple[list[float], list[float], list[float]]]
    atr_cache: dict[str, list[float]]
    
    # Кэш сигналов (последние M баров)
    signal_cache: dict[str, list[Signal]]  # strategy_id → [signals]
    
    # Кэш кворума (последние K баров)
    quorum_cache: list[dict]  # [{ts, side, votes, members_for, opposition}, ...]
    
    # Состояние для EngineRunner (позиции, cooldown, и т.д.)
    runner_state: dict  # Можно сериализовать из EngineRunner
    
    # Метрики
    last_signal_ts: int | None
    last_trade_ts: int | None
```

**Метод update:**

```python
class StreamingEnsemble:
    def __init__(self, figi: str, req: dict):
        self.state = EnsembleState(
            figi=figi,
            req=req,
            rsi_cache={},
            macd_cache={},
            atr_cache={},
            signal_cache={},
            quorum_cache=[],
            runner_state={},
            last_signal_ts=None,
            last_trade_ts=None,
        )
    
    def update(self, candle: Candle) -> Signal | None:
        """
        Обновляет состояние на новый бар, возвращает сигнал (если есть).
        """
        # 1. Обновить индикаторы (инкрементально)
        self._update_indicators(candle)
        
        # 2. Обновить сигналы стратегий
        signals = self._generate_signals(candle)
        
        # 3. Обновить кворум
        quorum = self._update_quorum(candle.ts, signals)
        
        # 4. Применить гейты
        if not self._apply_gates(quorum, candle):
            return None
        
        # 5. Проверить, есть ли сигнал для входа/выхода
        signal = self._check_signal(quorum, candle)
        
        # 6. Обновить метрики
        if signal:
            self.state.last_signal_ts = candle.ts
        
        return signal
```

**Преимущества:**

- O(1) на бар вместо O(N).
- Можно обрабатывать 20 тикеров в реальном времени.

***

## 3. Интеграция со стримами Т-Инвестиций

### 3.1. Что есть у Т-Инвестиций

**Стримы:**

1. **CandleStream:**
   - 1m свечи (или 5m, 15m, и т.д.).
   - `{figi, ts, open, high, low, close, volume}`.

2. **TradesStream:**
   - Сделки бота (если через T-Investments API).
   - `{order_id, figi, side, qty, price, ts}`.

3. **PositionsStream:**
   - Позиции бота.
   - `{figi, qty, entry_price, current_price, unrealized_pnl}`.

4. **OrdersStream:**
   - Ордера бота.
   - `{order_id, figi, side, qty, price, status, ts}`.

***

### 3.2. Bot Streaming Layer

**Задача:**

- Подписаться на стримы Т-Инвестиций.
- Преобразовать в единый формат.
- Передать в `StreamingEnsemble` и `Runtime`.

**Структура:**

```python
@dataclass
class BotCandle:
    figi: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int

@dataclass
class BotTrade:
    order_id: str
    figi: str
    side: str  # "BUY" | "SELL"
    qty: int
    price: float
    ts: int

@dataclass
class BotPosition:
    figi: str
    qty: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    ts: int

@dataclass
class BotOrder:
    order_id: str
    figi: str
    side: str
    qty: int
    price: float
    status: str  # "NEW", "FILLED", "CANCELLED", ...
    ts: int
```

**Класс BotStreamManager:**

```python
class BotStreamManager:
    def __init__(self):
        self.candle_callbacks: list[Callable[[BotCandle], None]] = []
        self.trade_callbacks: list[Callable[[BotTrade], None]] = []
        self.position_callbacks: list[Callable[[BotPosition], None]] = []
        self.order_callbacks: list[Callable[[BotOrder], None]] = []
    
    def subscribe_candles(self, figis: list[str]):
        """Подписаться на свечи."""
        # Запустить gRPC stream из T-Investments
        # При новой свече:
        #   candle = BotCandle(...)
        #   for cb in self.candle_callbacks:
        #       cb(candle)
    
    def subscribe_trades(self):
        """Подписаться на сделки."""
        ...
    
    def subscribe_positions(self):
        """Подписаться на позиции."""
        ...
    
    def subscribe_orders(self):
        """Подписаться на ордера."""
        ...
    
    def on_candle(self, candle: BotCandle):
        """Вызывается при новой свече."""
        for cb in self.candle_callbacks:
            cb(candle)
    
    def on_trade(self, trade: BotTrade):
        """Вызывается при новой сделке."""
        for cb in self.trade_callbacks:
            cb(trade)
    
    def on_position(self, position: BotPosition):
        """Вызывается при обновлении позиции."""
        for cb in self.position_callbacks:
            cb(position)
    
    def on_order(self, order: BotOrder):
        """Вызывается при обновлении ордера."""
        for cb in self.order_callbacks:
            cb(order)
```

***

### 3.3. Интеграция с Runtime

**Сейчас:**

```python
class PaperBotRuntime:
    async def start(self):
        feed = CandleFeed(figis=self.universe)
        async for candle in feed.stream():
            await self._process_candle(candle)
```

**Нужно:**

```python
class PaperBotRuntime:
    async def start(self):
        # 1. Подписаться на свечи
        self.stream_manager = BotStreamManager()
        self.stream_manager.subscribe_candles(self.universe)
        self.stream_manager.subscribe_trades()
        self.stream_manager.subscribe_positions()
        self.stream_manager.subscribe_orders()
        
        # 2. Зарегистрировать колбэки
        self.stream_manager.candle_callbacks.append(self._process_candle)
        self.stream_manager.trade_callbacks.append(self._process_trade)
        self.stream_manager.position_callbacks.append(self._process_position)
        self.stream_manager.order_callbacks.append(self._process_order)
        
        # 3. Запустить стримы
        await self.stream_manager.run()
    
    async def _process_candle(self, candle: BotCandle):
        """Обработка свечи."""
        # Обновить StreamingEnsemble
        signal = await self.ensemble.update(candle)
        
        # Проверить, есть ли сигнал
        if signal:
            # Проверить кворум, гейты
            # Открыть/закрыть позицию
        
        # Логирование, аналитика
    
    async def _process_trade(self, trade: BotTrade):
        """Обработка сделки."""
        # Обновить портфель
        # Логирование, аналитика
    
    async def _process_position(self, position: BotPosition):
        """Обработка позиции."""
        # Обновить PnL
        # Проверить SL/TP
    
    async def _process_order(self, order: BotOrder):
        """Обработка ордера."""
        # Обновить статус ордера
        # Логирование
```

***

## 4. Per-strategy, per-ticker, per-session анализ

### 4.1. Per-strategy stats

**Идея:**

- Для каждой стратегии считать:
  - Количество сигналов.
  - Количество сделок.
  - Win rate, PnL.

**Структура:**

```python
@dataclass
class StrategyStats:
    strategy_id: str
    signals: int = 0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades > 0 else 0.0
    
    def profit_factor(self) -> float:
        # Gross profit / Gross loss
        ...
```

**Обновление:**

```python
class Analytics:
    def __init__(self):
        self.strategy_stats: dict[str, StrategyStats] = {}
        self.ticker_stats: dict[str, TickerStats] = {}
        self.session_stats: dict[str, SessionStats] = {}
    
    def on_signal(self, signal: Signal):
        """При новом сигнале."""
        for strategy_id in signal.strategies:  # Какие стратегии в кворуме
            if strategy_id not in self.strategy_stats:
                self.strategy_stats[strategy_id] = StrategyStats(strategy_id)
            self.strategy_stats[strategy_id].signals += 1
    
    def on_trade(self, trade: BotTrade, pnl: float):
        """При новой сделке."""
        # Обновить per-strategy stats
        # Обновить per-ticker stats
        # Обновить per-session stats
```

***

### 4.2. Per-ticker stats

**Идея:**

- Для каждого FIGI считать:
  - Количество сделок.
  - Win rate, PnL.
  - Среднее время удержания.

**Структура:**

```python
@dataclass
class TickerStats:
    figi: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    total_bars_held: int = 0
    
    def avg_bars_held(self) -> float:
        return self.total_bars_held / self.trades if self.trades > 0 else 0.0
```

***

### 4.3. Per-session stats

**Идея:**

- Для каждой сессии (morning/day/evening) считать:
  - Количество сделок.
  - Win rate, PnL.

**Структура:**

```python
@dataclass
class SessionStats:
    session: str  # "morning", "day", "evening"
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
```

**Определение сессии:**

```python
def get_session(ts: int) -> str:
    """Определить сессию по времени."""
    dt = datetime.fromtimestamp(ts, tz=timezone("Europe/Moscow"))
    hour = dt.hour
    minute = dt.minute
    
    if 6 <= hour < 10:  # 06:50–09:50
        return "morning"
    elif 10 <= hour < 19:  # 09:50–18:45
        return "day"
    else:  # 19:05–23:50
        return "evening"
```

***

## 5. Экспорт аналитики

### 5.1. В БД

**Таблицы:**

```sql
-- Per-strategy stats
CREATE TABLE strategy_stats (
    strategy_id TEXT,
    ts INTEGER,
    signals INTEGER,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    net_pnl REAL
);

-- Per-ticker stats
CREATE TABLE ticker_stats (
    figi TEXT,
    ts INTEGER,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    net_pnl REAL
);

-- Per-session stats
CREATE TABLE session_stats (
    session TEXT,
    ts INTEGER,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    net_pnl REAL
);
```

**Обновление:**

```python
async def persist_stats(analytics: Analytics, db: SessionLocal):
    """Сохранить статистику в БД."""
    for strategy_id, stats in analytics.strategy_stats.items():
        await db.execute(
            "INSERT INTO strategy_stats (...) VALUES (...)",
            {...}
        )
```

***

### 5.2. В Prometheus

**Метрики:**

```python
from prometheus_client import Counter, Gauge, Histogram

# Per-strategy
strategy_signals = Counter("bot_strategy_signals_total", "Total signals", ["strategy"])
strategy_trades = Counter("bot_strategy_trades_total", "Total trades", ["strategy"])
strategy_pnl = Gauge("bot_strategy_pnl", "Net PnL", ["strategy"])

# Per-ticker
ticker_trades = Counter("bot_ticker_trades_total", "Total trades", ["figi"])
ticker_pnl = Gauge("bot_ticker_pnl", "Net PnL", ["figi"])

# Per-session
session_trades = Counter("bot_session_trades_total", "Total trades", ["session"])
session_pnl = Gauge("bot_session_pnl", "Net PnL", ["session"])
```

**Обновление:**

```python
def update_prometheus(analytics: Analytics):
    for strategy_id, stats in analytics.strategy_stats.items():
        strategy_signals.labels(strategy=strategy_id).inc(stats.signals)
        strategy_trades.labels(strategy=strategy_id).inc(stats.trades)
        strategy_pnl.labels(strategy=strategy_id).set(stats.net_pnl)
```

***

### 5.3. В Grafana

**Дашборды:**

1. **Overview:**
   - Total PnL (real-time).
   - Total trades today.
   - Win rate.

2. **Per-strategy:**
   - Signals per strategy.
   - Trades per strategy.
   - PnL per strategy.

3. **Per-ticker:**
   - Trades per ticker.
   - PnL per ticker.

4. **Per-session:**
   - Trades per session.
   - PnL per session.

***

## 6. План реализации

### Этап 1 — StreamingEnsemble

1. Создать `StreamingEnsemble` с кэшем индикаторов.
2. Реализовать `update(candle)` → Signal.
3. Протестировать на исторических данных (сравнить с `compute_ensemble`).

### Этап 2 — BotStreamManager

1. Создать `BotStreamManager`.
2. Подписаться на CandleStream из T-Investments.
3. Преобразовать в `BotCandle`.
4. Передать в `StreamingEnsemble`.

### Этап 3 — Интеграция с Runtime

1. Обновить `PaperBotRuntime` для работы со стримами.
2. Добавить `_process_trade`, `_process_position`, `_process_order`.
3. Логирование в реальном времени.

### Этап 4 — Analytics

1. Создать `Analytics` с per-strategy, per-ticker, per-session stats.
2. Обновлять при каждом сигнале/сделке.
3. Экспорт в БД / Prometheus.

### Этап 5 — Визуализация

1. Настроить Grafana дашборды.
2. Добавить алерты (например, если PnL < -X за день).

***

## 7. С чего начать

**Сейчас:**

1. **StreamingEnsemble:**
   - Написать базовую версию с кэшем RSI.
   - Протестировать на 1 тикере, 1 дне.

2. **BotStreamManager:**
   - Подписаться на CandleStream.
   - Логировать свечи в реальном времени.

3. **Analytics:**
   - Начать с per-ticker stats.
   - Выводить в лог после каждой сделки.

***

Если хочешь, могу написать код для `StreamingEnsemble` (с кэшем RSI/MACD) или `BotStreamManager` (подписка на gRPC стримы).