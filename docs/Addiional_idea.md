По скриншоту видно, что фронт уже превратился из простого графика в рабочий **конструктор сигналов**:

- есть выбор timeframe;
- включение/выключение индикаторов;
- каталог стратегий карточками;
- волны и семейства;
- параметры стратегий прямо в карточках;
- кнопка расчёта выбранных;
- вкладки «Стратегии» и «Отчёты»;
- отображение сигналов на графике;
- кэш свечей;
- отдельные стратегии MACD, Donchian, RSI, Bollinger, EMA pullback, VWAP reclaim и compression breakout.

Это уже хорошая база. Теперь «безумные идеи» лучше скармливать ИИ не в виде бесконечного списка индикаторов, а как **исследовательские режимы**, которые помогут понять рынок глубже и не разрушат архитектуру.

## Сначала важное ограничение

Не надо просить нейросеть «сделать прибыль любой ценой». За оставшуюся неделю разумнее максимально нагружать её задачами, которые:

- расширяют возможности исследования;
- улучшают визуальный контроль;
- не смешивают signal, execution и P&L;
- сохраняют versioning;
- не используют будущее в торговом режиме;
- дают полезные данные даже при отрицательном результате.

Лучше получить 10 качественных исследовательских инструментов, чем 50 непроверенных стратегий.

## Идея 1: режим «идеальный hindsight» для анализа

Добавить на график отдельный режим, который **разрешает смотреть вперёд**, но не выдаёт себя за торговый сигнал.

Он должен показывать:

- где задним числом был лучший long;
- где был лучший short;
- какой максимум движения был доступен;
- какой минимум риска был до этого;
- сколько R можно было получить;
- какое время удержания было оптимальным;
- какие текущие индикаторы были видны перед этим входом.

Название:

```text
Oracle / Hindsight Analysis
```

Важно:

```text
Oracle = исследовательская разметка
Causal signals = реальные торговые сигналы
```

На графике эти слои должны иметь разные цвета и предупреждение:

```text
ВНИМАНИЕ: этот слой использует будущие свечи и непригоден для торговли.
```

Это позволит сравнить вашу интуицию «вот тут были три хорошие сделки» с тем, что реально могло быть известно в момент входа.

## Идея 2: market regime detector

Добавить индикатор состояния рынка:

```text
TREND_UP
TREND_DOWN
RANGE
HIGH_VOLATILITY
LOW_VOLATILITY
CHAOS
```

Режим можно строить по:

- наклону EMA;
- ADX;
- ATR percentile;
- ширине Bollinger;
- отношению направленного движения к общему диапазону;
- количеству смен направления;
- distance from VWAP.

На графике:

- фон одного цвета для трендового роста;
- другого — для падения;
- нейтральный — для диапазона;
- красный — для хаотичной волатильности.

После этого любая стратегия сможет показывать:

```text
RSI reversal работает только в RANGE
Donchian работает только в TREND
Bollinger reclaim работает только при LOW/HIGH volatility
```

Это полезнее, чем просто добавлять новые индикаторы.

## Идея 3: signal quality score

Для каждого сигнала рассчитывать не только BUY/SELL, но и score:

```text
signal_quality = 0..100
```

Разложить score на компоненты:

```text
trend alignment
volatility suitability
volume confirmation
distance from level
spread/cost risk
recent whipsaw penalty
```

На графике:

```text
BUY 82
SELL 31
```

Но score сначала должен быть прозрачным, не нейросетевым:

```text
+20 trend aligned
+20 volatility suitable
+15 volume confirmation
-25 recent whipsaw
-20 too close to session end
```

Это создаёт мост к будущему ML и помогает понять, какие признаки действительно имеют смысл.

## Идея 4: strategy disagreement map

Показывать не только отдельные сигналы, но и согласие стратегий по каждой свече:

```text
BUY: 5
SELL: 1
HOLD: 2
```

На графике:

- сила зелёного цвета — число long-голосов;
- сила красного — число short-голосов;
- серый — нет согласия.

Отдельно показывать:

```text
2 of 7 strategies agree
5 of 7 strategies agree
```

Это позволит визуально проверить кворум до того, как запускать Lab.

## Идея 5: кворум с объяснением причин

