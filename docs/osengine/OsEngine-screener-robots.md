# Шесть семейств роботов на скринере — как устроены

Продолжение `OsEngine-screener-module.md`. Разобраны: мониторы рынка, индексный арбитраж, синтетические облигации, сетки, секторальные наборы, дивидендные стратегии.
Все номера строк — по клону `master`, HEAD `34daed3`.

---

## 0. Общая канва

Все шесть семейств используют `BotTabScreener` **по-разному** — и это видно по тому, на какое событие они подписаны:

| Семейство | Событие-триггер | Роль скринера |
|---|---|---|
| Мониторы (3 из 4) | `CandleFinishedEvent` / `CandleUpdateEvent` | независимые инструменты, каждый сам по себе |
| Монитор объёма, сетки с рейтингом | `CandlesSyncFinishedEvent` | **снимок всего рынка** для кросс-секционного ранжирования |
| Индексный арбитраж | `SpreadChangeEvent` у `BotTabIndex` | скринер = «корзина-исполнитель» |
| Синтетические облигации | `CandleFinishedEvent` скринера фьючерсов | скринер = **серии фьючерсов** на одну базу |
| Секторальные наборы | `CandleFinishedEvent` + серверное `EndNextMinuteWithCandlesEvent` | **9 скринеров = 9 секторов** |
| Дивидендные | `CandleFinishedEvent` | фильтр по внешней базе дивидендов |

Общий для всех набор приёмов:

- **`NonTradePeriods`** — объект неторговых периодов создаётся в конструкторе с преднастройкой под MOEX (00:00–10:05, клиринг 13:54–14:06, 18:01–23:58, выходные выключены), кнопка-параметр `CreateParameterButton("Non trade periods")` открывает диалог. Проверка в логике: `_tradePeriodsSettings.CanTradeThisTime(tab.TimeServerCurrent)`.
- **Кастомная вкладка-монитор** в окне параметров: `ParamGuiSettings.CreateCustomTab(" Monitor ")` → `AddChildren(_hostTable)` → свой `DataGridView`. Так роботы рисуют собственные таблицы, которых нет в стандартном UI.
- **Лимит на корзину**: `_tabScreener.PositionsOpenAll.Count >= _maxPositions` или `SourceWithGridsCount >= _maxGridsCount`.
- **Айсберги**: `BuyAtStopMarketIceberg`, `CloseAtStopMarketIceberg`, `BuyAtIcebergMarket`.
- **Авто-деплой**: списки бумаг захардкожены в роботе и разворачиваются в скринер кнопкой (в реале — из Т-Инвестиций, в тестере — из выбранного сета, где имена бумаг с `.txt`).

---

## 1. Мониторы рынка — `Robots/Monitors/` (4 робота, 4544 строки)

| Робот | строк | Индикатор | Идея |
|---|---|---|---|
| `MonitorRsi` | 1113 | RSI | движение RSI за N свечей |
| `MonitorImpulse` | 1087 | — (цены) | импульс по ценам за N свечей |
| `MonitorHighLow` | 1073 | `PriceChannel` | близость к максимуму/минимуму за период |
| `MonitorVolume` | 1271 | `Volume` + `KeltnerChannel` | **ранкинг по денежному объёму** |

### Что это такое

Это **не совсем роботы**, а «алерт-панели с опциональной торговлей». У каждого три режима работы:

1. **Таблица** — все бумаги скринера с их текущим показателем, живьём;
2. **Сигналы** — звук + запись в лог (можно в `LogMessageType.Error`, чтобы попало в алерты);
3. **Торговля** — лонг и/или шорт со стопом, профитом и закрытием по времени.

Параметр `Regime` у трёх из них: `Off / OnCandleUpdate / OnCandleFinish` — то есть можно смотреть «на лету» по незакрытой свече или только по закрытым.

### Как хранится состояние на бумагу

Словарь по имени инструмента — ключевой приём всех мониторов:

