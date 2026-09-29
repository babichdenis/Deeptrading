# Реестр роботов OsEngine — по классам: что делают, чем входят, чем выходят

Дата: 2026-09-26. Источник: клон `~/OsEngine` → `project/OsEngine/Robots` (217 .cs, только чтение).

Детекция по фактическим торговым вызовам в коде робота:
- **Вход**: `BuyAtMarket / BuyAtLimit / BuyAtStop(…)` и `SellAt…` (любые варианты, вкл. `…OnServer`);
- **Докупки**: `…ToPosition` (доливка в позицию — второй/третий вход);
- **Выход**: `ClosePositionAtMarket / AtProfit / AtStop / ByStop`, `CloseAllPosition`, `TrailingStop`.
**Торговый** = прописаны ОБА контура (вход и выход) — кандидаты на тесты входа/выхода.
Служебные классы (AutoTestBots, Screeners, Monitors, Helpers, Engines, Rebalancers, корень) — не торговые.

## Сводка по классам

| Класс | Роботов | Торговых (вход+выход) | Только вход | Класс — это |
|---|---:|---:|---:|---|
| (root) | 2 | 0 | 0 | корень Robots/ — BotFactory и базовые классы; инфраструктура, не роботы |
| AlgoStart | 4 | 4 | 0 | стартовые алго-роботы OsEngine (шаблоны автора) |
| AutoTestBots | 44 | 0 | 0 | СЛУЖЕБНЫЕ тест-боты OsEngine (серверы/ордера/данные) — НЕ торговые |
| BotsFromStartLessons | 18 | 14 | 1 | учебные роботы курса «C# для алготрейдера» — простые явные правила входа/выхода |
| CounterTrend | 7 | 4 | 0 | контренд: вход против движения (перекупленность/перепроданность, развороты) |
| CurrencyArbitrage | 2 | 0 | 0 | арбитраж валютных пар |
| Dividends | 3 | 3 | 0 | дивидендные стратегии (календарь отсечек) |
| Engines | 7 | 0 | 0 | инфраструктурные «движки» панелей — не самостоятельные роботы |
| Funding | 3 | 0 | 2 | фандинг-стратегии (крипто: ставка фандинга) |
| FuturesScreeners | 2 | 0 | 0 | скринеры фьючерсов — отбор, не торгуют |
| FuturesStart | 2 | 2 | 0 | стартовые фьючерсные стратегии |
| FuturesTrend | 2 | 2 | 0 | трендовые стратегии на фьючерсах |
| Grids | 8 | 0 | 0 | сетки: усреднение/пирамидинг, серии ордеров |
| Helpers | 5 | 0 | 0 | вспомогательные классы — не роботы |
| High Frequency | 3 | 3 | 0 | высокочастотные стратегии |
| IndexArbitrage | 4 | 4 | 0 | индексный арбитраж (фьючерс vs индекс) |
| MarketMaker | 7 | 4 | 0 | маркет-мейкинг: двустороннее котирование |
| Monitors | 4 | 0 | 0 | мониторы состояния — не торгуют |
| NewsBots | 2 | 2 | 0 | новостные стратегии |
| OnScriptIndicators | 15 | 15 | 0 | роботы на скриптовых индикаторах (кастомная логика индикаторов кодом) |
| Options | 1 | 1 | 0 | опционные стратегии |
| PairArbitrage | 3 | 0 | 0 | парный арбитраж (две коррелированные бумаги) |
| Patterns | 8 | 7 | 0 | паттернные: свечные/фигурные сетапы |
| PositionsMicromanagement | 8 | 8 | 0 | микроуправление позицией: трейлинг/докупки поверх базовой логики |
| Rebalancers | 3 | 0 | 0 | ребалансировка портфелей — не спекулятивные |
| Screeners | 9 | 0 | 0 | скринеры бумаг — отбор, не торгуют |
| Sectors | 2 | 2 | 0 | секторные стратегии/ротации |
| SpeculantSet | 2 | 2 | 0 | комбинированный набор «спекулянт» |
| SyntheticBond | 5 | 4 | 0 | синтетические облигации |
| TechSamples | 16 | 7 | 1 | технические примеры для разработчиков |
| Trend | 12 | 10 | 0 | трендовые: вход на пробой/продолжение движения, выход по трейлингу/развороту |
| VolatilityStageRotationSamples | 4 | 4 | 0 | ротация по стадиям волатильности |

## Кандидаты на тесты входа/выхода (102 торговых роботов)

Порядок = приоритет теста: тренд/контренд/паттерны — ядро; ниже — специализированные.

