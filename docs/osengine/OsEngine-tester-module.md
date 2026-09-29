# OsTester — тестовый модуль (эмулятор биржи)

Разбор `project/OsEngine/Market/Servers/Tester/` и примыкающих частей.
Все утверждения ниже взяты из кода клона (`master`, HEAD `34daed3`).

---

## 1. Что это и где лежит

**OsTester — это фейковая биржа.** Он реализует тот же интерфейс `IServer`, что и настоящие коннекторы к Binance/MOEX/IB, поэтому роботы не знают, торгуют они вживую или на истории.

```
Market/ServerMaster.cs:875     newServer = new TesterServer();   <- единственное место создания
Market/Servers/Tester/
    TesterServer.cs            7293 строки  <- вся логика
    TesterServerUi.xaml.cs     3115 строк   <- окно настроек
    TesterMarginRatesEditUi    194          <- редактор тарифов ГО
    GoToUi.xaml.cs             123          <- перемотка к дате
    Итого                     10 725 строк
```

Внутри `TesterServer.cs` — 11 типов (5 классов + 6 enum'ов):

| Тип | Назначение |
|---|---|
| `TesterServer` | сам эмулятор |
| `SecurityTester` | один инструмент + его файл с историей (строка 6204) |
| `TesterRegime` | `NotActive / Pause / Play / PlusOne` |
| `TesterDataType` | чем «кормим» роботов: `Candle`, `TickAllCandleState`, `TickOnlyReadyCandle`, `MarketDepthAllCandleState`, `MarketDepthOnlyReadyCandle` |
| `TesterSourceDataType` | `Set` (набор OsData) или `Folder` (произвольная папка) |
| `SecurityTesterDataType` | что лежит в файле: `Candle / Tick / MarketDepth` |
| `TimeAddInTestType` | шаг симулятора: `FiveMinute / Minute / Second / Millisecond` |
| `OrderExecutionType` | как исполняется лимитник: `Touch / Intersection / FiftyFifty` |
| `OrderClearing` | клиринг (снятие заявок) по расписанию |
| `NonTradePeriod` | период без новых позиций и заявок |
| `PendingDividendPayment` | отложенная дивидендная выплата |

`StartProgram.IsTester` — флаг, по которому весь остальной код понимает, что он под тестером.

---

## 2. Ядро: один фоновый поток

Конструктор стартует поток `TesterServerThread` (`WorkThreadArea`, строка 972). Это вся «биржа»:

```csharp
while (true) {
    ...
    if (TesterRegime == Pause || NotActive) { Thread.Sleep(500); continue; }
    if (TesterRegime == PlusOne) { LoadNextData(); CheckOrders(); continue; }   // пошагово
    else if (TesterRegime == Play) {
        LoadNextData();                    // сдвинуть время, раздать данные
        CheckOrders();                     // свести ордера
        CheckDividends(TimeNow);           // дивиденды
        CheckMarginAndTaxes(TimeNow);      // ГО, налоги
    }
}
```

Режимы управления (`#region Management`, строки 660–966):

- `TestingStart()` — чистит ордера, серию свечей, переподключается, ждёт стабилизации числа активных серий;
- `TestingPausePlay()`, `TestingPlusOne()` — пауза/шаг;
- `TestingFastOnOff()` / `TestingFastIsActivate` — «перемотка»: гонит историю до следующего события в позиции (`ToNextPositionActionTestingFast`);
- `ToDateTimeTestingFast(DateTime)` — перемотка к конкретной дате (`GoToUi`).

### Шаг времени

`LoadNextData()` (1147) двигает `TimeNow` на фиксированный квант и вызывает `securityTester.Load(TimeNow)` для каждой активной бумаги. Квант выбирается автоматически по типу данных (строка 5847):

| Данные | Шаг симулятора |
|---|---|
| Свечи, все ТФ ≥ 1 мин | **1 минута** |
| Свечи с секундным ТФ | 1 секунда |
| Тики | 1 секунда |
| Стакан | **1 миллисекунда** |

Пока данных нет — шаг тоже 1 секунда.

---

## 3. Данные

- Источник №1 — **сеты OsData**: папки `Data\Set_<имя>` (`CheckSet()`, 4076). Тестер читает **только** папку `Data`, больше ниоткуда (это же зафиксировано в `bin/Debug/Data/СхемаХраненияДанных.docx`).
- Источник №2 — произвольная папка (`SetFolderPath`).
- Формат файлов — собственный бинарный, LEB128-сжатие: `OsData/BinaryEntity/` → `DataBinaryReader`, `DataBinaryWriter`, `DealsStream`, `Leb128.cs`, `ULeb128.cs`. Типы потоков: `Quotes 0x10 / Deals 0x20 / OwnOrders 0x30 / OwnTrades 0x40 / Messages 0x50 / AuxInfo 0x60 / OrdLog 0x70`.
- Загрузка: `LoadCandleFromFolder` (4290), `LoadTickFromFolder` (4625), `LoadMarketDepthFromFolder` (4889).
- **Синхронизатор** (`SynchSecurities`, 5698) собирает список инструментов со всех роботов сразу: одиночные табы, пары (`TabsPair` → `PairToTrade.Tab1/Tab2`) и скринеры (`TabsScreener.SecuritiesNames`). Так достигается «много инструментов и много таймфреймов одновременно с одним портфелем».
- Дополнительно на каждый инструмент подтягиваются `Lot`, `GO` (MarginBuy/MarginSell), `PriceStepCost`, `PriceStep`, `Expiration`, `MinTradeAmount`, `VolumeStep`, `SecurityType` — из файла настроек бумаг (`SetToSecuritiesDopSettings`, 3775).

---

## 4. Исполнение заявок — самая важная часть

Три раздельных движка сведения в зависимости от типа трансляции:

| Метод | Строка | Когда |
|---|---|---|
| `CheckOrdersInCandleTest` | 1311 | свечи |
| `CheckOrdersInTickTest` | 1545 | тики |
| `CheckOrdersInMarketDepthTest` | 1747 | стакан |

### Рыночный ордер
Исполняется **по open следующей свечи** (`decimal realPrice = lastCandle.Open;`), слиппаж **0**. Заявка, созданная на текущей свече, не исполняется на ней же (`if (order.TimeCreate >= lastCandle.TimeStart) return false;`) — честная защита от заглядывания вперёд.

### Лимитный ордер (buy)
```csharp
(Intersection && order.Price > minPrice)   // цена пробила уровень
|| (Touch && order.Price >= minPrice)      // достаточно касания
|| (FiftyFifty && ...чередование...)
```
`FiftyFifty` — режим «честной монеты»: глобальная переменная `_lastOrderExecutionTypeInFiftyFiftyType` переключается между Touch и Intersection после каждого исполнения, то есть половина спорных заявок проходит, половина нет.

### Стоп / тейк-профит
Отдельная ветка `if (order.IsStopOrProfit)`. Цена исполнения = заявленная, но если случился **гэп** через уровень — исполняется по open свечи:
```csharp
if (order.Side == Buy  && minPrice > realPrice) realPrice = lastCandle.Open;   // гэп вверх против стопа
if (order.Side == Sell && maxPrice < realPrice) realPrice = lastCandle.Open;
```
На стоп применяется `_slippageToStopOrder`, на лимитник — `_slippageToSimpleOrder`.

### Слипедж
`ExecuteOnBoardOrder(order, price, time, slippage)` (2263): buy → `price += PriceStep * slippage`, sell → `price -= PriceStep * slippage`. То есть слиппаж задаётся **в шагах цены**, а не в процентах.

### Клиринг и нерабочие периоды
- `OrderClearing` — время + вкл/выкл; `CheckRejectOrdersOnClearing`, `CheckOrderBySessionLife`, `CheckOrderByDayLife` снимают заявки (2431–2520).
- `NonTradePeriod(DateStart, DateEnd, IsOn)` — в периоде нельзя открывать новые позиции и ставить новые заявки (2525–2627).

### Портфель
`CreatePortfolio()` (1074) создаёт единственный портфель с номером **«GodMode»** и стартовым депозитом 1 000 000 (`StartPortfolio`).

---

## 5. Экономика: ГО, налоги, дивиденды

Это то, чего нет в большинстве бэктестеров.

### Маржа / ГО (`#region Margin and taxes`, 3172–3711)
`CheckMargin(dayToProcess)` идёт по всем включённым роботам (`OsTraderMaster.Master.PanelsArray`), собирает открытые позиции, считает ГО long/short и депозит, затем начисляет плату за перенос.

Режимы (`ComboBoxMarginRegime`, TesterServerUi.xaml.cs:205):
- `Off` — по умолчанию;
- `Summ` — таблица сумм (`ListTableSumm { Summ, TypeValue: Absolute|Percent, Rate }`);
- `Percent` — годовые ставки по годам (`ListTablePeriods { Year, Rate }`).

Расчёт: `commission = Math.Round(margin * rate / 100, 2)` (3395) — то есть ставка трактуется как годовая. Начисление проводится как сервисная позиция `CreateChargePosition(bot, "Margin", commission, …)` и вычитается из прибыли.

Ставки берутся из `bin/Debug/KeyRates.txt` — там **история ключевой ставки ЦБ РФ и ставки ФРС** по датам (например `15.09.2025-17,0 + 28.07.2025-18,0 + …` и блок `FEDRatesv`).

### Налоги (`CheckTaxes(year)`, 3429)
Годовой расчёт по российским правилам: есть `IsExcludedFromTaxBase(position)` и отдельная проверка `IsCommodityFuture(position)` (товарные фьючерсы считаются иначе), плюс `MarginTaxTables.cs`. Итог — тоже сервисная позиция-списание.

### Дивиденды (`#region Dividends`, 2767–3170)
- `CheckDividends(currentServerTime)` проходит открытые позиции, сверяется с **Wiki-базой дивидендов** (`WikiDividendRecord`), кэширует по тикеру;
- если дата закрытия реестра наступила — создаётся `PendingDividendPayment { Ticker, BotName, RegistryCloseDate, PositionCreateDate, ExpectedPaymentDate, Volume, Sum }`;
- `ProcessPendingDividendPayments` в нужный день проводит выплату через `AddDividendToPortfolio`;
- защита от двойного начисления — `_processedDividendKeys` (HashSet).

---

## 6. Что считается в итоге

Результаты живут в `Journal/`:

- `Journal/Journal.cs` — позиции, ордера, сделки;
- `Journal/Internal/PositionController.cs` — сводит сделки в позиции и проставляет им `CommissionType` / `CommissionValue`;
- `Journal/Internal/DealStatisticGenerator.cs` (1164 строки) — метрики:

```
GetAllProfitInAbsolute / GetAllProfitPercent   (с учётом флага ignoreTax)
GetAllDealsCount, GetProfitDeal, GetProfitDialPercent
GetMiddleProfitInAbsolute, GetMiddleProfitInPercentOneContract
GetSharpRatio(deals, riskFreeProfitInYear)
GetMaxDownPercent          (макс. просадка)
GetProfitFactor, GetPayOffRatio, GetRecovery
GetCommissionAmount, GetAverageTimeOnPoses
```

Отчёт собирается в `GetStatisticNew(positions, withMaxDrawDown)` и выводится с форматированием `ru-RU`.

---

## 7. Настройки (что крутится в UI)

Вкладки `TesterServerUi.xaml`: **Broadcast data**, **Portfolio**, **Order execution / Accruals and Charges**, **Margin**, **Taxes**, **Dividends**, **Performance settings**, **Logging**.

Элементы: Sets, Source, Translation type, Regime, Start test, Initial deposit, Enable portfolio calculation, Limit slippage, Stop slippage, Order execution, Non-trading periods, Orders clearing system, Remove trades from memory, «>> go to», «>> next pos», «+ 1».

Всё сохраняется в простой текстовый файл `Engine\TestServer.txt` — построчно: активный сет, слиппаж лимитников, депозит, тип данных, тип источника, путь к папке, слиппаж стопов, тип исполнения, `profitMarketIsOn`, `guiIsOpenFullSettings`, `removeTradesFromMemory`, `dividendsIsOn`, `marginRegime`, `taxesIsOn`. Дочитывается с проверками `reader.EndOfStream` — так сделана обратная совместимость со старыми конфигами.

---

## 8. Доступ через MCP API

Модуль `MCP/Modules/TesterApi.cs` — **14 инструментов**:

```
tester_data_get_config          tester_data_get_available_sets
tester_data_set_config          tester_get_securities
tester_execution_get_config     tester_execution_set_config
tester_portfolio_get_config     tester_portfolio_set_config
tester_start                    tester_pause
tester_fast_forward             tester_step_forward
tester_stop                     tester_get_status
```

Схема `tester_execution_set_config`:
```json
{ "slippage_to_simple_order": int, "slippage_to_stop_order": int,
  "order_execution_type": "Touch|Intersection|FiftyFifty",
  "non_trade_periods": [] }
```
Схема `tester_data_set_config` (из `CONTEXT_MCP_SCENARIO_V2.md`):
```json
{ "source_type": "Set", "set_name": "...", "type_tester_data": "Candle",
  "date_from": "2024-01-01T00:00:00", "date_to": "2024-03-31T00:00:00",
  "delete_trades_from_memory": true }
```
События: `tester.test.started / .finished / .paused / .resumed / .progress` шлются как `notifications/message`.

Типовой агентный сценарий из документации: загрузить сет → `tester_get_securities` → создать робота → `tester_start {fast_forward:true}` → поллить `tester_get_status` до `regime: Pause` и `progress_percent: 100` → забрать статистику из журнала.

⚠️ Документация прямо предупреждает: **имена бумаг в тестере — это имена файлов с расширением** (`SBER.txt`, не `SBER`).

---

## 9. Optimizer — не переиспользует TesterServer

Важная деталь: `Market/Servers/Optimizer/OptimizerServer.cs` (3571 строка) — **отдельная реализация `IServer`**, а не обёртка над `TesterServer`. У него те же имена событий (`TesterServer_NewCandleEvent`, `TesterServer_NewTradesEvent`, `TesterServer_NeedToCheckOrders`, `TesterServer_NewMarketDepthEvent` — строки 2074–2185), но работают они от `SecurityOptimizer`. Плюс `OsOptimizer/OptEntity/AsyncBotFactory.cs` — фабрика ботов для параллельного прогона. То есть логика исполнения заявок продублирована в двух местах.

---

## 10. Наблюдения и риски

1. **Биржевой комиссии нет.** В `TesterServer.cs` слово commission встречается только в контексте `GetMarginCommission`. Трейдовая комиссия задаётся **не в тестере, а на каждом роботе**: `tab.CommissionType` / `tab.CommissionValue` → `PositionController` → `Position.CommissionTotal()`. Забыл выставить на боте — бэктест без комиссии.
2. **Рыночный ордер всегда по open следующей свечи со слиппажом 0.** Это систематически оптимистично для импульсных стратегий; реалистичность надо добивать вручную через `slippage_to_stop_order` / лимитники.
3. **Один поток на всю биржу** (`Thread.Sleep(500)` в паузе, `Thread.Sleep(2000)` на старте). Просто и предсказуемо, но параллелизма нет — за ускорение отвечает режим fast-forward.
4. **`FiftyFifty` — глобальное состояние.** `_lastOrderExecutionTypeInFiftyFiftyType` один на весь тестер и переключается после каждого исполнения, поэтому результат зависит от порядка сведения заявок нескольких инструментов.
5. **GodMode-портфель.** Один общий портфель на все роботы; проверка «хватило ли денег» — на стороне робота/журнала, а не «биржи».
6. **`TesterServer.cs` — 7293 строки** в одном классе: данные, исполнение, клиринг, налоги, дивиденды, логирование. Править можно, но покрытие юнит-тестами нулевое — проверяется только стендами `Tests/OsDataTestStand`, `Tests/StopOrdersTestStand` и `Tests/McpTestStand` (модуль `Tester`) на Windows.

---

## 11. Отдельно: `project/Tests/` — тестовые стенды

В проекте слово «тестовый» относится ещё и к шести exe-стендам. Юнит-тестов (xunit/NUnit/MSTest) в решении нет — только эти интеграционные прогонщики, каждый со своим `CONTEXT_*.md`:

| Стенд | .cs | строк | Что проверяет |
|---|---|---|---|
| `McpTestStand` | 28 | 18 019 | весь MCP API, 20 модулей (Protocol, Logs, Settings, Config, ServerManagement, ServerInstance, SSE, Errors, WikiRobots, WikiIndicators, WikiSecurities, WikiDividends, Data, **Tester**, Terminal, SystemLoad, ComparePositions, Proxy, Optimizer, Encryption) |
| `OsDataTestStand` | 15 | 5 656 | загрузка исторических данных |
| `StopOrdersTestStand` | 9 | 3 039 | стоп-ордера |
| `WikiConnectionTest` | 13 | 1 704 | Wiki-подключения (бумаги, дивиденды) |
| `DividendsUpdater` | 11 | 825 | обновление базы дивидендов |
| `OsEngineStarter` | 1 | 72 | запуск терминала в нужном режиме |

MCP-стенд поднимает реальный процесс OsEngine (`OsEngineProcessController.cs`), гоняет по нему JSON-RPC через `McpApiClient.cs` и валидирует протокол (`ProtocolValidator.cs`). Запуск по модулям: `OsEngine.McpApi.TestStand.exe --module Tester` или `--module 5,6`. Целевые показатели из `AGENTS.md`: **200/200** для `--transport v2` и **191/191** для `--transport v1`, прогон ≈4 минуты.

---

## 12. Что не проверено

Код не запускался: в песочнице нет .NET SDK (`dotnet: command not found`), а проект таргетит `net10.0-windows` с WPF — на Linux не собирается. Стенды из `project/Tests/` тоже не запускались (они поднимают GUI-процесс OsEngine на Windows). Всё выше — чтение исходников; числа получены `wc -l` / `grep` по клону.