```csharp
private Dictionary<string, MoveData> _checkMoveTimes = new();     // MonitorRsi:199

private void UpdateMoveData(List<Candle> candles, BotTabSimple tab)
{
    if (_checkMoveTimes.TryGetValue(tab.Connector.SecurityName, out myData) == false)
    { myData = new MoveData { SecurityName = ..., Tab = tab }; _checkMoveTimes.Add(...); }

    Aindicator rsi = (Aindicator)tab.Indicators[0];
    // max/min RSI за _candlesToAnalyze свечей
    myData.MoveUp   = Math.Round(currentPrice - minPrice, 3);
    myData.MoveDown = Math.Round(-(maxPrice - currentPrice), 3);
}
```

Классы `MoveData` и `SignalData` объявлены в конце файла (`MonitorImpulse.cs:1067, 1082`) и переиспользуются всеми мониторами.

### Сигналы с дедупликацией по свече

```csharp
if (mySignalData.Time == candles[^1].TimeStart) return;   // одна свеча = один сигнал
mySignalData.Time = candles[^1].TimeStart;
DropSignal(myData, "Up signal", _upSignalsMusic.ValueString, _upSignalsErrorLogIsOn.ValueBool);
```

`DropSignal` → `PlaySound(soundName)` через `SoundPlayer` поверх встроенных ресурсов (`Resources.Bird`, `Resources.Duck`, `Resources.wolf01`) + `tab.SetNewLogMessage(messageValue, messageType)`.

### Монитор объёма — единственный кросс-секционный

`MonitorVolume` подписан не на свечу инструмента, а на **`CandlesSyncFinishedEvent`** (строка 112) — событие «свечи закрылись по всем бумагам». Дальше:

```csharp
private void _tabScreener_CandlesSyncFinishedEvent(List<BotTabSimple> tabs)
{
    ProcessRanking();     // пересчитать ранкинг по всем бумагам
    TryUpdateTable();
    for (int i = 0; i < tabs.Count; i++) MainLogic(tabs[i].CandlesFinishedOnly, tabs[i]);
}
```

Класс `VolumesRanking` (1091) считает **денежный объём** `Σ(Candle.Center × Volume) × Lot` за последние N часов, дважды — «сейчас» и «N свечей назад», — сортирует по убыванию, нумерует и вычитает:

```csharp
int move = valueHistory.SecurityRankingNum - valueNow.SecurityRankingNum;
valueNow.SecurityRankingMove = move;      // насколько бумага поднялась в ранкинге
```

Это и есть «взлетевшая по объёму бумага». В таблице монитора колонки `Volume index` и `Volume value`.

⚠️ `GetSummVolume` считает часы, сравнивая `startTime.Hour != allCandles[i].TimeStart.Hour` — то есть «24 часа» это 24 **смены значения часа**, что на минутках не равно суткам, если в данных есть пропуски.

---

## 2. Индексный арбитраж — `Robots/IndexArbitrage/` (4 робота, 1703 строки)

Здесь скринер — **не источник сигнала, а исполнитель**. Сигнал даёт `BotTabIndex` (индекс, собранный по формуле из корзины бумаг), а скринер торгует ноги.

### `IndexArbitrageClassic` (451 строка) — 2×2 источника

```csharp
TabCreate(BotTabType.Index);      _indexFirst  = TabsIndex[0];
TabCreate(BotTabType.Index);      _indexSecond = TabsIndex[1];
TabCreate(BotTabType.Screener);   _screenerFirst  = TabsScreener[0];
TabCreate(BotTabType.Screener);   _screenerSecond = TabsScreener[1];

_indexFirst.SpreadChangeEvent  += ...;    // спред между двумя индексами
_indexSecond.SpreadChangeEvent += ...;
```

Логика (132–226): дождались совпадения `TimeStart` последних свечей обоих индексов → `CorrelationBuilder.ReloadCorrelationLast(...)` (фильтр: корреляция ≥ 0.8) → `CointegrationBuilder.ReloadCointegration(...)` → если `SideCointegrationValue == Up`, лонг первая корзина / шорт вторая, и наоборот.

Исполнение — **веером по всем ногам корзины**:

```csharp
decimal firstLegVolumeOneSec = _moneyPercentFromDepoOnOneLeg.ValueDecimal / _screenerFirst.Tabs.Count;
for (int i = 0; i < _screenerFirst.Tabs.Count; i++)
    _screenerFirst.Tabs[i].BuyAtMarket(GetVolume(_screenerFirst.Tabs[i], firstLegVolumeOneSec), curSideCointegration);
for (int i = 0; i < _screenerSecond.Tabs.Count; i++)
    _screenerSecond.Tabs[i].SellAtMarket(...);
```

