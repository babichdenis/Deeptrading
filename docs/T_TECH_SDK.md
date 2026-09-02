# T-Tech Invest SDK (t-tech-investments) — обзор для проекта

Дата: 2026-08-26. Установлен в локальный venv:
```
pip install t-tech-investments --index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
```
Версия: 1.49.3. Модуль: `t_tech.invest` (НЕ `tinkoff.invest` — старый tinkoff-invest 1.0.5 заморожен в pip).

ВАЖНО: текущий код проекта (backend/app/services/tinvest.py) использует старый
`from tinkoff.invest import Client` — он работает только пока старый пакет стоит.
При переходе на новый SDK импорты надо менять на `from t_tech.invest import Client`.

## Структура пакета

```
t_tech/invest/
  services.py          — 11 gRPC-сервисов
  schemas.py           — 368 pydantic-подобных схем
  async_services.py    — async-версии сервисов
  clients.py           — Client (sync/async)
  sandbox/             — SandboxClient (песочница, готова!)
  caching/             — кэш инструментов и market data (готовая реализация!)
  strategies/          — готовые стратегии (moving_average + plotting)
  market_data_stream/  — стрим свечей/сделок (менеджеры)
  retrying/            — retry-логика (sync/async)
  constants.py         — INVEST_GRPC_API_SANDBOX и др.
```

## Сервисы (services.py)

- InstrumentsService: shares, bonds, etfs, futures, options, currencies,
  indicatives, dfas, structured_notes + find_instrument, get_instrument_by,
  get_asset_by, get_asset_fundamentals, get_asset_reports, get_dividends,
  get_bond_coupons, get_bond_events, get_insider_deals, get_consensus_forecasts,
  get_risk_rates, get_futures_margin, get_accrued_interests, trading_schedules,
  favorites (get_favorites, edit_favorites, create/delete groups)
- MarketDataService: get_candles, get_last_prices, get_close_prices,
  get_last_trades, get_order_book, get_market_values, get_trading_status(es),
  **get_tech_analysis** (см. ниже)
- MarketDataStreamService: стрим свечей/сделок/стаканов/обновлений
- OperationsService: операции/портфель/позиции
- OrdersService: заявки; StopOrdersService: стоп-заявки
- UsersService: счета/аккаунты
- SandboxService: песочница
- SignalService: сигналы

## 💡 Что полезно ДЛЯ НАШЕГО ПРОЕКТА

### 1. get_tech_analysis — серверный тех. анализ (ВАЖНО!)
Индикаторы считаются на стороне Т-Инвестиций по историческим свечам:
```python
request = GetTechAnalysisRequest(
    indicator_type=IndicatorType.INDICATOR_TYPE_RSI,  # BB/EMA/MACD/RSI/SMA
    instrument_uid="...",
    from_=datetime(...), to=datetime(...),
    interval=IndicatorInterval.INDICATOR_INTERVAL_FIVE_MINUTES,
    type_of_price=TypeOfPrice.TYPE_OF_PRICE_CLOSE,
    length=14, deviation=..., smoothing=...,
)
resp = client.market_data.get_tech_analysis(request=request)
# resp.indicators: [{timestamp, middle_band, upper_band, lower_band, signal, macd}]
```
Индикаторы: BB (bollinger), EMA, MACD, RSI, SMA. Интервалы: 1m..4h, day, week, month.
Можно использовать как **независимый источник сигналов** для сравнения с нашими
функциями каталога (rsi_reversal, bollinger_reclaim, macd_cross) — валидация
правильности нашей реализации индикаторов!

### 2. get_market_values — рыночные метрики
```python
GetMarketValuesRequest(instrument_id=[...],
    values=[MarketValueType.INSTRUMENT_VALUE_LAST_PRICE,
            MarketValueType.INSTRUMENT_VALUE_EVENING_SESSION_PRICE,
            MarketValueType.INSTRUMENT_VALUE_OPEN_INTEREST,
            MarketValueType.INSTRUMENT_VALUE_THEOR_PRICE])
```
Полезно: close/evening price, open interest (для фьючерсов), теоретическая цена.

### 3. sandbox — ГОТОВАЯ песочница
```python
from t_tech.invest.sandbox import SandboxClient
client = SandboxClient(token)
```
Внутри — полный функционал Т-Инвестиций без реальных денег. Идеально для
будущего paper/live-теста стратегии (см. roadmap: этап paper mode).

### 4. strategies/ — каркас стратегий (moving_average)
- base/: InvestStrategy (fit/observe/predict), Signal, TraderBase, SignalExecutorBase,
  StrategySupervisor, AccountManager, StrategySettingsBase
- moving_average/: готовая MA-стратегия с plotter, supervisor, trader
- plotting/: plotter для визуализации

Это готовая архитектура «стратегия → сигнал → исполнение → аккаунт» — можно
изучить как референс для нашего EngineRunner/policy слоя (но НЕ заменять:
наш движок канонический).

### 5. caching/ — кэш инструментов и market data
- instruments_cache: кэш инструментов (готовое решение для нашего кэша FIGI/uid)
- market_data_cache: кэш свечей (может заменить ручной кэш в БД?)

### 6. market_data_stream/ — стримы
MarketDataStreamManager / async-версия: подписка на свечи/сделки/стаканы.
Для live-бота: готовые менеджеры переподключения.

### 7. retrying/ — retry-обёртки (sync/async)
Готовая логика повторов — полезно для сетевых вызовов.

## Как НАЙТИ uid/figi инструмента (пример IMOEX)
```python
from t_tech.invest import Client
with Client(token) as client:
    res = client.instruments.find_instrument(query="IMOEX")
    for item in res.instruments:
        if item.instrument_type == "index":  # IMOEX
            print(item.uid, item.figi)  # 4821c9aa-... , BBG00KDWPPW2
```

## IMOEX (индекс МосБиржи) — для наблюдателя в research pack
- uid: 4821c9aa-36e8-4743-b37c-861e58581b25
- figi: BBG00KDWPPW2
- тип: index, class TQBR, первый 1m бар: 2021-02-24
- first1dayCandleDate: 1997-09-22
- Есть также IMOEX2 (доп. сессия, uid 83ef5122-...) и IMOEXF (фьючерс, uid 5bcff194-...)
- ⚠️ history-data API (скачивание архивов) для индексов вернул ПУСТОЙ ответ —
  проверять через get_candles (gRPC) вместо history-data

## Примечания
- Для истории индексов history-data (архивы) может не работать — использовать
  get_candles с chunking (как в tinvest.fetch_candles).
- Старый пакет tinkoff-invest 1.0.5 в PyPI заморожен (не обновляется).
- Токен тот же (TINKOFF_TOKEN из .env).
