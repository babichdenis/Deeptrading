# MOEX-индексы в коде и измерители тренда

Два вопроса: (1) как MOEX-индексы влияют на торговлю, (2) есть ли измеритель тренда.
Все утверждения проверены по клону `master`, HEAD `34daed3`.

---

## Часть 1. MOEX-индексы: где они реально есть

### Сначала — поправка

**Тикеры отраслевых индексов MOEX в коде не используются вообще.** Проверил:

```
grep -rn 'MOEXOG|MOEXFN|MOEXMM|MOEXCN|MOEXEU|MOEXTN|MOEXTL|MOEXCH|MOEXIT' .
→ ни одного совпадения в .cs
```

В `CONTEXT_SECTORS_SET.md` они стоят рядом с названиями секторов как подписи (строки 20–28). Там же есть примечание: «Расчёт индекса MOEXTL биржа остановила 03.2026 — бумаги торгуем, индекс не живой» — то есть авторы сами фиксируют, что отраслевой индекс как данные им не нужен. Секторальные роботы определяют сектор **списком акций** (`SectorsSetBollingerMomentum.cs:262–270`):

```csharp
CreateSector("Oil&Gas",   new[] { "GAZP","LKOH","ROSN","NVTK","TATN","SNGS","SNGSP","TATNP","TRNFP","BANEP" });
CreateSector("Finance",   new[] { "SBER","SBERP","VTBR","T","MOEX","BSPB" });
CreateSector("Metals",    new[] { "PLZL","GMKN","ALRS","MAGN","CHMF","NLMK","MTLR","SELG","TRMK" });
CreateSector("IT",        new[] { "OZON","YDEX","VKCO","ASTR","POSI","HEAD","CNRU" });
// + Consumer, Power, Transport, Telecom, Chemistry — всего 40 бумаг
```

Индексных данных они не получают: `grep -c 'BotTabType.Index|BotTabIndex'` по обоим файлам `Robots/Sectors/` → **0**. «Сектор» — именованная корзина акций, показатель сектора = средний RSI этих акций, посчитанный самим роботом.

### Реальных канала четыре

#### 1. `Benchmark` — только картинка, на торговлю не влияет

`OsData/Benchmark.cs` (286 строк), enum `BenchmarkSecurity { Off, BTC, MCFTR, SnP500, IMOEX }` (строка 278).

```csharp
if (benchmark == BenchmarkSecurity.IMOEX.ToString())
{
    _serverType = ServerType.MoexDataServer;
    _secName    = "IMOEX";
    _secId      = "IMOEX#stock#index#SNDX#Индексы фондового рынка";
    _secClass   = "Индексы фондового рынка#SNDX";
    _fileSetBenchmark = @"Data\Benchmark\IMOEX\Day\IMOEX.txt";
}
if (benchmark == BenchmarkSecurity.MCFTR.ToString())   // индекс полной доходности «брутто»
    _secId = "MCFTR#stock#index#RTSI#Индексы РТС";
```

Единственный потребитель — **Журнал**: `JournalUi2.xaml.cs:68–73` добавляет эти пункты в `ComboBoxBenchmark`, по клику «Refresh» скачиваются дневные свечи и рисуются поверх кривой эквити. Это визуальное сравнение «робот против рынка». **Ни один ордер от бенчмарка не зависит.**

#### 2. `BotTabIndex` — свой конструктор индекса. Вот это влияет на торговлю

`OsTrader/Panels/Tab/BotTabIndex.cs` — **3363 строки**. Это не подключение к биржевому индексу, а **синтетический индекс, который пользователь собирает сам** из любых бумаг по формуле:

```
(A0 + A1 + A2) / 3          — равносредняя корзина
A0 - A1                      — спред двух бумаг
A0 / (0.033*A1 + 0.013*A2 + 0.021*A3)   — взвешенный
```

`A0…A9` — номера бумаг в списке таба. Разбор формулы — **собственный рекурсивный парсер по строке**, не Roslyn:

- `ConvertFormula` (752) — валидация посимвольно: разрешены только `/ * + - ( ) A 0-9 . ,`, запрещены два знака подряд, срезается конструкция `(A0)`;
- `Calculate` (1042) — рекурсия: сначала раскрывает скобки, потом делит по знакам, с предохранителем `if (_iteration > 1000) return "";`.

