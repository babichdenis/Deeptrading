Да, машинное обучение стоит добавить, но **не как замену каталогу стратегий и не как автономный оптимизатор, который сам ищет прибыль на DESIGN**.

Правильная роль ML на первом этапе:

> использовать машинную модель как дополнительный слой оценки уже сформированных сигналов: разрешить, отклонить, ранжировать или оценить вероятность успешного движения.

Это хорошо вписывается в вашу архитектуру «сигналы = данные»:

```text
свечи
→ стратегия создаёт raw signals
→ ML оценивает signal
→ signal policy принимает решение
→ canonical trade engine исполняет
→ Lab считает P&L
```

## Где разместить ML

Не так:

```text
candles → neural network → buy/sell → live
```

А так:

```text
strategy signal
+ features available at signal time
→ ML score
→ ACCEPT / REJECT / RANK
→ position engine
```

Например, RSI reversal создал `BUY`. ML получает:

- RSI;
- RSI slope;
- ATR;
- расстояние до VWAP;
- размер последних свечей;
- объём;
- направление старшего timeframe;
- время сессии;
- предыдущие сигналы;
- текущий volatility regime.

И возвращает:

```json
{
  "probability_up": 0.64,
  "expected_return": 0.004,
  "risk_score": 0.31,
  "decision": "ACCEPT"
}
```

Но модель не должна сама исполнять сделку. Она отдаёт оценку, а policy и engine принимают формальное решение.

## Какую задачу дать ML

Не начинать с прогноза точной цены. Лучше сделать одну из трёх задач.

### 1. Классификация результата сигнала

Для каждого raw signal:

```text
GOOD
BAD
NEUTRAL
```

Например:

```text
GOOD = price reaches +1R before -1R within 20 bars
BAD = reaches -1R before +1R
NEUTRAL = neither
```

Это прямо соответствует торговому вопросу: стоит ли разрешать конкретный вход.

### 2. Регрессия будущей доходности

Модель прогнозирует:

```text
forward_return_5m
forward_return_1h
forward_return_1d
```

Но приоритетнее использовать не точное число, а диапазон или ожидаемую доходность после costs.

### 3. Ранжирование акций

Для каждого момента модель оценивает все FIGI:

```text
SBER  score 0.64
GAZP  score 0.51
ROSN  score 0.42
```

Потом система выбирает только верхние кандидаты при достаточном score.

Для вашего проекта я бы начал с варианта 1 — **ML-фильтр raw signal**. Он проще, понятнее и хорошо стыкуется с существующим каталогом стратегий.

## Как хранить ML как отдельный слой

Нужно добавить отдельные сущности:

```text
ml_models
ml_features
ml_training_runs
ml_predictions
ml_policies
```

### Model metadata

```json
{
  "model_id": "signal_filter_rsi_v1",
  "model_type": "logistic_regression",
  "version": "1.0.0",
  "target": "hit_1R_before_minus_1R",
  "feature_schema_version": "features_v1",
  "training_dataset_id": "...",
  "training_from": "...",
  "training_to": "...",
  "created_at": "...",
  "artifact_uri": "...",
  "status": "TRAINED"
}
```

### Prediction

```json
{
  "model_id": "signal_filter_rsi_v1",
  "signal_id": 12345,
  "figi": "BBG004730N88",
  "ts": "2026-06-15T10:00:00Z",
  "probability_good": 0.64,
  "expected_return": 0.003,
  "decision": "ACCEPT",
  "features_version": "features_v1"
}
```

ML prediction должна ссылаться на конкретный `signal_id`, а не существовать отдельно от сигнала.

## Как не допустить утечку будущего

Для каждого сигнала:

```text
features_t = данные, доступные до закрытия бара t
label_t = результат после t
```

Features могут использовать только:

- прошлые и текущую закрытую свечи;
- прошлые сигналы;
- текущий session context;
- уже известные данные;
- доступный на тот момент universe.

Labels могут смотреть вперёд, потому что они нужны для обучения. Но labels никогда не должны попадать в features.

Особенно опасны:

- нормализация по всему dataset;
- расчёт индикатора с будущими свечами;
- случайное перемешивание строк;
- обучение на samples, чьи future label windows пересекаются с test;
- выбор параметров после просмотра всех test results.

Для временных рядов нужен walk-forward, а при forward labels — purging и embargo между train и test. [1036][1039][1041]

## Как обучать

Правильный pipeline:

```text
signals
→ build point-in-time features
→ build future labels
→ chronological train/validation/test split
→ fit preprocessing on train only
→ train model
→ freeze model
→ predict next period
→ send predictions to Lab
```

Пример:

```text
Train: 2026-01-01 → 2026-03-31
Validation: 2026-04-01 → 2026-04-30
Test: 2026-05-01 → 2026-05-31
```

Затем окно сдвигается:

```text
Train: 2026-02-01 → 2026-04-30
Test: 2026-05-01 → 2026-05-31
```

Модель в каждый момент знает только прошлое.

## Какую модель выбрать сначала

Не начинать с нейронной сети.

Первая волна:

1. Logistic regression.
2. Random forest.
3. Gradient boosting.
4. Простая модель вероятности по историческим группам.
5. Только потом — neural network.

Причина простая: если logistic regression и gradient boosting не дают устойчивого edge после costs, нейросеть чаще всего просто лучше запомнит шум. Исследования также показывают, что ML-результаты для акций чувствительны к transaction costs, а более сложная модель не гарантирует лучшего торгового результата. [1034]

## Как включить ML в текущий контракт

Существующая схема:

```text
strategy_runs
signals
```

остается неизменной.

Добавляется:

```text
signal_predictions
ml_runs
```

Pipeline:

```text
strategy run
→ signals stored
→ feature builder
→ ML prediction run
→ signal_decision
→ Lab
```

То есть ML не пересчитывает стратегию и не меняет raw signals.

Это сохраняет ваше важное правило:

> смена SL/TP/trailing/кворума/ML-порога не пересчитывает стратегию.

## Новые API

### ML catalog

```http
GET /api/v1/ml/models
GET /api/v1/ml/models/{model_id}
GET /api/v1/ml/features/catalog
```

### Training

```http
POST /api/v1/ml/training-runs
GET /api/v1/ml/training-runs/{id}
GET /api/v1/ml/training-runs/{id}/metrics
```

### Predictions

```http
POST /api/v1/ml/predictions/compute
GET /api/v1/ml/predictions/{run_id}
```

### ML policy

```http
POST /api/v1/ml/policies
GET /api/v1/ml/policies
```

Пример:

```json
{
  "model_id": "signal_filter_v1",
  "base_signal_run_id": "...",
  "threshold": 0.60,
  "action_below_threshold": "REJECT"
}
```

Эта policy уже применяется в Lab:

```text
raw signal
→ ML score
→ threshold
→ accepted/rejected
→ position engine
```

## Как это увидит frontend

В карточке стратегии:

```text
RSI Reversal
  raw signals: 42
  ML filter: signal_filter_v1
  threshold: 0.60
  accepted: 17
  rejected: 25
```

На графике:

- raw signal;
- ML score;
- accepted;
- rejected;
- reason `probability_below_threshold`;
- future label можно показывать только в research view, не в live view.

В reports:

- probability bins;
- фактический outcome по каждому bin;
- calibration;
- P&L до ML;
- P&L после ML;
- сколько сигналов удалено;
- какая доля gross edge потеряна;
- costs и turnover.

## Что ML не должен делать

Не разрешать модели:

- менять `trade_engine_v1`;
- менять CostModel;
- менять свечи;
- удалять raw signals;
- ретроспективно менять labels;
- выбирать параметры по VAL;
- самостоятельно переключать paper/live;
- отправлять live orders;
- обучаться на текущем live/test без версии и approval;
- принимать решение на основе полного будущего графика.

ML должен быть **плагином оценки**, а не владельцем всей системы.

## Первая ML-гипотеза

Я бы зафиксировал её так:

```text
Hypothesis:
для raw-сигналов выбранной стратегии существуют point-in-time признаки,
которые позволяют отличить сигналы с положительным forward outcome
от сигналов, которые будут выбиты stop после costs.
```

Первая реализация:

```text
Base strategy: rsi_reversal
Timeframe: 5m
Target: +1R before -1R within 20 bars
Model: logistic regression
Features: only pre-signal features
Output: probability_good
Policy: accept if probability_good >= 0.60
Execution: canonical trade_engine_v1
```

Сначала:

- 1 день;
- несколько FIGI;
- потом DESIGN;
- потом VAL.

Не обучать модель сразу на всех 15 стратегиях. Сначала один сигнал и один target.

## Правильные статусы

Для ML нужны отдельные статусы:

```text
MODEL_DRAFT
TRAINED
VALIDATED
FROZEN
REJECTED
PAPER_ONLY
LIVE_APPROVED
```

