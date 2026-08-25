Да. **Lab должен быть отдельной рабочей зоной для проверки готовых конфигураций из Warehouse**, а не вторым конструктором стратегий.

Главная модель:

```text
Warehouse:
собрать → посмотреть → поправить → сохранить configuration

Lab:
принять configuration → проверить на пуле → посчитать → сравнить → вынести verdict
```

Для оценки Lab нужно смотреть не только на итоговый net P&L, но и на equity, drawdown, turnover, per-FIGI, стабильность по периодам и полный ledger. Это стандартный принцип отчётности backtest-систем: drawdown считается от исторического high-water mark, а walk-forward проверяет, сохраняется ли результат на последующих временных окнах. [1082][1086][1088]

# 1. Главный экран Lab

Главный экран лучше разделить на пять зон:

```text
┌─────────────────────────────────────────────────────────────┐
│ 1. Configuration header 
колонки с перемещению конйигурации - список тестируемых / в данный момент на тесте / прошедшие тест                                    │
├─────────────────────────────────────────────────────────────┤
│ 2. Run controls / dataset / universe                         │
├─────────────────────────────────────────────────────────────┤
│ 3. Results summary                                           │
├─────────────────────────────────────────────────────────────┤
│ 4. Equity / drawdown / P&L charts                            │
├─────────────────────────────────────────────────────────────┤
│ 5. Tabs: Trades | Signals | FIGI | Days | QC | Report        │
└─────────────────────────────────────────────────────────────┘
```

Lab не должен начинать с пустой формы «выберите стратегию». Он начинает с:

```text
Configuration from Warehouse:
cfg_001
```

## Header

Показывать:

```text
Configuration:
RSI + Bollinger + VWAP, quorum 2/3

Status:
READY_FOR_LAB / RUNNING / COMPLETED / REJECTED

Source:
Warehouse configuration cfg_001 v1.0

Engine:
trade_engine_v1

Cost model:
canonical_v1
```

Кнопки:

```text
[Запустить QC]
[Запустить DESIGN]
[Создать fork]
[Сравнить]
[Экспорт]
```

Кнопка `Создать fork` нужна, если пользователь хочет изменить параметр. Нельзя менять завершённый эксперимент на месте.

# 2. Блок входной конфигурации

Это read-only отображение того, что пришло из Warehouse.

## Signal pipeline

```text
BIAS:
1h EMA Trend

SETUP:
5m Pullback EMA

FILTER:
VWAP same side

ENTRY:
1m Micro Breakout
```

## Strategies

```text
RSI Reversal
  period: 14
  oversold: 35
  overbought: 65

Bollinger Reclaim
  period: 20
  k: 2.0

VWAP Reclaim
  deviation: 2.0
```

## Quorum

```text
Members: RSI, Bollinger, VWAP
Rule: 2 of 3
Window: same bar
```

## Position policy

```text
One position per FIGI
Same-side: ignore
Opposite: exit
Min hold: 0
Cooldown: 0
```

## Exit

```text
Initial stop: ATR(14) × 1.5
Target: 2R
Trailing: off
Session close: enabled
```

## Execution

```text
Signal: closed bar
Entry: next available open
Signal timeframe: 5m
Execution timeframe: 1m
Quantity: 1
```

Внизу:

```text
Configuration is immutable.
To change parameters, create a fork.
```

# 3. Dataset и universe

Lab должен отдельно показывать, **на чём именно он тестируется**.

## Dataset

```text
Dataset:
warehouse_design_v1

Period:
2026-06-15 → 2026-07-14

Timeframes:
1h / 5m / 1m

Timezone:
Europe/Moscow

Data version:
v1

Availability:
13/14 FIGI complete
1 FIGI partial
```

## Universe

Список акций:

```text
[✓] SBER
[✓] GAZP
[✓] ROSN
[✓] MTSS
[✓] MGNT
...
```

Показывать:

- количество FIGI;
- количество свечей по каждому;
- gaps;
- partial data;
- short availability;
- execution availability.

Нельзя допускать, чтобы пользователь не заметил, что результат построен только на одном инструменте.

## Режимы пула

```text
Selected FIGI
Warehouse universe
Custom pool
Sector
Top volatility
```

Но выбранный pool сохранять в конфигурации Lab.

# 4. Запуск Lab

## QC

QC — маленькая проверка механики:

```text
1 FIGI
1 день
```

Показывает:

- raw signals;
- accepted signals;
- rejected signals;
- предполагаемые входы;
- реальные trades;
- exit reasons;
- P&L;
- ledger.

Цель QC:

> убедиться, что конфигурация и движок работают так, как ожидается.

QC не используется для торгового verdict.

## DESIGN

DESIGN — основной исследовательский прогон:

```text
полный период
пул FIGI
реалистичное execution
canonical costs
```

Показывает:

- результаты по всем инструментам;
- per-day;
- H1/H2;
- equity;
- drawdown;
- устойчивость.

## VAL

VAL нельзя запускать из произвольной конфигурации.

Условия:

```text
Warehouse configuration frozen
DESIGN completed
DESIGN verdict allows VAL
parameters locked
VAL dataset separate
```

После запуска VAL нельзя менять параметры внутри того же VAL run.

## Прогресс

```text
Stage: EXECUTION
Progress: 57%

FIGI:
SBER

Processed:
8 / 14 FIGI

Bars:
154 000 / 280 000

Signals:
2 340

Accepted:
482

Trades:
217
```

Stages:

```text
VALIDATING
LOADING_DATA
LOADING_SIGNALS
BUILDING_CONTEXT
EXECUTION
METRICS
REPORT
COMPLETED
```

# 5. Results summary

После завершения наверху показывать карточки.

```text
Net P&L       −18.5 ₽
Gross P&L     +12.4 ₽
Costs         −30.9 ₽
PF            0.82
Trades        392
Max DD        −31.2 ₽
Win rate      43.1%
Expectancy    −0.047 ₽
```

## Обязательно разделять

```text
Gross P&L
Commission
Slippage
Other fees
Net P&L
```

Пользователь должен видеть, не уничтожают ли costs небольшой физический edge.

## Дополнительные cards

```text
Long trades
Short trades
Average hold
Median hold
Turnover
Positive FIGI
Positive days
Max consecutive losses
```

# 6. Воронка Lab

Warehouse показывает предварительную воронку. Lab показывает, что произошло на полном пуле.

```text
Raw strategy signals        2 840
Signals after filters       1 120
Quorum signals                430
Order intents                 398
Executed entries              370
Closed trades                 356
Rejected no next bar             3
Rejected session cutoff        21
Ignored same-side             690
Pending opposite               74
Confirmed flips                18
```

Эта воронка должна быть кликабельной.

При клике на `Ignored same-side` показываются соответствующие сигналы и причины.

# 7. Главный график Lab

График Lab должен строиться **только из фактического Lab ledger и Lab decisions**.

Не использовать Warehouse preview trades как реальные сделки.

## Слои

```text
Candles
Raw signals
Filtered signals
Quorum signals
Accepted intents
Actual entries
Actual exits
Position zones
Initial stop
Target
Trailing
Session boundaries
```

## Отличие preview от actual

Если показывается Warehouse preview:

```text
пунктир / прозрачный слой / подпись PREVIEW
```

Если Lab trade:

```text
яркая стрелка / сплошная линия / подпись ACTUAL LAB
```

## Position zone

Пример:

```text
LONG
from 2026-06-15 10:05
to 2026-06-15 11:05
net: +0.84 ₽
exit: TAKE_PROFIT
```

Цвет:

- зелёный — net positive;
- красный — net negative;
- серый — zero;
- жёлтый — open/unfinished.

# 8. Таблица сделок

Основная вкладка `Trades`.

| # | FIGI | Side | Signal | Entry | Exit | Reason | Hold | Gross | Costs | Net |
|---|---|---|---|---|---|---|---:|---:|---:|---:|

Пример:

```text
17 | SBER | LONG | 10:00 | 10:05 @324.30 |
11:05 @325.50 | TAKE_PROFIT | 60m |
+1.20 | -0.36 | +0.84
```

Фильтры:

```text
FIGI
Long/Short
Profit/Loss
Exit reason
Day
Duration
Strategy member
```

Сортировка:

```text
worst trades
best trades
largest duration
largest costs
```

Клик по сделке:

- центрирует график;
- открывает подробный ledger;
- показывает все сигналы внутри позиции;
- показывает stop/target/trail;
- показывает причину выхода.

# 9. Детали отдельной сделки

Пример правой панели:

```text
Trade #17

FIGI:
SBER

Side:
LONG

Signal:
RSI + Bollinger quorum

Signal time:
10:00

Entry:
10:05 @ 324.30

Exit:
11:05 @ 325.50

Exit reason:
TAKE_PROFIT

Gross:
+1.20 ₽

Commission:
−0.32 ₽

Slippage:
−0.04 ₽

Net:
+0.84 ₽

Bars held:
12

Minutes held:
60
```

## Signal context

```text
RSI:
31.2

Bollinger:
reclaimed lower band

VWAP:
same side

Quorum:
2/3

1h bias:
LONG_ALLOWED

5m setup:
SETUP_LONG

1m trigger:
BREAKOUT_LONG
```

## Signals during position

```text
same-side ignored: 3
opposite pending: 1
opposite cancelled: 1
cooldown rejects: 0
```