| # | Класс | Робот | Вход | Докупки | Выход | Индикаторы |
|---:|---|---|---|---|---|---|
| 1 | Trend | BreakLinearRegressionChannel | BuyAtMarket, BuyAtStopCancel, SellAtMarket, SellAtStopCancel | — | CloseAtStop | Sma, PriceChannel, Candle |
| 2 | Trend | EnvelopTrend | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop | Envelop, Candle |
| 3 | Trend | MomentumMacd | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Macd, Momentum, Candle |
| 4 | Trend | ParabolicBollinger | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop, ClosePosition | Sma, Bollinger, Parabolic, Candle |
| 5 | Trend | ParabolicPriceChannel | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop, ClosePosition | Sma, PriceChannel, Parabolic, Candle |
| 6 | Trend | ParabolicSarTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Parabolic, Candle |
| 7 | Trend | PriceChannelTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | PriceChannel, Candle |
| 8 | Trend | SmaStochastic | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Stochastic, Candle |
| 9 | Trend | StrategyBillWilliams | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Alligator, Fractal, Williams, Candle |
| 10 | Trend | TwoTimeFramesBot | BuyAtMarket | — | CloseAtMarket | Sma, PriceChannel, Candle |
| 11 | CounterTrend | ClusterCountertrend | BuyAtLimit, SellAtLimit | — | CloseAtMarket | Candle |
| 12 | CounterTrend | RsiContrtrend | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Rsi, Candle |
| 13 | CounterTrend | StrategyBollinger | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Bollinger, Candle |
| 14 | CounterTrend | WilliamsRangeTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Rsi, Williams, Candle |
| 15 | Patterns | CandlePatternBoost | BuyAtMarket, BuyAtStopCancel, SellAtMarket, SellAtStopCancel | — | CloseAtLimit, CloseAtTrailingStop, ClosePosition | Sma, Candle |
| 16 | Patterns | CustomCandlesImpulseTrader | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Candle |
| 17 | Patterns | PinBarTrade | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Sma, Candle |
| 18 | Patterns | PivotPointsRobot | BuyAtLimit, SellAtLimit | — | CloseAtLimit, CloseAtMarket | Candle |
| 19 | Patterns | ThreeSoldier | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | Candle |
| 20 | Patterns | ThreeSoldierVolatilityAdaptive | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | Candle |
| 21 | Patterns | VolatilityAdaptiveCandlesTrader | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Candle |
| 22 | FuturesTrend | FuturesTrendBollinger | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Bollinger, Candle |
| 23 | FuturesTrend | FuturesTrendPriceChannel | BuyAtIcebergMarket | — | CloseAtIcebergMarket | PriceChannel, Candle |
| 24 | VolatilityStageRotationSamples | BollingerTrendVolatilityStagesFilter | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | Bollinger, Candle |
| 25 | VolatilityStageRotationSamples | PriceChannelScreenerOnIndexVolatility | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | PriceChannel, Atr, Candle |
| 26 | VolatilityStageRotationSamples | PriceChannelTrendAtrFilter | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | PriceChannel, Atr, Candle |
| 27 | VolatilityStageRotationSamples | ZigZagChannelScreenerRsiFilter | BuyAtMarket | — | CloseAtMarket, CloseAtTrailingStop | Sma, Rsi, Candle |
| 28 | BotsFromStartLessons | Lesson3Bot1 | BuyAtMarket | — | CloseAtTrailingStopMarket | Candle |
| 29 | BotsFromStartLessons | Lesson3Bot2 | BuyAtLimit | — | CloseAtMarket | Sma, Candle |
| 30 | BotsFromStartLessons | Lesson3Bot3 | BuyAtMarket | — | CloseAtMarket | Sma, Candle |
| 31 | BotsFromStartLessons | Lesson4Bot1 | BuyAtMarket | — | CloseAtMarket | Sma, Candle |
| 32 | BotsFromStartLessons | Lesson5Bot1 | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Sma, Candle |
| 33 | BotsFromStartLessons | Lesson5Bot2 | BuyAtMarket | BuyAtMarketToPosition | CloseAtTrailingStopMarket | Alligator, PriceChannel, Candle |
| 34 | BotsFromStartLessons | Lesson6Bot1 | BuyAtStop | — | CloseAtTrailingStop | Bollinger, Atr, Candle |
| 35 | BotsFromStartLessons | Lesson7Bot1 | BuyAtMarket | — | CloseAtTrailingStopMarket, TrailingStop | Sma, Candle |
| 36 | BotsFromStartLessons | Lesson8Bot1 | BuyAtLimit | — | CloseAtProfitMarket, CloseAtStopMarket | — |
| 37 | BotsFromStartLessons | Lesson8Bot2 | BuyAtStop | — | CloseAtTrailingStopMarket | Candle |
| 38 | BotsFromStartLessons | Lesson9Bot1 | BuyAtFake, BuyAtIceberg, BuyAtIcebergMarket, BuyAtLimit, BuyAtMarket, BuyAtStop, BuyAtStopCancel, BuyAtStopMarket, SellAtFake, SellAtIceberg, SellAtIcebergMarket, SellAtLimit, SellAtMarket, SellAtStop, SellAtStopCancel, SellAtStopMarket | — | CloseAtMarket | Candle |
| 39 | BotsFromStartLessons | Lesson9Bot2 | BuyAtIcebergToPositionMarket, BuyAtLimitToPositionUnsafe, BuyAtMarket, BuyAtStopCancel, SellAtIcebergToPositionMarket, SellAtLimitToPositionUnsafe, SellAtMarket, SellAtStopCancel | BuyAtIcebergToPosition, BuyAtLimitToPosition, BuyAtMarketToPosition, SellAtIcebergToPosition, SellAtLimitToPosition, SellAtMarketToPosition | CloseAtMarket | — |
| 40 | BotsFromStartLessons | Lesson9Bot3 | BuyAtMarket, SellAtMarket | — | CloseAtFake, CloseAtIceberg, CloseAtIcebergMarket, CloseAtLimit, CloseAtLimitUnsafe, CloseAtMarket | — |
| 41 | BotsFromStartLessons | Lesson9Bot4 | BuyAtMarket, SellAtMarket | — | CloseAtMarket, CloseAtProfit, CloseAtProfitLimitMethod, CloseAtProfitMarket, CloseAtProfitMarketMethod, CloseAtStop, CloseAtStopLimitMethod, CloseAtStopMarket, CloseAtStopMarketMethod, CloseAtTrailingStop, CloseAtTrailingStopLimitMethod, CloseAtTrailingStopMarket, CloseAtTrailingStopMarketMethod | Candle |
| 42 | High Frequency | Fisher | BuyAtLimit, SellAtLimit | — | CloseAllPositions, CloseAtLimit | Sma, Bollinger |
| 43 | High Frequency | HighFrequencyTrader | BuyAtLimit, SellAtLimit | — | CloseAtMarket, CloseAtProfit, CloseAtStop, ClosePositionThreadArea | Candle |
| 44 | High Frequency | MarketDepthScreener | BuyAtLimit | — | CloseAtLimit, CloseAtMarket | Momentum |
| 45 | MarketMaker | MarketMakerBot | BuyAtMarket, SellAtMarket | BuyAtMarketToPosition, SellAtMarketToPosition | CloseAtMarket | Candle |
| 46 | MarketMaker | PairTraderSimple | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Candle |
| 47 | MarketMaker | PairTraderSpreadSma | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Candle |
| 48 | MarketMaker | TwoLegArbitrage | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Rsi, Candle |
| 49 | IndexArbitrage | IndexArbitrageClassic | BuyAtMarket, SellAtMarket | — | CloseAllPositionsByMarket, CloseAtMarket | Candle |
| 50 | IndexArbitrage | MultiExchangePairArbitrageOnTheIndex | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle |
| 51 | IndexArbitrage | MultiOneLegArbitrageInTrend | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Candle |
| 52 | IndexArbitrage | MultiOneLegArbitrageMeanReversion | BuyAtLimit, SellAtLimit | — | CloseAtLimit, ClosePosition | Candle |
| 53 | SyntheticBond | SyntheticBondsArbitrage | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle |
| 54 | SyntheticBond | SyntheticBondsCurveArbitrage | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle |
| 55 | SyntheticBond | SyntheticBondsCurveMonitor | BuyAtMarket, SellAtMarket | — | CloseAtMarket, ClosePositionsOnTab | Candle |
| 56 | SyntheticBond | SyntheticBondsScalper | BuyAtLimit, BuyAtMarket, SellAtLimit | BuyAtLimitToPosition, BuyAtMarketToPosition | CloseAtLimit, CloseAtMarket | Candle |
| 57 | Dividends | DividendCaptureScreener | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle |
| 58 | Dividends | KeltnerDividendScreener | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Candle |
| 59 | Dividends | ShortBadDividends | SellAtIcebergMarket | — | CloseAtIcebergMarket | PriceChannel, Candle |
| 60 | NewsBots | NewsAIBot | BuyAtMarket, SellAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | News |
| 61 | NewsBots | TelegramCryptoXBot | BuyAtLimit, BuyAtMarket, SellAtLimit, SellAtMarket | — | CloseAtLimitUnsafe, CloseAtStopMarket | Candle, News |
| 62 | Sectors | SectorsSetAlligatorTurtle | BuyAtStopCancel, BuyAtStopMarketIceberg | — | CloseAtStopMarketIceberg | Alligator, PriceChannel, Williams, Candle |
| 63 | Sectors | SectorsSetBollingerMomentum | BuyAtStopCancel, BuyAtStopMarketIceberg | — | CloseAtStopMarketIceberg | Bollinger, Momentum, Envelop, Candle |
| 64 | SpeculantSet | SpeculantSetAtrKeltner | BuyAtStopCancel, BuyAtStopMarketIceberg, SellAtStopCancel, SellAtStopMarketIceberg | — | CloseAtStopMarketIceberg | Bollinger, Atr, Candle |
| 65 | SpeculantSet | SpeculantSetMomentumAdaPc | BuyAtStopCancel, BuyAtStopMarketIceberg, SellAtStopCancel, SellAtStopMarketIceberg | — | CloseAtStopMarketIceberg | PriceChannel, Momentum, Envelop, Candle |
| 66 | OnScriptIndicators | BbPowerTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | BearsPower, BullsPower, Candle |
| 67 | OnScriptIndicators | BollingerRevers | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Bollinger, Candle |
| 68 | OnScriptIndicators | BollingerTrailing | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Bollinger, Candle |
| 69 | OnScriptIndicators | CciTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Cci, Candle |
| 70 | OnScriptIndicators | FundBalanceDivergenceBot | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle |
| 71 | OnScriptIndicators | MacdRevers | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Macd, Candle |
| 72 | OnScriptIndicators | MacdTrail | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Macd, Candle |
| 73 | OnScriptIndicators | OneLegArbitrage | BuyAtLimit, SellAtLimit | — | CloseAtMarket | Sma, Candle |
| 74 | OnScriptIndicators | PairRsiTrade | BuyAtMarket, SellAtMarket | — | CloseAllPositions, CloseAtMarket | Rsi, Candle |
| 75 | OnScriptIndicators | PriceChannelBreak | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | PriceChannel, Candle |
| 76 | OnScriptIndicators | PriceChannelVolatility | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop | PriceChannel, Atr, Candle |
| 77 | OnScriptIndicators | RsiTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Rsi, Candle |
| 78 | OnScriptIndicators | RviTrade | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Macd, Candle |
| 79 | OnScriptIndicators | SmaTrendSample | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop, ClosePositionLogic | Sma, Envelop, Candle |
| 80 | OnScriptIndicators | TimeOfDayBot | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | — |
| 81 | PositionsMicromanagement | AlligatorTrendAverage | BuyAtMarket, SellAtMarket | BuyAtMarketToPosition, SellAtMarketToPosition | CloseAtMarket | Alligator, Candle |
| 82 | PositionsMicromanagement | CandlesTurnaroundPattern | BuyAtMarket | — | CloseAtLimit, CloseAtStopMarket | Atr, Candle |
| 83 | PositionsMicromanagement | CustomIcebergSample | BuyAtMarket, SellAtMarket | BuyAtMarketToPosition, SellAtMarketToPosition | CloseAtMarket, ClosePositionMethod | Bollinger, Candle |
| 84 | PositionsMicromanagement | EnvelopsCountertrend | BuyAtMarket, BuyAtStopMarket, SellAtMarket, SellAtStopMarket | — | CloseAtProfitMarket, CloseAtStopMarket | Envelop, Candle |
| 85 | PositionsMicromanagement | PriceChannelCounterTrend | BuyAtMarket, SellAtMarket | — | CloseAtLimit, CloseAtStopMarket | PriceChannel, Candle |
| 86 | PositionsMicromanagement | TwoEntrySample | BuyAtMarket | — | CloseAtTrailingStopMarket | PriceChannel, Envelop, Candle |
| 87 | PositionsMicromanagement | UnsafeAveragePosition | BuyAtLimitToPositionUnsafe, BuyAtMarket, SellAtLimitToPositionUnsafe, SellAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | Envelop, Candle |
| 88 | PositionsMicromanagement | UnsafeLimitsClosingSample | BuyAtMarket, SellAtMarket | — | CloseAtLimitUnsafe, CloseAtStopMarket | Envelop, Candle |
| 89 | TechSamples | ChangePriceBotExtStopMarket | BuyAtLimit, SellAtLimit | — | CloseAtProfitMarket, CloseAtStopMarket, ClosePositionAtStopAndProfit | — |
| 90 | TechSamples | CustomParamsUseBotSample | BuyAtMarket | — | CloseAtProfit, CloseAtStop | Sma, Atr, Candle |
| 91 | TechSamples | CustomTableInParamWindowSample | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStop | Candle |
| 92 | TechSamples | OpenInterestBotSample | BuyAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | Candle |
| 93 | TechSamples | ServerStopOrdersSample | BuyAtStopCancel, BuyAtStopOnServer | — | CloseAtStopOnServer | Candle |
| 94 | TechSamples | StopByTradeFeedSample | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | PriceChannel, Candle |
| 95 | TechSamples | TradeLineExample | BuyAtMarket | — | CloseAtMarket, CloseAtProfitMarket, CloseAtStopMarket | Candle |
| 96 | AlgoStart | AlgoStart1LinearRegression | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle |
| 97 | AlgoStart | AlgoStart2Soldiers | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle |
| 98 | AlgoStart | AlgoStart3PriceChannel | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, PriceChannel, Candle |
| 99 | AlgoStart | AlgoStart4Railway | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Rsi, Candle |
| 100 | FuturesStart | FuturesStart1Bollinger | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Bollinger, Candle |
| 101 | FuturesStart | FuturesStart2Keltner | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Candle |
| 102 | Options | OptionsSpread | BuyAtMarket, SellAtMarket | — | CloseAtMarket | — |

## (root) (2)

