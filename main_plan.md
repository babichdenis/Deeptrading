# Главная цель проекта

Создать автономного торгового бота, который на основе исторических и текущих рыночных данных находит наиболее перспективные краткосрочные движения в ликвидных и волатильных акциях, открывает позиции в направлении ожидаемого движения, управляет риском и закрывает сделки по заранее определённым правилам, чтобы получать максимально возможную устойчивую прибыль после комиссий, проскальзывания и прочих торговых издержек.

Бот должен работать не как набор отдельных индикаторов, а как единая воспроизводимая система:

```text
рыночные данные
→ анализ волатильности и структуры
→ определение направления
→ поиск торговой возможности
→ вход в позицию
→ управление открытой позицией
→ защита капитала
→ выход из сделки
→ расчёт результата
→ адаптация параметров на основе новых данных
```

## Что именно должен делать бот

Бот должен:

- находить акции с достаточной ликвидностью и выраженным движением;
- отличать направленный тренд от бокового шума;
- определять, когда вероятность роста или падения выше вероятности противоположного движения;
- выбирать направление сделки: long, short или отсутствие сделки;
- открывать не множество хаотичных микропозиций, а контролируемое количество осмысленных позиций;
- удерживать прибыльные движения достаточно долго;
- не входить повторно в одну и ту же позицию без необходимости;
- учитывать текущую волатильность при расчёте размера позиции, стопа и цели;
- защищать капитал от резких неблагоприятных движений;
- учитывать комиссии, налоги, проскальзывание, ликвидность и ограничения исполнения;
- закрывать позиции по достижении цели, защитного уровня, смене рыночного режима или окончании торговой сессии;
- автоматически прекращать торговлю при достижении дневного или портфельного лимита убытка;
- сохранять полный журнал каждого решения и каждой сделки.

## Что означает «заработать как можно больше денег»

Цель проекта — не получить максимальную прибыль на одном историческом участке и не выбрать параметры, которые идеально подходят под прошлый график.

Под максимальной прибылью понимается:

> максимальный устойчивый net P&L при контролируемом риске, приемлемой просадке и возможности воспроизвести результат на новых данных.

Поэтому бот должен оптимизироваться не по одному показателю, а по совокупности:

- net P&L после всех costs;
- стабильность результата по времени;
- стабильность по акциям;
- profit factor;
- expectancy на сделку;
- максимальная просадка;
- отношение прибыль/риск;
- частота сделок;
- оборот;
- концентрация результата;
- устойчивость к изменению параметров;
- результат на данных, которые не использовались при разработке.

Большая прибыль при неприемлемой просадке или только на одной акции не считается успехом.

## Стратегическая идея

Бот должен специализироваться на волатильных акциях, но не торговать каждое движение цены.

Он должен:

1. Найти подходящие инструменты.
2. Оценить текущую волатильность.
3. Определить рыночный режим:
   - направленный рост;
   - направленное падение;
   - боковой диапазон;
   - резкий импульс;
   - хаотичная высокая волатильность.
4. Включать подходящую торговую логику только в подходящем режиме.
5. Пропускать ситуации, где движение слишком шумное или стоимость исполнения уничтожает преимущество.
6. Входить в одну контролируемую позицию.
7. Управлять позицией до выхода.
8. Не смешивать несколько несовместимых стратегий без отдельной проверки.

## Основные требования к торговой логике

### Выбор акций

Бот должен учитывать:

- средний дневной и внутридневной оборот;
- спред;
- доступность торгов;
- среднюю волатильность;
- величину ATR;
- частоту резких движений;
- ликвидность на входе и выходе;
- возможность short;
- торговые ограничения;
- концентрацию риска.

Волатильность сама по себе не является преимуществом. Высокая волатильность может давать большую прибыль, но одновременно увеличивает риск проскальзывания, stop-out и потери капитала.

### Определение направления

Для определения направления могут использоваться:

- структура максимумов и минимумов;
- тренд на старшем timeframe;
- импульс;
- объём;
- VWAP;
- breakout;
- relative strength;
- статистическая модель;
- прогноз направления на выбранном горизонте.

Но каждая логика должна проверяться отдельно и через единый торговый движок.

### Вход

Вход должен происходить только после того, как сигнал стал известен.

