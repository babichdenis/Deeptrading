# OsOptimizer — модуль оптимизации (walk-forward)

Разбор `project/OsEngine/OsOptimizer/` + `Market/Servers/Optimizer/` + MCP-обвязки.
Все утверждения взяты из кода клона (`master`, HEAD `34daed3`), номера строк — оттуда же.

---

## 1. Из чего состоит

| Файл | строк | Роль |
|---|---|---|
| `OsOptimizer/OptimizerUi.xaml.cs` | 4450 | окно настройки и результатов (7 вкладок) |
| `OsOptimizer/OptimizerMaster.cs` | 2181 | синглтон-состояние: фазы, параметры, фильтры, торговые настройки |
| `OsOptimizer/OptimizerExecutor.cs` | 1535 | **алгоритм**: перебор, параллелизм, фильтрация |
| `OsOptimizer/OptimizerReportUi.xaml.cs` | 1477 | окно отчёта |
| `OsOptimizer/OptimizerReportCharting.cs` | 1469 | графики серий + robustness-метрика |
| `OsOptimizer/OptimizerReport.cs` | 642 | модель отчёта (`OptimizerFazeReport` / `OptimizerReport` / `OptimizerReportTab`) |
| `OsOptimizer/OptEntity/AsyncBotFactory.cs` | 155 | пул создания ботов |
| `OsOptimizer/OptEntity/WalkForwardPeriodsPainter.cs` | 217 | визуализация фаз |
| `OsOptimizer/OptimizerProfitStagesSaveUi.xaml.cs` | 167 | сохранение «этапов прибыли» |
| **Итого `OsOptimizer/`** | **12 511** | |
| `Market/Servers/Optimizer/OptimizerServer.cs` | 3571 | **своя** реализация `IServer` — «биржа» для одного прогона |
| `Market/Servers/Optimizer/OptimizerDataStorage.cs` | 2390 | общий кэш истории (`DataStorage`) |
| **Итого `Market/Servers/Optimizer/`** | **7920** | |
| `MCP/Modules/OptimizerApi.cs` | 2294 | 29 MCP-инструментов `optimizer_*` |

---

## 2. Архитектура: «одна биржа на один прогон»

Главная идея оптимизатора — **не переиспользовать тестер, а поднять N независимых мини-бирж**.

```
OptimizerMaster (singleton, Master)
 ├─ OptimizerDataStorage          <- ОДИН на всех: кэш свечей/тиков/стаканов
 ├─ OptimizerExecutor             <- поток "OptimizerExecutorThread", перебор
 │    ├─ AsyncBotFactory          <- 10 потоков, создают BotPanel заранее
 │    └─ List<OptimizerServer>    <- по одному на активный прогон, каждый со своей Task
 └─ BotToTest                     <- бот-эталон: с него копируются табы/источники данных
```

- `OptimizerServer` создаётся через `ServerMaster.CreateNextOptimizerServer(storage, num, startDeposit)` (`ServerMaster.cs:1179`) и в конструкторе сразу стартует **`Task.Run(WorkThreadArea)`** (`OptimizerServer.cs:50`). То есть каждый параллельный прогон — отдельная «биржа» со своим потоком.
- Внутри `OptimizerServer` лежит **полный дубликат логики исполнения**: `CheckOrdersInCandleTest` (655), `CheckOrdersInTickTest` (888), `CheckOrdersInMarketDepthTest` (1083), `ExecuteOnBoardOrder` (1561), клиринг (1652–1746), нерабочие периоды (1751), дивиденды, маржа и налоги — всё как в `TesterServer`, но переписано заново. Инструмент описывается классом `SecurityOptimizer` (`OptimizerServer.cs:3119`), а не `SecurityTester`.

**Следствие:** любое изменение в исполнении заявок надо править в двух местах. Это самый заметный архитектурный долг модуля.

### Общий кэш данных

`OptimizerDataStorage.GetStorageToSecurity(security, timeFrame, timeStart, timeEnd)` (`OptimizerDataStorage.cs:2003`) под `lock (_storageLocker)` ищет готовый `DataStorage` в `_storages`, иначе читает с диска и кладёт в кэш. Все `OptimizerServer` получают **ссылку** на один и тот же объект — история в памяти одна на все потоки.

Ключ кэша включает `timeStart`/`timeEnd`, поэтому разные фазы walk-forward — это разные записи (одна и та же бумага грузится по разу на фазу).