*корень Robots/ — BotFactory и базовые классы; инфраструктура, не роботы*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| BotCreateUi2 | Interaction logic for BotCreateUi2.xaml | — | — | — | News | служебный |
| BotFactory | Ignore if assembly can't be loaded | — | — | — | Candle | служебный |

## AlgoStart (4)

*стартовые алго-роботы OsEngine (шаблоны автора)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| AlgoStart1LinearRegression | Description Trading robot for osEngine The trend robot-screener on LinearRegression channel and Volatility group. Buy: 1. The candle closed above the upper line of the Linear Regression Channel 2. Filter by volatility groups. All screener p … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle | торговый |
| AlgoStart2Soldiers | Description Trading robot-screener for osEngine The trend robot on three growing candles that must be of a certain size to the current volatility and Volatility group. Buy: 1. When we see three growing candles of a certain size to the curre … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle | торговый |
| AlgoStart3PriceChannel | Description Trading robot for osEngine The trend robot-screener on Adaptive Price Channel and Volatility group. Buy: 1. The candle closed above the upper line of the Price Channel 2. Filter by volatility groups. All screener papers are divi … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, PriceChannel, Candle | торговый |
| AlgoStart4Railway | Description Trading robot for osengine The trend robot-screener on ZigZag Channel and Volatility group. Buy: 1. The candle is buried above the ZigZag Channel's inclined channel level. 2. Filter by volatility groups. All screener papers are … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Rsi, Candle | торговый |

## AutoTestBots (44)

*СЛУЖЕБНЫЕ тест-боты OsEngine (серверы/ордера/данные) — НЕ торговые*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| BotTabSimple_1_OpenStopLimit | Test for BotTabSimple.BuyAtStopOnServer / SellAtStopOnServer (server StopLimit position opening) | BuyAtStopOnServer, SellAtStopOnServer | — | CloseAtMarket, CloseAtStopOnServer | — | служебный |
| BotTabSimple_2_OpenStopMarket | Test for BotTabSimple.BuyAtStopMarketOnServer / SellAtStopMarketOnServer (server StopMarket position opening) | BuyAtStopMarketOnServer, SellAtStopMarketOnServer | — | CloseAtMarket, CloseAtStopMarketOnServer | — | служебный |
| BotTabSimple_3_CloseAtStop | Test for BotTabSimple.CloseAtStopOnServer / CloseAtStopMarketOnServer (server stop position closing) | BuyAtMarket | — | CloseAtMarket, CloseAtStopMarketOnServer, CloseAtStopOnServer | — | служебный |
| BotTabSimple_4_ToPosition | Test for BotTabSimple.BuyAtStopOnServerToPosition / BuyAtStopMarketOnServerToPosition (adding volume to a position by server stop orders) | BuyAtMarket | BuyAtStopMarketOnServerToPosition, BuyAtStopOnServerToPosition, SellAtStopMarketOnServerToPosition, SellAtStopOnServerToPosition | CloseAtMarket | — | служебный |
| BotTabSimple_5_Cancel | Test for BotTabSimple.CloseAtStopOnServerCancel (cancel of server stop orders of one position / all positions) | BuyAtMarket | — | CloseAtLimit, CloseAtMarket, CloseAtStopOnServer, CloseAtStopOnServerCancel | — | служебный |
| BotTabSimple_6_AutoRestCancel | Test for the automatic cancel of the remaining server stop orders when a position is closed by another way (BotTabSimple.TryCancelRestStopOrders) | BuyAtMarket | — | CloseAtMarket, CloseAtStopOnServer | — | служебный |
| Conn_1_Status | — | — | — | — | — | служебный |
| Conn_2_SubscrAllSec | — | — | — | — | Candle | служебный |
| Conn_3_Stress_Memory | — | — | — | — | Candle | служебный |
| Conn_4_Validation_Candles | 1 null быть не должно. Это должны быть свечи 2 правильно ли расположено время в массиве. Сначала - старые данные. К концу массива - новые. 3 нет ли задвоения свечек | — | — | — | Candle | служебный |
| Conn_5_Screener | Bids – уровни заявок на покупку. 0 индекс самый высокий.И далее, чем больше индекс тем меньше цена | — | — | — | Candle | служебный |
| Data_1_Integrity | 6.1.Тесты на коротком периоде. 2 дня 6.1.1.Выкачивать все данные которые есть в ServerPermission как разрешённые к скачке Взять один инструмент и попробовать скачать все за два дня. И по каждому источнику должно быть именно 2 дня. 6.1.2.Уме … | — | — | — | Candle | служебный |
| Data_2_Validation_Candles | 7.2.Странные запросы 7.2.1.Не падать / зависать если запрашивают очень старые данные. И данные из будущего. 7.2.2.Время старта больше время конца. 7.2.3.Актуальное время больше конца | — | — | — | Candle | служебный |
| Data_3_Validation_Trades | 7.2.Странные запросы 7.2.1.Не падать / зависать если запрашивают очень старые данные. И данные из будущего. 7.2.2.Время старта больше время конца. 7.2.3.Актуальное время больше конца | — | — | — | — | служебный |
| Data_4_Stress_Candles | 1.Скачать по N инструментам все имеющиеся свечи за 1 год.Просмотреть входящие в сет данные.Проверить время старта и конца данных. 2.Скачать по N инструментам все имеющиеся свечи за ПРОШЛЫЙ год.Просмотреть входящие в сет данные.Проверить вре … | — | — | — | Candle | служебный |
| Data_5_Stress_Trades | 3.Скачать по 2 инструментам трейды. За последние 10 дней.Просмотреть входящие в сет данные.Проверить время старта и конца данных. 4.Скачать трейды по 2 инструментам за 10 дней три месяца назад. Просмотреть входящие в сет данные.Проверить вр … | — | — | — | — | служебный |
| Orders_10_RequestLostDoneOrder | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_11_RequestLostMyTrades | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_12_RequestOrdersList | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_13_StopOrders | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_14_StopLimitPlaceCancel | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_15_StopLimitRequestOnReconnect | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_16_StopTriggerOnReconnect | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_1_FakeOrders | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_2_LimitsExecute | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_3_MarketOrders | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_4_LimitCancel | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_5_ChangePrice | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_6_ChangePriceError | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_7_Add_Move_Cancel_Spam | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_8_RequestOnReconnect | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Orders_9_RequestLostActivOrder | 1.NumberUser – нужно указывать чтобы OsEngine распознал данный ордер как свой. 2.NumberMarket – номер ордера на бирже 3.SecurityNameCode – название бумаги 4.SecurityClassCode – название класса бумаги 5.PortfolioNumber – название портфеля 6. … | — | — | — | — | служебный |
| Portfolio_1_Validation | Обязательные поля в портфеле 1.Number – по сути это название портфеля.На Moex это очень длинный номер, а вернее их почти всегда N.И на разных номерах счетов лежат разное кол-во денег и возможны разные валюты.В крипте – это обычно просто наз … | — | — | — | — | служебный |
| TestBotCandlesComparison | Description TestBot for OsEngine. Do not turn on - a robot for testing the synchronism of candles created inside OsEngine and requested from the exchange. | — | — | — | Candle | служебный |
| TestBotConnection | Description TestBot for OsEngine. Do not turn on - robot for connection testing. | — | — | — | — | служебный |
| TestBotConnectionParams | Логика взаимодействия для TestBotConnectionParams.xaml | — | — | — | — | служебный |
| TestBotOpenAndCanselOrders | Description TestBot for OsEngine. Do not enable - robot for testing the opening and closing of orders. | BuyAtLimit, SellAtLimit | — | — | — | служебный |
| TestBotOption | Description TestBot for OsEngine. Do not turn on - robot for Option testing. | — | — | — | — | служебный |
| TestBotTradesInCandlesTest | Description TestBot for OsEngine. Do not enable - a robot for testing the synchronism of the array of trades in the candle and the candles themselves. | — | — | — | Sma, Candle | служебный |
| Var_1_Securities | 2.1. НЕ обязательные поля 2.1.1. Go – это гарантированное обеспечение для фьючерсной площадки МОЕКС. Не нужное 2.1.2. OptionType – тип опциона. Не нужно нам пока 2.1.3. Strike – тоже опционная тематика. Не нужно 2.1.4. Expiration – опционы. … | — | — | — | — | служебный |
| Var_2_MarketDepth | 5.1. Требования к данным MarketDepth 5.1.1. Главный объект стакана котировок 5.1.2. Bid никогда не должен быть равен Ask 5.1.3. Bid не должен быть выше Ask 5.1.4. SecurityNameCode – обязательное поле. Не может быть равен null или содержать … | — | — | — | — | служебный |
| Var_3_Trades | Требования к данным Trade * * 1. Последовательность - нельзя высылать устаревшие данные. * 2. Время трейдов не должно совпадать * 3. ID трейдов не должно быть null * 4. Цена трейда не должна быть 0 * 5. Объём трейда не должен быть 0 * 6. У … | — | — | — | — | служебный |
| WServerTester | — | — | — | — | — | служебный |
| WServerTesterDescriptionUi | Test description window for the WServerTester robot Окно описания теста для робота WServerTester | — | — | — | — | служебный |

## BotsFromStartLessons (18)