Нельзя использовать данные свечи до её закрытия, если стратегия формируется на close.

Типовой порядок:

```text
закрытие свечи
→ расчёт сигнала
→ проверка фильтров и риска
→ заявка
→ исполнение на следующем доступном open/market price
```

### Одна позиция

По умолчанию:

- одна позиция на FIGI;
- повторные сигналы в текущую сторону игнорируются;
- усреднение запрещено;
- пирамидинг запрещён;
- переворот long → short является отдельной policy;
- каждая позиция получает уникальный trade id;
- всё состояние позиции восстанавливается после перезапуска.

Это необходимо, чтобы бот торговал крупные направленные движения, а не создавал десятки перекрывающихся микросделок.

### Управление риском

До входа бот должен знать:

- размер допустимого риска;
- размер позиции;
- уровень защитного выхода;
- максимальное количество одновременно открытых позиций;
- максимальный риск по сектору;
- дневной лимит убытка;
- лимит просадки;
- максимальный оборот;
- допустимое проскальзывание.

При недоступных данных, слишком широком спреде или нарушении лимитов бот не открывает сделку.

### Выход

Бот должен уметь:

- закрыть позицию по защитному стопу;
- закрыть по целевому уровню;
- использовать trailing stop;
- закрыть по смене рыночного режима;
- закрыть по противоположному подтверждённому сигналу;
- закрыть перед концом сессии;
- закрыть при data gap или технической ошибке;
- остановить новые входы при превышении риска.

Выход не должен подбираться задним числом под лучший максимум или минимум на графике.

## Архитектурная цель

Бот должен строиться вокруг единого canonical engine:

```text
Data Layer
  ↓
Market Context
  ↓
Strategy
  ↓
Signal Policy
  ↓
Risk Manager
  ↓
Position Engine
  ↓
Execution Adapter
  ↓
Trade Ledger
  ↓
Metrics / Monitoring
```

Один и тот же position engine должен использоваться в:

- historical backtest;
- Warehouse research;
- Lab;
- paper trading;
- live trading.

Различаться должны только источники данных и execution adapters:

```text
historical candles → historical fill adapter
paper market data  → paper fill adapter
broker market data → live broker adapter
```

## Надёжность и контроль

Бот не должен торговать, если:

- нет свежих данных;
- данные неполные или противоречивые;
- неизвестен статус позиции;
- нет подтверждения исполнения;
- цена слишком сильно отклонилась;
- достигнут дневной лимит убытка;
- превышен риск;
- брокерская связь нестабильна;
- нарушена сессия;
- обнаружена ошибка в состоянии.

Каждое действие должно записываться:

- какой сигнал возник;
- какие фильтры прошли или не прошли;
- почему сделка разрешена или запрещена;
- какой был расчёт размера;
- какая заявка отправлена;
- как она исполнилась;
- какие costs возникли;
- почему позиция закрыта;
- какой итоговый net P&L.

## Критерий успеха

Бот считается готовым к paper/live только если:

- backtest воспроизводим;
- Lab и paper используют одну торговую логику;
- costs и slippage реалистичны;
- нет look-ahead;
- стратегия положительна после costs;
- результат не зависит от одной акции;
- результат не исчезает на второй половине периода;
- просадка приемлема;
- параметры не требуют точной подгонки;
- trade ledger полный;
- аварийные сценарии протестированы;
- paper-режим подтверждает соответствие backtest;
- live execution имеет отдельные ограничения риска.

## Итоговая формулировка

> Создать API-first автономного торгового бота для торговли волатильными и ликвидными акциями, который выявляет направленные движения, фильтрует рыночный шум, открывает ограниченное количество обоснованных позиций, удерживает прибыльные движения, контролирует риск и закрывает сделки по реалистичным правилам исполнения, стремясь к максимальному устойчивому net P&L после комиссий и издержек, без использования будущих данных и без зависимости от подгонки под прошлый рынок.

***

# Техническое задание: Trading Research Warehouse + Lab

## 1. Цель проекта

Создать с нуля исследовательскую платформу для алгоритмической торговли ликвидными акциями через свечные данные.

На первом этапе система должна состоять из двух связанных частей:

1. **Warehouse** — хранилище и API для свечей, сигналов, стратегий и результатов исследований.
2. **Lab** — последовательный реалистичный симулятор торговли, который использует Warehouse и считает сделки через единый движок позиции.

Торговый бот подключается только после того, как Warehouse и Lab будут корректны, воспроизводимы и полностью протестированы.

Главная цель:

> Проверять торговые гипотезы на исторических данных с реалистичным исполнением, комиссиями, проскальзыванием и одной согласованной моделью позиции.

Система не должна искать красивые результаты задним числом. Она должна отвечать:

> Есть ли у стратегии устойчивое преимущество после реалистичного исполнения и затрат?

## 2. Принципы проекта

### Единый движок

Warehouse, Lab, график, paper и будущий live-бот должны использовать одну и ту же модель:

```text
свечи
→ стратегия
→ сигнал
→ signal policy / noise policy
→ order intent
→ position engine
→ execution model
→ trade ledger
→ metrics
```

Нельзя, чтобы один компонент считал каждый сигнал отдельной сделкой, а другой использовал одну позицию.

### Разделение ответственности

- Warehouse хранит данные и результаты.
- Strategy создаёт сигналы.
- Signal policy решает, какие сигналы разрешить, объединить или игнорировать.
- Position engine управляет состоянием позиции.
- Execution model моделирует входы, выходы и fill.
- Cost model считает комиссии и проскальзывание.
- Metrics считает статистику.
- Lab запускает исследования.
- API управляет всеми процессами.
- Frontend отображает свечи, сигналы, позиции и результаты.

### Воспроизводимость

Каждый эксперимент обязан сохранять:

- версию данных;
- список FIGI;
- timeframe;
- период;
- версию стратегии;
- параметры стратегии;
- signal/noise policy;
- exit policy;
- engine version;
- execution model;
- cost model;
- размер позиции;
- timezone;
- полную конфигурацию;
- hash конфигурации;
- версию кода или git commit.

Нельзя получить результат только из текущих настроек приложения.

## 3. Общая архитектура

```text
Frontend / API client / CLI
            ↓
          FastAPI
            ↓
       Research API
            ↓
   PostgreSQL metadata/results
            ↓
   Parquet candle storage
            ↓
    Research job queue
            ↓
      Lab workers
            ↓
       Strategy registry
            ↓
  Signal policy / Position engine
            ↓
    Execution + Cost model
            ↓
       Ledger + Metrics
```

### Компоненты

```text
api/
  FastAPI routes and schemas

warehouse/
  candle storage
  dataset versions
  imports
  availability
  data validation

strategies/
  strategy interfaces
  registry
  versioned strategies

policies/
  signal/noise policies
  entry policies
  exit policies

engine/
  position state machine
  execution model
  cost model
  session rules
  risk rules

lab/
  experiment orchestration
  jobs
  matrix runs
  walk-forward runs

metrics/
  trade statistics
  equity
  drawdown
  per FIGI
  per day
  splits

artifacts/
  reports
  JSON
  CSV
  ledgers
  charts
```

## 4. Warehouse

Warehouse — это слой данных и исследовательских артефактов.

### Поддерживаемые данные

На первом этапе:

- OHLCV candles;
- timeframe: 1m, 5m, 15m, 1h, 1d;
- ticker;
- FIGI;
- timestamp;
- open;
- high;
- low;
- close;
- volume;
- trading status;
- timezone;
- source.

### Модель свечи

```json
{
  "figi": "BBG004730N88",
  "ticker": "SBER",
  "time": "2026-06-15T10:00:00Z",
  "timeframe": "5m",
  "open": 324.10,
  "high": 324.35,
  "low": 323.90,
  "close": 324.20,
  "volume": 125000,
  "source": "warehouse"
}
```

### Требования к свечам

Проверять:

- timestamp корректен;
- свечи отсортированы;
- нет дубликатов;
- OHLC положительные;
- `high >= max(open, close)`;
- `low <= min(open, close)`;
- volume неотрицателен;
- timeframe соответствует расстоянию между свечами;
- gaps выявляются и записываются;
- timezone единый;
- сессии определяются явно.

### Dataset

Dataset — неизменяемая версия набора данных.

Хранить:

```text
dataset_id
source
timeframe
from
to
figis
timezone
storage_path
data_hash
created_at
availability
validation_status
```

Свечи лучше хранить в Parquet, а не целиком в PostgreSQL. PostgreSQL использовать для metadata, статусов, индексов и результатов.

### Dataset API

```http
POST /api/v1/datasets
GET /api/v1/datasets
GET /api/v1/datasets/{dataset_id}
GET /api/v1/datasets/{dataset_id}/candles
GET /api/v1/datasets/{dataset_id}/candles/{figi}
GET /api/v1/datasets/{dataset_id}/availability
POST /api/v1/datasets/{dataset_id}/validate
```

Пример создания:

```json
{
  "source": "local_parquet",
  "timeframe": "5m",
  "from": "2026-06-15",
  "to": "2026-07-14",
  "figis": [
    "BBG004730N88"
  ],
  "timezone": "Europe/Moscow"
}
```

## 5. Стратегии

Стратегия не считает прибыль и не открывает сделки напрямую.

Она получает:

- исторические свечи до текущего момента;
- индикаторный контекст;
- состояние рынка;
- состояние позиции;
- время/сессию.

Она возвращает сигнал:

```text
BUY
SELL
HOLD
```

или более подробный signal object:

```json
{
  "strategy_id": "macd_cross",
  "strategy_version": "1.0.0",
  "figi": "BBG004730N88",
  "time": "2026-06-15T10:00:00Z",
  "side": "BUY",
  "confidence": null,
  "features": {
    "macd": 0.12,
    "signal": 0.08,
    "histogram": 0.04
  },
  "reason": "bullish_cross"
}
```

### Strategy interface

```python
class Strategy(Protocol):
    strategy_id: str
    version: str

    def warmup_bars(self) -> int:
        ...

    def on_bar(
        self,
        candles: Sequence[Candle],
        context: MarketContext,
    ) -> Signal:
        ...
```

Стратегия должна использовать только данные, доступные на момент закрытия текущей свечи.

### Strategy registry

```python
STRATEGY_REGISTRY = {
    "macd_cross": MacdCrossStrategy,
    "donchian_breakout": DonchianStrategy,
    "vol_expansion": VolExpansionStrategy,
}
```

Не принимать произвольный Python-код через API.

### Strategy API

```http
POST /api/v1/strategies
GET /api/v1/strategies
GET /api/v1/strategies/{strategy_id}
GET /api/v1/strategies/{strategy_id}/versions
POST /api/v1/strategies/{strategy_id}/validate
```

Каждая версия стратегии immutable после запуска эксперимента.

## 6. Signal policy и устранение шума

Raw signal не обязан становиться сделкой.

Pipeline:

```text
raw strategy signal
→ signal policy
→ decision
→ order intent
```

Примеры signal policy:

- `none`;
- `ignore_same_side`;
- `confirmed_opposite`;
- `min_hold`;
- `stop_cooldown`;
- `episode_first_signal`;
- `session_filter`;
- `higher_timeframe_gate`.

Possible decisions:

```text
ACCEPT
IGNORE_SAME_SIDE
PENDING_OPPOSITE
CONFIRMED_OPPOSITE
REJECT_MIN_HOLD
REJECT_COOLDOWN
REJECT_SESSION_CUTOFF
REJECT_NO_NEXT_BAR
REJECT_RISK
```

Каждое решение записывается в audit log.

Через API должно быть видно:

> Был ли сигнал? Почему он не стал входом? Он был проигнорирован из-за позиции, cooldown, session cutoff или noise policy?

## 7. Position engine

Position engine — центральная часть проекта.

### Состояния

```text
FLAT
LONG
SHORT
```

Для каждого FIGI отдельное состояние.

### Базовые правила

- максимум одна позиция на FIGI;
- same-side signal не открывает новую позицию;
- усреднение запрещено в v1;
- пирамидинг запрещён в v1;
- flip — отдельная policy;
- position state обновляется последовательно по времени;
- порядок обработки событий детерминирован.

### Position model

```json
{
  "figi": "BBG004730N88",
  "state": "LONG",
  "quantity": 1,
  "entry_time": "2026-06-15T10:05:00Z",
  "entry_price": 324.20,
  "initial_stop": 323.70,
  "target": 325.20,
  "bars_held": 3,
  "minutes_held": 15
}
```