Класс `DataStorage` объявлен в `OptimizerDataStorage.cs:2335`.

---

## 3. Walk-forward: как нарезаются фазы

Состояние (`OptimizerMaster`, `#region Optimization phases`, 953–1219):

| Поле | Смысл |
|---|---|
| `TimeStart` / `TimeEnd` | общий период |
| `IterationCount` | число пар InSample+OutOfSample |
| `PercentOnFiltration` | длина OutOfSample в % от InSample |
| `LastInSample` | завершать ли период «голым» InSample |
| `Fazes` | готовый список `OptimizerFaze { TypeFaze, TimeStart, TimeEnd, Days }` |

`OptimizerFazeType` = `InSample | OutOfSample`.

### Расчёт длины InSample

`GetInSampleRecurs` (1061) — рекурсивный подбор. В комментарии прямо написана формула:

```
х = Y + Y/P * С
x — общая длина в днях (известна)
Y — длина InSample
P — процент OutOfSample от InSample
C — количество отрезков
```

Функция уменьшает `Y` на 1 день (а при превышении более чем на 20% — сразу на 5 или 10), пока `allLength <= allDays`. Рекурсия без явного ограничения глубины.

### Сборка фаз

`ReloadFazes()` (1103) идёт по периоду и на каждую итерацию кладёт `InSample`, затем `OutOfSample`:

```csharp
newFazeOut.TimeStart = newFaze.TimeStart.AddDays(daysOnInSample);
newFazeOut.TimeEnd   = newFazeOut.TimeStart.AddDays(daysOnForward);
newFazeOut.TimeStart = newFazeOut.TimeStart.AddDays(1);   // <- сдвиг на 1 день
```

⚠️ Порядок присваиваний такой, что `TimeEnd` вычисляется от **не сдвинутого** `TimeStart`, а потом `TimeStart` сдвигается на день вперёд. В итоге окно OutOfSample получается на один день короче заявленного `daysOnForward`, а между фазами остаётся «дырка» в сутки. Визуально это незаметно, но фактическая длина OOS-окна ≠ настройке.

В конце файла лежит **закомментированный блок** (~40 строк) с алгоритмом точной подгонки суммы дней фаз к `dayAll` — то есть расхождение известно и было отключено.

---

## 4. Перебор параметров

Параметры робота (`IIStrategyParameter`) для каждого прогона клонируются (`CopyParameters`, 701) и прокручиваются как **счётчик с переносом**: инкрементируется первый параметр, при достижении `Stop` сбрасывается в `Start` и перенос переходит к следующему (`ReloadParam` сбрасывает все предыдущие).

Шаг бывает двух видов (`StrategyParameterStepType`):
- `Absolute` — `value += step`;
- `Percent` — `value += |value * step / 100|`, с защитой: если приращение скруглилось до нуля — `+1`, и не выше `Stop`.

Поддерживаются `Int`, `Decimal`, `DecimalCheckBox` (у `Bool`/`String`/`TimeOfDay` перебора нет — только фиксированное значение).

### Оценка числа проходов

`BotCountOneFaze()` (204) **прокручивает весь перебор вхолостую** и считает итерации. Защита от взрыва:

```csharp
if (countBots > 5000000) { SendLogMessage("Iteration count > 5000000. Warning!!!"); return countBots; }
```

Через MCP это доступно как `optimizer_get_pass_count` («нельзя вызывать во время оптимизации»).

### Троттлинг

Параллелизм ограничен настройкой `ThreadsCount` (1..50). В `StartOptimizeFazeInSample`:

```csharp
while (_servers.Count  >= _master.ThreadsCount) { Thread.Sleep(1); }
while (_botsInTest.Count >= _master.ThreadsCount) { Thread.Sleep(1); }
StartNewBot(...);
```

То есть одновременно живёт не больше `ThreadsCount` серверов-«бирж», каждый со своей `Task`. После финиша фазы исполнитель ждёт, пока `_servers` опустеет (`while (true) { Thread.Sleep(50); if (_servers.Count == 0) break; }`).

Оценка остатка времени пересчитывается каждые 20 завершённых прогонов: `secondsToEndAllTests / ThreadsCount` (1456).

### AsyncBotFactory — 10 потоков создания ботов

Создание `BotPanel` (конструктор робота, индикаторы, параметры) дорогое, поэтому его вынесли в пул:

- в конструкторе стартует **10 потоков** `WorkerArea`, имена потоков = их индекс;
- `CreateNewBots()` раскладывает имена по 10 очередям «по кругу» (`i2 += _botsToStart.Count`);
- `GetBot()` крутится в `while(true)` с `Thread.Sleep(1)`, пока нужный бот не появится в `_bots`.

⚠️ `_bots` — обычный `List<BotPanel>`, доступ к нему частично под `lock (_botLocker)`, но **чтение в цикле `GetBot` идёт вне лока**. При 10 писателях и активном читателе это гонка.

---

## 5. Фильтрация между фазами

После каждой InSample-фазы вызывается `EndOfFazeFiltration` (756): из отчётов оставляются только те, что прошли `OptimizerMaster.IsAcceptedByFilter` (955). Пять фильтров, каждый со своим `IsOn`:

| Фильтр | Условие отбраковки |
|---|---|
| `FilterProfit` | `TotalProfit < FilterProfitValue` |
| `FilterMaxDrawDown` | `MaxDrawDawn < FilterMaxDrawDownValue` |
| `FilterMiddleProfit` | `AverageProfitPercentOneContract < FilterMiddleProfitValue` |
| `FilterProfitFactor` | `ProfitFactor < FilterProfitFactorValue` |
| `FilterDealsCount` | `PositionsCount < FilterDealsCountValue` |

Значения по умолчанию из конструктора: profit 10 (выкл), drawdown −10 (выкл), middle profit 0.001 (выкл), profit factor 1 (выкл).

⚠️ Если после фильтрации не осталось **ни одного** отчёта, ветка с предупреждением **закомментирована** — оптимизация молча продолжится с пустым списком, а OutOfSample-фаза просто ничего не запустит. Это ровно та ситуация, о которой предупреждает MCP-документация: «Если `optimizer_get_report` вернул `reports_count: 0` при завершённой оптимизации — проверить `optimizer_filters_get`».

Выжившие боты переносятся в OutOfSample с тем же номером: имя `" InSample"` заменяется на `" OutOfSample"` (`StartAsuncBotFactoryOutOfSample`, 171). Так отчёты потом сопоставляются попарно.

---

## 6. Отчёт и метрики

`OptimizerReport.LoadState(bot)` (281) собирает статистику:

1. собирает все табы (включая вложенные табы скринера);
2. **отбрасывает** позиции со статусами `OpeningFail` / `ClosingFail`;
3. открытые позиции дооценивает по текущему bid/ask (`pos.SetBidAsk(...)`);
4. считает метрики по каждому табу и агрегирует.

Метрики берутся из `PositionStatisticGenerator` (`Journal/Internal/DealStatisticGenerator.cs`, класс внутри называется именно `PositionStatisticGenerator`) — **того же генератора, что рисует статистику в Журнале**. То есть цифры оптимизатора и цифры журнала сопоставимы.

Поля `OptimizerReport`: `PositionsCount`, `ProfitPositionPercent`, `TotalProfit`, `TotalProfitPercent`, `MaxDrawDawn`, `AverageProfit`, `AverageProfitPercentOneContract`, `ProfitFactor`, `PayOffRatio`, `Recovery`, `SharpRatio`, `AverageTimeInPosition`.

⚠️ Два момента в расчёте:
- `tab.AverageProfit = tab.TotalProfit / (posesArray.Length + 1);` — деление на **N+1**, не на N;
- `PositionStatisticGenerator.GetSharpRatio(posesArray, 7)` — безрисковая ставка **жёстко зашита как 7%**.

Отчёт сериализуется в строку (`GetSaveString` / `LoadFromString`), что позволяет сохранять и загружать результаты прогонов (`optimizer_save_report` / `optimizer_load_report`).

### Сортировка результатов

`OptimizerFazeReport.SortResults` (60) — **пузырьковая сортировка** O(n²) по 12 критериям (`SortBotsType`: TotalProfit, PositionCount, ProfitPositionPercent, MaxDrawDawn, AverageProfit, AverageProfitPercent, AverageTime, ProfitFactor, PayOffRatio, Recovery, SharpRatio, BotName). При десятках тысяч прогонов это узкое место.

### Robustness-метрика

