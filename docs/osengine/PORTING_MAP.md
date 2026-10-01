# PORTING_MAP — полный порт OsEngine → Deeptrading

Дата: 2026-09-28. Источник скана: `~/OsEngine/project/OsEngine/Robots` (без
`Engines/` и `Test*`) — 201 файл. Скрипт: `docs/osengine/scan_robots.py`,
полный реестр: `docs/osengine/robots_registry.tsv` (family, class, entry,
exit, trail, индикаторы, special, путь).

## Итоги скана

| Категория | Шт | Суть |
|---|---|---|
| портировано | 15 | OnScriptIndicators 15/15 — Wave A (6): bollinger, envelop_trend, price_channel, rsi_contrtrend, rsi_trade, sma_stoch; Wave B (+9): cci_trade, bb_power, rvi_trade, macd_revers, macd_trail, bollinger_revers, bollinger_trailing, sma_trend, pc_volatility |
| direct | 58 | один BotTabSimple, вход+выход обычными заявками — порт 1:1 |
| SPECIAL | 40 | скринеры/мульти-таб/синтетические инструменты/арбитраж — нужна отдельная инфраструктура |
| no-entry | 57 | без вызовов Buy/Sell: гриды (позиции напрямую), мониторы-хелперы, UI-фабрики частично |
| skip_family | 29 | BotsFromStartLessons, TechSamples, AutoTestBots — учебные/тестовые, не портим |
| skip_ui | 12 | `*Ui.xaml.cs` — UI-обёртки над уже учтёнными роботами |

## Волны портов OnScriptIndicators (15/15 — done)