*учебные роботы курса «C# для алготрейдера» — простые явные правила входа/выхода*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| Lesson1HelloWorld | Description Robot-example from the course of lectures "C# for algotreader". This script creates a button in the parameters of the robot. When you press the button, the Log error with the text "Hello world!" | — | — | — | — | контур не найден |
| Lesson2Bot1 | Description Robot-example from the course of lectures "C# for algotreader". In this code 5 buttons are created in the parameters of the robot. Different buttons are responsible for the interaction with different types of data. button #1 bri … | — | — | — | — | контур не найден |
| Lesson2Bot2 | Description Robot-example from the course of lectures "C# for algotreader". This code describes the creation of 5 different parameters for the robot. 1)Mode is an example of the parameter of the mode of operation of the robot. It includes a … | — | — | — | Sma, Bollinger | контур не найден |
| Lesson3Bot1 | Description Robot-example from the course of lectures "C# for algotreader". the robot is called when the candle is closed. Buy: When the second to last and last candle grew Sell: Trailing Stop by Low-value second to last candle. | BuyAtMarket | — | CloseAtTrailingStopMarket | Candle | торговый |
| Lesson3Bot2 | Description Robot-example from the course of lectures "C# for algotreader". the robot is called when the candle is closed. Buy: if low-value from Last Candle < Sma and close-value from Last Candle > Sma. Buy At Limit. Sell: position is open … | BuyAtLimit | — | CloseAtMarket | Sma, Candle | торговый |
| Lesson3Bot3 | Description Robot-example from the course of lectures "C# for algotreader". the robot is called when the candle is closed. Buy: SmaFast > SmaSlow. Buy At Market. Sell: SmaFast < SmaSlow. Close At Market. | BuyAtMarket | — | CloseAtMarket | Sma, Candle | торговый |
| Lesson4Bot1 | Description Robot example from the lecture course "C# for algotreader". This robot shows the tracking of various source events. Buy: SmaFast > SmaSlow. Buy at the market. Sell: SmaFast < SmaSlow. Close at the market. | BuyAtMarket | — | CloseAtMarket | Sma, Candle | торговый |
| Lesson5Bot1 | Description Robot example from the lecture course "C# for algotreader". Buy: low-value from Candle < last value Sma and close-value Candle > last value Sma. Buy at market. Sell: high-value from Candle > last value Sma and close-value Candle … | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Sma, Candle | торговый |
| Lesson5Bot2 | Description Robot example from the lecture course "C# for algotreader". Buy: if alligator lips > teeth > jaw (lips - fast, teeth - medium, jaw - slow), additional open if last value AO > previous value AO and previous value AO > previous pr … | BuyAtMarket | BuyAtMarketToPosition | CloseAtTrailingStopMarket | Alligator, PriceChannel, Candle | торговый |
| Lesson6Bot1 | Description Robot example from the lecture course "C# for algotreader". Buy: 1) Buy At Stop when the price breaks the upper Bollinger Band. 2) Add a second position: Buy At Stop at EntryPrice + ATR × MultOne. 3) Add a third position: Buy At … | BuyAtStop | — | CloseAtTrailingStop | Bollinger, Atr, Candle | торговый |
| Lesson7Bot1 | Description Robot example from the lecture course "C# for algotreader". Buy: Three growing candles in a row. Sma does not fall. Volatility of three candles > HeightSoldiers. Volatility of each candle > MinHeightOneSoldier. Exit: Close at tr … | BuyAtMarket | — | CloseAtTrailingStopMarket, TrailingStop | Sma, Candle | торговый |
| Lesson8Bot1 | Description Robot example from the lecture course "C# for algotreader". Buy: If best bid volume bigger on value of _percentInFirstBid, than volume in the summary bid below. Buy at limit price Exit: Close At Stop Market and Close At Profit M … | BuyAtLimit | — | CloseAtProfitMarket, CloseAtStopMarket | — | торговый |
| Lesson8Bot2 | Description Robot example from the lecture course "C# for algotreader". Buy: Buy At Stop high price channel. Exit: Close At Trailing Stop Market low price channel | BuyAtStop | — | CloseAtTrailingStopMarket | Candle | торговый |
| Lesson9Bot1 | Description Robot example from the lecture course "C# for algotreader". Stores examples of different methods for entering in position. When you click on the button in robot parameters, an order of the selected type is created. You can close … | BuyAtFake, BuyAtIceberg, BuyAtIcebergMarket, BuyAtLimit, BuyAtMarket, BuyAtStop, BuyAtStopCancel, BuyAtStopMarket, SellAtFake, SellAtIceberg, SellAtIcebergMarket, SellAtLimit, SellAtMarket, SellAtStop, SellAtStopCancel, SellAtStopMarket | — | CloseAtMarket | Candle | торговый |
| Lesson9Bot2 | Description Robot example from the lecture course "C# for algotreader". Stores examples of different methods for modification a position. The buttons are used to create more orders for the position. | BuyAtIcebergToPositionMarket, BuyAtLimitToPositionUnsafe, BuyAtMarket, BuyAtStopCancel, SellAtIcebergToPositionMarket, SellAtLimitToPositionUnsafe, SellAtMarket, SellAtStopCancel | BuyAtIcebergToPosition, BuyAtLimitToPosition, BuyAtMarketToPosition, SellAtIcebergToPosition, SellAtLimitToPosition, SellAtMarketToPosition | CloseAtMarket | — | торговый |
| Lesson9Bot3 | Description Robot example from the lecture course "C# for algotreader". Stores examples of different methods for close position. | BuyAtMarket, SellAtMarket | — | CloseAtFake, CloseAtIceberg, CloseAtIcebergMarket, CloseAtLimit, CloseAtLimitUnsafe, CloseAtMarket | — | торговый |
| Lesson9Bot4 | Description Robot example from the lecture course "C# for algotreader". Stores examples of different methods for close position from stops and profits. | BuyAtMarket, SellAtMarket | — | CloseAtMarket, CloseAtProfit, CloseAtProfitLimitMethod, CloseAtProfitMarket, CloseAtProfitMarketMethod, CloseAtStop, CloseAtStopLimitMethod, CloseAtStopMarket, CloseAtStopMarketMethod, CloseAtTrailingStop, CloseAtTrailingStopLimitMethod, CloseAtTrailingStopMarket, CloseAtTrailingStopMarketMethod | Candle | торговый |
| Lesson9Bot5 | Description Robot example from the lecture course "C# for algotreader". Stores examples of different methods for manage position. | BuyAtLimit | — | — | — | вход без выхода |

## CounterTrend (7)

*контренд: вход против движения (перекупленность/перепроданность, развороты)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| ClusterCountertrend | Description The Countertrend robot Buy: we buy if we are under the largest volume for sale in the last 10 candles. Sell: sell if we are above the largest purchase volume for the last 10 candles. Exit logic: By return signal. | BuyAtLimit, SellAtLimit | — | CloseAtMarket | Candle | торговый |
| RsiContrtrend | Description Overbought / Oversold RSI Countertrend Strategy with Trend Filtering via MovingAverage Buy: 1. Sma more price. 2. Rsi is higher than UpLine. Sale: 1. Sma less price. 2. Rsi is less than DownLine. Exit: By return signal | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Rsi, Candle | торговый |
| RsiContrtrendUi | — | — | — | — | Rsi | контур не найден |
| StrategyBollinger | Description The Countertrend robot Buy: 1. Price below BollingerDownLine. Sell: 1. The price is more than BollingerUpLine. Exit: 1. At the intersection of Sma with the price | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Bollinger, Candle | торговый |
| StrategyBollingerUi | — | — | — | — | — | контур не найден |
| WilliamsRangeTrade | Description Counter Trend Strategy Based on Willams% R Indicator Buy: 1. Williams Range is smaller than DownLine. sell: 1. Williams Range is larger than UpLine. exit: 1. On the return signal | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Rsi, Williams, Candle | торговый |
| WilliamsRangeTradeUi | — | — | — | — | Williams | контур не найден |

## CurrencyArbitrage (2)

*арбитраж валютных пар*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| CurrencyArbitrageClassic | Description Arbitrage robot for OsEngine. A robot for classic currency arbitrage. | — | — | — | — | контур не найден |
| CurrencyMoveExplorer | Description Arbitrage robot for OsEngine. Robot for research. Saves slices of the situation on the bundle of instruments within 3 seconds after receiving a signal that there is profit on the sequence. | — | — | — | — | контур не найден |

## Dividends (3)

*дивидендные стратегии (календарь отсечек)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| DividendCaptureScreener | Description Trading robot for osEngine The robot-screener buys shares a few days before the dividend registry close date and sells on the next day after the registry close. Entry: 1. The security has future dividends within the next N days. … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle | торговый |
| KeltnerDividendScreener | Description Trading robot for osEngine The trend robot-screener on Keltner Channel with dividend filter. The robot trades only Long through a BotTabScreener. Buy: 1. The candle closed above the upper line of the Keltner Channel. 2. The secu … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Candle | торговый |
| ShortBadDividends | Description Trading robot for osEngine The robot-screener on Adaptive Price Channel with a "bad dividends" filter. The robot trades only Short through a BotTabScreener. Short entry: 1. The candle closed below the lower line of the Adaptive … | SellAtIcebergMarket | — | CloseAtIcebergMarket | PriceChannel, Candle | торговый |

## Engines (7)

*инфраструктурные «движки» панелей — не самостоятельные роботы*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| CandleEngine | — | — | — | — | Candle | служебный |
| ClusterEngine | — | — | — | — | — | служебный |
| EnginePair | — | — | — | — | — | служебный |
| NewsEngine | — | — | — | — | News | служебный |
| OptionsEngine | — | — | — | — | — | служебный |
| PolygonalEngine | — | — | — | — | — | служебный |
| ScreenerEngine | — | — | — | — | — | служебный |

## Funding (3)

*фандинг-стратегии (крипто: ставка фандинга)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| ArbitrageFunding | — | BuyAtMarket, SellAtMarket | — | — | — | вход без выхода |
| TakeFunding | — | BuyAtMarket, SellAtMarket | — | — | — | вход без выхода |
| TestBotFunding | — | — | — | — | — | контур не найден |

## FuturesScreeners (2)