Кворум должен возвращать не только итоговый BUY, но и состав:

```json
{
  "side": "BUY",
  "votes": 4,
  "members": [
    "rsi_reversal",
    "pullback_ema",
    "vwap_reclaim",
    "squeeze_breakout"
  ],
  "opposition": [
    "macd_cross"
  ],
  "window_bars": 0
}
```

На frontend:

```text
BUY 4/7
RSI ✓
EMA pullback ✓
VWAP ✓
Squeeze ✓
MACD ✕
```

Позже можно сравнить:

- same-bar quorum;
- causal lookback quorum;
- weighted quorum;
- quorum по семействам, а не по отдельным стратегиям.

## Идея 6: weighted quorum

Обычный кворум считает все стратегии одинаковыми. Более интересный вариант:

```text
RSI = 1.0
Bollinger = 1.0
EMA pullback = 1.5
VWAP = 1.5
```

Сигнал проходит, если сумма весов выше порога.

Но веса должны быть:

- видны пользователю;
- зафиксированы в конфигурации;
- не меняться автоматически по результату текущего теста;
- сохраняться в experiment config.

Иначе это станет скрытым переобучением.

## Идея 7: signal lifetime

Для каждой стратегии определить, сколько времени сигнал считается действительным:

```text
signal_lifetime_bars = 1
signal_lifetime_bars = 3
signal_lifetime_bars = 6
```

Frontend должен показывать:

- момент возникновения;
- срок действия;
- был ли сигнал исполнен;
- истёк ли без сделки;
- был ли отменён противоположным сигналом.

Это особенно важно для вашей идеи, что движение может начинаться не ровно на одной свече.

## Идея 8: entry zone вместо одной стрелки

Вместо сигнала в одной точке показывать зону:

```text
entry_zone:
  from: 324.10
  to: 324.40
  valid_for: 3 bars
```

Long-зона может строиться вокруг:

- уровня пробоя;
- VWAP reclaim;
- EMA pullback;
- swing support;
- ATR buffer.

Short — зеркально.

Это ближе к реальной торговле: не всегда нужно войти ровно в одну цену, важнее попасть в допустимую область.

Но в Lab нужно отдельно моделировать:

- вход по первой доступной цене;
- вход по лимитной заявке;
- вход после подтверждения;
- пропуск, если цена ушла за пределы зоны.

## Идея 9: explainable trade replay

Для каждой сделки сделать режим пошагового проигрывания:

```text
10:00 — стратегия была FLAT
10:05 — RSI reversal candidate BUY
10:05 — VWAP filter passed
10:05 — quorum 3/5
10:10 — position LONG opened
10:15 — same-side signal ignored
10:20 — opposite signal pending
10:25 — confirmation failed
10:40 — trail activated
10:55 — exit by ATR trail
```

На графике кнопки:

```text
◀ предыдущий бар
▶ следующий бар
▶▶ до следующего события
```

Это один из самых полезных инструментов для проверки, почему ваш взгляд на график расходится с машиной.

## Идея 10: counterfactual analysis

Для каждого rejected signal считать, что было бы, если бы его приняли.

Например:

```text
signal rejected by cooldown
counterfactual:
  entry: 324.20
  exit: 324.80
  hypothetical net: +0.45 ₽
```

Но это должно быть явно обозначено:

```text
COUNTERFACTUAL — не реальная сделка
```

Особенно полезно для:

- rejected by noise;
- rejected by quorum;
- rejected by session cutoff;
- rejected by ML threshold;
- rejected by existing position.

Так вы увидите, действительно ли фильтр устраняет плохие входы или удаляет хорошие.

## Идея 11: parameter sensitivity surface

Не просто таблица «лучший параметр», а карта устойчивости:

```text
MACD fast × slow
ATR stop × target
RSI oversold × confirmation
```

Цвет:

- зелёный — положительный;
- жёлтый — около нуля;
- красный — отрицательный.

Главная цель — увидеть плато, а не одну зелёную клетку.

Хороший кандидат выглядит примерно так:

```text
1.5–2.0 ATR → близкие результаты
RSI 30–35 → близкие результаты
```

Плохой:

```text
только RSI=32.7 и ATR=1.83 дают плюс
```

Такой график надо использовать для диагностики, а не для автоматического выбора параметра.

