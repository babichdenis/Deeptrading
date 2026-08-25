Лучший интерфейс для вашего Warehouse — не чистый drag-and-drop и не обычная форма с десятками полей, а **гибридный конструктор конфигураций**:

```text
слева: каталог блоков
в центре: визуальный pipeline
справа: параметры выбранного блока
снизу: график сигналов и preview-сделок
```

Такой подход похож на визуальные strategy builders, где условия, выходы и риск собираются блоками, но для вашего проекта важно сохранить отдельные роли `BIAS → SETUP → ENTRY → EXIT`, а не превращать всё в одну бесформенную логическую схему. [1066][1072]

# Рекомендуемый экран

```text
┌──────────────────────────────────────────────────────────────┐
│ Header: FIGI | период | timeframe | config status | Save      │
├───────────────┬──────────────────────┬───────────────────────┤
│ Каталог блоков│ Конструктор           │ Параметры блока        │
│               │                       │                       │
│ Strategies    │ 1h BIAS               │ selected: RSI          │
│ Filters       │       ↓               │ period: 14             │
│ Quorum        │ 5m SETUP              │ oversold: 35           │
│ Exits         │       ↓               │ overbought: 65         │
│ Risk          │ 1m ENTRY              │ [apply]                │
│               │                       │                       │
├───────────────┴──────────────────────┴───────────────────────┤
│ Graph: candles + signals + zones + preview trades             │
├──────────────────────────────────────────────────────────────┤
│ Funnel | Signals | Preview trades | P&L | Debug               │
└──────────────────────────────────────────────────────────────┘
```

## Почему не чистый drag-and-drop

Чистый canvas быстро превращается в непонятную схему:

```text
RSI → AND → VWAP → OR → EMA → quorum → exit → stop → ...
```

Через несколько блоков пользователь уже не понимает:

- что определяет направление;
- что ищет вход;
- что является фильтром;
- что относится к выходу;
- какие таймфреймы участвуют;
- почему сигнал был отклонён.

Поэтому canvas должен быть **типизированным**. Нельзя соединить любой блок с любым.

Например:

```text
BIAS → SETUP → ENTRY → POSITION
```

А `EXIT` и `RISK` подключаются сбоку к позиции:

```text
                 ┌── EXIT
BIAS → SETUP → ENTRY → POSITION
                 └── RISK
```

# Главный принцип UI

Пользователь должен собирать не «стратегию вообще», а конфигурацию из понятных секций:

```text
1. Контекст
2. Направление
3. Setup
4. Точка входа
5. Кворум и фильтры
6. Управление позицией
7. Выход
8. Preview
```

Это соответствует вашей архитектуре Warehouse:

```text
сигналы → конфигурация → preview → Lab
```

# Левая панель: каталог блоков

Каталог лучше разделить по типам.

## Direction / Bias

- EMA trend;
- higher-timeframe trend;
- MACD regime;
- daily direction;
- volatility regime;
- range/trend detector.

## Setup

- RSI reversal;
- Bollinger reclaim;
- Pullback EMA;
- VWAP reclaim;
- squeeze breakout;
- support reclaim;
- Fibonacci pullback.

## Entry trigger

- close above previous high;
- breakout local range;
- micro pullback;
- candle confirmation;
- volume confirmation;
- 1m MACD turn;
- 1m structure break.

## Filters

- VWAP side;
- EMA trend;
- ADX;
- ATR percentile;
- volume ratio;
- session time;
- spread/liquidity;
- cooldown;
- minimum hold;
- setup expiry.

## Quorum / Orchestration

- `AND`;
- `OR`;
- `K-of-N`;
- same-bar quorum;
- causal lookback quorum;
- sequential confirmation.

## Exit

- fixed stop/target;
- ATR stop;
- signal exit;
- confirmed opposite;
- trailing;
- session close;
- time stop.

## Risk

- fixed quantity;
- fixed cash risk;
- percent of equity;
- max concurrent positions;
- max daily loss;
- sector limit.

Каждый блок должен иметь цвет и тип:

```text
blue   — direction
purple — setup
green  — entry
orange — filter
pink   — quorum
red    — exit
gray   — risk
```

# Центральная панель: pipeline

В центре не нужно сразу показывать все детали. Основной вид должен быть компактным:

```text
BIAS
1h EMA Trend
LONG_ALLOWED / SHORT_ALLOWED / NEUTRAL
        ↓
SETUP
5m Pullback to EMA
SETUP_LONG / SETUP_SHORT
        ↓
FILTER
VWAP same side
PASS / REJECT
        ↓
ENTRY
1m Breakout
BUY / SELL
        ↓
POSITION
one position per FIGI
```

При клике блок раскрывается.

## Пример конфигурации