`OptimizerReportCharting.UpdateRobustnessChart()` (435+) считает, **насколько устойчив чемпион**: для каждого InSample-отчёта берётся бот под номером `_sortBotNumber`, находится его результат в OutOfSample, вычисляется процентиль `botNum = (i2 + 1) / Count * 100`, и попадания раскладываются по квинтилям:

```
countBestTwenty (≤20%), count20_40, count40_60, count60_80, countWorst20
```

Идея правильная: если лучший на истории параметр стабильно попадает в верхний квинтиль на новых данных — стратегия не переобучена.

---

## 7. Торговые настройки оптимизатора

`OptimizerMaster` держит **собственный** набор (`#region Trade servers settings`, 572–683): `OrderExecutionType`, `SlippageToSimpleOrder`, `SlippageToStopOrder`, `StartDeposit` (по умолчанию 100 000 — не миллион, как в тестере), `CommissionType` + `CommissionValue`, `ClearingTimes`, `NonTradePeriods`.

Всё это пробрасывается в каждый `OptimizerServer` при создании (`CreateNewServer`, 848).

**Важное отличие от тестера:** здесь комиссия задаётся **централизованно** (`master.CommissionType/Value`), а не на каждом боте вручную. Через MCP — `optimizer_trade_settings_set`.

Дивиденды/маржа/налоги берутся из `OptimizerDataStorage` (`DividendsIsOn`, `MarginRegime` по умолчанию `"Off"`, `TaxesIsOn`) и копируются в каждый сервер.

Всё состояние пишется в `Engine\OptimizerSettings.txt`.

---

## 8. Кэш индикаторов

`CacheIndicatorsIsOn` переключает `AindicatorCacheServer.IsOn` (`Indicators/AindicatorCacheServer.cs`, 206 строк). Кэш-ключ:

```csharp
AindicatorCache { IndicatorName, IndicatorSettingsSpecification,
                  SecurityName, CandlesSpecification, CandleStart, CandleEnd }
```

При переборе меняются, например, только стопы — а SMA(200) на тех же свечах считается один раз и переиспользуется всеми прогонами. При каждом старте оптимизации кэш чистится: `AindicatorCacheServer.Clear()` (`OptimizerExecutor.Start`, 55).

---

## 9. Управление

| Метод | Что делает |
|---|---|
| `OptimizerMaster.Start()` | `CheckReadyData()` (модальные окна) → `OptimizerExecutor.Start()` |
| `OptimizerMaster.StartHeadless()` | то же **без** диалогов — для MCP |
| `CheckReadyDataHeadless()` | проверки готовности, возвращает `List<string>` ошибок вместо MessageBox |
| `OptimizerExecutor.Start()` | сбрасывает счётчики, `_serverNum = 1`, стартует поток |
| `Stop()` | ставит `_needToStop`; исполнитель дожидается освобождения серверов и отдаёт частичный отчёт |

`OptimizerExecutor.IsRunning` = `_primeThreadWorker != null`.

Вкладки UI: **Control**, **Fazes**, **Parameters**, **Filters**, **Series and Results**, **Results**, **Out of sample statistic**. Элементы: Threads, Initial funds, Iteration count, «% of time OutOfSample», Last inSample, Cache indicators, Commission Type/Value, «Create optimization scheme», Sort by, Robustness metric, Time to end.

---

## 10. MCP API: 29 инструментов

```
Данные      optimizer_data_get_config / _set_config / _get_status
Дивиденды   optimizer_dividends_get_config / _set_config
Бот         optimizer_bot_get / _set / _tab_get_config / _tab_set_config
Торговля    optimizer_trade_settings_get / _set
Параметры   optimizer_params_get / _set / _reset
Прогон      optimizer_get_pass_count / _get_threads / _set_threads (1..50)
            optimizer_start / _stop / _get_status / _get_report
Отчёты      optimizer_save_report / _load_report
Walk-forward optimizer_phases_get / _set, optimizer_filters_get / _set
Позиции     optimizer_position_support_get / _set
```

Схема `optimizer_phases_set`:
```json
{ "time_start": "ISO", "time_end": "ISO",
  "iteration_count": 3, "percent_on_filtration": 25, "last_in_sample": false }
```

События по SSE: `optimizer.test.progress`, `optimizer.test.finished`.