И для prediction run:

```text
PENDING
RUNNING
COMPLETED
FAILED
```

## Итог

Да, машинное обучение стоит добавить в проект, но как отдельный слой:

```text
Strategy → Raw signals
ML → score/probability
Policy → accept/reject/rank
Engine → execute/manage position
Lab → calculate result
```

Первый ML-модуль должен быть простым, объяснимым и ограниченным:

- logistic regression;
- point-in-time features;
- один target;
- walk-forward;
- costs;
- отдельные train/VAL/test;
- без изменения canonical engine.

Главная задача ML — не «предсказать рынок вообще», а помочь ответить:

> Какие из уже сформированных сигналов имеют повышенную вероятность дать движение, достаточное для покрытия риска и costs?


Можно — и это хорошая идея, **если понимать oracle как генератор разметки**, а не как торговый сигнал. Oracle знает будущее и поэтому не может участвовать в live decision, но он может сказать модели задним числом: «в этой точке движение было достаточно хорошим, чтобы его стоило брать».

Лучшее применение: обучить ML не «копировать точную точку oracle», а быть **meta-filter** поверх существующего rule-based ансамбля.

## Что не надо делать

Нельзя обучить модель так:

```text
вход = точный oracle low/high
цель = повторить oracle entry
```

Почему:

- oracle low/high определяются будущими барами;
- точная экстремальная цена недоступна в момент решения;
- модель быстро научится подстраиваться под look-ahead разметку;
- в live она не сможет знать, что текущий бар окажется локальным минимумом.

Модель с такой целью может показать фантастический backtest, но будет «подглядывать» через target definition.

## Что надо делать

Есть два правильных варианта.

### Вариант 1 — ML как meta-label для сигналов ансамбля

Это наиболее практичный путь.

Rule-based ансамбль говорит:

```text
есть BUY или SELL candidate
```

ML отвечает:

```text
какова вероятность, что эту конкретную сделку стоит взять?
```

Разметка для каждого **каузального кандидата на вход**:

```text
1 — после этого входа target достигнут раньше stop
0 — stop достигнут раньше target
0 — истёк max holding time без target
```

Это именно meta-labeling: базовая стратегия задаёт сторону, а ML учится решать, принимать или пропускать конкретный сигнал. Такой подход используют для отсечения false positives, сохраняя primary signal как источник направления. [1297][1301]

### Вариант 2 — oracle как дополнительная training label

Oracle можно использовать для обогащения цели, но не как единственный target.

Например, для каждого candidate signal:

```text
oracle_alignment = 1,
если BUY-сигнал возник до/около oracle low
или SELL-сигнал возник до/около oracle high
```

Но это должна быть вторичная диагностическая/вспомогательная метка. Финальная торговая target-label всё равно должна быть экономической:

```text
net after costs > 0
```

или:

```text
target before stop
```

Иначе модель будет учиться «угадывать рисунок ZigZag», а не торговать исполнимое движение.

## Как сделать конкретно у вас

### Набор строк обучения

Не брать все 1m бары — будет море шума и imbalance классов.

Брать только события:

```text
raw 5m setup
или quorum candidate
или entry-breakout candidate
```

Для каждого события сохранять то, что было доступно на момент `decision_ts`.

```json
{
  "decision_ts": "...",
  "figi": "RUAL",
  "side": "BUY",

  "rsi_5m": 34.2,
  "bollinger_z": -1.7,
  "ema_distance_5m": -0.4,
  "vwap_distance": -0.3,
  "macd_histogram": 0.02,
  "donchian_position": 0.08,

  "bias_1h": "bearish",
  "bias_alignment": false,
  "quorum_count": 3,
  "functions_mask": "...",
  "session": "main",
  "minutes_from_open": 83,

  "breakout_strength": 0.18,
  "atr_5m": 0.11,
  "realized_volatility": 0.24,
  "spread_bps": null,
  "obi_5": null,
  "ofi_5s": null
}
```

Когда подключите стакан, его признаки добавлять только при наличии **исторических snapshot на момент решения**, а не задним числом.

### Разметка

Использовать фактические правила движка:

```text
entry = open следующего доступного 1m-бара
TP = текущий target
SL = текущий stop
vertical barrier = max hold N баров или session close
costs = commission + slippage
```

Цель:

```text
y = 1, если TP был достигнут раньше SL и net > 0
y = 0 во всех иных случаях
```