```text
[BIAS | 1h | EMA Trend 20/50]
             ↓
[SETUP | 5m | Pullback to EMA20]
             ↓
[FILTER | 5m | Price above VWAP]
             ↓
[ENTRY | 1m | Break previous high]
             ↓
[POSITION | one per FIGI]
```

Сбоку:

```text
[EXIT | ATR stop 1.5R + target 2R]
[RISK | qty 1]
[SESSION | close at session end]
```

Так визуально сразу понятно:

- старший timeframe задаёт направление;
- средний ищет ситуацию;
- младший выбирает точку;
- exit не является частью raw signal;
- risk не является стратегией.

# Правая панель: параметры

Параметры нельзя показывать как свободный JSON по умолчанию. Нужна форма, созданная из `params_schema`.

Для блока RSI:

```text
RSI Reversal
Timeframe: [5m]
Period:  [luatrade](https://luatrade.com/)
Oversold: [35]
Overbought: [65]
Confirmation: [close > previous high]
```

Для блока EMA:

```text
EMA Trend
Timeframe: [1h]
Fast EMA:  [docs.buildalgos](https://docs.buildalgos.com/strategyguide/buildingstrategies/)
Slow EMA: [50]
Slope bars:  [craftyourstartup](https://craftyourstartup.com/cys-docs/cookbooks/fastapi-websocket-guide/)
Neutral zone: [enabled]
```

Для блока VWAP:

```text
VWAP Filter
Timeframe: [5m]
Side: [same side as bias]
Deviation: [2.0σ]
```

Каждый параметр должен иметь:

- default;
- min;
- max;
- step;
- description;
- warning;
- compatible timeframes.

Например:

```json
{
  "name": "oversold",
  "type": "float",
  "default": 35,
  "min": 5,
  "max": 50,
  "step": 1,
  "description": "Нижняя граница RSI для кандидата long"
}
```

# Отдельный режим для MTF

Для схемы `1h → 5m → 1m` лучше иметь переключатель:

```text
[Single timeframe] [Multi-timeframe pipeline]
```

В MTF-режиме появляются три обязательные колонки:

| Роль | Timeframe | Что делает |
|---|---|---|
| Bias | 1h | Разрешает long/short |
| Setup | 5m | Ищет подготовку |
| Entry | 1m | Выбирает точку |

Нельзя разрешать пользователю случайно поставить:

```text
Bias = 1m
Setup = 1h
Entry = 5m
```

Если это технически возможно, UI должен показать предупреждение:

```text
Необычный порядок timeframe. Bias обычно должен быть старше setup,
а setup — старше entry.
```

# Нижняя часть: график и результат

График должен быть главным рабочим местом, а не декоративным блоком.

## Переключатели слоёв

```text
☑ Candles
☑ Bias state
☑ Setup zones
☑ Raw strategy signals
☑ Filtered signals
☑ Quorum signals
☑ Entry triggers
☑ Preview positions
☑ Stops
☑ Targets
☑ Trailing
☑ Rejected signals
```

## Для MTF на графике

Лучше дать два режима.

### Режим A: один общий график

На основном timeframe показывать:

- свечи 1m;
- цветной фон bias от 1h;
- setup zones от 5m;
- entry markers от 1m.

Это удобно для торговли и детального анализа.

### Режим B: синхронные панели

```text
┌────────────── 1h ──────────────┐
│ EMA, bias, trend background    │
├────────────── 5m ──────────────┤
│ setup, pullback, levels        │
├────────────── 1m ──────────────┤
│ trigger, entry, exit           │
└────────────────────────────────┘
```

Все панели должны иметь общую временную ось и синхронный курсор.

## Синхронный курсор

При наведении на 1m-свечу показывать:

```text
Time: 10:26

1h:
  source candle: 10:00
  bias: LONG_ALLOWED
  EMA20: ...
  EMA50: ...

5m:
  setup: SETUP_LONG
  setup age: 2 bars
  pullback: true

1m:
  trigger: BREAKOUT_LONG
  volume ratio: 1.4

Final:
  alignment: PASS
  entry: next 1m open 10:27
```

Это особенно важно, чтобы нейросеть и пользователь видели не только итоговый BUY, но всю цепочку его формирования.

# Блок “Why no entry?”

Для каждой свечи без входа нужно показывать причину.

Например:

```text
BIAS: LONG_ALLOWED ✓
SETUP: SETUP_LONG ✓
FILTER: VWAP same side ✕
ENTRY: not evaluated
FINAL: NO_ENTRY
```

Или:

```text
BIAS: NEUTRAL ✕
SETUP: not evaluated
ENTRY: not evaluated
FINAL: NO_ENTRY
Reason: bias_neutral
```

Причины должны быть кодами:

```text
BIAS_NEUTRAL
BIAS_CONFLICT
SETUP_MISSING
SETUP_EXPIRED
FILTER_REJECTED
ENTRY_NOT_CONFIRMED
AGAINST_BIAS
SESSION_CUTOFF
POSITION_ALREADY_OPEN
COOLDOWN
NO_NEXT_BAR
```

# Панель Funnel

В Warehouse это один из важнейших экранов.

Пример:

```text
1h bars: 120
  LONG_ALLOWED: 48
  SHORT_ALLOWED: 39
  NEUTRAL: 33

5m setup candidates: 420
  aligned with 1h: 140
  rejected against bias: 180
  expired: 100

1m entry candidates: 1800
  aligned with setup: 67
  rejected by filter: 34
  no active setup: 1660
  accepted entries: 33
```

Так пользователь видит, не слишком ли строгая конфигурация.

# Панель Preview

После нажатия «Preview» Warehouse запускает быстрый расчёт на текущем инструменте/периоде.

Показывать:

```text
Preview status: COMPLETED
Source: SBER, 5m/1m, 2026-06-15
```

## Summary

```text
Raw bias states: 87
Setups: 42
Entry candidates: 18
Preview positions: 11
Win rate: 45%
Estimated net: +3.2 ₽
```

Обязательно крупная надпись:

```text
PREVIEW ONLY
Not validated on the full universe
```

## Preview trades

| # | Side | Bias | Setup | Trigger | Entry | Exit | Reason | Est. net |
|---|---|---|---|---|---:|---:|---|---:|

При клике — график центрируется на позиции.

# Сравнение конфигураций

Сделать режим “Compare configs”.

Пользователь выбирает две или больше конфигураций:

```text
Config A:
1h EMA → 5m pullback → 1m breakout

Config B:
1h EMA → 5m VWAP reclaim → 1m MACD

Config C:
1h MACD → 5m Bollinger → 1m breakout
```

Сравнение:

| Metric | A | B | C |
|---|---:|---:|---:|
| Signals | 42 | 28 | 61 |
| Entries | 18 | 12 | 24 |
| Preview trades | 11 | 9 | 15 |
| Estimated net | +3.2 | +1.8 | −2.1 |
| Median hold | 34m | 21m | 12m |
| Costs share | 31% | 45% | 62% |

Но сравнение должно быть помечено как:

```text
Warehouse preview
```

Только после отправки в Lab появится строгая сравнительная таблица.

# Как сохранить конфигурацию

Кнопка:

```text
Save as configuration
```

Сохраняет:

```json
{
  "configuration_id": "cfg_001",
  "configuration_version": "1.0.0",
  "status": "DRAFT",
  "layers": {
    "bias": {...},
    "setup": {...},
    "filters": [...],
    "entry": {...},
    "quorum": {...},
    "exit_preview": {...},
    "risk_preview": {...}
  }
}
```

После preview:

```text
DRAFT → PREVIEW_COMPLETED
```

Если пользователь уверен:

```text
READY_FOR_LAB
```

Кнопка:

```text
Send to Lab
```

создаёт Lab experiment.

# Что нельзя делать в интерфейсе

Не надо делать один экран, где одновременно свободно редактируются:

- strategy params;
- quorum;
- exit;
- costs;
- position state;
- portfolio risk;
- Lab execution.

Это приведёт к путанице: пользователь не будет понимать, изменение чего дало новый результат.

Нужно визуально разделять:

```text
Signals
Configuration
Preview
Lab
```

# Простой MVP

Для первой версии не реализовывать полноценный свободный графический редактор.

Сделать:

## Шаг 1

Фиксированный pipeline:

```text
BIAS → SETUP → ENTRY
```

## Шаг 2

Карточки выбора:

```text
Bias strategy
Setup strategy
Entry strategy
```

## Шаг 3

Параметры справа.

## Шаг 4

Фильтры и quorum отдельными секциями.

## Шаг 5

Preview на графике.

## Шаг 6

Сохранить configuration.

## Шаг 7

Отправить в Lab.

После того как такой режим станет понятным, можно добавить свободное перемещение блоков.

# Итоговая рекомендация

Лучший интерфейс для Warehouse:

```text
Гибрид:
- карточки для выбора стратегий;
- типизированный pipeline для ролей;
- формы параметров;
- отдельная панель фильтров/quorum;
- синхронный график;
- funnel;
- preview-сделки;
- сохранение configuration;
- кнопка Send to Lab.
```

Главная визуальная модель:

```text
[1h BIAS]
     ↓
[5m SETUP]
     ↓
[Filters / Quorum]
     ↓
[1m ENTRY]
     ↓
[Preview Position]
     ↓
[Send to Lab]
```

И самое важное: Warehouse должен позволять **собрать и увидеть торговую идею**, а не просто выбрать индикатор. Lab начинается только после того, как эта идея зафиксирована как configuration.