## Идея 12: automatic data quality audit

Перед каждым расчётом frontend/API должен показывать:

```text
candles: 98.7% available
duplicates: 0
gaps: 3
timezone: Europe/Moscow
next-bar coverage: 96%
1m intrabar coverage: 91%
```

И предупреждения:

```text
HYDR: PARTIAL
SBER: READY
```

Если данных недостаточно, experiment должен получить статус:

```text
TECHNICAL_WARN
```

а не молча считать результат полноценным.

## Идея 13: feature explorer

Для каждой свечи и каждого сигнала показывать доступные признаки:

```text
RSI: 31.2
MACD histogram: -0.42
EMA20 slope: +0.03
ATR percentile: 78
VWAP distance: -1.4σ
volume ratio: 1.8
range compression: true
regime: RANGE
```

Можно фильтровать:

```text
показать все BUY-сигналы,
где RSI<35 и ATR percentile>70
```

Это позволит исследовать идеи визуально до написания нового `.py` файла.

## Идея 14: playbook builder

Создать во frontend визуальный конструктор:

```text
IF:
  RSI < 35
AND:
  price below VWAP
AND:
  regime = RANGE
AND:
  close reclaims previous high
THEN:
  BUY
```

На первом этапе он должен генерировать JSON-конфигурацию, а не произвольный Python-код.

```json
{
  "conditions": [
    {
      "feature": "rsi",
      "operator": "<",
      "value": 35
    },
    {
      "feature": "regime",
      "operator": "=",
      "value": "RANGE"
    }
  ],
  "action": "BUY"
}
```

Затем сервер валидирует эту конфигурацию и создаёт зарегистрированную strategy version.

## Идея 15: portfolio-level research

Сейчас большая часть анализа по одному FIGI. Следующий мощный слой:

- ежедневно выбрать top-N акций по score;
- ограничить число позиций;
- не держать несколько сильно коррелированных бумаг;
- ограничить секторный риск;
- выбирать long/short из общего universe;
- распределять риск одинаково.

Это уже не просто «какая стратегия дала сигнал», а:

> какие позиции выбрать из всех доступных бумаг одновременно?

Но делать это только после корректных signal runs и position engine.

## Идея 16: прогноз направления на несколько дней

В каталоге можно добавить экспериментальный режим:

```text
D1 trend
→ H1 setup
→ 5m entry
```

Или модель:

```text
UP / FLAT / DOWN через 1, 3, 5 дней
```

Но это должно быть отдельным timeframe/strategy family, а не смешиваться с 1m intraday сигналами.

## Идея 17: ML-фильтр сигналов

В будущем:

```text
strategy signal
→ features
→ ML probability
→ signal decision
→ engine
```

Frontend должен показывать:

```text
raw BUY
ML probability: 0.68
threshold: 0.60
decision: ACCEPT
```

Но модель должна быть versioned, а обучение — через отдельный job. Нельзя позволить ей менять engine или сразу включать live.

## Идея 18: виртуальная торговая песочница

Добавить режим ручного replay:

- пользователь кликает по свечам;
- выбирает long/short;
- задаёт stop/target/trailing;
- система показывает, как торговый engine обработал бы решение;
- сохраняется manual trade ledger.

Это поможет сравнить:

```text
что вы бы сделали вручную
vs
что сделал бы алгоритм
```

И часто это лучший способ формализовать вашу визуальную торговую интуицию.

## Что я бы дал нейросети в первую очередь

Не все 18 идей одновременно. Вот приоритетный пакет.

### Пакет A: сделать фронт настоящей исследовательской панелью

```text
1. Strategy catalog
2. Signal layers
3. Explainable tooltips
4. Strategy disagreement map
5. Signal lifetime
6. Regime background
7. Counterfactual rejected signals
8. Data quality audit
```

### Пакет B: подключить Lab

```text
9. Signal run → Lab experiment
10. Position zones
11. Trade replay
12. Full ledger
13. Cost/commission display
14. H1/H2 and per-FIGI reports
```

### Пакет C: исследовательские расширения

```text
15. Parameter sensitivity matrix
16. Oracle/hindsight layer
17. Feature explorer
18. ML signal filter
19. Portfolio selection
20. MTF D1 → H1 → 5m
```