**Нормализация** (`Normalization`, 2115) — ключ к тому, чтобы складывать бумаги разной цены. Каждая бумага приводится к базе 100:

```csharp
decimal curValue = 100;
decimal curMovementPercent = (Close - Open) / (Open / 100);
curValue += curMovementPercent;      // накапливаем проценты, а не рубли
```

`CalculationDepth = 1500` свечей. Флаг `PercentNormalization` (918) переключает режим и используется в 15 местах расчёта.

**Авто-формула** (`IndexFormulaBuilder`, 2263) умеет собирать индекс сама, по расписанию:

| Настройка | Значения |
|---|---|
| `IndexMultType` | `PriceWeighted`, `VolumeWeighted`, `EqualWeighted`, `Cointegration` |
| `SecuritySortType` | `FirstInArray`, `VolumeWeighted`, `MaxVolatilityWeighted`, `MinVolatilityWeighted` |
| расписание | `dayOfWeekToRebuildIndex`, `hourInDayToRebuildIndex` |
| состав | `indexSecCount`, `daysLookBackInBuilding` |

Настройки живут в `Engine\<bot>IndexAutoFormulaSettings.txt`; в оптимизаторе (`IsOsOptimizer`) загрузка/сохранение отключены.

**Главное правило** (зафиксировано в `CONTEXT_INDEX_AND_SPREAD.md`):

> `BotTabIndex` — **только для анализа**. Открывать позиции через `_indexTab.BuyAtMarket()` нельзя. Реальные сделки совершаются на `BotTabSimple` или `BotTabScreener`.

Результат — свечи индекса в `Candles` + событие `SpreadChangeEvent(List<Candle>)` (2078). На индекс можно навесить индикаторы (`_indexTab.CreateCandleIndicator(...)`), и они будут считаться по индексным свечам.

#### 3. `MoexIssDataServer` — источник индексных данных

`Market/Servers/MOEX/MoexIssDataServer.cs:588, 606` — при разборе справочника ISS отдельно обрабатывает `RTSI` и класс индексов. То есть индекс можно скачать как обычный инструмент и торговать/анализировать его как бумагу.

#### 4. Отраслевые «индексы» в секторальных роботах — самодельные

Как описано выше: средний RSI бумаг корзины, сортировка, топ-N. К биржевым индексам отношения не имеет.

### Как индекс попадает в торговое решение — три паттерна

**А. Индекс = сигнал, скринер = исполнитель** (`IndexArbitrageClassic`, 4 индекса/скринера):

```csharp
_indexFirst.SpreadChangeEvent += ...;       // спред двух корзин
// корреляция >= 0.8 → коинтеграция → BuyFirstSellSecond()
for (i = 0; i < _screenerFirst.Tabs.Count; i++)  _screenerFirst.Tabs[i].BuyAtMarket(...);
for (i = 0; i < _screenerSecond.Tabs.Count; i++) _screenerSecond.Tabs[i].SellAtMarket(...);
```

**Б. Одна нога против индекса** (`MultiOneLegArbitrageInTrend/MeanReversion`): индекс не торгуется, торгуется отклонившаяся бумага.

**В. Индекс = фильтр режима рынка** (`PriceChannelScreenerOnIndexVolatility`, 745 строк) — самый интересный паттерн:

```csharp
TabCreate(BotTabType.Index);      _tabIndex = TabsIndex[0];
TabCreate(BotTabType.Screener);   _tabScreener = TabsScreener[0];

_volatilityStagesOnIndex = IndicatorsFactory.CreateIndicatorByName("VolatilityStagesAW", ...);
_volatilityStagesOnIndex = (Aindicator)_tabIndex.CreateCandleIndicator(_volatilityStagesOnIndex, "VolaStagesArea");
```

И дальше индекс **полностью выключает торговлю**, если фаза волатильности не подходит:

```csharp
decimal currentStage = _volatilityStagesOnIndex.DataSeries[0].Last;
if (currentStage == 0
    || (currentStage == 1 && _volatilityStageOneIsOn.ValueBool == false)
    || (currentStage == 2 && _volatilityStageTwoIsOn.ValueBool == false)
    || (currentStage == 3 && _volatilityStageThreeIsOn.ValueBool == false))
{
    _securitiesToTrade.ValueString = "";      // <- список бумаг очищается, торговля стоит
    SendNewLogMessage("No trading. Current volatility stage on index: " + currentStage, LogMessageType.Error);
    return;
}
```
(строки 229–243)