Либо использовать три класса:

```text
+1 = TP first
 0 = time exit
-1 = SL first
```

Разметка «какой из TP/SL/time barriers достигнут первым» соответствует triple-barrier подходу; она лучше фиксированного future-return, потому что отражает путь цены и реальную risk policy. [1294][1299]

### Где здесь oracle

Добавить поля исключительно для анализа качества разметки:

```text
oracle_nearby
oracle_direction_match
oracle_lag_minutes
oracle_move_size
```

Затем проверить:

```text
P(y=1 | oracle_nearby)
P(y=1 | no_oracle_nearby)
```

Если oracle-aligned кандидаты действительно чаще доходят до TP, он подтверждает ценность вашей каузальной signal geometry.

Но не подавать `oracle_nearby` как feature в live-модель — это было бы look-ahead.

## Самая полезная модель в вашей ситуации

С учётом того, что:

```text
raw coverage oracle = 96%
bias/quorum режут большую часть сигналов
```

ML лучше всего поставить **вместо hard gates**, а не после всех фильтров:

```text
raw 5m candidates
→ feature extraction
→ ML probability score
→ threshold / position sizing
→ engine
```

Тогда ML может научиться, что контртрендовый BUY при bearish EMA иногда хороший, вместо того чтобы `bias` навсегда его запрещал.

Но не заменять всё разом. Сравнить:

```text
R0: current bias=info + quorum=2
R1: raw candidates + ML meta-filter
R2: quorum candidates + ML score
R3: current baseline + ML score only
```

В первой итерации я бы выбрал **R3**: ML только ранжирует или фильтрует существующие входы. Так проще отследить, улучшает ли он baseline, а не меняет всю стратегию.

## Как избежать утечки

Обязательно:

- признаки только до `decision_ts`;
- entry только на следующем доступном 1m open;
- labels строятся после завершения сделки;
- train/test делить строго по времени;
- purge или embargo вокруг границы train/test, потому что сделки и labels могут перекрываться;
- fit scaler/encoder только на train;
- не смешивать строки будущего месяца в train;
- не использовать `oracle_*` как live features;
- сохранять model version и набор признаков.

При пересекающихся во времени торговых labels обычный случайный `train_test_split` создаёт утечку; временное разделение и purge/embargo снижают этот риск. [1304][1306]

## Правильный эксперимент

Не сразу подключать ML к основному бэктесту.

```text
Train: апрель–июнь
Validation: июль
Locked holdout: август
```

На июле выбрать только один threshold, например:

```text
p_success >= 0.55
```

Затем заморозить модель и threshold.

На августе проверить:

```text
baseline ensemble_main_v1
vs
ensemble_main_v1 + ML meta-filter
```

И сравнить:

```text
trades
gross
costs
net
PF
max drawdown
net/trade
recall profitable trades
ML-rejected counterfactual net
calibration: predicted probability vs actual success
```

Если ML просто сократил число сделок, но не улучшил `net/trade`, PF, drawdown или out-of-sample net — его не подключать.

## Итог

Да: **обучать ML с использованием oracle можно**, но oracle должен быть учителем-разметчиком/диагностикой, а не входным признаком и не прямой целью «поймай точный минимум».

Наиболее безопасная формула:

```text
Oracle:
  анализирует, где были хорошие движения

Rule-based ensemble:
  создаёт каузальные BUY/SELL candidates

ML:
  оценивает вероятность,
  что candidate дойдёт до TP раньше SL после всех costs
```

Так ML получает пользу от исторического знания будущего в label, но на live использует только информацию, доступную в момент сигнала.


Да: чтобы ML учился сильнее, ему нужен не просто «год свечей», а **год или больше качественно размеченных событий**. Для вашей схемы правильная единица обучения — не каждый 1m-бар, а каждый каузальный кандидат на вход, созданный текущим ансамблем.

И ML я бы не ставил равным «голосом» рядом с RSI/Bollinger. Лучше использовать его как **meta-filter и скорер** после rule-based кандидата.

## Что именно обучать

Текущий ансамбль уже умеет генерировать сторону:

```text
BUY / SELL candidate
```

У ML другая задача:

```text
какова вероятность, что этот конкретный candidate
дойдёт до TP раньше SL после costs?
```

Это называется meta-labeling: первичный слой ищет много возможностей с высоким recall, а второй слой отсекает ложные входы и повышает precision. Типичная цена — меньше сделок, поэтому нужно контролировать coverage, чтобы снова не получить «4 сделки из 3000». [1313][1319]

