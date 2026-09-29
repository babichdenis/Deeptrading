# PORT NOTES — OsEngine → Deeptrading: свечной контур (вход в Фазу C)

Дата: 2026-09-26.
Источник: клон `~/OsEngine` (AlexWan/OsEngine, master) — код изучен без запуска
(решение «без Wine», см. ROADMAP.md).
Назначение: входные заметки для Фазы C — CandleHub. Портируем механики и идеи;
код не переносим (у них C#/.NET/WinForms, у нас Python/asyncio + lightweight-charts).

## 1. Что разбирали (OsEngine)

- `Candles/`: CandleManager.cs, CandleSeries.cs, TimeFrameBuilder.cs, Candle.cs,
  Factory/CandleFactory.cs, Series/ACandlesSeriesRealization.cs, Series/Simple.cs
- `Charts/CandleChart/`: ChartCandleMaster.cs, WinFormsChartPainter.cs,
  IChartPainter.cs, Elements/ (IChartElement, Line, ...), Indicators/MovingAverage.cs
- Для Фазы D (пилот роботов): Robots/ (категории), OsTrader/Panels/BotPanel.cs,
  RiskManager.cs — отдельная заметка будет позже

## 2. Механики → что берём в CandleHub

### 2.1. Одна точка владения сериями (их CandleManager → наш CandleHub)
У них: CandleManager — единственный владелец серий. StartSeries/StopSeries,
подписки на поток сервера; на каждую серию — своя Realization; один инструмент
с разными ТФ не плодит подписки на поток.
У нас: свечи идут напрямую feed.py (live) / replay_feed.py (тест) → runtime
(~5.7 тыс. строк); стратегии копят свои списки свечей по каждому figi
(ensemble_strategy: собственные candles + FRESH_MIN + 5m-логика на лету).
Цель: `app/engine/candlehub.py` — единый владелец серий. Писатели (live-feed,
реплей, бэкфилл) кормят hub; потребители (runtime, тест-режим, API для фронта)
подписываются.

### 2.2. Два события на свечу: «изменилась» и «закрыта»
У них: CandleSeries различает ChangeCandleEvent (текущий, незакрытый бар) и
UpdateFinishedCandleEvent (закрытый).
У нас: runtime видит только закрытые 1m-бары; «незакрытой» свечи в контуре нет.
Цель: CandleSeries(on_closed, on_updated). В live on_updated питается последним
баром/тиками, в реплее — только on_closed. Стратегии сами выбирают, на что
реагировать.

### 2.3. Любые ТФ из минутных (их TimeFrameBuilder)
У них: границы фрейма считает TimeFrameBuilder — включая нестандартные длины
(у них есть «Univer»-ТФ и секундные).
У нас: в БД лежат 1m-бары; 5m-граница вычисляется в ensemble_strategy на лету;
других ТФ в рантайме нет.
Цель: candlehub строит любой ТФ из 1m на лету (build_tf) + кэш. В БД — только 1m,
без дублирования строк под каждый ТФ.

### 2.4. Серия как объект
У них: CandleSeries — хранилище с ограничением ёмкости, доступ по индексу/времени,
CandlesOnlyTime для лёгких потребителей.
У нас: Candle — frozen dataclass в engine/models.py (оставляем как есть); списки
свечей размазаны по потребителям.
Цель: candlehub.CandleSeries — deque(maxlen=...), события (см. 2.2),
snapshot()/range(from_ts) для API и стратегий.

### 2.5. Тонкий слой между данными и рендером (их Chart — справочно)
У них: WinFormsChartPainter рисует напрямую (GDI+), зум/скролл без пересборки;
элементы чарта — простые интерфейсы. Chart быстрый именно потому, что между
данными и пикселями мало слоёв.
У нас: рендер уже на фронте — lightweight-charts (frontend/src/labchart.ts,
294 строки: свечи, объёмы, маркеры сделок, зоны). Бекенду рисовать нечего.
Вывод: переносим принцип, не код — API отдаёт плоские готовые серии
(уже склеенные по ТФ), без агрегации на клиенте.

## 3. Что НЕ переносим

- WinForms/GDI и весь их UI
- Кластерные/тиковые серии, объёмные профили — не нужны в первых версиях
- Их контур коннекторов/серверов — наш T-Invest + MOEX остаётся
- OsOptimizer/Journal — вернёмся в Фазе D выборочно

## 4. Маппинг кода (сейчас → станет)

| Сейчас | Станет |
|---|---|
| app/bot/feed.py — CandleFeed (live, T-Invest) | писатель #1: пишет 1m в hub и БД |
| app/bot/replay_feed.py — реплей из БД | писатель #2: гонит историю в hub (только on_closed) |
| app/bot/runtime.py _process_candle | подписчик hub: on_closed |
| app/bot/ensemble_strategy.py — свои списки свечей + 5m-логика | берёт готовые серии 1m/5m из hub |
| app/bot/moex.py ensure_moex_candles | бэкфилл 1m через hub |
| лаборатория: свой путь к данным | GET /api/v1/candles?figi=&tf=&limit= → labchart.ts |
| engine/models.py Candle | без изменений + новый candlehub.CandleSeries |

## 5. Шаги Фазы C и критерии

1. [ГОТОВО v2, 2026-09-26] `app/engine/candlehub.py`: CandleHub + CandleSeries
   + build_tf; тесты склейки ТФ (стык сессий, гэпы, дубли, неполный фрейм)
   + валидатор/приоритет источников/дыры — 64 теста; см. секцию 7
2. Live-прокорм за флагом (feed.py пишет в hub; бот продолжает работать по-старому)
3. Реплей через hub; ensemble_strategy переключается на серии hub
   (сверка: те же сделки, что сейчас)
4. API /api/v1/candles + перевод графика лабы на него
5. Готовность: тест-режим на hub воспроизводит текущие сделки 1:1; фронт
   показывает 1m/5m/1h без отдельного кода на каждый ТФ

## 6. Риски

- Не сломать работающего бота: всё за флагом, поэтапно; сначала read-only
  (построение ТФ из БД), потом события
- Память: серии с обязательным maxlen (их подход к ёмкости) — 20 тикеров ×
  несколько ТФ
- Границы ТФ: референс — их TimeFrameBuilder; отдельные кейсы — стык сессий
  MOEX и вечерняя сессия
- git поверх SMB медленный: git-операции делаем на .8 (nadts@192.168.1.8, ssh без пароля) — рабочая машина с 2026-09-26; .2 — живой прототип, не трогать без нужды

## 7. CandleHub v2 — реализовано (2026-09-26, вечер)

`backend/app/engine/candlehub.py` v2 (702 строки, фикс от тестов включён) +
`tests/test_candlehub.py` (655 строк, 64 теста). Канон — .8 (64 passed из
venv); тесты гонялись и локально в staging-копии. Поверх базовой механики
(close-time конвенция, склейка ТФ, события on_closed/on_updated, seed/finalize)
движок научился жить с неидеальными данными:

- `validate_candle()` — каждая свеча проходит OHLCV-проверку: NaN/inf, цены
  <= 0, volume < 0, high/low против open/close (с допуском rel_eps — float-шум
  округления не рождает ложных отказов). Битая свеча отбрасывается с событием
  `on_rejected(candle, source, reason)` и счётчиком. Нулевой объём и плоская
  свеча (o==h==l==c) — ЗАКОННЫЕ данные, не ошибки.
- Приоритет источников: `SOURCE_PRIORITY = {live: 0, rest: 1, db: 2}`,
  настраивается `CandleHub(source_priority=...)` (оркестратор может поднять,
  например, MOEX ISS выше live-стрима для индексов). Та же минута от более
  доверенного источника ЗАМЕНЯЕТ принятую (replace), от равного/худшего —
  игнорируется.
- Опоздавшие минуты (ts < последней) для 1m-ряда — это дыры: свеча вставляется
  на своё место (insert), производные ТФ целиком перестраиваются из 1m-истории
  (rebuild + `on_rebuilt()` — перечитай snapshot). Коррекция задним числом не
  оставляет расхождений между ТФ.
- `gap_report(...)` — дыры 1m-ряда (что докачивать). Календарь сессий
  передаётся снаружи колбэком `is_trading_minute` — движок ничего не знает о
  расписаниях.
- `build_tf()` — та же агрегация, что и в живых сериях: одна точка склейки ТФ
  на весь проект (параллельные resample-реализации в ensemble.py и
  candle_cache.py больше не нужны).

### 7.1. Граница ролей (решение 2026-09-26)

CandleHub — ЧИСТЫЙ ДВИЖОК свечей: приём/валидация/дедуп/приоритет
источников/склейка ТФ/гэп-отчёт/seed. БЕЗ сети, БД и подписок. Подписки
тикеров (live add/remove), исполнение докачки по gap_report, сигнальные фиды
(IMOEX → нефть/золото/валюты) — ОТДЕЛЬНАЯ роль оркестратора ПОВЕРХ движка
(аналог CandleManager vs ServerMaster/коннекторы в OsEngine). Движок
наращивается без колупания оркестратора, и наоборот.

### 7.2. Баг, пойманный тестами

Off-by-one в `CandleSeries._register`: при регистрации последнего элемента
`_pos` указывал на позицию СЛЕДУЮЩЕГО бара (`start+len` вместо `start+len-1`)
— replace по `_pos` переписывал соседнюю свечу. До фикса 58 passed /
6 failed, после фикса 64/64 (локально в staging и на .8 из venv). Без теста
replace молча портил бы историю при смене источника на более доверенный.

## Журнал

- 2026-09-26 — заметки собраны по итогам Фазы B (разбор Candles/Charts OsEngine
  без запуска). Фаза B закрыта.
- 2026-09-26 (вечер) — CandleHub v2 написан, протестирован (64/64), доставлен
  на .8 поверх v1; добавлена секция 7: v2-механики, граница ролей, пойманный
  баг _register.


Да — но я бы **не делал CandleHub “генератором индикаторов” в смысле: хранить 15 готовых DataFrame на каждый инструмент**. Лучше сделать его центральным market-data + feature layer, где индикаторы считаются **инкрементально и кэшируются**, а не раздувают базу.

И я бы сразу заложил вот такой состав:

```text
                    ┌─────────────────────┐
T-Invest / Replay ─▶│     CandleHub       │
                    │                     │
                    │  candles            │
                    │  corrections        │
                    │  gaps/backfill       │
                    │  subscriptions      │
                    │  indicators/features │
                    │  events              │
                    │  persistence         │
                    │  logging             │
                    └──────────┬──────────┘
                               │
             ┌─────────────────┼─────────────────┐
             ▼                 ▼                 ▼
          Strategy          Screener          Recorder
```

### Что я бы сразу добавил в CandleHub

**1. CandleStore**

Для каждого:

```text
instrument
timeframe
timestamp
open
high
low
close
volume
state = FORMING / CLOSED
```

Причём хранить именно свечи как первичный источник истины.

---

**2. IndicatorHub**

Не 15 колонок, записанных навсегда в БД, а состояние:

```text
FIGI
  ├── 1m
  │    ├── RSI
  │    ├── ATR
  │    ├── EMA
  │    ├── MACD
  │    └── ...
  │
  ├── 5m
  │    ├── RSI
  │    ├── ATR
  │    └── ...
  │
  └── 15m
       └── ...
```

То есть условно:

```python
hub.indicators.get(
    figi="...",
    timeframe="5m",
    name="rsi"
)
```

и получаем текущее состояние.

**В БД все индикаторы писать не обязательно.**

Это очень важный момент.

---

### 3. Базовый набор индикаторов

Я бы сразу подготовил не только те, которые сейчас используются стратегиями.

#### Trend

* SMA
* EMA
* WMA
* HMA
* VWAP
* Anchored VWAP — позже
* ADX
* +DI / -DI
* MACD
* MACD histogram

#### Momentum

* RSI
* Stochastic
* Stoch RSI
* ROC
* Momentum
* Williams %R
* CCI

#### Volatility

* ATR
* ATR%
* Bollinger Bands
* Bollinger width
* Keltner Channels
* historical volatility
* realized volatility

#### Volume

* volume SMA/EMA
* relative volume
* volume ratio
* OBV
* volume delta — если данные позволят
* volume percentile

#### Price structure

* Donchian High/Low
* rolling high/low
* distance to high/low
* returns:

  * 1 bar
  * 3 bar
  * 5 bar
  * 10 bar
  * 20 bar
* gap
* candle body
* upper/lower wick
* range
* range/ATR

Это уже становится не просто `IndicatorHub`, а **FeatureHub**.

И вот это для Deeptrading особенно интересно.

---

## 4. Не забыть Rank/Relative Features

Вот это я бы **обязательно заложил сразу**.

Потому что когда у нас будет 100–500 инструментов, абсолютный:

```text
RSI = 62
ATR = 1.7%
volume = 2x
```

не всегда так полезен, как положение инструмента относительно остальных.

Например:

```text
BTC-like instrument:
ATR percentile = 93%
Volume percentile = 88%
Momentum percentile = 96%
```

То есть CandleHub/FeatureHub должен потенциально уметь:

```text
momentum_percentile
volume_percentile
atr_percentile
volatility_percentile
trend_percentile
return_rank
```

Но **сам ranking лучше держать отдельным Screener/Ranking слоем**, а CandleHub только предоставляет нормализованные признаки.

---

# 5. Multi-timeframe

Это тоже надо заложить сразу.

Один инструмент:

```text
SBER
 ├── 1m
 ├── 5m
 ├── 15m
 ├── 1h
 └── 1d
```

И очень важно:

**5m не должен самостоятельно пересчитывать исторические 1m свечи.**

Должна быть единая система:

```text
raw candles
      ↓
CandleHub
      ↓
resampler
 ├── 1m
 ├── 5m
 ├── 15m
 ├── 1h
 └── 1d
      ↓
IndicatorHub
```

Тогда стратегия может сказать:

```python
ctx.rsi("5m")
ctx.atr("15m")
ctx.ema("1h")
```

---

# 6. Forming / Closed — обязательно

Это одна из самых важных частей всей архитектуры.

Для каждой свечи:

```text
FORMING
   ↓
FORMING update
   ↓
FORMING update
   ↓
CLOSED
```

И индикаторы должны иметь два режима:

```python
indicator.preview(forming_candle)
indicator.commit(closed_candle)
```

Иначе мы снова получим проблему, которую уже увидели с stateful RSI: **формирующаяся свеча начнёт несколько раз загрязнять состояние индикатора**.

---

# 7. Gap / Backfill

CandleHub должен сам понимать:

```text
10:00
10:01
10:02
10:04
```

→ отсутствует 10:03.

И выдавать:

```text
CANDLE_GAP
```

Дальше:

```text
detect gap
    ↓
request backfill
    ↓
merge
    ↓
deduplicate
    ↓
recalculate affected indicators
```

Это я бы считал **обязательным production-функционалом**, а не nice-to-have.

---

# 8. Dedup + ordering + correction

Тоже внутрь CandleHub:

```text
duplicate candle
out-of-order candle
corrected candle
late candle
```

На выходе стратегии должна видеть **чистый поток**.

Не:

```text
T-Invest → стратегия → разбирайся сам
```

а:

```text
T-Invest
   ↓
CandleHub
   ├── dedup
   ├── ordering
   ├── correction
   ├── gap detection
   ├── state
   └── resampling
   ↓
Strategy
```

---

# 9. Warmup

Очень важно для индикаторов.

При добавлении нового инструмента:

```text
ADD SBER
   ↓
load last N candles
   ↓
warm indicators
   ↓
READY
```

И только потом:

```text
SUBSCRIPTION_READY
```

Стратегия не должна получить:

```text
RSI = None
ATR = None
EMA = None
```

и самостоятельно гадать, когда всё прогреется.

---

# 10. Persistence — отдельно

И вот здесь ответ на твой вопрос про размер базы:

### Я бы НЕ сохранял все индикаторы в основной candle DB.

Например:

```text
500 instruments
× 5 timeframes
× 10 years
× 10 indicators
```

может довольно быстро превратиться в огромный объём.

Лучше:

```text
               CandleHub
                   │
          ┌────────┴────────┐
          ▼                 ▼
    CandleStore         IndicatorCache
          │                 │
          ▼                 ▼
        DB/R2            RAM
```

**Свечи — persistent.**

**Индикаторы — преимущественно derived/cache.**

Если конкретные features нужны для ML dataset — тогда отдельно:

```text
FeatureStore
```

и мы сохраняем **только те признаки, которые реально участвуют в датасете**.

---

# 11. EventBus

Я бы сразу сделал:

```text
CANDLE_FORMING
CANDLE_UPDATED
CANDLE_CLOSED
CANDLE_CORRECTED
CANDLE_GAP

SUBSCRIPTION_ADDED
SUBSCRIPTION_REMOVED
SUBSCRIPTION_READY

INDICATOR_UPDATED
INDICATOR_ERROR

FEED_CONNECTED
FEED_DISCONNECTED
FEED_FALLBACK
```

Тогда:

```text
CandleHub
    ↓
EventBus
 ├── Strategy
 ├── Screener
 ├── Recorder
 ├── Metrics
 └── Logger
```

И не получится, что каждый компонент самостоятельно слушает T-Invest.

---

# 12. Structured logging

Тоже прямо внутрь.

Например:

```text
CANDLE_RECEIVED
CANDLE_CLOSED
CANDLE_GAP
CANDLE_CORRECTED

INDICATOR_UPDATED
INDICATOR_ERROR

SUBSCRIPTION_ADDED
SUBSCRIPTION_FAILED

BACKFILL_STARTED
BACKFILL_COMPLETED
BACKFILL_FAILED
```

С:

```text
instrument
figi
timeframe
timestamp
event
result
reason
latency
source
```

Тогда потом можно будет реально расследовать:

> Почему SBER в 14:32 не дал сигнал?

И увидеть:

```text
14:31:00 candle received
14:31:00 RSI updated
14:31:00 ATR updated
14:31:01 gap detected
14:31:02 backfill started
14:31:02 backfill completed
14:31:02 candle corrected
14:31:02 indicators recalculated
14:31:02 strategy resumed
```

---

# А вот чего я бы НЕ пихал в CandleHub

Это принципиально.

### Не надо:

```text
CandleHub
 ├── Strategy
 ├── Portfolio
 ├── Risk
 ├── Orders
 ├── Position
 ├── Allocation
 └── Execution
```

CandleHub должен отвечать за **market data → clean candles → derived market features → events**.

А дальше:

```text
CandleHub
    ↓
Feature/Indicator layer
    ↓
Screener
    ↓
Ranking
    ↓
Selection
    ↓
Portfolio
    ↓
Risk
    ↓
Execution
```

---

# И я бы немного поменял название архитектурно

Не обязательно в коде прямо сейчас, но концептуально:

```text
CandleHub
```

лучше понимать как:

> **MarketDataHub**

внутри которого есть:

```text
MarketDataHub
│
├── CandleStore
├── Resampler
├── IndicatorHub
├── FeatureHub
├── WarmupManager
├── GapManager
├── SubscriptionManager
├── EventBus
├── Persistence
└── StructuredLogger
```

А вот **Screener/Ranking не надо смешивать с ним**.

---

## Самое важное

Я бы сейчас заложил архитектуру именно под такой масштаб:

```text
              500 instruments
                    │
             SubscriptionManager
                    │
              ┌─────▼─────┐
              │ CandleHub │
              └─────┬─────┘
                    │
       ┌────────────┼────────────┐
       ▼            ▼            ▼
    1m candles   5m candles   15m candles
       │            │            │
       └────────────┼────────────┘
                    ▼
              IndicatorHub
                    │
       ┌────────────┼─────────────┐
       ▼            ▼             ▼
    Trend       Momentum       Volatility
       │            │             │
       └────────────┼─────────────┘
                    ▼
               FeatureHub
                    │
                    ▼
                Screener
                    │
                    ▼
                 Ranking
                    │
                    ▼
                Selection
```

И **не бояться 10–15 индикаторов × инструмент**: если они считаются инкрементально и живут как состояние в RAM, это вообще не та проблема, что хранить 10–15 дополнительных временных рядов в БД.

Причём я бы пошёл ещё дальше: **не создавать 15 объектов индикаторов вручную на каждый тикер**, а сделать декларативный registry:

```python
INDICATORS = {
    "trend": ["ema20", "ema50", "adx14"],
    "momentum": ["rsi14", "roc10", "stoch14"],
    "volatility": ["atr14", "bb20", "hv20"],
    "volume": ["rv20", "obv"],
}
```

и `CandleHub` сам понимает, какие состояния нужно создать при `ADD_SYMBOL`.

**Это сейчас, на мой взгляд, одна из самых важных частей всей архитектуры Deeptrading.** Потому что после этого и OsEngine-подобный Screener, и hot-add инструментов, и ML features, и live/replay parity начинают строиться поверх одного источника истины, а не каждый со своей свечной логикой.