---

## Часть 2. Измерители тренда

Прямого ответа «вот один класс TrendMeter» нет — вместо этого четыре разных механизма.

### 1. `VolatilityStagesAW` — фазы волатильности (главный «режимный» измеритель)

`Indicators/Scripts/VolatilityStagesAW.cs`, 572 строки. Описание из самого индикатора:

> «классифицирует текущую волатильность на 2, 3 или 4 стадии относительно канала скользящих средних волатильности, показывая фазы низкой, средней и высокой активности. В фазе низкой волатильности ищут пробой, в высокой — фиксируют прибыль или воздерживаются от входов.»

Параметры: `Volatility stages regime` = 2/3/4, `Volatility base type`, `lenSmaSlow`, `lenSmaFast`, `channelDeviation`. Результат — `DataSeries[0]` со значением стадии (0/1/2/3).

Это **не измеритель направления тренда, а измеритель режима**: он отвечает на вопрос «сейчас рынок спит, идёт или штормит», а не «куда он идёт».

### 2. `EfficiencyRatio` — самый близкий к «измерителю тренда»

`Indicators/Scripts/EfficiencyRatio.cs`. Коэффициент Кауфмана:

> «сравнивает чистое изменение цены за период с суммой всех внутрипериодных движений, показывая, насколько направленным было движение. Низкое значение указывает на шум и боковик, высокое — на устойчивый тренд.»

Формула: `|Close[t] − Close[t−n]| / Σ|Close[i] − Close[i−1]|`. Это и есть нормированная «прямолинейность» движения — самый честный измеритель силы тренда из имеющихся.

### 3. `SuperTrend` — направление тренда + готовый трейлинг

`Indicators/Scripts/SuperTrend_indicator.cs`:

> «строит trailing-stop уровни на основе ATR и центральной цены свечи, переключая направление при пробое уровня ценой, и визуально отображает текущий тренд.»

### 4. Классические трендовые индикаторы — их много

Из `Indicators/Scripts/` (107 файлов) к тренду относятся:

| Группа | Индикаторы |
|---|---|
| Сила/направление | `ADX`, `Aroon`, `CCI`, `Momentum`, `ROC`, `Trix`, `RAVI`, `QStick`, `UltimateOscilator`, `RVI` |
| Каналы и линии | `LinearRegressionChannel`, `LinearRegressionChannelFast_Indicator`, `LinearRegressionLine`, `LinearRegressionCurve`, `PriceChannel`, `PriceChannelAdaptive`, `PriceChannelOffset`, `DonchianChannel`, `KeltnerChannel`, `SmaChannel`, `Envelops`, `Ichimoku`, `ParabolicSAR`, `ParabolicBollinger_indicator`, `ParabolicPriceChannel_indicator` |
| Скользящие | `Sma`, `Ema`, `HMA`, `Ssma`, `OsMa`, `VWMA`, `OffsetSma/Ema/Ssma/Vwma`, `SmaRestricted` |
| Адаптивные | `AdaptiveLookBack` (подбор периода по свингам), `KalmanFilter`, `EfficiencyRatio`, `PriceChannelAdaptive` |
| Зигзаги | `ZigZag` + **22 варианта** (`ZigZagAD/AO/Asi/BP/CCI/CMO/Chaikin/Channel/FI/MACD/MFI/Momentum/OBV/OsMa/ROC/RVI/Rsi/SMI/Stochastic/Trix/Ultimate/Volume`) |

Отдельно `AdaptiveLookBack`:

> «анализирует последовательные свинговые точки на графике и динамически вычисляет оптимальный lookback-период, адаптируясь под текущую рыночную структуру.»

### 5. Кросс-секционные измерители (не индикаторы, а код роботов)

**`Entity/VolatilityStageClusters.cs` (186 строк)** — делит список бумаг на три кластера по волатильности:

```csharp
sourcesWithCandles = sourcesWithCandles.OrderBy(x => x.Volatility).ToList();
decimal oneLotInArray = sourcesWithCandles.Count / 100;
if ((i+1) <= ClusterOnePercent * oneLotInArray)              ClusterOne.Add(...);   // 33%
else if ((i+1) <= (ClusterOnePercent+ClusterTwoPercent)*oneLotInArray) ClusterTwo.Add(...);
else                                                          ClusterThree.Add(...); // 34%
```