Деньги на ногу делятся **поровну между бумагами корзины** — вот где скринер незаменим: ног может быть 20, и создавать их руками бессмысленно.

`HavePositions()` (171) проверяет наличие позиции обходом `Tabs[i].PositionsOpenAll` обоих скринеров.

### `MultiOneLegArbitrage*` — одна нога против индекса

```csharp
TabCreate(BotTabType.Index);     _index = TabsIndex[0];      _index.SpreadChangeEvent += ...;
TabCreate(BotTabType.Screener);  _screener = TabsScreener[0];
_screener.CreateCandleIndicator(1, "VolatilityAverage", null, "Area2");
_screener.CandleFinishedEvent += _screener_CandleFinishedEvent;
```

Торгуется **только отклонившаяся бумага**, вторая «нога» — сам индекс (не торгуется). Два варианта: `InTrend` (возврат к индексу без моментума) и `MeanReversion`. Добавлен фильтр фазы волатильности (`VolatilityAverage` + `volatilityStageToTrade`).

---

## 3. Синтетические облигации — `Robots/SyntheticBond/` (4 робота, 11 869 строк)

Самое большое семейство и самое проработанное (есть отдельный `CONTEXT_SYNTHETIC_BOND.md`).

### Конструкция

Синтетическая облигация = **лонг акция + шорт фьючерс на неё**. В контанго к экспирации цены сходятся, раздвижка забирается как доходность.

Источники — **10 пар** на робота, в каждой паре:

```csharp
_base1  = (BotTabSimple)TabCreate(BotTabType.Simple);      // базовая акция
_futs1  = (BotTabScreener)TabCreate(BotTabType.Screener);  // ВСЕ серии фьючерсов на неё
... (до _base10 / _futs10)
_tabLqdt = (BotTabSimple)TabCreate(BotTabType.Simple);     // парковка свободных денег
```

(`SyntheticBondsArbitrage.cs:1381–1411`.) Бумаги: SBER, SBERP, GAZP, ROSN, LKOH, VTBR, GMKN, ALRS, AFLT, MGNT.

**Здесь скринер играет роль, которой нет ни у кого другого:** он перебирает **серии фьючерсов одного базового актива** (ближайшая, следующая, …). То есть скринер = «все экспирации».

### Метрика решений

```csharp
private (decimal ContangoAbs, decimal YieldAnn) CalculateContango(BotTabSimple baseSource, BotTabSimple futuresSource, decimal mult)
{
    if (lastBaseC.TimeStart != lastFutC.TimeStart) return (0, 0);   // свечи должны быть синхронны
    if (lastFutC.Close / mult <= lastBaseC.Close)  return (0, 0);   // бэквордация — не торгуем

    decimal deviation = futuresSource.PriceBestBid / mult - baseSource.PriceBestAsk;
    deviation = deviation / (baseSource.PriceBestAsk / 100);        // % 

    int daysToExpiration = (futuresSource.Security.Expiration - futuresSource.TimeServerCurrent).Days;
    decimal yieldAnn = daysToExpiration > 0 ? deviation * 365 / daysToExpiration : 0;
    return (deviation, yieldAnn);
}
```
(строки 980–1029)

Цены берутся **консервативно**: фьючерс продаём по биду, базу покупаем по аску. Серии разных сроков сравниваются **только по годовым** — сырое контанго % систематически тянуло бы выбор в дальние серии.

### Четыре робота

| Робот | строк | Суть |
|---|---|---|
| `SyntheticBondsArbitrage` | 3230 | классика: одна ближайшая серия, метрика — абсолютное контанго %, перенос позиции на более доходную пару |
| `SyntheticBondsCurveArbitrage` | 3135 | арбитраж по кривой: торгуются **две серии** на бумагу, метрика — только `yieldAnn` |
| `SyntheticBondsCurveMonitor` | 1986 | монитор кривой + сигналы + **ручное** открытие/закрытие пар через свои окна |
| `SyntheticBondsScalper` | 3212 | скальпер: вход **лимитками по стакану**, свой фоновый поток `LogicThreadWorker` каждые 5 с, только реальная торговля |