Сценарий из `CONTEXT_MCP_SCENARIO_V2.md` и его предупреждения:
1. имена бумаг — **с расширением** (`SBER.txt`);
2. настроить **все** вкладки робота, иначе `optimizer_start` вернёт `No securities configured in robot tabs`;
3. включить перебор хотя бы одного параметра (`on: true`), иначе старт отклонится;
4. после `optimizer_data_set_config` перечитать конфиг — даты подстраиваются под реальный диапазон данных;
5. большие диапазоны идут минуты–десятки минут, не прерывать пока `is_running == true`.

---

## 11. Сильные стороны

1. **Честный walk-forward**, а не один проход по истории: InSample → фильтр → OutOfSample, несколько итераций, плюс robustness-метрика по квинтилям.
2. **Настоящий параллелизм**: N независимых «бирж» в отдельных задачах, единый кэш истории — данные в памяти не дублируются.
3. **Кэш индикаторов** — большая экономия на переборе, где индикаторы не меняются.
4. **Метрики общие с Журналом** — результат оптимизации сопоставим с боевой статистикой.
5. **Headless-режим** (`StartHeadless`, `CheckReadyDataHeadless`) — полноценное управление из ИИ-агента без GUI.
6. **Комиссия централизована** — в отличие от тестера.

## 12. Риски и слабые места

1. **Дублирование логики исполнения.** `OptimizerServer` (3571 строка) повторяет `TesterServer` (7293 строки): те же `CheckOrdersInCandleTest/TickTest/MarketDepthTest`, клиринг, дивиденды, маржа, налоги. Два независимых «эмулятора биржи» в одном продукте.
2. **Окно OutOfSample на день короче** заданного из-за порядка присваиваний в `ReloadFazes`; алгоритм точной подгонки дней закомментирован.
3. **Пустая фильтрация проходит молча** — предупреждение и MessageBox закомментированы, OutOfSample тихо отрабатывает в ноль прогонов.
4. **Гонка в `AsyncBotFactory`**: `_bots` читается в `GetBot()` вне `lock`, при 10 потоках-писателях.
5. **Пузырьковая сортировка** результатов при потенциальных миллионах прогонов.
6. **Шарп с безрисковой ставкой 7%** зашит в код, не настраивается.
7. **`AverageProfit` делится на N+1** — систематическое занижение средней прибыли.
8. **Рекурсия `GetInSampleRecurs` без ограничения глубины** — при агрессивных настройках фаз уходит в глубокую рекурсию.
9. **Троттлинг через `while(...) Thread.Sleep(1)`** в горячем цикле перебора.
10. **Юнит-тестов нет**; модуль проверяется только стендом `Tests/McpTestStand --module Optimizer` (интеграционно, на Windows, через GUI-процесс).

---

## 13. Что не проверено

Код не запускался: .NET SDK в песочнице отсутствует (`dotnet: command not found`), проект таргетит `net10.0-windows` с WPF и на Linux не собирается. Всё выше — чтение исходников; номера строк и числа получены `grep`/`wc` по клону. Поведение `ReloadFazes` (пункт 2 рисков) выведено из порядка операторов в коде, но не подтверждено прогоном.


Да. Я бы **не останавливался на 1+2** — из этого уже можно собрать маленький, но правильный **Optimizer/Experiment Engine**, не превращая проект в копию OsEngine Optimizer.

Я бы выстроил развитие так:

### Этап 0 — зафиксировать контракт одного прогона

До sweep важно, чтобы один запуск имел строгую модель:

`Spec → Resolve → Run → Metrics → Trades → Report`

И у каждого запуска был неизменяемый `run_id`/`experiment_id`.

Это потом сильно упростит кэш, WF, сравнение и БД.

---

### Этап 1 — `spec` + `bot-config`

То, что вы предложили, действительно первый шаг.

```text
configs/ose_runs/envelop_v1.json
        ↓
bt_ose_sweep.py real
        ↓
reports/...
```

И:

```text
bt_ose_sweep.py bot-config
        ↓
/bot/mode
        ↓
test_name = experiment_id
```

**Ключевое правило:** один и тот же spec является источником истины. Не должно быть ситуации:

> sweep запущен с одними параметрами, а bot-config случайно с другими.

Я бы сразу добавил в spec:

```json
{
  "experiment": {
    "id": "EXP-000025",
    "name": "envelop_baseline"
  },
  "robot": {
    "name": "ose_envelop_trend",
    "params": {}
  },
  "timeframe": "10min",
  "universe": [],
  "period": {},
  "costs": {},
  "execution": {},
  "outputs": {}
}
```