*скринеры фьючерсов — отбор, не торгуют*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| FuturesScreenerLrAdaPc | Берём фьюч: 1) Если уже есть позиция 2) Берём ближайший фьюч 2.2) Если до ближайшего фьючерса меньше 3 дней до экспирации, не учитываем его как точку входа. 2.3) Но не дальше чем 100 дней, на случай если пропущена серия в тестере. | BuyAtStopCancel, BuyAtStopMarketIceberg, SellAtStopCancel, SellAtStopMarketIceberg | — | CloseAtIcebergMarket, CloseAtStopMarketIceberg | PriceChannel, Candle | служебный |
| FuturesScreenerLrSma | Берём фьюч: 1) Если уже есть позиция 2) Берём ближайший фьюч 2.2) Если до ближайшего фьючерса меньше 3 дней до экспирации, не учитываем его как точку входа. 2.3) Но не дальше чем 100 дней, на случай если пропущена серия в тестере. | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Candle | служебный |

## FuturesStart (2)

*стартовые фьючерсные стратегии*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| FuturesStart1Bollinger | Трендовушка на пробое боллинджера. С фильтром по стадии отклонения фьючерса от базы. Рассчитана на рынок фьючерсов на акции MOEX Индикаторы Bollinger ВХОД в позицию Пересечение верхней или нижней линии боллинджера Выход из позиции Пересечен … | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Bollinger, Candle | торговый |
| FuturesStart2Keltner | Трендовушка на пробое канала Келтнера. С фильтром по стадии отклонения фьючерса от базы. Рассчитана на рынок фьючерсов на акции MOEX Индикаторы Keltner Channel ВХОД в позицию Пересечение верхней или нижней линии канала Выход из позиции Пере … | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Candle | торговый |

## FuturesTrend (2)

*трендовые стратегии на фьючерсах*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| FuturesTrendBollinger | Берём фьюч: 1) Если уже есть позиция 2) Берём ближайшую фьючерс 2.2) Если до ближайшего фьючерса меньше 3 дней до экспирации, не учитываем его как точку входа. 2.3) Но не дальше чем 100 дней месяца, на случай если пропущена серия в тестере. | BuyAtIcebergMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket | Bollinger, Candle | торговый |
| FuturesTrendPriceChannel | Берём фьюч: 1) Если уже есть позиция 2) Берём ближайшую фьючерс 2.2) Если до ближайшего фьючерса меньше 3 дней до экспирации, не учитываем его как точку входа. 2.3) Но не дальше чем 100 дней месяца, на случай если пропущена серия в тестере. | BuyAtIcebergMarket | — | CloseAtIcebergMarket | PriceChannel, Candle | торговый |

## Grids (8)

*сетки: усреднение/пирамидинг, серии ордеров*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| GridBollinger | Description A robot demonstrating grid operation in countertrend. Throws out a grid of "MarketMaking" type Bollinger indicator serves as a signal for grid throwing. When the candlestick closing price is above the upper channel line of the i … | — | — | — | Bollinger, Candle | контур не найден |
| GridBollingerScreener | Description Grid counter-trend screener. Bollinger and ADX. We turn on the grid on reduced volatility and breakdown of the level. Volatility is viewed by ADX Additionally: Work days / Non-trading periods intraday | — | — | — | Bollinger, Candle | контур не найден |
| GridLinearRegression | Description Robot showing the work with the grid. Throws a grid of the “Position Opening” type and closes the grid by a general trailing stop order. The linear regression indicator serves as a signal for grid throwing. When the candlestick … | — | — | — | Candle | контур не найден |
| GridPair | Description Market maker's grid for trading in a pair. A graph of minimum residuals from the difference of two price series with an optimal multiplier is calculated. Extreme deviations of two securities from each other are traded. | — | — | — | Candle | контур не найден |
| GridScreenerAdaptiveSoldiers | Description Grid Trend screener. Adaptive by volatility. We turn on the grid on when forming a pattern of three growing / falling candles Stop by lifetime and positions count Additionally: Work days / Non-trading periods intraday Trailing U … | — | — | — | Sma, Candle | контур не найден |
| GridTwoSides | Description Ejection of two grids in both directions at the same time. Signal to start trading: Atr fell in M than it was N candles ago Signal to stop trading: Atr became higher than it was N candles ago | — | — | — | Atr, Candle | контур не найден |
| GridTwoSignals | Description Ejection of two grids in one direction First buy signal: Breakdown of Price-Channel down Second buy signal: There is the first grid + price returned to the center of the channel. Output: By the number of closed lines Output 2: B … | — | — | — | Bollinger, PriceChannel, Candle | контур не найден |
| GridVolumeBollingerRankingScreener | — | — | — | — | Bollinger, Candle | контур не найден |

## Helpers (5)

*вспомогательные классы — не роботы*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| DcaTimeBot | Description A robot that helps you buy and sell security at a specific time interval Робот помогающий покупать и продавать активы с определённым временным интервалом | BuyAtMarket, SellAtMarket | — | — | — | служебный |
| LiquidityAnalyzer | Робот-анализатор мгновенной ликвидности Составляет таблицу с данными за указанное время Один инструмент - одна строка Данные по инструменту собираются за указанное время, раз в пять секунд Данные о том, на каком расстоянии от центра стакана … | — | — | — | — | служебный |
| PayOfMarginBot | Description Робот работает только в Тестере. Робот предназначен для ежедневного списывания маржинальной комиссии, если сумма взятых ордеров превышает размеры депозита. The bot only works in the Tester. The bot is designed to charge a margin … | — | — | — | Candle | служебный |
| TaxPayer | Description Робот работает только в Тестере. При тестировании робот по окончанию года проверяет Журналы всех ботов. Считает в Журнале профит сделок за последний год, расчитывает налог, и списывает этот налог из депозита. The bot only works … | — | — | CloseAllPositions | Candle | служебный |
| TmonRebalancer | Description Робот предназначен для покупки TMON вечером на свободные средства и продаже всего объема TMON утром. The robot is designed to buy TMON in the evening with available funds and sell the entire volume of TMON in the morning. | BuyAtIceberg, BuyAtLimit | BuyAtIcebergToPosition, BuyAtLimitToPosition | CloseAtMarket, ClosePositions | Candle | служебный |

## High Frequency (3)

*высокочастотные стратегии*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| Fisher | Description Fisher based on multithreading Entering a position - we are waiting for a sharp price deviation by the specified number of percent from the edge of the order book. Exit a position when the price rolls back by 50 percent or more … | BuyAtLimit, SellAtLimit | — | CloseAllPositions, CloseAtLimit | Sma, Bollinger | торговый |
| HighFrequencyTrader | Logic Last time check marketDepth | BuyAtLimit, SellAtLimit | — | CloseAtMarket, CloseAtProfit, CloseAtStop, ClosePositionThreadArea | Candle | торговый |
| MarketDepthScreener | Description Trading robot for osengine. The trend robot on MarketDepth Screener. Buy: 1. Step First: Analyze the latest Momentum value: if it is below the minimum acceptable value (MinMomentumValue), proceed to the next entry step. 2. Step … | BuyAtLimit | — | CloseAtLimit, CloseAtMarket | Momentum | торговый |

## IndexArbitrage (4)

*индексный арбитраж (фьючерс vs индекс)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| IndexArbitrageClassic | Description Index Arbitrage robot for OsEngine. Classic trading with two index. | BuyAtMarket, SellAtMarket | — | CloseAllPositionsByMarket, CloseAtMarket | Candle | торговый |
| MultiExchangePairArbitrageOnTheIndex | Description Index Arbitrage robot for OsEngine. Arbitrage of several currency pairs on the index. | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle | торговый |
| MultiOneLegArbitrageInTrend | Description Index Arbitrage robot for OsEngine. Securities that deviate from the broad market without momentum are traded on a return to the index. | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Candle | торговый |
| MultiOneLegArbitrageMeanReversion | Description Index Arbitrage robot for OsEngine. Securities that deviate from the broad market without momentum are traded on a return to the index. | BuyAtLimit, SellAtLimit | — | CloseAtLimit, ClosePosition | Candle | торговый |

## MarketMaker (7)

*маркет-мейкинг: двустороннее котирование*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| MarketMakerBot | Description MarketMaker trading for OsEngine. Buy: If the price crosses above a line from below, it is considered a buy signal. Sell: If the price crosses below a line from above, it is considered a sell signal. Exit: Opposite signal. | BuyAtMarket, SellAtMarket | BuyAtMarketToPosition, SellAtMarketToPosition | CloseAtMarket | Candle | торговый |
| MarketMakerBotUi | — | — | — | — | — | контур не найден |
| PairTraderSimple | Pair trading robot for OsEngime Robot for pair trading. trading two papers based on their acceleration to each other by candle. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Candle | торговый |
| PairTraderSimpleUi | Interaction logic for PairTraderSimpleUi.xaml | — | — | — | — | контур не найден |
| PairTraderSpreadSma | pair trading robot building spread and trading based on the intersection of MA on the spread chart SmaLong crossed SmaShort from top to bottom - the first tab sells, the second one buys. SmaLong crossed SmaShort from the bottom up - the fir … | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Candle | торговый |
| PairTraderSpreadSmaUi | — | — | — | — | — | контур не найден |
| TwoLegArbitrage | Description Pair trading based on index analysis. Buy: when the RSI indicator built on the index goes below the oversold level. Sell: when the RSI indicator built on the index goes above the oversold level. Exit: by reverse system. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Rsi, Candle | торговый |

## Monitors (4)