Общие черты: гейт доходности против LQDT/TMON (`PassLqdtGate`: `yieldAnn > LqdtYield + _minYieldDiffOverLqdt`), выход за 7 дней до экспирации, аварийные выходы (открылась одна нога, ноги в разные дни, нога до 10:00), авто-мультипликаторы фьючерсов по историческим датам (VTB — 20 до 15.07.2024, потом 100; GMKN — 100 до 04.04.2024, потом 10), отчёт `YearSummary` по годам с альфой против LQDT.

---

## 4. Сетки — `Robots/Grids/` (3 скринер-робота)

Ключевое: **сетка здесь не пишется в роботе.** В `BotTabSimple` уже есть полноценный движок сеток `OsTrader/Grids/` (9040 строк: `TradeGrid`, `TradeGridsMaster`, `TradeGridCreator`, `TrailingUp`, `TradeGridStopBy`, `TradeGridNonTradePeriods`, `TradeGridErrorsReaction`). Робот-скринер — это **стратегия включения/выключения сеток по бумагам**.

### `GridBollingerScreener` (406 строк)

```csharp
private void _screenerTab_CandleFinishedEvent(List<Candle> candles, BotTabSimple tab)
{
    if (tab.GridsMaster.TradeGrids.Count != 0) LogicCloseGrid(candles, tab);
    if (tab.GridsMaster.TradeGrids.Count == 0) LogicCreateGrid(candles, tab);
}
```

Условия создания: `_tabScreener.SourceWithGridsCount >= _maxGridsCount` — **вот ради чего в скринере есть этот агрегат**; ADX в коридоре `[min, max]` (сетка любит флэт); цена вышла за полосу Боллинджера → сетка в противоположную сторону.

Создание сетки — декларативно, 10 шагов (строки 257–325):

```csharp
TradeGrid grid = tab.GridsMaster.CreateNewTradeGrid();
grid.GridType = TradeGridPrimeType.MarketMaking;
grid.GridCreator.StartVolume / TypeVolume / FirstPrice / LineCountStart / LineStep / TypeStep / TypeProfit / ProfitStep / GridSide
grid.GridCreator.CreateNewGrid(tab, TradeGridPrimeType.MarketMaking);
CopyNonTradePeriodsSettingsInGrid(grid);            // неторговые периоды пробрасываются в сетку
grid.TrailingUp.TrailingUpStep / TrailingUpLimit / TrailingUpIsOn = true;
grid.TrailingUp.TrailingDownStep / TrailingDownLimit / TrailingDownIsOn = true;
grid.StopBy.StopGridByPositionsCountReaction = TradeGridRegime.CloseForced;
grid.StopBy.StopGridByPositionsCountValue = _closePositionsCountToCloseGrid.ValueInt;
grid.Save();
grid.Regime = TradeGridRegime.On;
```

Изменение параметров на лету — обходом живых сеток: `tab.GridsMaster.TradeGrids[0]` → правка полей → пересоздание.

### `GridVolumeBollingerRankingScreener` (640 строк)

Сетка + **два кросс-секционных фильтра**: `SetBollingerRanking(candles, tab)` и `SetVolumeRanking(candles, tab)` (строки 162–163). Сетка ставится только на бумагу, которая прошла оба ранкинга (`volumeRanking < _volumeRankingMaxPosition`). Кнопки-параметры «Show bollinger ranking» / «Show volume ranking» открывают таблицы ранкингов.

### `GridScreenerAdaptiveSoldiers` (629 строк)

Сетка, запускаемая по паттерну «три солдата» с адаптацией порогов.

---

## 5. Секторальные наборы — `Robots/Sectors/` (2 робота, 3005 строк)

### Конструкция: 9 скринеров = 9 секторов

```csharp
private List<SectorData> _sectors = new List<SectorData>();     // :66

TabCreate(BotTabType.Screener);
SectorData sector = new SectorData();
sector.Screener = TabsScreener[TabsScreener.Count - 1];
_sectors.Add(sector);                                            // :275–282
```