### State transitions

```text
FLAT + BUY  → LONG
FLAT + SELL → SHORT

LONG + BUY  → LONG, ignore
SHORT + SELL → SHORT, ignore

LONG + SELL → policy-dependent:
  ignore
  pending
  exit
  exit_and_flip

SHORT + BUY → policy-dependent:
  ignore
  pending
  exit
  exit_and_flip
```

## 8. Execution model

### Canonical timing

Для каждого бара:

1. Закрывается свеча `t`.
2. Стратегия рассчитывает сигнал только по данным до close `t`.
3. Signal policy принимает решение.
4. Order intent ставится в очередь.
5. Исполнение происходит на следующем доступном open.
6. После fill проверяются stop/target/trailing.
7. Позиция и ledger обновляются.
8. Затем переходим к следующей свече.

### Timeframe execution

Поддержать:

- signal timeframe = execution timeframe;
- 5m signal + 1m execution;
- 1h signal + 5m execution;
- D1 direction + 1h/5m execution.

Для каждого эксперимента явно записывать:

```text
signal_timeframe
execution_timeframe
entry_execution_rule
exit_execution_rule
```

### Intrabar

Если доступны более мелкие свечи:

- использовать их для исполнения stop/trailing;
- записывать execution resolution;
- не смешивать разные уровни времени незаметно.

Если данных нет:

```text
execution_resolution = fallback
```

Это должно быть видно в отчёте.

### Stop/target conflict

Если одна OHLC-свеча затронула и stop, и target:

- использовать консервативное заранее заданное правило;
- например, STOP_LOSS_FIRST;
- записывать `same_bar_conflict_rule`;
- не выбирать результат задним числом.

## 9. Cost model

Единый CostModel для всех исследований.

Поддержать:

- commission per side;
- slippage;
- tick size;
- lot size;
- minimum commission;
- long/short rules;
- rounding;
- exchange fees;
- borrow/short costs при необходимости.

Пример:

```json
{
  "cost_model_id": "canonical_v1",
  "commission_rate": 0.0005,
  "slippage_bps": 2,
  "qty": 1,
  "tick_size": 0.01,
  "lot_size": 1
}
```

Все результаты должны включать:

```text
gross_pnl
commission
slippage
fees
net_pnl
```

## 10. Exit policies

Exit policy отделена от strategy.

Возможные варианты:

- fixed SL/TP;
- ATR stop;
- signal exit;
- confirmed opposite;
- time stop;
- trailing stop;
- chandelier;
- session close;
- structure exit;
- delayed trailing.

Каждая exit policy имеет:

- id;
- version;
- params;
- описание;
- тесты.

Пример:

```json
{
  "id": "atr_trailing",
  "version": "1.0.0",
  "params": {
    "atr_period": 14,
    "initial_stop_atr": 2.0,
    "activation_atr": 1.0,
    "trail_distance_atr": 2.0
  }
}
```

## 11. Session rules

Сессии должны быть отдельным компонентом.

Хранить:

- session open;
- session close;
- timezone;
- trading calendar;
- holidays;
- late entry cutoff;
- whether overnight is allowed;
- forced session close.

Пример:

```json
{
  "session_policy_id": "moex_intraday_v1",
  "timezone": "Europe/Moscow",
  "force_close": true,
  "entry_cutoff_bars": 6,
  "overnight": false
}
```

Нельзя открывать позицию, если она немедленно будет закрыта из-за session close, если это запрещено policy.

## 12. Lab

Lab запускает эксперименты поверх Warehouse и canonical engine.

### Experiment configuration

```json
{
  "dataset_id": "dataset_5m_design_20260615_20260714",
  "strategy": {
    "id": "macd_cross",
    "version": "1.0.0",
    "params": {}
  },
  "signal_policy": {
    "id": "confirmed_opposite",
    "version": "1.0.0",
    "params": {
      "confirm_bars": 2,
      "min_hold_bars": 3
    }
  },
  "exit_policy": {
    "id": "atr_stop",
    "version": "1.0.0",
    "params": {
      "period": 14,
      "multiplier": 1.5
    }
  },
  "execution": {
    "engine_version": "trade_engine_v1",
    "signal_timeframe": "5m",
    "execution_timeframe": "1m",
    "entry": "next_open",
    "qty": 1
  },
  "cost_model_id": "canonical_v1",
  "session_policy_id": "moex_intraday_v1"
}
```