Волатильность в `SourceVolatility.Calculate` — примитивная: `(maxHigh − minLow) / (minLow/100)` за N свечей. То есть это **диапазон**, а не стандартное отклонение.

**Секторальный рейтинг** (`SectorsSetBollingerMomentum.cs:838–845`) — средний RSI как мера силы сектора (`ranked.Sort` на 839, `InTop` на 844–845):

```csharp
ranked.Sort((a, b) => b.AverageRsi.CompareTo(a.AverageRsi));
ranked[i].Rank  = i + 1;
ranked[i].InTop = ranked[i].Rank <= _topSectorsCount.ValueInt && ranked[i].AverageRsi >= 0;
```

**Отбор бумаг по «бета-подобной» метрике** (`PriceChannelScreenerOnIndexVolatility.cs:361–386`) — самое близкое к классической теории:

```csharp
// Volatility. We take the intraday volatility over N days for the stock (V1)
// and for the index (V2) in percentage terms. Then, we divide V1 by V2.
decimal volSec   = GetVolatility(sec, len);
decimal volIndex = GetVolatility(index, len);
return volSec / volIndex;
```

Волатильность — средняя дневная `(maxHigh − minLow)` в процентах за N дней. Отношение к индексной — по сути **бета по волатильности**. Робот оставляет бумаги в коридоре `Vol Diff Min (1.0) … Vol Diff Max (1.4)`: не мёртвые и не слишком дикие.

Полный конвейер отбора (`CheckSecuritiesRating`, 203), **раз в день**:

1. проверка фазы волатильности на индексе → если не подходит, список очищается;
2. по каждой бумаге — денежный объём `Σ(Volume × Open)` и отношение волатильностей;
3. сортировка по объёму, срез `GetRange(0, _topVolumeSecurities)` = топ-15;
4. фильтр по коридору волатильности;
5. результат — строкой в параметр: `_securitiesToTrade.ValueString = "SBER GAZP LKOH "`.

### 6. ATR-фильтр роста волатильности

В том же роботе — простой измеритель «тренд пошёл»:

```csharp
decimal atrLast     = atr.DataSeries[0].Values[Count - 1];
decimal atrLookBack = atr.DataSeries[0].Values[Count - 1 - _atrGrowLookBack.ValueInt];
decimal atrGrowPercent = atrLast / (atrLookBack / 100) - 100;
if (atrGrowPercent < _atrGrowPercent.ValueDecimal) return;    // волатильность не выросла — не входим
```

---

## Наблюдения

1. **Биржевые индексы MOEX почти не участвуют в торговле.** IMOEX/MCFTR — только бенчмарк на графике эквити. Всё, что реально влияет на решения, — это **собственные индексы пользователя** через `BotTabIndex`.
2. **Формула индекса считается самописным строковым парсером** с лимитом в 1000 итераций рекурсии, а не готовым движком. Работает, но это место, где легко получить молчаливый `return ""` при опечатке.
3. **«Измеритель тренда» в проекте — не один класс, а три разных идеи:** режим рынка (`VolatilityStagesAW`), сила направленности (`EfficiencyRatio`), направление (`SuperTrend`, ADX, каналы). Для стратегии «торгуй только в тренде» естественная связка — `EfficiencyRatio` как порог + `SuperTrend`/канал как направление.
4. **Кросс-секционная метрика «волатильность бумаги / волатильность индекса»** — по сути бета, и это самый содержательный измеритель в наборе. Но считается по диапазону дня, а не по отклонениям.
5. **Группировка дней по `.Day`** в `GetVolatility`/`CalculateVolume` — одно и то же число в разных месяцах попадёт в одну группу, если окно длиннее месяца. На `_topCandlesLookBack = 5` дней это не проявляется, но параметр поднимается до 20.
6. **Сортировки пузырьком** встречаются и здесь (`CheckSecuritiesRating`, 278–289) — при 40 бумагах не критично.
7. **Юнит-тестов нет** ни на `BotTabIndex`, ни на индикаторы; проверка только через тестер/оптимизатор на Windows.

---

## Что не проверено

Код не запускался: .NET SDK в песочнице отсутствует (`dotnet: command not found`), таргет `net10.0-windows` с WPF на Linux не собирается. Формулы и номера строк сверены с исходниками; замечания про группировку по `.Day` и про поведение парсера формул — выводы из кода, прогоном не подтверждены.