- **Wave A (29.09)** — 6 роботов: bollinger, envelop_trend, price_channel, rsi_contrtrend, rsi_trade, sma_stoch. Свип 72/72 Mac↔Win8 бит-в-бит.
- **Wave B (30.09)** — +9 роботов: cci_trade, bb_power, rvi_trade, macd_revers, macd_trail, bollinger_revers, bollinger_trailing, sma_trend, pc_volatility (оригинальные дефолты из C#, реверс-лимитники, трейлинг, ATR-логика PCV). Smoke 9 роботов × 2 seed → FAILS 0.
  Свип: `backend/reports/bt_ose_sweep_mac_waveb.json` — 153 runs (15 роботов × 4 конфига × 3 seedа), 57.1s, EXIT=0, code_sha256=`1aaeafc70a0c0589`.
  Лучшие на синтетике: macd_trail trail=1.0 (pnl 165–183, pf 38–108), bollinger_revers/trailing (pf=inf, pnl 100–139), sma_trend (pf 118–242), pc_volatility (pnl ~102–107, dd ≤7.3).
  CCI-контртренд на синус-серии в минус (свойство стратегии, не баг порта). Для bb_power в сетку добавлены step 0.5/1/2 — дефолт Step=100 на синтетике ~100 инертен (честный дефолт оригинала).
  Индикаторы: `+bulls_power/bears_power` (High/Low − SMA(Close), семантика BullsPower.cs/BearsPower.cs).
- Хвост: бандл на Win8 для межмашинной сверки — code_sha256 уже в отчёте, сверка запуском одной команды.

Трейлинг (`CloseAtTrailingStop*`) используют 29 роботов — механика уже
реализована в TesterTab (трейлинг на стоп-слоте), см. PORT_NOTES_EXITS.md.

## Маппинг API заявок (OsEngine → app/engine/ose/robots.py)

Вход:
- `BuyAtMarket/SellAtMarket` → `buy_at_market/sell_at_market` — ЕСТЬ
- `BuyAtLimit/SellAtLimit` → `buy_at_limit/sell_at_limit` — ЕСТЬ
- `BuyAtStop/SellAtStop(+Cancel)` → `buy_at_stop/sell_at_stop` + `cancel_pending_stops` — ЕСТЬ
- `BuyAtStopMarket/SellAtStopMarket` → стоп-маркет слот — ЕСТЬ
- `BuyAtStopOnServer` → серверный стоп = тот же слот без отличий в бар-реплее — ЕСТЬ
- `BuyAtIceberg*` → айсберг-заявки — НЕТ, волна F
- `*ToPosition`, `*Unsafe` (усреднение/небезопасные лимиты) — НЕТ, волна E

Выход (слоты на позиции, OCO, TryReload-семантика — Wave A готова):
- `CloseAtProfit/CloseAtStop` (лимитная пара) → `close_at_profit/close_at_stop` — ЕСТЬ
- `CloseAtProfitMarket/CloseAtStopMarket` → `close_at_profit_market/close_at_stop_market` — ЕСТЬ
- `CloseAtTrailingStop(-Market)` → `close_at_trailing_stop(_market)` — ЕСТЬ
- `CloseAtFake` → `close_at_fake` — ЕСТЬ
- `CloseAtIceberg*` — волна F
- `CloseAllAtMarket/CloseAllPositions/CloseAllOrderToPosition` → `close_all_*` хелперы — дозакрыть по волне
- `CancelStopOrders` → `cancel_pending_stops` — ЕСТЬ

## Маппинг на движок Deeptrading (проверено по коду, 2026-09-28)

Проверено чтением `app/engine/{runner,exits,policies,quorum,models}.py`.
Принцип: **выходы портируются отдельно от входных роботов** — политикой
(SignalPolicy/ExitPolicy), а не кодом внутри каждого робота.

| OsEngine | Deeptrading | Проверенная семантика |
|---|---|---|
| Вход `Buy/SellAtMarket/Limit/Stop` | `Strategy.on_bar → Signal(kind="entry")` | голос на баре i, исполнение по open бара i+1 (`_open`, fill через `CostModel`) |
| Close on reverse | `Signal(kind="exit")` + `SignalPolicy` | `_poll` принимает exit только ПРОТИВ позиции; опции `opposite_hold` / `exit_confirm_window_bars` / `confirm_flip` → `exit_candidate`; исполнение по open следующего бара (pending exit/flip). В робота НЕ копируем |
| Слоты StopLoss/StopProfit | `ExitPolicy.plan_entry → initial_stop/target` | исполнение в runner: `intrabar_exit` по касанию хвостом (low/high), гэп открытия → цена `bar.open`; `SAME_BAR_CONFLICT_RULE="STOP_LOSS_FIRST"` — совпадает с CheckStop > CheckProfit в BotTabSimple |
| `CloseAtTrailingStop(-Market)` | `ExitPolicy.update_stop` + `trailing_activated` | после активации трейлинг срабатывает по close/гэпу (close_based=True, ложный хвост прощается); signal/tp-выходы отключены (`HOLD_TRAILING`) |
| `TryReloadStop/Profit` | перезарядка слота в TesterTab / `update_stop` каждый бар | Wave A |
| Усреднение `*ToPosition`, айсберг | в EngineRunner НЕТ (qty фиксирован) | волны E/F: TesterTab-путь или расширение движка |
| Комиссия/слиппедж | `CostModel.fill_price/commission` | |
| Торговые сессии | `SessionPolicy` (`force_flat_at_session_end` → `SESSION_CLOSE`) | |
| Параметры/метаданные | Params dataclass + `StrategyCard` (catalog.py) | |
| Ансамбль роботов | `quorum.merge_quorum` + `OseAllStrategy` | голос робота = смена его позиции на закрытии бара |

Два пути исполнения:
1. **Основной движок** — `Strategy.on_bar → Signal + ExitPolicy/SignalPolicy`:
   для роботов, чьи выходы = стоп/тейк/трейлинг/обратный сигнал. Всё, что
   OsEngine-робот делал через слоты позиции, выражается политикой.
2. **ose-адаптер** (`app/engine/ose/`) — TesterTab со слотами `close_at_*`:
   для роботов со сложной выходной механикой (усреднение, перезарядка пар,
   отмены). Роботы отдают голоса, агрегация — quorum.

Статусы записи в реестре портирования: MAPPED / REUSE_EXISTING /
NEEDS_IMPLEMENTATION / NEEDS_SPECIAL_ENGINE / UNSUPPORTED / VALIDATED.
Перед новым Strategy — сначала поиск эквивалента в STRATEGY_REGISTRY
(rsi_reversal, bollinger_reclaim, macd_cross, donchian_breakout и др.).

## Волны порта

### Волна A — инфраструктура выходов — DONE
Слоты стоп/тейк на позиции (OCO, перезарядка как TryReloadStop/TryReloadProfit),
трейлинг на стоп-слоте, стопы/трейлинг между барами по касанию диапазоном.
Документ: PORT_NOTES_EXITS.md. Тесты: test_ose_robots.py (49+ зелёных).

### Волна B — OnScriptIndicators, прямые (13) — инвентаризация 2026-09-28
Портим (10): RsiTrade (rsi, market-вход, close-on-reverse), RviTrade (rvi),
CciTrade (cci), BbPowerTrade (bollinger+power), MacdRevers (macd),
BollingerRevers (bollinger есть), MacdTrail (macd+трейлинг),
BollingerTrailing (трейлинг), SmaTrendSample (sma+envelops, лимит-входы
со slippage×PriceStep, базовый стоп % от входа + трейлинг-перезарядка),
PriceChannelVolatility (pricechannel+atr, стоп-входы Buy/SellAtStop с
Cancel при каждой перестановке, трейлинг по линии канала ± ATR·k).
Индикаторы допортить в ose/indicators.py: atr, macd, cci, rvi,
bulls/bears power (rsi, sma, bollinger, envelops, price_channel,
stochastic уже есть).
Отложить: PairRsiTrade (два таба/два инструмента → волна H),
FundBalanceDivergenceBot (индикатор FBD = внешние данные квартальных
балансов → UNSUPPORTED до появления источника), TimeOfDayBot (вход по
времени суток на тиках — при порте адаптировать как hour-фильтр бара;
в оригинале баг: stopPrice/profitPrice считаются от 0 — НЕ переносить,
использовать activation-цены).

### Волна C — Trend (8) + CounterTrend (2) — 6/10 готово (hub-канон)
- [x] MomentumMacd → `momentum_macd_hub` (strategies.py): MACD>signal И Momentum>100 — крест state-условия; зеркально SELL.
- [x] ParabolicSarTrade → `parabolic_sar_hub` (strategies.py): флип SAR-тренда (Wilder, Af 0.02 / MaxAf 0.2).
- [x] WilliamsRangeTrade → `williams_range_hub` (strategies.py): %R(14) крест downline -80 → BUY, крест upline -20 → SELL.
- [x] PriceChannelTrade → `price_channel_hub` (strategies.py): пробой канала L=21 со сдвигом [-2] (уровень предыдущего бара), крест пробоя; бар, пробивший обе стороны, входа не даёт.
- [x] ParabolicBollinger → `parabolic_bollinger_hub` (strategies.py): крест касания BB-границы (std: делитель L-1 при L>30, иначе L — квирк C#) при параболике P строго внутри полос.
- [x] ParabolicPriceChannel → `parabolic_price_channel_hub` (strategies.py): крест касания границы канала при P строго внутри; пробой по >=/<=, как в C#-индикаторе.
Индикаторы IndicatorHub: williams_r, momentum, parabolic_sar; параболическая линия P — общий инкрементальный хелпер `_hub_parabolic` в strategies.py (квирк C#: volMult применяется в среднем второй раз). Выходы трейлингом по P из C# отдаются рантайму (SignalPolicy/exit-policy раннера) — конвенция hub-канона. Каталожные карточки wave=6.
- [ ] BreakLinearRegressionChannel, StrategyBillWilliams, TwoTimeFramesBot,
ClusterCountertrend — TODO.
Нужно: regression-канал, fractal (BillWilliams), второй таймфрейм/два таба (→ волна H); price_channel/bollinger/parabolic — есть.

### Волна D — Patterns (7) + Monitors (4) — TODO
CandlePatternBoost, CustomCandlesImpulseTrader, PinBarTrade, PivotPointsRobot,
ThreeSoldier, ThreeSoldierVolatilityAdaptive, VolatilityAdaptiveCandlesTrader;
MonitorHighLow, MonitorImpulse, MonitorRsi, MonitorVolume.
Нужно: pivot, highest/lowest (есть), atr-адаптивность.

### Волна E — PositionsMicromanagement (8) — TODO
AlligatorTrendAverage, CandlesTurnaroundPattern, CustomIcebergSample,
EnvelopsCountertrend, PriceChannelCounterTrend, TwoEntrySample,
UnsafeAveragePosition, UnsafeLimitsClosingSample.
Нужно: `*ToPosition` (докупка в позицию), `BuyAtLimitToPositionUnsafe`,
`CloseAtLimitUnsafe`, `BuyAtStopMarket` на входе.

### Волна F — айсберг + фьючерсные прямые (10) — TODO
AlgoStart1LinearRegression, AlgoStart2Soldiers, AlgoStart3PriceChannel,
AlgoStart4Railway, FuturesStart1Bollinger, FuturesStart2Keltner,
FuturesTrendBollinger, FuturesTrendPriceChannel + High Frequency: Fisher,
HighFrequencyTrader.
Нужно: айсберг-заявки в TesterTab (`BuyAtIcebergMarket`, `CloseAtIcebergMarket`),
индикаторы adx, pricechanneladaptive, zigzag, fisher, keltner.

### Волна G — остатки прямых (5) — TODO
SpeculantSetAtrKeltner, SpeculantSetMomentumAdaPc,
BollingerTrendVolatilityStagesFilter, PriceChannelTrendAtrFilter, DcaTimeBot,
TakeFunding.
Нужно: atrgrowpercent (рост ATR в %).

### Волна H — SPECIAL, мульти-таб (40) — TODO, после прямой части
Screeners (9), SyntheticBond (4), MarketMaker/PairTrader (4), IndexArbitrage (4),
Dividends (3), Rebalancers (3), Sectors (2), FuturesScreeners (2),
VolatilityStageRotation SPECIAL (2), NewsBots (2), OneLegArbitrage,
OptionsSpread, MarketDepthScreener, LiquidityAnalyzer, ArbitrageFunding.
Зависимость: скринер-инфраструктура (набор инструментов на один робот,
распределение сигналов), синтетические инструменты (SyntheticBond),
кластерные данные (ClusterCountertrend уже в C — проверить источник данных).
NewsBots — внешние данные (AI/Telegram): пометить N/A до появления
соответствующих источников.

### Вне плана
Grids (8) — отдельное решение: позиционная сетка без entry-заявок;
портить только если появится сценарий под сетки. Lessons/TechSamples/AutoTest —
не портим.

## Интеграция в Deeptrading

- Каждый порт = робот со своим TesterTab (изолированный бар-реплей), голос —
  смена позиции на закрытии бара; семейство собирается в `ose_*`-стратегию с
  quorum (см. app/engine/ose/strategy.py, OseAllStrategy).
- Новые роботы регистрируются в `_ROBOTS`/`_ROBOT_ORDER` (robots.py),
  параметрах (ose/strategy.py), каталоге (catalog.py) и реестрах strategies.py.
- Регрессия после каждой волны: pytest ose-тестов на Mac + на .8
  (nadts@192.168.1.8), затем синк файлов на .2.
- Реестр скана перегенерируется: `python3 docs/osengine/scan_robots.py`.


Да. Тут нужен не просто список этапов, а **инструкция для нейронки в формате “делай строго по шагам, вот TF, вот что фиксировать, вот когда переходить дальше”**.

Ниже даю именно такой протокол для нашего Robot Lab: без привязки к режимной теории, с постепенным тестированием роботов из реестра.

# Пошаговый протокол тестирования торговых роботов

## 0. Главный принцип

Не пытаться сразу строить сложный ансамбль.

Каждый робот сначала рассматривается как набор:

`ENTRY → FILTERS → POSITION MANAGEMENT → EXIT`

Цель первого этапа — понять:

1. есть ли у идеи собственный сигнал;
2. на каком таймфрейме он работает;
3. насколько результат зависит от выхода;
4. насколько результат зависит от фильтров;
5. не является ли результат случайностью;
6. не дублирует ли робот уже найденные стратегии.

Нельзя менять одновременно Entry + Exit + TF + фильтры + параметры.

**Один эксперимент = одно изменение относительно контрольной версии.**

---

# 1. Зафиксировать тестовый стенд

Перед тестированием любого робота создать один неизменный `BASELINE`.

Зафиксировать:

* список инструментов;
* период;
* торговые часы;
* session rules;
* long/short;
* размер позиции;
* максимальное число позиций;
* максимальную экспозицию;
* комиссию;
* slippage;
* lot size;
* модель исполнения;
* EOD;
* overnight;
* правила открытия;
* правила закрытия.

Все роботы в рамках одного этапа должны тестироваться на одинаковом стенде.

Если изменился execution model, комиссия или universe — это уже новый экспериментальный стенд.

---

# 2. Использовать стандартный набор таймфреймов

Не тестировать произвольные TF для каждого робота.

Использовать фиксированную матрицу:

### Основные TF

* `1m`
* `5m`
* `10m`
* `15m`
* `30m`
* `1h`

### Расширенные TF

Использовать только если робот логически этого требует:

* `2h`
* `4h`
* `1D`

Для первой массовой проверки достаточно:

`1m / 5m / 10m / 15m / 30m / 1h`

---

# 3. Что означает TF

Очень важно не смешивать три разных понятия.

### Signal TF

На этом TF формируется торговый сигнал.

Например:

`MACD cross на 15m`

### Filter TF

На этом TF находится дополнительное подтверждение.

Например:

`15m MACD BUY + 1h SMA trend UP`

### Execution TF

На этом TF определяется фактическая точка входа.

Например:

`15m setup + 1m entry trigger`

---

# 4. Первая серия: чистый Entry

Каждый новый робот сначала тестируется **без сложных фильтров**.

Например, для `RSI reversal`:

```text
Entry:
RSI(14) < 30 → BUY

Exit:
standard exit

Filters:
NONE
```

Не добавлять:

* H1 bias;
* regime;
* order book;
* volume filter;
* news;
* AI;
* market filter;
* correlation;
* cluster;
* queue;
* adaptive thresholds.

Иначе мы не узнаем, есть ли у самого Entry какая-либо ценность.

---

# 5. Для каждого Entry обязательно сделать TF sweep

Для каждого нового Entry прогнать:

```text
ENTRY TF = 1m
ENTRY TF = 5m
ENTRY TF = 10m
ENTRY TF = 15m
ENTRY TF = 30m
ENTRY TF = 1h
```

То есть:

```text
RSI reversal 1m
RSI reversal 5m
RSI reversal 10m
RSI reversal 15m
RSI reversal 30m
RSI reversal 1h
```

То же самое для:

* breakout;
* pullback;
* MACD;
* Bollinger;
* SMA;
* price channel;
* candle patterns;
* momentum;
* VWAP;
* volume;
* etc.

---

# 6. Но не все Entry подходят всем TF

Перед запуском разрешается задать `TF compatibility`.

Пример:

### Scalping

```text
1m
5m
10m
```

### Intraday

```text
5m
10m
15m
30m
```

### Trend

```text
15m
30m
1h
```

### Position

```text
30m
1h
2h
4h
1D
```

Если робот логически не подходит TF, ставить:

`NOT_APPLICABLE`

а не искусственно тестировать его.

---

# 7. Первый стандартный Exit

Чтобы сравнивать Entry между собой, всем первым тестам назначается одинаковый выход.

Например:

```text
SL = фиксированный стандарт
TP = фиксированный стандарт
```

или стандартный ATR exit:

```text
SL = X × ATR
TP = Y × ATR
```

Главное — один и тот же Exit для всех Entry.

Это позволяет ответить:

> какой Entry даёт лучший поток сделок при одинаковой механике выхода?

---

# 8. Серия E1 — первичный скрининг

Для каждого робота:

```text
Robot
 ×
разрешённые Entry TF
 ×
standard Exit
 ×
no filters
```

Например:

```text
MACD
1m
5m
10m
15m
30m
1h
```

Результат:

```text
MACD_1m
MACD_5m
MACD_10m
MACD_15m
MACD_30m
MACD_1h
```

---

# 9. Минимальные метрики E1

Для каждого варианта сохранить:

```text
trades
wins
losses
win_rate

gross_profit
gross_loss
net_profit

profit_factor
expectancy

avg_trade
median_trade
avg_win
avg_loss

max_drawdown
sharpe
sortino

commission
slippage

avg_hold_bars
median_hold_bars
max_hold_bars

max_positions
turnover
```

Дополнительно:

```text
PnL/day
PnL/instrument
trades/day
```

---

# 10. Не выбирать победителя только по Net PnL

Первичный отбор:

### REJECT

Если:

* слишком мало сделок;
* отрицательный expectancy;
* PF < 1;
* результат полностью уничтожается комиссиями;
* огромный DD относительно прибыли;
* очевидная ошибка исполнения;
* сигнал фактически не работает.

### CANDIDATE

Если:

* есть достаточное число сделок;
* expectancy положительный;
* PF > 1;
* результат не зависит от одной сделки;
* есть приемлемая стабильность по дням;
* результат сохраняется после costs.

---

# 11. Минимальное количество сделок

Не использовать один универсальный магический порог для всех роботов.

Использовать уровни:

```text
< 20 trades
→ INSUFFICIENT_SAMPLE

20–49
→ PRELIMINARY

50–99
→ USABLE

100+
→ STRONG_SAMPLE
```

Это не критерий качества.

Это только показатель количества наблюдений.

---

# 12. После Entry screening — тестировать Exit

После первого screening выбрать кандидатов Entry.

Теперь Entry фиксируется.

Например:

```text
MACD 15m
```

И уже меняем только Exit.

---

# 13. Серия E2 — Exit sweep

Для каждого выбранного Entry протестировать:

### X01

Fixed SL / Fixed TP

### X02

ATR SL / ATR TP

### X03

ATR SL / trailing

### X04

Swing SL / fixed TP

### X05

Swing SL / trailing

### X06

Signal reversal exit

### X07

Time stop

### X08

Break-even

### X09

Partial TP

### X10

Trailing after profit threshold

### X11

Hybrid

Например:

```text
MACD 15m
+
ATR SL
+
2R TP
```

затем:

```text
MACD 15m
+
ATR SL
+
trailing
```

и т.д.

**Меняется только Exit.**

---

# 14. Отдельно проверить exit по разным TF

Если Entry:

```text
MACD 15m
```

не надо автоматически делать Exit тоже 15m.

Проверить:

```text
Entry 15m
Exit 15m

Entry 15m
Exit 5m

Entry 15m
Exit 1m
```

Для трендовых стратегий дополнительно:

```text
Entry 15m
Exit 30m
Exit 1h
```

Но только после того, как чистая версия уже показала результат.

---

# 15. Серия E3 — Entry TF × Exit TF

Для сильных кандидатов построить матрицу:

```text
              EXIT
          1m  5m  15m 30m 1h
ENTRY
1m
5m
10m
15m
30m
1h
```

Не делать такую матрицу для всех 217 роботов.

Только для кандидатов после E1/E2.

---

# 16. Серия E4 — фильтры

После фиксации:

```text
ENTRY + EXIT
```

начинается тестирование фильтров.

**Один фильтр за один эксперимент.**

Порядок:

```text
BASE
↓
+ volume
↓
+ volatility
↓
+ trend
↓
+ higher-TF confirmation
↓
+ liquidity
↓
+ market filter
↓
+ order book
```

Не делать сразу:

```text
Entry
+ H1
+ volume
+ volatility
+ IMOEX
+ orderbook
+ news
+ AI
```

Иначе невозможно понять, что именно изменило результат.

---

# 17. Higher-Timeframe filter

После чистого Entry протестировать MTF.

Сначала:

```text
Entry = 5m
Filter = 15m
```

затем:

```text
Entry = 5m
Filter = 30m
```

затем:

```text
Entry = 5m
Filter = 1h
```

Для более быстрых Entry:

```text
Entry 1m
Filter 5m
Filter 15m
Filter 30m
```

Для более медленных:

```text
Entry 15m
Filter 30m
Filter 1h
```

---

# 18. MTF тестировать двумя способами

### A. Direction filter

Например:

```text
1h SMA trend UP
→ разрешены только LONG
```

### B. Confirmation filter

Например:

```text
5m breakout
+
30m momentum > threshold
```

Это разные эксперименты.

Не объединять их сразу.

---

# 19. Затем тестировать confirmation

Для каждого кандидата:

```text
Entry
+
confirmation
```

Например:

```text
Bollinger breakout 5m
+
volume > MA(volume)
```

или:

```text
MACD 15m
+
RSI 15m
```

или:

```text
breakout 5m
+
trend 30m
```

---

# 20. Затем тестировать execution TF

Только после того, как найден хороший setup.

Пример:

```text
SETUP:
breakout 15m

EXECUTION:
1m
```

Тогда 15m определяет:

> есть ли возможность войти?

А 1m определяет:

> когда именно войти?

Проверить:

```text
15m setup + 1m trigger
15m setup + 5m trigger
15m setup + immediate entry
```

---

# 21. Не использовать 30 голосов сразу

Пока не делать:

```text
3 TF × 10 indicators × BUY/SELL
```

Сначала каждый элемент должен быть проверен самостоятельно.

Правильный порядок:

```text
Robot A
↓
лучший TF
↓
лучший Exit
↓
лучший Filter
↓
лучший Execution
↓
только потом Ensemble
```

---

# 22. Серия E5 — parameter sweep

Параметры оптимизировать только после того, как структура стратегии доказала жизнеспособность.

Например RSI:

```text
period:
10
12
14
16
18

oversold:
25
30
35

overbought:
65
70
75
80
```

Но не запускать огромную комбинацию сразу.

Сначала:

```text
period
```

потом:

```text
oversold / overbought
```

потом:

```text
SL/TP
```

---

# 23. Оптимизация должна иметь TRAIN / VALIDATION / TEST

Например:

```text
TRAIN
→ подбор параметров

VALIDATION
→ проверка

TEST
→ финальная проверка
```

TEST никогда не использовать для выбора параметров.

После первого прохода TEST считается закрытым.

---

# 24. Для каждого кандидата делать OOS

Минимальная схема:

```text
TRAIN
████████████████

VALIDATION
        ████████

TEST
                ████████
```

Если стратегия работает только TRAIN:

`REJECT / OVERFIT`

Если работает TRAIN + VALIDATION:

`CANDIDATE`

Если сохраняется TEST:

`VALIDATED`

---

# 25. Затем walk-forward

Для кандидатов:

```text
Train 1 → Test 1
Train 2 → Test 2
Train 3 → Test 3
Train 4 → Test 4
```

Смотреть не на одну красивую кривую, а на повторяемость.

---

# 26. Обязательно считать degradation

Для каждого кандидата:

```text
TRAIN PnL
VALIDATION PnL
TEST PnL
```

и:

```text
Validation degradation
Test degradation
```

Также:

```text
PF train
PF validation
PF test
```

```text
DD train
DD validation
DD test
```

---

# 27. Обязательно анализировать сделки

Не только агрегированные показатели.

Для каждого trade:

```text
strategy_id
robot
entry_model
exit_model

instrument
side

entry_time
entry_price

exit_time
exit_price

qty

gross_pnl
commission
slippage
net_pnl

exit_reason

MFE
MAE

bars_held
```

---

# 28. Анализировать распределение PnL

Для кандидата проверить:

```text
top 1 trade contribution
top 5 trades contribution
top 10 trades contribution
```

Если практически вся прибыль сделана несколькими сделками:

```text
FRAGILE
```

---

# 29. Анализировать по инструментам

Каждый кандидат:

```text
ALL
MCK
GAZP
SBER
LKOH
...
```

Если стратегия прибыльна только на одном инструменте:

не считать её универсальным роботом.

Записать:

```text
UNIVERSE_DEPENDENT
```

---

# 30. Анализировать по дням

Минимально:

```text
day
trades
net
PF
DD
```

И смотреть:

```text
сколько прибыльных дней
сколько убыточных
max losing streak
```

---

# 31. Анализировать по времени суток

Разбить:

```text
09:50–11:00
11:00–13:00
13:00–15:00
15:00–17:00
17:00–18:40
```

Но сами интервалы должны соответствовать фактической торговой сессии тестового рынка.

Цель:

понять, когда стратегия реально генерирует результат.

Не использовать этот анализ для подгонки без отдельного OOS-теста.

---

# 32. Проверить long / short отдельно

Для каждого кандидата:

```text
ALL
LONG
SHORT
```

Если:

```text
LONG +5000
SHORT -3000
```

это не значит, что робот плох.

Нужно сохранить обе характеристики.

Например:

```text
LONG_SUPPORTED
SHORT_UNSUPPORTED
```

и затем отдельно проверить, является ли отключение одной стороны устойчивым на OOS.

---

# 33. Проверить cost sensitivity

Для кандидата прогнать:

```text
commission = 1.0 ×
commission = 1.5 ×
commission = 2.0 ×
```

и:

```text
slippage = 0
slippage = base
slippage = 2 × base
```

Если стратегия исчезает при небольшом увеличении costs:

```text
COST_SENSITIVE
```

---

# 34. Проверить execution sensitivity

Минимально:

### Test A

next bar open

### Test B

next bar open + slippage

### Test C

conservative fill

### Test D

worst-case intrabar ambiguity

Если результат существует только при идеальном исполнении:

`REJECT / EXECUTION_FRAGILE`

---

# 35. После этого — robustness

Для кандидата менять небольшими диапазонами:

```text
period ±10–20%
threshold ±10–20%
SL ±10–20%
TP ±10–20%
```

Не оптимизировать.

Просто проверить соседние параметры.

Хорошая стратегия не должна разрушаться от небольшого изменения параметров.

---

# 36. Проверить parameter plateau

Например:

```text
RSI period

12  +1200
13  +1450
14  +1520
15  +1490
16  +1510
17  +1380
```

Это лучше, чем:

```text
12   +300
13   +500
14   +5200
15   +400
16   -100
```

Цель — искать устойчивую область, а не пик.

---

# 37. Проверить корреляцию стратегий

Когда найдено 10–20 кандидатов, построить:

```text
strategy × strategy
```

по:

* daily PnL;
* trade PnL;
* equity returns.

Нужно обнаружить:

```text
A ≈ B
```

Если два робота почти всегда дают одинаковые сделки:

не считать их двумя независимыми источниками alpha.

---

# 38. Классифицировать стратегию

Каждый прошедший кандидат получает тип:

```text
BREAKOUT
TREND
PULLBACK
MOMENTUM
MEAN_REVERSION
REVERSAL
VWAP
VOLUME
PATTERN
MULTI_FACTOR
```

и:

```text
FAST
INTRADAY
SLOW
POSITION
```

---

# 39. Создать Strategy Card

Для каждого кандидата сохранять:

```text
Strategy ID
Source robot
Entry model
Entry TF
Filter TF
Execution TF
Exit model
Exit TF

Parameters

Universe
Direction

Trades
Win rate
PF
Expectancy
Net
MaxDD
Sharpe
Sortino

Average hold
Median hold

Commission
Slippage

TRAIN
VALIDATION
TEST
WALK-FORWARD

Robustness
Cost sensitivity
Execution sensitivity

Correlation with existing strategies

Status
```

---

# 40. Статусы

Использовать только фиксированный набор:

```text
DISCOVERED
IMPLEMENTED
SANITY_CHECK
SCREENED
CANDIDATE
PARAM_TESTED
VALIDATED
FROZEN
FORWARD_TEST
```

Отрицательные:

```text
REJECTED
INVALID
UNSTABLE
OVERFIT
INSUFFICIENT_SAMPLE
COST_SENSITIVE
EXECUTION_FRAGILE
REDUNDANT
```

---

# 41. Правило перехода между этапами

### DISCOVERED → IMPLEMENTED

Робот реализован в стандартном интерфейсе.

### IMPLEMENTED → SANITY_CHECK

Есть минимум несколько ручных/автоматических проверок сигналов.

### SANITY_CHECK → SCREENED

Результат технически корректен.

### SCREENED → CANDIDATE

Есть достаточная выборка и положительная базовая статистика.

### CANDIDATE → PARAM_TESTED

Проведён ограниченный parameter sweep.

### PARAM_TESTED → VALIDATED

Результат сохранился OOS.

### VALIDATED → FROZEN

Прошёл robustness и execution/cost checks.

### FROZEN → FORWARD_TEST

Запущен на новых данных без изменения параметров.

---

# 42. Что тестировать первым

Из реестра сначала брать простые и хорошо декомпозируемые роботы.

Первая очередь:

```text
SMA cross
Price/SMA cross
MACD
RSI reversal
Bollinger reversal
Bollinger breakout
Price Channel breakout
Donchian breakout
VWAP reclaim
Pullback EMA
Momentum
Three Soldiers
Pin Bar
ATR breakout
Volume breakout
```

Потом:

```text
adaptive strategies
multi-factor
screeners
order-book
market-depth
ML
arbitrage
grid
special systems
```

AutoTestBots и технические сервисные роботы не использовать как обычные alpha-стратегии.

---

# 43. Для каждого робота использовать один и тот же порядок

## STEP A

Разобрать исходного робота:

```text
ENTRY
EXIT
FILTER
POSITION MANAGEMENT
```

## STEP B

Определить допустимые TF.

## STEP C

Запустить чистый Entry.

## STEP D

Прогнать TF sweep.

## STEP E

Выбрать кандидатов.

## STEP F

Зафиксировать Entry.

## STEP G

Прогнать Exit sweep.

## STEP H

Прогнать Entry TF × Exit TF.

## STEP I

Добавлять фильтры по одному.

## STEP J

Проверить MTF.

## STEP K

Проверить execution trigger.

## STEP L

Только теперь оптимизировать параметры.

## STEP M

TRAIN / VALIDATION / TEST.

## STEP N

Robustness.

## STEP O

Cost / slippage / execution sensitivity.

## STEP P

Walk-forward.

## STEP Q

Correlation / redundancy.

## STEP R

Freeze.

## STEP S

Forward test.

---

# 44. Формат одного эксперимента

Каждый запуск должен иметь уникальный ID.

Например:

```text
EXP-000127
```

Описание:

```text
Robot: MACD
Entry TF: 15m
Filter TF: none
Execution TF: none

Exit: ATR_SL_TP
SL: 2 ATR
TP: 3 ATR

Filters: none

Dataset:
2026-09-01 → 2026-09-11

Universe:
MCK

Commission:
...

Slippage:
...
```

---

# 45. Результат эксперимента

```text
EXP-000127

TRADES: 184
WIN RATE: 54.3%

NET: +1,842
GROSS: +2,310

PF: 1.31
EXPECTANCY: +10.01

AVG WIN: +48
AVG LOSS: -35

MAX DD: 620

COMMISSION: 312
SLIPPAGE: 156

AVG HOLD: 17 bars
MEDIAN HOLD: 11 bars

LONG:
  121 trades
  +1,950

SHORT:
  63 trades
  -108

STATUS:
CANDIDATE
```

---

# 46. Delta относительно baseline

Каждый эксперимент обязан иметь:

```text
Δ Net
Δ PF
Δ DD
Δ Expectancy
Δ Trades
Δ WR
Δ Commission
Δ Slippage
```

Например:

```text
BASE:
Net +1200
DD 700
PF 1.18

EXP:
Net +1842
DD 620
PF 1.31

DELTA:
Net +642
DD -80
PF +0.13
```

---

# 47. Что нейронке запрещено делать

Не разрешать:

```text
1. выбирать стратегию по одному Net PnL;

2. оптимизировать TEST;

3. менять одновременно много компонентов;

4. добавлять фильтры без отдельного эксперимента;

5. считать один очень прибыльный trade доказательством edge;

6. скрывать отсутствие данных;

7. сравнивать стратегии на разных execution settings;

8. менять комиссию между экспериментами;

9. менять universe между экспериментами без явной маркировки;

10. смешивать optimization и validation;

11. удалять неудачные эксперименты;

12. перезаписывать старые результаты;

13. считать одинаковые коррелированные стратегии независимыми;

14. запускать огромный Optuna search до первичного screening.
```

---

# 48. Главное правило экономии вычислений

Не делать:

```text
217 robots
× 6 TF
× 10 exits
× 10 filters
× 100 parameters
```

Сначала:

```text
217 robots
↓
~100–200 usable Entry implementations
↓
TF screening
↓
20–40 candidates
↓
Exit screening
↓
10–20 candidates
↓
Filters
↓
5–15 candidates
↓
parameter test
↓
OOS
↓
walk-forward
↓
5–10 frozen strategies
```

---

# 49. После получения frozen strategies

Только после этого разрешается строить ensemble.

Например:

```text
Strategy A — breakout
Strategy B — pullback
Strategy C — mean reversion
Strategy D — momentum
Strategy E — VWAP
```

И сначала тестировать их **без голосования**.

Просто:

```text
portfolio of independent strategies
```

Потом:

```text
correlation filter
```

Потом:

```text
capital allocation
```

И только в самом конце:

```text
ensemble / quorum / weighted voting
```

---

# 50. Финальный pipeline

Вся система должна работать так:

```text
ROBOTS REGISTRY
       ↓
ROBOT PARSER
       ↓
ENTRY IMPLEMENTATION
       ↓
TF SCREENING
       ↓
STANDARD EXIT
       ↓
ENTRY CANDIDATES
       ↓
EXIT SCREENING
       ↓
ENTRY/EXIT TF MATRIX
       ↓
FILTER ABLATION
       ↓
MTF TEST
       ↓
EXECUTION TEST
       ↓
PARAMETER TEST
       ↓
TRAIN / VALIDATION / TEST
       ↓
ROBUSTNESS
       ↓
COST / SLIPPAGE
       ↓
WALK-FORWARD
       ↓
CORRELATION
       ↓
FROZEN STRATEGIES
       ↓
FORWARD TEST
       ↓
PORTFOLIO
       ↓
ENSEMBLE
```

---

# 51. Что нейронка должна выдавать после каждого этапа

Не просто:

`готово`.

А строго:

```text
STAGE:
E1_ENTRY_SCREEN

INPUT:
217 robots

TESTED:
...

SKIPPED:
...

FAILED:
...

CANDIDATES:
...

REJECTED:
...

TOP CANDIDATES:
...

REASON:
...

NEXT STAGE:
...
```

И обязательно сохранять результаты в машиночитаемом формате:

```text
experiments.jsonl
trades.parquet/csv
summary.csv
strategy_cards.json
```

---

# 52. Самый важный принцип

**Сначала выяснить, работает ли идея.**

Потом:

**на каком TF она работает.**

Потом:

**какой Exit ей подходит.**

Потом:

**нужен ли фильтр.**

Потом:

**какие параметры устойчивы.**

Потом:

**работает ли она OOS.**

Потом:

**не дублирует ли другие стратегии.**

И только после этого:

**включать её в портфель/ансамбль.**

Не наоборот.

Это уже можно отдавать нейронке как **рабочий протокол**, а не как общую концепцию. Следующим шагом я бы сделал из него **машиночитаемый `ROBOT_TEST_PROTOCOL.md` + `StrategySpec/ExperimentConfig/ExperimentResult` schemas**, чтобы нейронка не могла трактовать этапы по-своему.





Да. Посмотрел репозиторий. И там есть несколько вещей, которые **нам реально стоит забрать в Deeptrading**, причём я бы немного изменил наш предыдущий план Robot Lab.

Самое важное: OsEngine уже фактически разделяет то, что мы сейчас строим сами — **робот → параметры → несколько TF/инструментов → тестер → optimizer → walk-forward → отчёты**. В README прямо указаны OsTester с несколькими стратегиями/инструментами/таймфреймами и OsOptimizer с walk-forward, фильтрами между фазами и сохранением отчётов. ([GitHub][1])

## Что конкретно нам оттуда пригодится

### 1. Не копировать роботов целиком — копировать их структуру

Это, пожалуй, самое ценное.

Типичный OsEngine-робот имеет:

```text
Base
 ├─ regime
 ├─ slippage
 ├─ order type
 └─ volume

Indicator
 ├─ period
 ├─ threshold
 └─ indicator parameters

Exit
 ├─ stop
 ├─ profit
 └─ trailing
```

Например, `StrategyEmaADX` разделяет EMA/ADX как параметры входа и отдельно задаёт trailing exit. ([GitHub][2])

`StrategyOnAOAndStoh` аналогично имеет базовые параметры, параметры объёма, параметры AO/Stochastic и отдельную логику stop/profit. ([GitHub][3])

**Для Deeptrading это практически готовая модель `StrategySpec`.**

---

# 2. Нам обязательно нужен Parameter Registry

У OsEngine параметры робота — не захардкоженная куча переменных, а описанные параметры с названием, типом, диапазоном и группой.

Например:

```text
Fast EMA
10 ... 300

ADX
10 ... 300

Volume
1 ... 50

Slippage
0 ... 20
```

Это очень хорошо ложится на наш Robot Lab.

Нам надо сделать:

```python
ParameterSpec(
    name="ema_period",
    type="int",
    default=100,
    min=10,
    max=300,
    step=10,
    group="entry"
)
```

И тогда нейронке вообще не надо вручную придумывать, какие параметры можно оптимизировать.

Она получает от робота:

```text
ENTRY PARAMETERS
EXIT PARAMETERS
FILTER PARAMETERS
POSITION PARAMETERS
```

и работает с ними автоматически.

---

# 3. Особенно полезна идея `Order Type`

В OsEngine у робота параметр:

```text
Market
Limit
```

Это нам очень пригодится.

Потому что сейчас в нашем исследовании есть риск смешать:

```text
signal quality
```

и

```text
execution quality
```

А это разные вещи.

Поэтому в Deeptrading:

```text
ExecutionSpec
```

должен быть отдельным объектом:

```text
MARKET_NEXT_OPEN
MARKET_WITH_SLIPPAGE
LIMIT
LIMIT_WITH_TIMEOUT
```

И тестировать это **после обнаружения сигнала**, а не вместе с ним.

OsEngine показывает, что execution действительно является параметризуемой частью стратегии, а не чем-то, что обязательно должно быть зашито внутрь Entry. ([GitHub][3])

---

# 4. Очень важная вещь: `Regime` у робота

У OsEngine в нескольких роботах есть стандартный параметр:

```text
Off
On
OnlyLong
OnlyShort
OnlyClosePosition
```

Например, это видно в `StrategyDpoAndAlligator` и других роботах. ([GitHub][4])

**Но я бы НЕ переносил это как нашу режимную теорию.**

Мы можем использовать это гораздо проще:

```text
TradeMode:
    BOTH
    LONG_ONLY
    SHORT_ONLY
    CLOSE_ONLY
    OFF
```

Это **операционный режим робота**, а не market regime detector.

Это очень полезно для:

* тестирования long/short отдельно;
* отключения стратегии;
* аварийной остановки;
* forward test;
* A/B экспериментов.

---

# 5. Очень полезна идея `Non-trade periods`

В OsEngine у роботов есть отдельная настройка торговых периодов. Это видно даже в достаточно простых роботах. ([GitHub][3])

Для нас это означает, что Time Filter должен быть отдельным модулем:

```text
TradingSchedule
```

а не частью каждого робота.

Например:

```json
{
  "session": "MOEX_STOCK",
  "allow": [
    ["10:00", "18:30"]
  ],
  "deny": [
    ["18:30", "19:00"]
  ]
}
```

Тогда мы можем проверить:

```text
Strategy
vs
Strategy + time filter
```

не переписывая Entry.

---

# 6. Самое интересное — OsTester

Вот это я бы **прямо заложил в нашу архитектуру**.

OsTester умеет тестировать:

* несколько стратегий;
* несколько инструментов;
* несколько таймфреймов;
* единый портфель.

Это прямо соответствует следующему этапу нашего Robot Lab. ([GitHub][5])

То есть наш тестер должен иметь два режима.

### Single Strategy

```text
1 robot
1 instrument
1 TF
```

Для первичного исследования.

### Portfolio Test

```text
N robots
N instruments
N TF
shared capital
shared risk
```

Для финальной проверки.

**Не смешивать их.**

Сначала:

```text
robot alpha
```

потом:

```text
portfolio behavior
```

---

# 7. Multi-timeframe надо сделать нативно

Это особенно важно после просмотра OsEngine.

Нам не надо заставлять каждый робот самостоятельно ресемплировать:

```text
1m → 5m → 15m → 1h
```

Нужен единый `DataFeed`:

```text
M1
M5
M10
M15
M30
H1
```

и стратегия получает:

```python
ctx.candles("M5")
ctx.candles("M30")
ctx.candles("H1")
```

Например:

```text
Entry TF = 5m
Filter TF = 30m
Execution TF = 1m
```

Это должно быть стандартным конфигом, а не специальной логикой каждого робота.

OsEngine прямо заявляет поддержку трансляции нескольких TF и инструментов в тестере. ([GitHub][5])

---

# 8. Очень полезный паттерн: индикаторы как отдельные объекты

В коде OsEngine роботы не реализуют EMA/MACD/RSI вручную.

Они создают индикатор и задают его параметры.

Например `StrategyRsiAndADX` отдельно создаёт RSI и ADX, а затем использует их в логике стратегии. ([GitHub][6])

Это подтверждает нашу идею:

```text
Indicator Library
        ↓
Entry Models
        ↓
Strategy
```

А не:

```text
MACDRobot.py
RSIRobot.py
BollingerRobot.py
...
```

---

# 9. Нам стоит сделать гораздо более богатую библиотеку индикаторов

OsEngine уже содержит большую библиотеку индикаторов, которую используют роботы.

Например, в найденных исходниках есть:

* EMA;
* RSI;
* ADX;
* MACD;
* AO;
* Stochastic;
* Alligator;
* DPO;
* OBV;
* и другие. ([GitHub][4])

Для нас это означает:

```text
не делать 100 роботов,
которые каждый внутри содержит собственный RSI/MACD.
```

Вместо этого:

```text
Indicator Engine
 ├── SMA
 ├── EMA
 ├── WMA
 ├── RSI
 ├── MACD
 ├── ADX
 ├── ATR
 ├── Bollinger
 ├── Stochastic
 ├── AO
 ├── CCI
 ├── OBV
 ├── VWAP
 ├── Alligator
 ├── DPO
 ├── ...
```

А Robot Lab комбинирует их.

---

# 10. Очень интересный класс роботов — divergence

Вот здесь я бы расширил наш каталог.

В OsEngine есть отдельные реализации divergence:

```text
Price ↔ AO
Price ↔ OBV
Price ↔ Momentum
```

Например `DevergenceMomentum` строит сигнал из расхождения экстремумов цены и индикатора, а `DevergenceOBV` делает аналогичную вещь с OBV. ([GitHub][7])

Это не просто ещё один RSI/MACD.

Нам нужен отдельный primitive:

```text
DIVERGENCE
```

с параметрами:

```text
price_source
indicator
pivot_left
pivot_right
min_distance
max_distance
min_swing
confirmation
```

И тогда можно автоматически тестировать:

```text
Price / RSI
Price / MACD
Price / AO
Price / OBV
Price / CCI
Price / Momentum
```

Это уже серьёзно расширяет нашу библиотеку Entry.

---

# 11. Очень полезный класс — indicator confirmation

Например `StrategyRsiAndADX`:

```text
RSI signal
+
ADX > threshold
+
ADX growing
```

([GitHub][6])

А `StrategyOnAOAndStoh`:

```text
Stochastic zone
+
AO direction
```

([GitHub][3])

Это идеально ложится на наш этап:

```text
ENTRY
↓
CONFIRMATION
```

То есть мы можем автоматически проверять:

```text
RSI
RSI + ADX
RSI + volume
RSI + EMA
RSI + ATR
RSI + momentum
```

Но **последовательно**, а не всё сразу.

---

# 12. Ещё один хороший паттерн — breakout + volatility

В реестре мы уже видели Price Channel и volatility strategies.

В самом OsEngine есть целые группы трендовых, volatility и screener роботов. README отдельно выделяет трендовые, контртрендовые, скринеры, HFT и т.д. ([GitHub][5])

Поэтому я бы добавил в наш Robot Lab отдельный тип:

```text
BREAKOUT
```

с возможными подтверждениями:

```text
Channel breakout
+
ATR expansion

Channel breakout
+
volume expansion

Channel breakout
+
momentum

Channel breakout
+
higher TF trend
```

---

# 13. Что я бы НЕ переносил

Очень важно.

Не надо превращать Deeptrading в копию OsEngine.

Я бы пока исключил из обычного Robot Lab:

### Grid

Потому что это отдельная модель управления позицией.

### Market making

Нужна другая модель исполнения и стакана.

### Arbitrage

Нужна синхронная работа нескольких ног.

### HFT

Нужны tick/order-book данные и другой execution engine.

### Dividend

Другая временная структура.

### Portfolio rebalancing

Это уже portfolio strategy, а не обычный Entry/Exit.

OsEngine сам разделяет такие классы — grid/market making, arbitrage, screeners, HFT, dividends и portfolio strategies. ([GitHub][5])

И наш предыдущий реестр это тоже показывает.

---

# 14. А вот Screeners нам пригодятся

Но не как обычные роботы.

Например:

```text
Universe
   ↓
Screener
   ↓
Candidates
   ↓
Entry Model
```

Это может стать вторым уровнем нашей архитектуры.

Например:

```text
SMA screener
→ выбрать инструменты

Price Channel
→ сигнал

1m execution
→ вход
```

Но это **после** базового тестирования отдельных Entry.

---

# 15. OsOptimizer — очень важная идея для нашего Optuna

Сам факт наличия в OsEngine отдельного optimizer с:

* walk-forward phases;
* фильтрами отбора между фазами;
* сохранением отчётов

нам подтверждает, что optimizer нельзя делать просто:

```text
maximize(net_profit)
```

([GitHub][5])

Для нашего Optuna я бы теперь сделал объект:

```text
OptimizationObjective
```

и отдельно:

```text
OptimizationFilter
```

Например:

```text
OBJECTIVE:
maximize expectancy

FILTERS:
trades >= 100
PF >= 1.15
MaxDD <= X
validation degradation <= Y
```

А потом:

```text
walk-forward
```

---

# 16. И ещё одна очень полезная идея из свежего OsEngine

В репозитории сейчас прямо заявлен **MCP API**, через который AI-агент может:

* загружать историю;
* создавать/настраивать роботов;
* запускать backtest;
* запускать walk-forward optimization;
* смотреть equity;
* сверять позиции с биржей.

Заявлено более 100 инструментов и real-time events. ([GitHub][5])

Для нашего проекта это особенно интересно архитектурно.

Потому что мы сейчас как раз строим:

```text
нейронка
   ↓
StrategySpec
   ↓
Backtest
   ↓
ExperimentResult
   ↓
анализ
   ↓
следующий эксперимент
```

То есть **AI-driven research loop**, который OsEngine уже реализует на уровне своей платформы.

Я бы не тащил их MCP к нам автоматически, но сам принцип стоит взять.

---

# 17. Что я бы добавил в наш проект после изучения OsEngine

Получается такая архитектура:

```text
                    ROBOT REGISTRY
                          │
                          ▼
                    StrategySpec
                          │
          ┌───────────────┼────────────────┐
          ▼               ▼                ▼
       ENTRY           FILTERS           EXIT
          │               │                │
          └───────────────┼────────────────┘
                          ▼
                    EXECUTION SPEC
                          │
                          ▼
                    POSITION MODEL
                          │
                          ▼
                    BACKTEST ENGINE
                          │
              ┌───────────┴───────────┐
              ▼                       ▼
         SINGLE TEST             PORTFOLIO TEST
              │                       │
              └───────────┬───────────┘
                          ▼
                    EXPERIMENT
                          │
                          ▼
                  TRAIN / VALIDATION
                          │
                          ▼
                    WALK-FORWARD
                          │
                          ▼
                     ROBUSTNESS
                          │
                          ▼
                    STRATEGY CARD
```

---

# 18. И я бы изменил наш предыдущий план тестирования

После просмотра OsEngine я бы добавил **ещё два обязательных слоя**.

### До Entry

```text
INDICATOR VALIDATION
```

Проверяем, что наш RSI/MACD/ATR/ADX/etc. математически совпадает с ожидаемой реализацией.

### После Strategy

```text
EXECUTION VALIDATION
```

Проверяем:

```text
Market
Limit
Slippage
Next bar
Intrabar
Stop
TP
Trailing
```

И только потом считаем стратегию валидной.

---

# 19. Самое ценное для нас

Если расставить не по «крутости», а по тому, **что реально стоит забрать в Deeptrading**, я бы выделил:

| Механика OsEngine         | Нам                              |
| ------------------------- | -------------------------------- |
| Parameter Registry        | **обязательно**                  |
| Indicator Library         | **обязательно**                  |
| Entry/Exit separation     | **обязательно**                  |
| Multi-TF engine           | **обязательно**                  |
| Multi-instrument tester   | **обязательно**                  |
| Single portfolio backtest | **обязательно**                  |
| Walk-forward              | **обязательно**                  |
| Optimizer filters         | **обязательно**                  |
| Time filters              | **обязательно**                  |
| Execution type            | **обязательно**                  |
| Divergence models         | **добавить**                     |
| Confirmation models       | **добавить**                     |
| Screener layer            | позже                            |
| Grid                      | отдельно                         |
| Arbitrage                 | отдельно                         |
| Market making             | отдельно                         |
| HFT                       | отдельно                         |
| Dividend                  | позже                            |
| Portfolio rebalance       | отдельно                         |
| MCP/AI loop               | **архитектурно очень интересно** |

---

## И главное

Я бы **не писал сейчас ещё 50 новых роботов**.

Сначала нужно сделать в Deeptrading четыре вещи:

```text
1. Indicator Library
2. Entry/Exit/Filter interfaces
3. Multi-TF Data Context
4. Experiment Runner + standardized results
```

После этого большую часть идей из OsEngine можно будет переводить **не как 217 отдельных роботов, а как комбинации примитивов**.

Например исходный OsEngine `StrategyRsiAndADX` превращается у нас в:

```text
ENTRY:
RSI threshold/cross

CONFIRMATION:
ADX > X
ADX rising

EXIT:
Trailing stop

PARAMETERS:
RSI period
RSI threshold
ADX period
ADX threshold
Trailing candles
```

А `StrategyDpoAndAlligator` — в:

```text
ENTRY:
Alligator alignment
+
Price position
+
DPO direction

EXIT:
Alligator slow-line cross
```

([GitHub][6])

И тогда **один и тот же движок** сможет автоматически проверить десятки комбинаций на `1m / 5m / 10m / 15m / 30m / 1h`, а не требовать отдельного Python-класса на каждый вариант.

Если идти дальше, я бы следующим сообщением собрал **конкретный “OsEngine → Deeptrading Porting Map”**: взять весь наш `ROBOTS_REGISTRY.md`, пройти по каждому классу и каждому роботу и сделать таблицу **«робот → какие primitives нужны → Entry → Exit → TF → параметры → приоритет теста»**. Это уже будет практически ТЗ для нейронки на реализацию Robot Lab.