### Lab API

```http
POST /api/v1/experiments
GET /api/v1/experiments
GET /api/v1/experiments/{id}
POST /api/v1/experiments/{id}/cancel
GET /api/v1/experiments/{id}/summary
GET /api/v1/experiments/{id}/metrics
GET /api/v1/experiments/{id}/signals
GET /api/v1/experiments/{id}/trades
GET /api/v1/experiments/{id}/positions
GET /api/v1/experiments/{id}/equity
GET /api/v1/experiments/{id}/drawdown
GET /api/v1/experiments/{id}/by-figi
GET /api/v1/experiments/{id}/by-day
GET /api/v1/experiments/{id}/artifacts
```

## 13. Long-running jobs

Расчёт одного дня может быть синхронным для разработки. Полный месяц, несколько FIGI или matrix run должны выполняться worker-ом.

### Job statuses

```text
PENDING
RUNNING
COMPLETED
FAILED
CANCELLED
```

### Job progress

```json
{
  "job_id": "job_123",
  "status": "RUNNING",
  "stage": "EXECUTION",
  "progress": 42.5,
  "current_figi": "BBG004730N88",
  "processed_figis": 5,
  "total_figis": 14,
  "processed_bars": 120000,
  "total_bars": 300000,
  "message": "Processing SBER"
}
```

API должен вернуть `202 Accepted` и job_id.

Для прогресса:
- polling;
- WebSocket;
- SSE.

Основной вариант:
- FastAPI;
- PostgreSQL;
- Redis;
- отдельный lab worker.

## 14. Matrix runs

Поддержать запуск нескольких заранее определённых экспериментов.

```json
{
  "base_config": {},
  "matrix": {
    "signal_policy": [
      "none",
      "confirmed_opposite",
      "stop_cooldown"
    ],
    "exit_policy": [
      "fixed_sl_tp",
      "atr_stop",
      "atr_trailing"
    ]
  }
}
```

Создавать:

```text
batch
├── experiment_1
├── experiment_2
├── experiment_3
└── ...
```

Каждый дочерний experiment immutable.

Не создавать бесконтрольный optimizer. Сначала исследование должно быть объяснимым и воспроизводимым.

## 15. Metrics

Для каждого эксперимента считать:

### Общие

- events;
- signals;
- intents;
- trades;
- long/short;
- gross;
- commissions;
- slippage;
- net;
- PF;
- expectancy;
- win rate;
- average/median hold;
- max drawdown;
- equity curve;
- trading days.

### По инструментам

- per FIGI;
- positive FIGI count;
- best/worst FIGI;
- concentration;
- net without top1;
- long/short per FIGI.

### По времени

- per day;
- first half / second half;
- month;
- session segment;
- hour of day.

### По причинам

- stop;
- target;
- trail;
- signal exit;
- flip;
- session close;
- data gap;
- cooldown;
- ignored same-side;
- pending opposite.

### QC

- missing bars;
- duplicate bars;
- unavailable data;
- rejected no-next-bar;
- ambiguous OHLC bars;
- fallback execution count;
- look-ahead checks;
- deterministic rerun hash.

## 16. Reports

Для каждого experiment создавать:

```text
summary.json
summary.md
trades.json
trades.csv
signals.json
metrics.json
equity.csv
drawdown.csv
per_figi.csv
per_day.csv
```

Отчёт должен различать:

1. Technical verdict:
   данные и расчёты корректны или нет.

2. Physical verdict:
   как двигалась цена после входов без учёта исполнения.

3. Execution verdict:
   что получилось с fills, costs и constraints.

4. Trading verdict:
   есть ли положительный результат после costs.

5. Research status:
   `REJECT`, `INCONCLUSIVE`, `DESIGN_CANDIDATE`, `VAL_CANDIDATE`, `FROZEN`.

## 17. Графики

График должен иметь режимы:

### Signals

Показывает все raw signals и причины отказа.

### Positions

Показывает реальные позиции из trade ledger:

- entry;
- exit;
- side;
- position zone;
- exit reason;
- net P&L;
- stop/target/trail;
- ignored same-side;
- pending opposite;
- cooldown.

### Equity

Показывает:

- cumulative net;
- drawdown;
- daily P&L.

График не должен самостоятельно пересчитывать сделки. Он получает ledger из Lab/API.

## 18. Тестирование

### Unit tests

Проверить:

- long entry;
- short entry;
- same-side ignore;
- opposite exit;
- opposite flip;
- pending confirmation;
- min hold;
- cooldown;
- session cutoff;
- session close;
- next open;
- ATR stop;
- trailing stop;
- stop/target same bar;
- gap;
- missing next candle;
- commission;
- slippage;
- tick rounding;
- deterministic replay.

### Integration tests

- Warehouse → strategy → engine → ledger;
- API creates experiment;
- worker runs experiment;
- result available via API;
- graph receives same ledger;
- repeated run produces same result.

### Golden tests

Создать маленькие искусственные candle sequences с заранее известным ответом:

```text
scenario_long_profit
scenario_long_stop
scenario_short_profit
scenario_flip
scenario_same_side_noise
scenario_session_close
scenario_gap
scenario_stop_target_conflict
```

## 19. Research discipline

Перед каждым исследованием:

1. Сформулировать гипотезу.
2. Зафиксировать параметры.
3. Зарегистрировать версии strategy/policy/engine/cost.
4. Запустить QC на одном дне и одном FIGI.
5. Проверить ledger.
6. Проверить отсутствие look-ahead.
7. Запустить DESIGN.
8. Проверить H1/H2, per-FIGI и net without top1.
9. Только устойчивый кандидат переводить на VAL.
10. Freeze — только после успешного VAL.

Нельзя выбирать лучшую policy только по одному красивому дню.

## 20. Oracle/hindsight режим

Можно добавить отдельный исследовательский режим, но он не является торговой стратегией.

Он может смотреть в будущее, чтобы изучать:

- MFE;
- MAE;
- потенциальные входы;
- потенциальные выходы;
- лучший горизонт;
- достижимость 1R/2R;
- повторяющиеся признаки перед сильным движением.

Но oracle-результат нельзя использовать как backtest. Future data допускается только для формирования label, не для features или реального решения.

Официальный backtest всегда должен быть causal:

```text
features at time t
→ signal at close t
→ execution at next available open
→ future used only to evaluate outcome
```

## 21. Будущий MTF

После базового engine можно добавить:

```text
D1 direction
→ 1h setup/regime
→ 5m entry
→ 1m execution
```

Роли:

- D1: разрешённая сторона;
- 1h: тренд/коррекция/setup;
- 5m: вход;
- 1m: точное исполнение и stop/trailing;
- engine: управление позицией.

Старший timeframe использует только последнюю закрытую свечу. Нельзя использовать незакрытую текущую дневную или часовую свечу.

## 22. Будущий бот

Бот подключается только после стабилизации Warehouse + Lab.

Бот должен использовать:

- тот же strategy registry;
- те же signal policies;
- тот же position state machine;
- тот же CostModel насколько это возможно;
- те же session rules;
- те же exit policies.

Отличаться будет только live execution adapter:

```text
historical fill adapter
paper fill adapter
broker live fill adapter
```

Логика позиции не должна быть переписана заново для live.

# Итоговая цель

Создать API-first платформу:

```text
POST candles/dataset
POST strategy
POST signal-policy
POST exit-policy
POST experiment
GET job status
GET signals
GET positions
GET trades
GET metrics
GET report
```

Которая позволяет:

- подключать любые новые свечные данные;
- добавлять новые стратегии;
- добавлять фильтры и шумовые политики;
- менять выходы;
- запускать матрицы;
- получать прогресс;
- видеть полный ledger;
- строить график по реальным позициям;
- сравнивать все эксперименты на одной математической основе;
- позже подключить paper/live-бота без переписывания торговой логики.

Главный критерий успеха проекта:

> Мы должны всегда точно знать, какие данные были доступны, какое решение приняла стратегия, почему появилась или не появилась позиция, по какой цене она исполнилась, сколько стоили комиссии, как сформировался P&L и можно ли воспроизвести результат повторным запуском.