## Готовое общее задание для новой нейросети

```text
У нас уже есть frontend для графика свечей и каталог стратегий.

Не переписывай архитектуру и не смешивай signal calculation с Lab/P&L.

Нужно развить frontend и API в исследовательскую платформу.

Главное правило:
- стратегии создают raw signals;
- signals сохраняются как данные;
- signal policies и quorum применяются поверх;
- canonical trade engine отдельно управляет позициями;
- frontend ничего не считает сам, а получает данные через API;
- все результаты versioned и воспроизводимы.

Сначала реализуй следующие функции:

1. На графике разделить слои:
   - raw signals;
   - quorum signals;
   - accepted signals;
   - rejected signals;
   - entries;
   - exits;
   - position zones;
   - stop/target/trailing.

2. В каждой точке показывать:
   - strategy;
   - run_id;
   - signal_ts;
   - side;
   - status;
   - reason;
   - features;
   - quorum votes;
   - policy decision;
   - execution preview.

3. Добавить Strategy Catalog:
   - карточка;
   - family;
   - wave;
   - timeframe;
   - long/short rules;
   - params_schema;
   - defaults;
   - status;
   - data requirements.

4. Добавить regime background:
   - TREND_UP;
   - TREND_DOWN;
   - RANGE;
   - HIGH_VOLATILITY;
   - CHAOS.

5. Добавить strategy disagreement map:
   - BUY votes;
   - SELL votes;
   - HOLD;
   - same-bar quorum;
   - causal lookback quorum отдельно.

6. Добавить counterfactual view:
   - почему сигнал отклонён;
   - что было бы при его принятии;
   - явно помечать COUNTERFACTUAL;
   - не смешивать с реальными trades.

7. Добавить explainable trade replay:
   - шаг по свечам;
   - список событий;
   - state FLAT/LONG/SHORT;
   - ignored signals;
   - pending;
   - cooldown;
   - exit reason.

8. Добавить Data Quality panel:
   - candle availability;
   - gaps;
   - duplicates;
   - timezone;
   - next-bar coverage;
   - 1m intrabar coverage;
   - fallback count.

9. Добавить Parameter Sensitivity view:
   - фиксированная ограниченная matrix;
   - heatmap;
   - результаты по net/PF/DD;
   - number of trials;
   - никакого неограниченного optimizer.

10. Добавить Oracle/Hindsight слой только как research visualization:
   - явный warning;
   - future data allowed only in oracle;
   - не передавать oracle results в live/causal engine;
   - не называть oracle результат backtest.

11. Подготовить архитектурные endpoints:
   - GET /strategies/catalog;
   - POST /signals/compute;
   - GET /signals/{run_id};
   - POST /quorum/compute;
   - POST /experiments;
   - GET /experiments/{id}/status;
   - GET /experiments/{id}/trades;
   - GET /experiments/{id}/metrics;
   - GET /experiments/{id}/artifacts.

12. Все долгие расчёты запускать как jobs:
   - PENDING;
   - RUNNING;
   - COMPLETED;
   - FAILED;
   - CANCELLED.
   API возвращает job_id и прогресс.

13. Не делать:
   - не запускать live orders;
   - не менять canonical trade engine автоматически;
   - не принимать произвольный Python-код через frontend;
   - не использовать future data в causal mode;
   - не выбирать стратегию по одному лучшему дню;
   - не менять SL/TP внутри strategy_run;
   - не пересчитывать signals при изменении exits/policies.

Сначала реализуй frontend/API для visual signal research.
Lab/P&L подключай только после того, как raw signals, quorum и decision layers
корректно отображаются на графике.
```

## Самая сильная идея

Если выбирать только одну «безумную», я бы выбрал не нейросеть и не ещё 20 индикаторов, а:

> **режим replay + oracle/counterfactual + explainable decisions.**

Он позволит увидеть:

- где вы глазами видите хорошую сделку;
- что было известно в тот момент;
- какой сигнал реально существовал;
- почему система вошла или не вошла;
- что произошло бы при другом выходе;
- где рынок был трендовым, а где просто шумел.

После этого идеи для новых стратегий будут появляться не из фантазии, а из анализа конкретных расхождений между графиком и алгоритмом.