Схема:

```text
5m rule-based candidates
→ ML probability score
→ threshold / sizing
→ EngineRunner
→ entry at next 1m open
```

Не так:

```text
RSI vote
Bollinger vote
ML vote
```

Потому что ML не является ещё одним независимым индикатором. Он видит контекст всех признаков одновременно и должен оценивать **качество уже возникшего сигнала**.

## Сколько истории нужно

### Минимум

```text
6 месяцев 1m + 5m данных
```

### Нормально для первой модели

```text
12 месяцев
```

### Лучше

```text
12–24 месяца
```

Но важнее не число свечей, а число размеченных кандидатов:

```text
raw candidates
quorum candidates
accepted entry candidates
```

Если на пяти FIGI получается, например:

```text
50–300 candidates на FIGI в месяц
```

то за год это даст несколько тысяч наблюдений — уже достаточно для простой регуляризованной модели.

Если кандидатов мало, не надо лечить это сложной нейросетью. Лучше:

- расширить universe ликвидными FIGI;
- добавить больше исторических месяцев;
- сохранять raw candidate события до hard filters;
- использовать более простую модель.

## Какие таймфреймы показывать ML

ML не нужно отдавать «все свечи всех ТФ». Нужно собрать признаки, доступные строго на `decision_ts`.

Для вашего pipeline оптимальна такая структура:

| Уровень | Таймфрейм | Роль |
|---|---|---|
| Entry context | 1m | исполнение, breakout, краткий импульс, spread/slippage |
| Setup context | 5m | основной сигнал и индикаторы |
| Regime context | 1h | тренд, волатильность, режим |
| Session context | calendar/time | минута от открытия, день недели, близость закрытия |
| Liquidity context | tick/L2, когда появится | spread, depth, OFI, OBI |

### 1m: микро-контекст входа

```text
return_1m
return_3m
return_5m
volume_ratio_1m
breakout_distance_bps
range_1m
ATR_1m
distance_to_1m_high_low
```

### 5m: основной контекст

```text
RSI
Bollinger z-score / position
MACD and histogram
EMA distance and slope
VWAP distance
Donchian position
Squeeze state
ATR / realised volatility
quorum_count
functions_mask
signal strength
```

### 1h: режим, а не жёсткий veto

```text
EMA50 slope
price vs EMA50
trend strength
volatility regime
hourly range percentile
bias_aligned
```

Поскольку `bias=info` уже оказался полезнее hard veto, это отличный признак для ML, а не бинарный запрет.

### Сессия

```text
minutes_from_main_open
minutes_to_main_close
opening_auction flag
lunch / middle-of-day bucket
day_of_week
instrument_id
```

Только не отдавайте модели «день месяца» или случайный raw timestamp: она сможет запомнить календарные совпадения вместо рыночного механизма.

### Стакан — позже

Если в live вы сможете получать и архивировать L2 на момент сигнала, добавить:

```text
spread_bps
depth_bid_5
depth_ask_5
OBI_1 / OBI_5
OFI_5s / OFI_30s
aggressive buy/sell volume
```

Но исторический стакан нельзя выдумывать по свечам. Пока нет синхронных исторических снимков L2, не включать book-features в backtest.

## Какие labels использовать

Для первой версии — ровно те же условия, что реально применяет `EngineRunner`.

```text
decision_ts
→ entry at next 1m open
→ TP / SL / max-hold / session rule
→ commission + slippage
```

### Бинарный label

```text
y = 1:
  target hit before stop
  и net after costs > 0

y = 0:
  stop hit first
  или time/session exit
  или net <= 0
```

### Лучше: multi-class label

```text
+1 = TP first
 0 = timeout / flat exit
-1 = SL first
```

Но для старта бинарный формат проще и лучше контролируется.

Разметка через `TP / SL / vertical time barrier` соответствует path-dependent реальной сделке: важно, какой барьер достигнут первым, а не только доходность через фиксированное время. [1299][1300]

## Как использовать oracle

Oracle можно и нужно держать в train dataset, но только в двух ролях:

```text
1. Проверка coverage кандидатов
2. Аналитическая target/diagnostic метка
```

Например:

```text
oracle_nearby
oracle_direction_match
oracle_lag_minutes
oracle_move_size
```

Но:

```text
oracle_* нельзя подавать в модель как live feature
```

Это look-ahead, поскольку oracle строится по будущему движению.