```csharp
public class SectorData                                          // :1483
{
    public string Name;
    public BotTabScreener Screener;
    public string[] Tickers;
    public decimal AverageRsi = -1;
    public int Rank = 100;
    public bool InTop = false;
}
```

Сектора (из `CONTEXT_SECTORS_SET.md`): Нефтегаз MOEXOG (10 бумаг), Финансы MOEXFN (6), Металлы MOEXMM (9), Потребсектор MOEXCN (2), Электроэнергетика MOEXEU (1), Транспорт MOEXTN (2), Телекомы MOEXTL (2), Химия MOEXCH (1), ИТ MOEXIT (7). Итого 40 бумаг + LQDT.

### Рейтингование секторов

```csharp
List<SectorData> ranked = new List<SectorData>(_sectors);
ranked.Sort((a, b) => b.AverageRsi.CompareTo(a.AverageRsi));      // :838–839
for (int i = 0; i < ranked.Count; i++)
{
    ranked[i].Rank = i + 1;
    ranked[i].InTop = ranked[i].Rank <= _topSectorsCount.ValueInt && ranked[i].AverageRsi >= 0;   // :843–845
}
```

Показатель сектора = **средний RSI его бумаг** (только «прогретых»). Входы разрешены только в топ-N секторов (`Monitor trade sectors count`, по умолчанию 5). Внутри сектора — отбор `Monitor entry filter` = None / Strongest / Weakest по RSI бумаги.

Здесь скринер нужен дважды: (1) как контейнер бумаг сектора, (2) как агрегатор — `sector.Screener.PositionsOpenAll` даёт «одна позиция на сектор».

### Триггеры по окружению

Робот по-разному получает сигнал «минута закончилась» в зависимости от того, где запущен:

| Окружение | Триггер |
|---|---|
| Реал | `CandleFinishedEvent` скринеров → одноразовый таймер 5 сек → `Logic()` |
| Тестер | `TesterServer.EndNextMinuteWithCandlesEvent` → `Logic()` (:196) |
| Оптимизатор | первая свеча как триггер → `OptimizerServer.EndNextMinuteWithCandlesEvent` (:202, 208) |

Плюс guard «одна свеча — одно действие»: `TimeStart` последней обработанной свечи хранится в словаре по табу.

### Исполнение

Только лонг. Вход `BuyAtStopMarketIceberg(volume, bollingerUp, bollingerUp, StopActivateType.HigherOrEqual, 1, "LongEntry", PositionOpenerToStopLifeTimeType.CandlesCount, ...)` — заявка живёт одну свечу, перед перевыставлением `tab.BuyAtStopCancel()`. Выход — ручной трейлинг `CloseAtStopMarketIceberg`, и только в сторону прибыли:

```csharp
if (position.StopOrderPrice == 0 || exitPrice > position.StopOrderPrice)     // :1029–1032
    tab.CloseAtStopMarketIceberg(position, exitPrice, ...);
```

В неторговое время стопы «засыпают»: `SetStopsActive(sector.Screener.PositionsOpenAll, false)` → `positions[i].StopOrderIsActive = false` (:1037–1049).

Общий лимит: `CountOpenPositionsTotal() >= _maxPositions` (:670, 937). Расчёт плеча набора из документации: 2 робота × 4 позиции × 12.5% депо = 100%, то есть первое плечо.

---

## 6. Дивидендные стратегии — `Robots/Dividends/` (3 робота, 1222 строки)

Общая черта: торговый сигнал берётся **не из индикатора, а из внешней базы дивидендов** `WikiMaster` (`Wiki/WikiMaster.cs`, 605 строк):

```csharp
WikiMaster.GetDividendsFuture(ticker, referenceDate)   // :89   ближайший будущий дивиденд
WikiMaster.GetDividendsPast(ticker, date)              // :126  ближайший прошлый
WikiMaster.GetDividendsHistory(ticker, date)           // :51   история
WikiMaster.SearchDividendsByDate(ticker, date)         // :163
```

Тип `WikiDividendRecord` (:568) содержит даты реестра и выплату.

### `DividendCaptureScreener` (434 строки) — захват дивиденда