*мониторы состояния — не торгуют*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| MonitorHighLow | MonitorHighLow Монитор Low и High по бумагам за N свечек Содержит в себе богатую визуальную часть, в которой видно таблицу близости к хаям по выбранным активам При фиксации определённого движения вверх или вниз, поддерживает: 1) Сигналы виз … | BuyAtMarket, SellAtMarket | — | CloseAtMarket, CloseAtProfitMarket, CloseAtStopMarket | PriceChannel, Candle | служебный |
| MonitorImpulse | MonitorImpulse Монитор для анализа движений вниз и вверх по отдельным активам за N свечек Содержит в себе богатую визуальную часть, в которой видно таблицу движений по выбранным активам При фиксации определённого движения вверх или вниз, по … | BuyAtMarket, SellAtMarket | — | CloseAtMarket, CloseAtProfitMarket, CloseAtStopMarket | Candle | служебный |
| MonitorRsi | MonitorRsi Монитор для анализа движений вниз и вверх по отдельным активам за N свечек Содержит в себе богатую визуальную часть, в которой видно таблицу движений по выбранным активам При фиксации определённого движения вверх или вниз, поддер … | BuyAtMarket, SellAtMarket | — | CloseAtMarket, CloseAtProfitMarket, CloseAtStopMarket | Rsi, Candle | служебный |
| MonitorVolume | MonitorVolume Монитор для анализа перемещения бумаг по ренкингу объёмов за N свечек Содержит в себе богатую визуальную часть, в которой видно таблицу движений по выбранным активам При фиксации определённого движения вверх или вниз, поддержи … | BuyAtMarket, SellAtMarket | — | CloseAtMarket, CloseAtProfitMarket, CloseAtStopMarket | Candle | служебный |

## NewsBots (2)

*новостные стратегии*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| NewsAIBot | Description Trading robot for OsEngine Works with a news source and a screener in which LLM analyzes the news and decides whether to trade or not. The LLM gets the role of a financial analyst and, based on the result of the news analysis, m … | BuyAtMarket, SellAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | News | торговый |
| TelegramCryptoXBot | Description Trading robot for OsEngine Works with a news source and a screener in which receives trading signals from the CryptoX\|Protruding Telegram channel, finds a security, makes a deal, sets take profit and stop loss. The signal has th … | BuyAtLimit, BuyAtMarket, SellAtLimit, SellAtMarket | — | CloseAtLimitUnsafe, CloseAtStopMarket | Candle, News | торговый |

## OnScriptIndicators (15)

*роботы на скриптовых индикаторах (кастомная логика индикаторов кодом)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| BbPowerTrade | Description trading robot for osengine Trend strategy based on two indicators BullsPower and BearsPower. Bulls + Bears is less than negative Step - close the position and enter Short. Bulls + Bears more than Step - close the position and en … | BuyAtLimit, SellAtLimit | — | CloseAtLimit | BearsPower, BullsPower, Candle | торговый |
| BollingerRevers | Description trading robot for osengine Trend Strategy Based on Breaking Bollinger Lines Buy: The price is more than BollingerUpLine. Sell: Price below BollingerDownLine. Exit: At the intersection of Sma with the price. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Bollinger, Candle | торговый |
| BollingerTrailing | Description trading robot for osengine Bollinger Bands trading bargaining robot with pull-up Trailing-Stop through Bollinger Bands. Buy: The price is more than BollingerUpLine. Sell: Price below BollingerDownLine. Exit: Trailing-Stop throug … | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Bollinger, Candle | торговый |
| CciTrade | Description trading robot for osengine Counter Trend Strategy Based on CCI Indicator. Max - 3 poses. Buy: CCI is less than DownLine. Sell: CCI more UpLine. Exit: on the return signal. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Cci, Candle | торговый |
| FundBalanceDivergenceBot | Description trading robot for osengine Counter Trend Strategy Based on FundBalanceDivergence Bot. Buy: FBD more Indicator Divergence. Sell: FBD is less than negative Indicator Divergence. Exit: after N number of days. | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle | торговый |
| MacdRevers | Description trading robot for osengine Trend strategy at the intersection of the MACD indicator. Logic of the first Enter: 1. lastMacdDown < 0 and lastMacdUp > lastMacdDown - Buy. 2. lastMacdDown > 0 and lastMacdUp < lastMacdDown - Sell. Ne … | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Macd, Candle | торговый |
| MacdTrail | Description trading robot for osengine Trend strategy based on the Macd indicator and trail stop. Logic Enter: 1. lastMacdDown < 0 and lastMacdUp > lastMacdDown - Buy. 2. lastMacdDown > 0 and lastMacdUp < lastMacdDown - Sell. Exit: By Trali … | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Macd, Candle | торговый |
| OneLegArbitrage | Description trading robot for osengine Trading robot on the index. The intersection of MA on the index from the bottom up long, with the reverse intersection of shorts | BuyAtLimit, SellAtLimit | — | CloseAtMarket | Sma, Candle | торговый |
| PairRsiTrade | Description trading robot for osengine Pair trading based on the RSI indicator. Logic: if RsiOne > RsiTwo + RsiSpread - then we are Sell on the first instrument and Buy on the second one. Logic: if RsiTwo > RsiOne + RsiSpread - then we are … | BuyAtMarket, SellAtMarket | — | CloseAllPositions, CloseAtMarket | Rsi, Candle | торговый |
| PriceChannelBreak | Description trading robot for osengine When the candle is closed outside the PriceChannel channel. We enter the position, the stop loss is at the extremum of the last candle from the entry candle. Take profit by the channel size from the cl … | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | PriceChannel, Candle | торговый |
| PriceChannelVolatility | Description trading robot for osengine Breakthrough of the channel built by PriceChannel + -ATR * coefficient,additional input when the price leaves below the channel line by ATR * coefficient. Trailing stop on the bottom line of the PriceC … | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop | PriceChannel, Atr, Candle | торговый |
| RsiTrade | Description trading robot for osengine RSI's concurrent overbought and oversold strategy. Logic: if RsiSecond <= DownLine - close position and open Long. Logic: if RsiSecond >= UpLine - close position and open Short. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Rsi, Candle | торговый |
| RviTrade | Description trading robot for osengine Trend strategy at the intersection of the indicator RVI. Buy: lastRviDown < 0 and lastRviUp > lastRviDown. Sell: lastRviDown > 0 and lastRviUp < lastRviDown. Exit: By return signal. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Macd, Candle | торговый |
| SmaTrendSample | Description trading robot for osengine Trend robot SmaTrendSample. Buy: lastCandlePrice > smaValue and lastCandlePrice > upChannel. Sell: lastCandlePrice < smaValue and lastCandlePrice < downChannel. Exit: By TralingStop. | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop, ClosePositionLogic | Sma, Envelop, Candle | торговый |
| TimeOfDayBot | — | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | — | торговый |

## Options (1)

*опционные стратегии*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| OptionsSpread | — | BuyAtMarket, SellAtMarket | — | CloseAtMarket | — | торговый |

## PairArbitrage (3)

*парный арбитраж (две коррелированные бумаги)*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| PairCointegrationSideTrader | Description Trading robot for osengine The robot trades on a chart of deviations of one instrument from another, calculated through their difference with the multiplier. Two lines, calculated from the standard deviation multiplied by the mu … | — | — | ClosePositionLogic, ClosePositions | — | контур не найден |
| PairCorrelationNegative | Description trading robot for osengine Bot - trading pairs in the trend If the correlation is below -0.8 and we are on some side of the cointegration - enter counting on a further spread Exit - when correlation rises above 0.8 | — | — | ClosePositionLogic, ClosePositions | — | контур не найден |
| PairCorrelationTrader | Description Trading robot for osengine if the correlation is higher than 0.9 and we are on some side of the cointegration - enter, counting on the pair convergence Exit by the inverse cointegration signal | — | — | ClosePositionLogic, ClosePositions | — | контур не найден |

## Patterns (8)