# 10. Вкладка Decisions

Эта вкладка отвечает:

> Почему raw signal стал или не стал сделкой?

| Time | Raw signal | State | Decision | Reason |
|---|---|---|---|---|
| 10:00 | BUY | FLAT | ACCEPT | quorum_pass |
| 10:05 | BUY | LONG | IGNORE | same_side |
| 10:20 | SELL | LONG | PENDING | opposite_first |
| 10:25 | BUY | LONG | CANCEL_PENDING | same_side_confirmed |

Коды причин:

```text
ACCEPTED
IGNORE_SAME_SIDE
PENDING_OPPOSITE
CONFIRMED_OPPOSITE
REJECTED_FILTER
REJECTED_QUORUM
REJECTED_MIN_HOLD
REJECTED_COOLDOWN
REJECTED_SESSION_CUTOFF
REJECTED_NO_NEXT_BAR
REJECTED_RISK
```

# 11. Equity curve

График cumulative net:

```text
X: time
Y: cumulative net P&L
```

Показывать:

- equity;
- high-water mark;
- drawdown;
- daily close;
- session boundaries.

При наведении:

```text
Date: 2026-06-24
Daily P&L: +27.9 ₽
Cumulative: −43.2 ₽
Drawdown: −12.4 ₽
Trades: 21
```

# 12. Drawdown

Drawdown считается от максимальной накопленной equity:

\[
DD_t = Equity_t - \max_{s \le t}(Equity_s)
\]

Показывать:

- maximum drawdown;
- date of peak;
- date of trough;
- recovery duration;
- current drawdown;
- max consecutive losing days.

Таблица:

| Episode | Peak | Trough | DD | Recovery |
|---|---|---|---:|---:|

# 13. P&L по дням

| Date | Trades | Gross | Costs | Net | Equity | DD |
|---|---:|---:|---:|---:|---:|---:|

Нужно видеть не только общий месяц, но и распределение результата:

- сколько положительных дней;
- сколько отрицательных;
- лучший день;
- худший день;
- средний день;
- медианный день;
- стандартное отклонение дневного P&L.

# 14. P&L по акциям

| Ticker | Trades | Gross | Costs | Net | PF | Win rate | DD |
|---|---:|---:|---:|---:|---:|---:|---:|

Отдельно показывать:

```text
Positive FIGI: 4 / 14
Net without top1: −12.7 ₽
Top1 contribution: +18.4 ₽
```

Это помогает отличать реальный широкий результат от результата одного инструмента.

# 15. Long/short analysis

| Side | Trades | Gross | Costs | Net | PF | Avg hold |
|---|---:|---:|---:|---:|---:|---:|
| LONG | 210 | ... | ... | ... | ... | ... |
| SHORT | 182 | ... | ... | ... | ... | ... |

Если short запрещён или технически unavailable, это должно быть видно.

# 16. H1/H2 и временные splits

Для DESIGN показывать:

```text
H1: первая половина периода
H2: вторая половина периода
```

| Split | Trades | Net | PF | Max DD | Positive FIGI |
|---|---:|---:|---:|---:|---:|
| H1 | 190 | −7.1 ₽ | 0.88 | −12.4 ₽ | 3/14 |
| H2 | 202 | −11.4 ₽ | 0.76 | −21.2 ₽ | 2/14 |

Если конфигурация хороша только в H1, Lab должен явно показать:

```text
Stability warning: H2 negative
```

# 17. Comparisons

Lab должен позволять сравнивать:

- несколько Warehouse configurations;
- разные exit policies;
- разные signal policies;
- разные engine versions;
- разные cost models.

Но comparison должен разделять:

```text
same signals, different exit
different signals, same exit
different universe
different dataset
```

Нельзя сравнивать эксперименты без предупреждения, если они используют разные данные.

## Таблица сравнения

| Config | Trades | Net | PF | DD | H1 | H2 | Positive FIGI |
|---|---:|---:|---:|---:|---:|---:|---:|

При клике показывать diff:

```text
Changed:
exit.stop_atr: 1.5 → 2.0
cooldown: 0 → 1
No signal changes
```

# 18. QC вкладка

Lab должен иметь отдельную техническую вкладку.

Проверки:

```text
✓ Dataset found
✓ Signals found
✓ No duplicate bars
✓ No duplicate trades
✓ Signal time before execution
✓ No future timeframe leakage
✓ Next bar available
✓ Costs applied
✓ Same-bar rule defined
✓ Engine version pinned
✓ Cost model pinned
✓ Run deterministic
```

Warnings:

```text
⚠ 1 FIGI partial
⚠ 12 fallback fills
⚠ 8 ambiguous OHLC bars
⚠ short data unavailable for 2 FIGI
```

Technical status отдельно от trading status:

```text
Technical verdict: PASS
Trading verdict: REJECT
```

# 19. Lab reports

После завершения создаются:

```text
experiment_summary.json
experiment_summary.md
trade_ledger.json
trade_ledger.csv
signal_decisions.csv
daily_pnl.csv
per_figi.csv
equity.csv
drawdown.csv
report.html
```

## Структура отчёта

1. Configuration.
2. Dataset.
3. Universe.
4. Execution assumptions.
5. Signal funnel.
6. Decisions.
7. Trades.
8. P&L.
9. Equity.
10. Drawdown.
11. Per-FIGI.
12. Per-day.
13. Long/short.
14. H1/H2.
15. QC.
16. Technical verdict.
17. Trading verdict.
18. Next action.

# 20. Verdict в Lab

Lab не должен автоматически говорить только «прибыльно/не прибыльно».

Использовать:

```text
TECH_PASS
TECH_WARN
TECH_REJECT

TRADE_REJECT
INCONCLUSIVE
DESIGN_CANDIDATE
VAL_CANDIDATE
VAL_PASS
VAL_FAIL
```

## Пример

```text
Technical verdict:
PASS

Trading verdict:
REJECT

Reasons:
- Net after costs < 0
- PF < 1
- H1 and H2 negative
- 2/14 positive FIGI
- Net without top1 negative

Next action:
Do not send to VAL
```

## Другой пример

```text
Technical verdict:
PASS

Trading verdict:
INCONCLUSIVE

Reasons:
- Net positive
- PF > 1
- H2 negative
- result concentrated in 1 FIGI
- high turnover

Next action:
Do not freeze; revise configuration
```

# 21. Pipeline Warehouse → Lab

В интерфейсе это должно выглядеть как последовательный переход.

```text
Warehouse configuration
        ↓
Preview completed
        ↓
User clicks "Send to Lab"
        ↓
Lab creates experiment snapshot
        ↓
Lab validates configuration
        ↓
QC
        ↓
DESIGN
        ↓
Verdict
        ↓
VAL candidate or Reject
```

## Snapshot

Когда конфигурация передаётся в Lab, Lab создаёт snapshot:

```json
{
  "lab_experiment_id": "exp_001",
  "source_configuration_id": "cfg_001",
  "configuration_snapshot": {...},
  "dataset_snapshot": {...},
  "engine_version": "trade_engine_v1",
  "cost_model_version": "canonical_v1"
}
```

Если Warehouse-конфигурация потом изменится, старый Lab experiment не изменится.

# 22. Что Lab не должен делать

Lab не должен:

- собирать стратегии вручную;
- менять кворум без создания новой конфигурации;
- пересчитывать raw signals без причины;
- скрывать проигнорированные сигналы;
- показывать preview P&L как реальный;
- менять параметры после запуска;
- использовать другой engine без явной версии;
- выдавать VAL без DESIGN;
- выбирать победителя только по net;
- скрывать отрицательные FIGI или H2.

# 23. Главное различие

```text
Warehouse:
"Вот конфигурация, которую мы собрали.
Вот как она примерно выглядит на SBER.
Вот предполагаемые входы, выходы и P&L."

Lab:
"Вот что получилось по этой же конфигурации
на всём пуле акций при строгом исполнении,
costs, position state и полном аудите."
```

# Финальная формулировка для разработчика

```text
Lab — это не второй конструктор стратегий.

Lab принимает immutable configuration_id из Warehouse.
Configuration уже содержит:
- стратегии;
- параметры;
- timeframe roles;
- фильтры;
- signal policies;
- quorum;
- preview exit rules;
- position sizing;
- session rules.

Lab добавляет только контекст проверки:
- полный dataset;
- universe/pool;
- execution resolution;
- canonical engine;
- canonical CostModel;
- research purpose: QC/DESIGN/VAL.

Lab запускает последовательную симуляцию:
raw signals → decisions → order intents → positions → exits → trades.

Lab не пересчитывает стратегии при изменении exit/policy,
если strategy runs уже существуют.

Главный экран Lab должен показывать:
1. входную configuration read-only;
2. dataset и universe;
3. progress;
4. signal funnel;
5. decision audit;
6. график actual positions;
7. trade ledger;
8. gross/commission/slippage/net P&L;
9. equity curve;
10. drawdown;
11. per-FIGI;
12. per-day;
13. long/short;
14. H1/H2;
15. QC;
16. verdict;
17. next action.

Warehouse создаёт и отбирает конфигурации.
Lab проверяет их на пуле и выносит независимый verdict.
```

Именно такая архитектура убирает прежнюю проблему, когда конфигурации собирались вручную в отдельных скриптах и было непонятно, какие параметры, фильтры и выходы реально участвовали в конкретном результате.