И ещё важнее: модель не обязана учиться ловить точный oracle-low/high. Её цель — взять исполнимые rule-based кандидаты, у которых после следующего 1m open реальный TP достигается раньше SL.

## Как обучать правильно

### Разбиение строго по времени

Например:

```text
Train: август 2025 — май 2026
Validation: июнь 2026
Test: июль 2026
Locked holdout: август/сентябрь 2026
```

Все feature transforms — scaler, encoder, imputer — fit только на train.

### Walk-forward

После первой проверки:

```text
Train: 6–12 предыдущих месяцев
→ predict: следующий месяц
→ сдвинуть окно на месяц
→ повторить
```

Получить один итог из прогнозов на каждом следующем месяце, а не красивую метрику на единственном split. Walk-forward сохраняет порядок времени, в отличие от случайного перемешивания строк. [1308][1311]

### Purge и embargo

У ваших labels есть будущий горизонт: сделка может жить несколько минут или часов. Соседние события могут иметь пересекающиеся окна цены.

Поэтому нельзя делать:

```python
train_test_split(..., shuffle=True)
```

Нужно:

```text
purge:
  убрать train-события,
  чьи label windows пересекаются с test window

embargo:
  после test boundary оставить буфер
```

Это снижает утечку между соседними по времени сигналами. [1310][1316]

## Какую модель выбрать

Не начинать с нейросети.

Первая линия:

```text
logistic regression + regularization
```

Плюсы:

- понятно, какие признаки важны;
- легко проверять калибровку вероятности;
- меньше риск переобучения;
- проще дебажить.

Вторая линия:

```text
CatBoost / LightGBM / XGBoost
```

Только если простая модель показала устойчивость, а данных достаточно.

Для ваших табличных multi-timeframe features я бы выбрал:

```text
Baseline: LogisticRegression
Candidate: CatBoostClassifier
```

Обе сравнивать на одинаковых walk-forward predictions.

## Как ставить ML в ансамбль

### Режим 1: shadow mode

Сначала ML не блокирует сделку:

```text
rule-based candidate
→ ML score записывается
→ движок торгует baseline
→ потом сравниваем:
   high score vs low score
```

Это самый безопасный первый этап.

### Режим 2: мягкий threshold

После OOS-проверки:

```text
p >= 0.50:
  normal position size

0.45 <= p < 0.50:
  reduced size

p < 0.45:
  skip
```

Но пороги задаются на validation и замораживаются до следующего holdout.

### Режим 3: position sizing

Лучше, чем сразу жёстко отбрасывать:

```text
size =
base_size × clamp(
  (p - 0.50) / 0.20,
  min=0.25,
  max=1.00
)
```

Однако sizing добавлять только после того, как probability окажется откалибрована на OOS.

## Защита от новой проблемы coverage

В API ML должен вернуть:

```text
ml_candidates_total
ml_scored
ml_accepted
ml_rejected
coverage_vs_baseline
average_score_accepted
average_score_rejected
```

И обязательные guardrails:

```text
min_trade_coverage
min_candidates_per_day
max_rejection_rate
```

Например, ML не имеет права отсеять 95% сигналов без явного предупреждения. Его ключевая метрика — не accuracy, а:

```text
OOS net after costs
PF
max drawdown
net per trade
trade coverage
rejected-trade counterfactual net
calibration
```

## Конкретный план

```text
1. Выгрузить 12 месяцев 1m/5m/1h данных по 5 FIGI.
2. Построить датасет из raw/quorum entry candidates.
3. Для каждого candidate сформировать features на decision_ts.
4. Разметить outcome теми же TP/SL/cost rules, что EngineRunner.
5. Oracle использовать только для диагностики, не как feature.
6. Обучить Logistic Regression на первых 8–9 месяцах.
7. На следующем месяце выбрать единственный threshold.
8. На последующих 2–3 месяцах провести frozen OOS.
9. Запустить shadow-mode в paper trade.
10. Лишь затем включать ML как soft filter/size score.
```

## Главный ответ

> Да, год свечей нужен, но не ради «больше баров». Нужен год **каузально размеченных входных кандидатов** из разных режимов рынка.

И ML лучше использовать не как ещё один голос «BUY/SELL», а как судью уже возникшего сигнала:

```text
ансамбль говорит: «есть идея LONG»
ML говорит: «вероятность, что именно эта идея окупит costs и достигнет TP раньше SL — 0,68»
движок решает: взять, пропустить или уменьшить размер.
```