*паттернные: свечные/фигурные сетапы*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| CandlePatternBoost | Description Trading robot for osengine. Trend strategy on Candle Pattern Boost. Buy: 1. The current closing price is above the level of _lastVGDevUp. 2. The percentage movement upward over this period exceeds the specified threshold. Sell: … | BuyAtMarket, BuyAtStopCancel, SellAtMarket, SellAtStopCancel | — | CloseAtLimit, CloseAtTrailingStop, ClosePosition | Sma, Candle | торговый |
| CustomCandlesImpulseTrader | Discription Trading robot for osengine Trading robot for adaptive by volatility candle series. if he sees a movement to one side in a short period of time, it enters the position | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Candle | торговый |
| PinBarTrade | Discription Trading robot for osengine Trend robot on the PinBar Trade. Buy: 1. The closing price must be **greater than or equal to** the level located at 1/3 of the candle's range from the bottom. 2. The opening price must also be **great … | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Sma, Candle | торговый |
| PivotPointsRobot | Discription Trading robot for osengine Trend robot on the Pivot Points Robot. Buy: 1. The closing price must be **above** resistance level R1. 2. The opening price must be **below** this level. Sell: 1. The closing price must be **below** s … | BuyAtLimit, SellAtLimit | — | CloseAtLimit, CloseAtMarket | Candle | торговый |
| PivotPointsRobotUi | — | — | — | — | — | контур не найден |
| ThreeSoldier | Description Trading robot Three Soldiers. When forming a pattern of three growing / falling candles, the entrance to the countertrend with a fixation on a profit or a stop. | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | Candle | торговый |
| ThreeSoldierVolatilityAdaptive | Description trading robot for osengine The trend robot on Three Soldier Volatility Adaptive. Logic: For the last three candles, it is checked that the absolute value of the difference between the opening and closing prices relative to the c … | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | Candle | торговый |
| VolatilityAdaptiveCandlesTrader | Description trading robot for osengine The trend robot on Volatility Adaptive Candles Trader. Buy: 1. If the difference between the opening and closing price of the current candle relative to its price is less than the specified threshold ( … | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Candle | торговый |

## PositionsMicromanagement (8)

*микроуправление позицией: трейлинг/докупки поверх базовой логики*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| AlligatorTrendAverage | Description trading robot for osengine The trend robot on Strategy Alligator Trend Average. Buy: 1. The current price is above all lines of the Alligator indicator. 2. The Alligator lines are arranged in order: fast > middle > slow. Sell: 1 … | BuyAtMarket, SellAtMarket | BuyAtMarketToPosition, SellAtMarketToPosition | CloseAtMarket | Alligator, Candle | торговый |
| CandlesTurnaroundPattern | Description trading robot for osengine The trend robot on Candles Turnaround Pattern. Buy:The last candle is a fast, large-bodied bullish candle, and the previous candle is a slow, large-bodied bearish candle. Exit:The position is closed by … | BuyAtMarket | — | CloseAtLimit, CloseAtStopMarket | Atr, Candle | торговый |
| CustomIcebergSample | Description trading robot for osengine Countertrend robot on bollinger indicator. Inside of which an example of entering a position by multiple orders through its own logic is implemented. | BuyAtMarket, SellAtMarket | BuyAtMarketToPosition, SellAtMarketToPosition | CloseAtMarket, ClosePositionMethod | Bollinger, Candle | торговый |
| EnvelopsCountertrend | — | BuyAtMarket, BuyAtStopMarket, SellAtMarket, SellAtStopMarket | — | CloseAtProfitMarket, CloseAtStopMarket | Envelop, Candle | торговый |
| PriceChannelCounterTrend | Description trading robot for osengine The Countertrend robot on PriceChannel. Buy: If the price is below the lower level _pc Sell: If the current price exceeds the upper level _pc Exit: Based on stop-loss and profit targets | BuyAtMarket, SellAtMarket | — | CloseAtLimit, CloseAtStopMarket | PriceChannel, Candle | торговый |
| TwoEntrySample | — | BuyAtMarket | — | CloseAtTrailingStopMarket | PriceChannel, Envelop, Candle | торговый |
| UnsafeAveragePosition | — | BuyAtLimitToPositionUnsafe, BuyAtMarket, SellAtLimitToPositionUnsafe, SellAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | Envelop, Candle | торговый |
| UnsafeLimitsClosingSample | — | BuyAtMarket, SellAtMarket | — | CloseAtLimitUnsafe, CloseAtStopMarket | Envelop, Candle | торговый |

## Rebalancers (3)

*ребалансировка портфелей — не спекулятивные*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| RebalancerByDividendQuality | Description Робот еженедельно переключает капитал между акциями с близкими дивидендами и LQDT. Если в скринере есть акции, по которым дата Т-1 ближайшего дивиденда находится в пределах 7 дней от текущей даты, весь капитал входит в эти акции … | BuyAtMarket | — | CloseAtMarket | Candle | служебный |
| RebalancerByMomentum | Description Робот ребалансирует капитал между акциями МосБиржи, золотом и LQDTMOEX по моментуму. Только лонг, без плеча. | BuyAtIcebergMarket, BuyAtMarket, SellAtIcebergMarket | — | CloseAtIcebergMarket, CloseAtMarket | Momentum, Candle | служебный |
| RebalancerClassicDividend | Description Робот еженедельно ребалансирует портфель из дивидендных акций и золота. Работает в двух режимах: 1. Классический режим: поддержание соотношения акции / золото по заданным весам. 2. Дивидендный режим: если у акций в скринере есть … | BuyAtMarket | BuyAtMarketToPosition | CloseAtMarket, ClosePositionsNotInTarget | Sma, Candle | служебный |

## Screeners (9)

*скринеры бумаг — отбор, не торгуют*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| BollingerMomentumScreener | Description trading robot for osengine The trend robot on Bollinger Momentum Screener. Buy: 1. If the closing price of the last candle (lastCandleClose) is above the Bollinger upper line (lastUpBollingerLine) 2. And the Momentum level excee … | BuyAtLimit | — | CloseAtTrailingStop | Bollinger, Momentum, Candle | служебный |
| LinearRegressionFastScreener | Description Trading robot for osengine The trend robot on LinearRegressionFast indicator. Buy: 1. If the ADX filter is active, and the value is zero — no entry occurs. 2. If the SMA filter is active, and the current candle close price (cand … | BuyAtIcebergMarket | — | CloseAtIcebergMarket | Sma, Adx, Candle | служебный |
| PinBarScreener | Discription Trading robot for osengine. Buy: 1. The last candle opened and closed in the upper third of the high-low range. 2. Price is above the SMA. Sell: 1. The last candle opened and closed in the lower third of the high-low range. 2. P … | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | Sma, Candle | служебный |
| PinBarVolatilityScreener | Description trading robot for osengine The trend robot on PinBar Volatility Screener. Buy: 1. The last candle's close and open prices are near the high of the candle, specifically within the top third of the candle's range. 2. The candle's … | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | Sma, Candle | служебный |
| PlateDetectorScreener | Description trading robot for osengine The trend robot on Plate Detector Screener. Buy: 1. If the Bid ratio is higher than the specified minimum value BestBidMinRatioToAll. Exit: based on stop and profit. | BuyAtLimit | — | CloseAtLimit, CloseAtMarket | — | служебный |
| PriceChannelAdaptiveRsiScreener | Description trading robot for osengine The trend robot on PriceChannel Adaptive RsiScreener. Entry long: 1. Verify that the total number of positions across all tabs does not exceed the maximum allowed (MaxPoses). 2. If RSI is below a minim … | BuyAtMarket | — | CloseAtTrailingStopMarket | Sma, Rsi, PriceChannel, Candle | служебный |
| PumpDetectorScreener | Description trading robot for osengine The trend robot on Pump Detector Screener. Buy: If the price change exceeds the specified threshold (MoveToEntry) Exit: by stop and profit. | BuyAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | — | служебный |
| SmaScreener | Description trading robot for osengine The trend robot on Sma Screener. Buy: If there is no position. Open long if the last N candles we were above the moving average Exit: by trailing stop. | BuyAtLimit | — | CloseAtTrailingStop | Sma, Candle | служебный |
| ThreeSoldierAdaptiveScreener | Description trading robot for osengine Trading robot Three Soldiers adaptive by volatility. When forming a pattern of three growing / falling candles, the entrance to the countertrend with a fixation on a profit or a stop. | BuyAtLimit, SellAtLimit | — | CloseAtProfit, CloseAtStop | Sma, Candle | служебный |

## Sectors (2)

*секторные стратегии/ротации*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| SectorsSetAlligatorTurtle | Description Торговый робот для OsEngine. Сборник «Секторальный набор», робот №2. Секторальный трендовик в духе "черепах" по акциям MOEX. 9 скринеров - по одному на сектор экономики (нефтегаз, финансы, металлы, потребсектор, электроэнергетик … | BuyAtStopCancel, BuyAtStopMarketIceberg | — | CloseAtStopMarketIceberg | Alligator, PriceChannel, Williams, Candle | торговый |
| SectorsSetBollingerMomentum | Description Торговый робот для OsEngine. Сборник «Секторальный набор», робот №1. Секторальный трендовый импульсник по акциям MOEX. 9 скринеров - по одному на сектор экономики (нефтегаз, финансы, металлы, потребсектор, электроэнергетика, тра … | BuyAtStopCancel, BuyAtStopMarketIceberg | — | CloseAtStopMarketIceberg | Bollinger, Momentum, Envelop, Candle | торговый |

## SpeculantSet (2)

*комбинированный набор «спекулянт»*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| SpeculantSetAtrKeltner | Description Торговый робот для OsEngine Трендовый импульсный робот. Импульс определяется по росту волатильности (ATR), уровни - по каналу Кельтнера, фильтр тренда - по Bollinger. Конструкция: два источника. 1. BotTabScreener для лонгов (Kel … | BuyAtStopCancel, BuyAtStopMarketIceberg, SellAtStopCancel, SellAtStopMarketIceberg | — | CloseAtStopMarketIceberg | Bollinger, Atr, Candle | торговый |
| SpeculantSetMomentumAdaPc | Description Торговый робот для OsEngine Трендовый импульсный робот. Конструкция: два источника. 1. BotTabScreener для лонгов (PriceChannelAdaptive + Envelop + Momentum на каждой бумаге). 2. BotTabScreener для шортов (независимые параметры д … | BuyAtStopCancel, BuyAtStopMarketIceberg, SellAtStopCancel, SellAtStopMarketIceberg | — | CloseAtStopMarketIceberg | PriceChannel, Momentum, Envelop, Candle | торговый |

## SyntheticBond (5)

*синтетические облигации*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| SyntheticBondsArbitrage | Арбитраж синтетических облигаций. Контанго-арбитраж на рынке фьючерсов на акции MOEX Конструкция позиции (синтетическая облигация в контанго) Лонг акция (база) + Шорт фьючерс Объёмы в лотах: база = контракты фьючерса × мульт / лот акции (в … | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle | торговый |
| SyntheticBondsCurveArbitrage | Арбитраж синтетических облигаций. Контанго-арбитраж на рынке фьючерсов на акции MOEX Конструкция позиции (синтетическая облигация в контанго) Лонг акция (база) + Шорт фьючерс Объёмы в лотах: база = контракты фьючерса × мульт / лот акции (в … | BuyAtMarket, SellAtMarket | — | CloseAtMarket | Candle | торговый |
| SyntheticBondsCurveMonitor | Монитор синтетических облигаций по кривой фьючерсов на акции MOEX Таблица: три ближайшие серии фьючерсов по каждой облигации с доходностью в % годовых. Серии - кнопки, открывают чарт соответствующего фьючерса. Мульт редактируется в таблице. … | BuyAtMarket, SellAtMarket | — | CloseAtMarket, ClosePositionsOnTab | Candle | торговый |
| SyntheticBondsCurveMonitorOpenUi | — | — | — | — | — | контур не найден |
| SyntheticBondsScalper | Скальпер синтетических облигаций. Контанго-арбитраж на рынке фьючерсов на акции MOEX. Работает круглосуточно (день/ночь/выходные), вход только лимитными ордерами по стакану. Конструкция позиции (синтетическая облигация в контанго) Лонг акци … | BuyAtLimit, BuyAtMarket, SellAtLimit | BuyAtLimitToPosition, BuyAtMarketToPosition | CloseAtLimit, CloseAtMarket | Candle | торговый |

## TechSamples (16)

*технические примеры для разработчиков*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| AllSourcesInOneSample | Description Tech sample for OsEngine. Example bot that initializes all available source types in OsEngine: Simple, Index, Pair, Screener, Polygon, Cluster, and News. | — | — | — | News | контур не найден |
| BlockIndicatorsOnScreenerSample | Description Tech sample for OsEngine: Demonstrates switching Bollinger, SMA, and ATR indicators on/off in a Screener tab. | — | — | — | Sma, Bollinger, Atr, Candle | контур не найден |
| BlockIndicatorsSample | Description Tech sample for OsEngine: Example showing blocking of indicators for calculating. Useful when optimizing robots that do not need all created indicators. | — | — | — | Sma, Bollinger, Atr | контур не найден |
| CandlesLoggingSample | Description TechSample robot for OsEngine An example of a robot for programmers, where you can see how logging works. | — | — | — | Candle | контур не найден |
| ChangePriceBotExtStopMarket | Description TechSample robot for OsEngine An example of a robot for programmers, where you can see how changing the order price works. | BuyAtLimit, SellAtLimit | — | CloseAtProfitMarket, CloseAtStopMarket, ClosePositionAtStopAndProfit | — | торговый |
| CustomChartInParamWindowSample | Description Sample “Chart in the parameters window” for osengine. It shows: • Dynamic graph: The graph updates in real time as new data becomes available. • User interaction: The user can change the scale of the graph and get values ​​at sp … | — | — | — | Candle | контур не найден |
| CustomDataInIndicatorSample | Description TechSample robot for OsEngine An example of drawing a series of indicator data calculated in the robot. | — | — | — | Candle | контур не найден |
| CustomParamsUseBotSample | Description TechSample robot for OsEngine This is an example of working with custom settings for the design of the Options window. | BuyAtMarket | — | CloseAtProfit, CloseAtStop | Sma, Atr, Candle | торговый |
| CustomTableInParamWindowSample | Description Sample “Custom Table In The Param Window” for osengine. It shows: • Dynamic table: The table is updated in real time as new data arrives. • User Interaction: The user can change data in the table and get values ​​in specific cel … | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStop | Candle | торговый |
| ElementsOnChartSampleBot | Description TechSample robot for OsEngine An example of a robot going short after a false upside breakout. | — | — | — | Macd, Candle | контур не найден |
| FakeOutExample | Description TechSample robot for OsEngine An example of a robot going short after a false upside breakout. | SellAtMarket | — | — | PriceChannel, Candle | вход без выхода |
| OpenInterestBotSample | Description An example of a robot requesting open interest in its logic. Enter Long when OI falls to the specified value. Exit by stop and profit orders. | BuyAtMarket | — | CloseAtProfitMarket, CloseAtStopMarket | Candle | торговый |
| ServerStopOrdersSample | Description Технический пример робота для OsEngine Пример робота, торгующего серверными стоп-лимит ордерами. Только лонг. Вход - серверный стоп-лимит на покупку. Триггер - по лучшему аску плюс Trigger offset percent, лимитная цена дочерней … | BuyAtStopCancel, BuyAtStopOnServer | — | CloseAtStopOnServer | Candle | торговый |
| StopByTradeFeedSample | Description TechSample robot for OsEngine Buy: price is above the upper line of the PriceChannel. Sell: price is below the lower line of the PriceChannel. Exit: by trailing stop. An example of a robot that pulls up the stop for a position b … | BuyAtLimit, SellAtLimit | — | CloseAtTrailingStop | PriceChannel, Candle | торговый |
| TradeLineExample | Description TechSample robot for osengine. Example of trading on sloping levels. | BuyAtMarket | — | CloseAtMarket, CloseAtProfitMarket, CloseAtStopMarket | Candle | торговый |
| VisualSettingsParametersExample | Description TechSample robot for OsEngine This is an example of working with settings for visual design of Parameters window | — | — | — | — | контур не найден |

## Trend (12)

*трендовые: вход на пробой/продолжение движения, выход по трейлингу/развороту*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| BreakLinearRegressionChannel | Description Trading robot for osengine. Trend strategy on Break LinearRegression Channel. Buy: If the closing price of the last candle is above the upper line of the PriceChannel. Sell: If the closing price of the last candle is below the l … | BuyAtMarket, BuyAtStopCancel, SellAtMarket, SellAtStopCancel | — | CloseAtStop | Sma, PriceChannel, Candle | торговый |
| EnvelopTrend | Description Trading robot for osengine. The trend robot on BreakEnvelops. Buy: The price is above the upper Envelops band. Sell: The price is below the lower Envelops band. Exit: Reverse side of the channel. | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop | Envelop, Candle | торговый |
| MomentumMacd | Description Trading robot for osengine. Trend strategy based on 2 indicators Momentum and Macd. Buy: If lastMacdUp > lastMacdDown and lastMom > 100 - close position and open Long. Sell: If lastMacdUp < lastMacdDown and lastMom < 100 - close … | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Macd, Momentum, Candle | торговый |
| ParabolicBollinger | Description Trading robot for osengine. Trend strategy on Parabolic and Bollinger. Buy: if the closing price is below the Bollinger Upper level. Sell: if the closing price is above the Bollinger Lower level. Exits are managed through traili … | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop, ClosePosition | Sma, Bollinger, Parabolic, Candle | торговый |
| ParabolicPriceChannel | Description Trading robot for osengine. Trend strategy on Parabolic PriceChannel. Buy: if the current closing price is below the upper level of the PriceChannel. Sell: if the current closing price is above the lower level of the PriceChanne … | BuyAtStop, BuyAtStopCancel, SellAtStop, SellAtStopCancel | — | CloseAtTrailingStop, ClosePosition | Sma, PriceChannel, Parabolic, Candle | торговый |
| ParabolicSarTrade | Description Trading robot for osengine. Trend strategy at the intersection of the ParabolicSar indicator. Buy: If Price > lastSar - close position and open Long. Sell: If Price < lastSar - close position and open Short. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Parabolic, Candle | торговый |
| PriceChannelTrade | Description Trading robot for osengine. Trend strategy on interseption PriceChannel indicator. Buy: If PriceHigh > _lastPriceChUp - close position and open Long. Sell: If PriceLow < _lastPriceChDown - close position and open Short. | BuyAtLimit, SellAtLimit | — | CloseAtLimit | PriceChannel, Candle | торговый |
| PriceChannelTradeUi | — | — | — | — | PriceChannel | контур не найден |
| SmaStochastic | Description Trading robot for osengine. Trend strategy based on 2 indicators Sma and Stohastic. Buy: If lastClose > lastSma + Step and secondLastStoh <= Downline and firstLastStoh >= Downline - Enter Long. Sell: If lastClose < lastSma - Ste … | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Sma, Stochastic, Candle | торговый |
| SmaStochasticUi | — | — | — | — | Sma | контур не найден |
| StrategyBillWilliams | Description Trading robot for osengine. Trend Strategy Bill Williams. Buy: 1. The current price must be above all three lines of the Alligator indicator. 2. Additionally, the price must be above the upward fractal level (_lastFractalUp). Wh … | BuyAtLimit, SellAtLimit | — | CloseAtLimit | Alligator, Fractal, Williams, Candle | торговый |
| TwoTimeFramesBot | Discription Trading robot for osengine Trend robot on the Two Time Frames Bot. Buy: 1.The current price must be above the PriceChannel Up level. 2. Additionally, the current price on the higher timeframe must be above the moving average. Ex … | BuyAtMarket | — | CloseAtMarket | Sma, PriceChannel, Candle | торговый |

## VolatilityStageRotationSamples (4)

*ротация по стадиям волатильности*

| Робот | Что делает | Вход | Докупки | Выход | Индикаторы | Статус |
|---|---|---|---|---|---|---|
| BollingerTrendVolatilityStagesFilter | Description trading robot for osengine The trend robot on Bollinger Trend VolatilityStages Filter. Buy: 1. The price has broken above the upper line of the main Bollinger band. 2. If the volatility filter is enabled, the current volatility … | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | Bollinger, Candle | торговый |
| PriceChannelScreenerOnIndexVolatility | Description trading robot for osengine The trend robot on PriceChannel Screener On Index Volatility. Buy: 1. The price breaks above the upper band of the Price Channel. 2. (If ATR filter is enabled) The ATR has grown by at least X% over the … | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | PriceChannel, Atr, Candle | торговый |
| PriceChannelTrendAtrFilter | Description trading robot for osengine The trend robot on PriceChannel and AtrFilter. Buy: 1.The price has broken above the upper line of the Price Channel 2.ATR has grown compared to AtrGrowLookBack candles ago. Sell: 1.The price has broke … | BuyAtMarket, SellAtMarket | — | CloseAtTrailingStopMarket | PriceChannel, Atr, Candle | торговый |
| ZigZagChannelScreenerRsiFilter | Description trading robot for osengine The trend robot on ZigZagChannel Screener RsiFilter. Buy: 1. The total number of open positions is below the maximum allowed. 2. The last candle’s close is above the upper ZigZag line. 3. The SMA is ri … | BuyAtMarket | — | CloseAtMarket, CloseAtTrailingStop | Sma, Rsi, Candle | торговый |