Вход: у бумаги есть дивиденд в ближайшие N дней (`Days before registry`, 5) + свеча закрылась выше SMA + нет позиции + не превышен лимит. Выход — на следующий день после отсечки, в заданное время (`Exit time` 10:05). И вход, и выход — айсбергами.

Тонкость, которую автор зафиксировал комментарием: `GetDividendsFuture` смотрит **вперёд**, поэтому после отсечки он вернёт уже следующий дивиденд. Чтобы выход сработал надёжно, дата реестра запоминается в момент входа:

```csharp
// Stores the registry close date for each ticker at the moment of entry.
// Used for reliable exit after the registry close, because GetDividendsFuture
// looks forward and returns the next dividend after the registry date.
private Dictionary<string, DateTime> _entryRegistryDates = new();      // :61
```

### `KeltnerDividendScreener` (391 строка) — тренд после дивиденда

Только лонг: пробой верхней линии Keltner + бумага платила дивиденды в последние N дней (`GetDividendsPast`). Выход — пробой нижней линии.

### `ShortBadDividends` (397 строк) — шорт «плохих» дивидендов

Только шорт: пробой нижней линии Adaptive Price Channel + бумага платила дивиденды в последние N дней + **доходность той выплаты ниже порога** `maxDividendYieldPercent`. Логика: слабый дивиденд не компенсирует дивидендный гэп — бумага продолжает падать.

---

## 7. Сводка: зачем каждому семейству скринер

| Семейство | Что даёт скринер | Агрегат, который используется |
|---|---|---|
| Мониторы | единая таблица по десяткам бумаг + один алерт-канал | `Tabs`, словарь по `SecurityName` |
| Монитор объёма | снимок всего рынка одним событием | `CandlesSyncFinishedEvent` |
| Индексный арбитраж | исполнитель на N ног корзины | `Tabs.Count` для деления денег, `PositionsOpenAll` |
| Синтетические облигации | **серии фьючерсов** одной базы | `Tabs` как список экспираций |
| Сетки | лимит «сколько бумаг сейчас с сеткой» | `SourceWithGridsCount` |
| Сектора | сектор = контейнер бумаг + единица рейтинга | `PositionOpenAll` на сектор, 9 скринеров |
| Дивиденды | фильтр по внешней базе сразу по всему списку | `PositionsOpenAll` на корзину |

## 8. Наблюдения

1. **Скринер используется в четырёх принципиально разных ролях**: «много независимых инструментов», «снимок рынка для ранкинга», «корзина-исполнитель», «список серий одного актива». Один класс закрывает все четыре — это и сила, и причина, по которой в нём 3605 строк.
2. **Кросс-секционная логика держится на `CandlesSyncFinishedEvent`**, а оно в реале приходит через отдельный поток с фиксированной задержкой 2 с (см. `OsEngine-screener-module.md`, раздел 4). Для ранкинговых роботов (`MonitorVolume`, `GridVolumeBollingerRankingScreener`) это означает, что снимок рынка может быть собран из свечей разной свежести.
3. **Секторальные роботы обходят эту проблему в лоб** — в тестере/оптимизаторе подписываются на серверное `EndNextMinuteWithCandlesEvent`, а в реале ставят таймер на 5 секунд. Три разных кодовых пути на одну задачу.
4. **Объём кода несоразмерен логике**: мониторы — 1000+ строк на робота при том, что значительная часть это построение `DataGridView` по колонкам вручную. То же в синтетических облигациях (11 869 строк на 4 робота) — там много UI и авто-деплоя.
5. **Документация семейств лучше, чем в среднем по проекту**: `CONTEXT_SYNTHETIC_BOND.md` и `CONTEXT_SECTORS_SET.md` описывают не только код, но и экономику (формулы контанго, мультипликаторы по датам, расчёт плеча набора, критерии отбора бумаг).

---

## 9. Что не проверено

Код не запускался: .NET SDK в песочнице отсутствует (`dotnet: command not found`), таргет `net10.0-windows` с WPF на Linux не собирается. Всё выше — чтение исходников и проектной документации; номера строк и формулы сверены с кодом, но поведение (включая замечание про подсчёт часов в `GetSummVolume`) прогоном не подтверждено.