---

### Этап 2 — Cartesian sweep

Твой пункт 3.

Но я бы сделал его **до массовых реальных прогонов**.

Например:

```json
"sweep": {
  "deviation": [0.3, 0.5, 0.7, 1.0],
  "trail_stop": [0.0, 0.1, 0.5]
}
```

получаем:

**4 × 3 = 12 независимых experiments.**

Важно: каждый получает собственный ID и сохраняет **полный resolved config**.

То есть после запуска должно быть видно не просто:

```text
EXP-31
```

а:

```text
EXP-31
robot=ose_envelop_trend
deviation=0.5
trail_stop=0.1
TF=10m
...
```

Это фундамент воспроизводимости.

---

### Этап 3 — нормальный результат эксперимента

Вот этого в твоих шести пунктах пока не хватает.

Я бы ввёл единый:

```text
ExperimentResult
```

Например:

```json
{
  "experiment_id": "EXP-000031",
  "status": "completed",
  "config_hash": "...",
  "code_sha": "...",

  "metrics": {
    "trades": 34,
    "win_rate": 0.64,
    "gross_pnl": 1234,
    "commission": 120,
    "slippage": 40,
    "net_pnl": 1074,
    "profit_factor": 2.31,
    "max_drawdown": 180,
    "turnover": 120000
  },

  "artifacts": {
    "trades": "...",
    "equity": "...",
    "signals": "..."
  }
}
```

**Optimizer должен работать с этим результатом, а не лазить внутрь конкретного робота.**

---

### Этап 4 — фильтры

Твой пункт 6.

Но я бы сделал их чуть мощнее:

```json
"filters": {
  "min_trades": 30,
  "min_profit_factor": 1.5,
  "min_net_pnl": 0,
  "max_drawdown": 500,
  "min_win_rate": 0.5
}
```

И результат:

```text
PASS
FAIL:min_trades
FAIL:max_drawdown
...
```

Причём **не удалять FAIL из отчёта**.

Optimizer должен показывать:

```text
312 runs
87 passed
225 failed
```

а не только 87 победителей.

---

### Этап 5 — ranking, но без «магического score»

Следующий естественный шаг после фильтра.

Например:

```text
--sort net_pnl
--sort profit_factor
--sort max_drawdown
--sort expectancy
```

И потом уже таблица:

| EXP |   PF |  Net |  DD | Trades |  WR |
| --- | ---: | ---: | --: | -----: | --: |
| 31  | 2.31 | 1074 | 180 |     34 | 64% |
| 42  | 2.05 | 1130 | 260 |     51 | 59% |
| 57  | 1.87 |  980 | 120 |     73 | 61% |

**Я бы пока не вводил composite score.** Он очень легко превращается в подгонку оптимизатора под желаемый результат.

---

### Этап 6 — кэш

Твой пункт 5.

Но ключ я бы сделал не просто hash параметров, а:

```text
SHA256(
    robot
    + resolved_params
    + exits
    + timeframe
    + universe
    + period
    + costs
    + execution
    + data_hash
    + code_sha
)
```

Особенно важен **`data_hash`**.

Иначе:

> «тот же эксперимент»

может оказаться экспериментом на уже изменившихся данных.

---

### Этап 7 — multi-seed / повторяемость

Для синтетических тестов это уже было у вас в Wave A.

Для реального рынка seed обычно не нужен, зато нужен:

```text
same spec
same data
same code
        ↓
same result
```

То есть optimizer должен иметь режим:

```bash
--verify-repeat
```

и проверять:

```text
metrics hash
equity hash
trades hash
```

Это очень полезно для вашего проекта, потому что вы уже специально проверяли Mac/Windows bit-identical результаты.

---

### Этап 8 — сравнение экспериментов

Отдельная команда:

```bash
bt_ose_sweep.py compare \
    --runs EXP-31 EXP-42 EXP-57
```

Она должна показывать не только метрики, но и **что именно изменилось**:

```text
EXP-31 → EXP-42

deviation: 0.5 → 0.7
trail:     0.1 → 0.5
TF:        unchanged
costs:     unchanged
```

Это очень удобно при исследовании.

---

### Этап 9 — OOS / Walk-Forward

И только **после этого** твой пункт 4.

Потому что WF тогда становится просто оркестратором уже существующего механизма:

```text
          ┌─ IS → sweep → filter → top K
period ───┤
          └─ OOS → run top K
```

Например:

```text
2025-01 → 2025-06 IS
2025-07 → 2025-08 OOS

2025-03 → 2025-08 IS
2025-09 → 2025-10 OOS

...
```

И главное — **OOS не участвует в выборе параметров**.

---

### Этап 10 — robustness

После WF я бы добавил ещё одну вещь, которой очень не хватает простому Optimizer:

**parameter neighborhood analysis.**

Допустим оптимум:

```text
deviation = 0.52
```

Но рядом:

```text
0.45 → хорошо
0.50 → хорошо
0.55 → хорошо
0.60 → хорошо
```

Это совсем другая ситуация, чем:

```text
0.50 → хорошо
0.51 → хорошо
0.52 → отлично
0.53 → плохо
0.54 → плохо
```

Второй вариант намного сильнее похож на параметрическую подгонку.

То есть optimizer должен уметь строить **поверхность устойчивости**, а не просто искать максимальное число.

---

### Этап 11 — OOS matrix

Следом:

```text
             OOS1   OOS2   OOS3   OOS4
parameter A   +      +      -      +
parameter B   +      +      +      +
parameter C   -      +      -      -
```

И считать:

* сколько OOS-периодов пережил каждый конфиг;
* median OOS P&L;
* worst OOS;
* dispersion;
* долю положительных OOS;
* degradation `IS → OOS`.

Это уже настоящий **research optimizer**, а не просто перебор параметров.

---

### Этап 12 — несколько роботов

После того как один робот проходит весь pipeline:

```text
EnvelopTrend
PriceChannel
RsiTrade
...
```

можно делать:

```json
"robots": [
  "ose_envelop_trend",
  "ose_price_channel",
  "ose_rsi_trade"
]
```

Но **не раньше**.

Сначала один робот должен полностью проходить:

**spec → sweep → cache → filter → compare → WF → robustness.**

---

## А уже потом — следующий уровень

Я бы разделил будущую систему на 3 слоя:

```text
                 RESEARCH
                    │
          ┌─────────┴─────────┐
          │                   │
       Optimizer          WalkForward
          │                   │
          └─────────┬─────────┘
                    ↓
              Experiment DB
                    ↓
        ┌───────────┴───────────┐
        ↓                       ↓
   Backtest/Replay          Bot Config
        ↓                       ↓
   EngineRunner             Paper/Live
```

И здесь появляется очень важная вещь:

### `Promotion`

Не:

> «Optimizer нашёл лучший параметр → запускаем его в боте».

А:

```text
GENERATED
   ↓
BACKTEST_PASS
   ↓
OOS_PASS
   ↓
ROBUSTNESS_PASS
   ↓
REFERENCE_TEST_PASS
   ↓
PAPER
   ↓
LIVE
```

То есть **optimizer ничего сам не включает в live**. Он только создаёт кандидатов.

---

## Я бы изменил вашу первоначальную последовательность

Не совсем `1 + 2`, а:

**MVP-1**

1. Spec-файл
2. Resolved config
3. Experiment ID/hash
4. Единый ExperimentResult

**MVP-2**
5. Cartesian sweep
6. Filters
7. Compare
8. Cache

**MVP-3**
9. Ranking
10. Walk-forward
11. OOS matrix
12. Robustness / parameter neighborhood

**MVP-4**
13. bot-config bridge
14. Reference Paper Test
15. Promotion pipeline
16. Paper/Live verification

Причём **bot-config bridge я бы технически сделал рано**, но концептуально не давал бы ему решать, какой конфиг «лучший».

---

### Самая важная архитектурная мысль

Я бы вообще не называл `bt_ose_sweep.py` просто sweep-скриптом.

Если вы сейчас это правильно заложите, он может стать:

```text
Experiment Runner
       │
       ├── single run
       ├── sweep
       ├── cache
       ├── filters
       ├── compare
       ├── walk-forward
       ├── robustness
       └── bot-config
```

А **Optimizer** будет всего лишь одним из режимов этого движка.

Это хорошо ложится именно на то, что уже сделали в Wave A: там у вас есть воспроизводимый запуск, EXP ID, seeds, метрики и deterministic verification. Теперь нужно не плодить отдельные скрипты, а превратить этот протокол в **единый контракт эксперимента**.
