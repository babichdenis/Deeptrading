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