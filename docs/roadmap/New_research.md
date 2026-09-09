# New_research — аудит источников данных (2026-09-09)

> Цель: найти **постоянный источник волатильности** акций (рыночный режим-фильтр + per-ticker + ликвидность universe). Все эндпоинты проверялись с сервера .3.

## Итог: что работает без ключей

| Источник | RVI | IMOEX | TQBR обороты | Per-ticker IV | Без ключа |
|---|:---:|:---:|:---:|:---:|:---:|
| **MOEX ISS** | ✅ live + свечи | ✅ daily, 20 строк | ✅ live, 506 акций | — | ✅ |
| Опционы MOEX | ~ | — | — | ⚠️ только через Black-76 | ✅ |
| ALGOPACK | — | — | — | — | ❌ (сеть с .3 не пускает, HTTP 000) |
| Finam Trade API | — | — | ~ | ~ | ❌ (301 + нужен ключ) |
| CBR DailyInfo (SOAP) | — | — | — | — | ✅ (но это ставки/валюты) |

**Вывод:** единственный полностью рабочий бесплатный источник — **MOEX ISS**. Закрывает 3 из 4 задач (RVI → режим-фильтр, IMOEX → universe, TQBR → ликвидность). Per-ticker IV — отдельной работой через опционы + модель Black-76, в текущей среде быстрее оставить.

---

## 1. RVI live — индекс волатильности российского рынка

**Эндпоинт:** `https://iss.moex.com/iss/engines/stock/markets/index/securities/RVI.json`
Отдаёт одну строку, обновление каждые ~15 сек.

### Колонки marketdata (все):

| Колонка | Значение (пример) | Смысл |
|---|---|---|
| `SECID` | RVI | Код |
| `BOARDID` | RTSI | Режим торгов |
| `LASTVALUE` | 37.34 | Последнее значение RVI = подразумеваемая волатильность рынка на 30 дней, в % (аналог VIX) |
| `CURRENTVALUE` | 36.58 | Текущее (самое свежее) |
| `OPENVALUE` | 39.2 | Значение на открытии |
| `LASTCHANGE` | -0.76 | Изменение за сессию (пункты) |
| `LASTCHANGEPRC` | -2.04 | Изменение за сессию в % |
| `LASTCHANGETOOPENPRC` | -6.68 | Изменение от открытия в % |
| `HIGH` / `LOW` | 50.76 / 36.35 | Максимум/минимум за день |
| `MONTHCHANGEPRC` | -18.84 | Изменение за месяц в % |
| `YEARCHANGEPRC` | +7.12 | Изменение за год в % |
| `UPDATETIME` / `TIME` / `SYSTIME` | 17:32:31 | Время обновления / торгов / сервера |
| `TRADEDATE` | 2026-09-09 | Торговая дата |
| `TRADINGSESSION` | 3 | Номер сессии (3 = основная/вечерняя) |

**Важно:** RVI 37.34 = вола ~37% годовых, заложенная в опционы/фьючерсы на RTS. Растёт при страхе, падает при спокойствии. Разброс HIGH/LOW за день может быть большим (сегодня 14 пунктов: 36.35–50.76).

### RVI свечи — для истории и бэктестов

**Эндпоинт:** `https://iss.moex.com/iss/engines/stock/markets/index/securities/RVI/candles.json?interval=60&from=2026-09-01`

- Те же OHLCV, что и у акций: `begin, open, close, high, low, volume`
- `interval`: **1, 10, 60 минут, day, week, month**
- **Есть 1-минутные свечи RVI** — можно хранить в своей БД (как свечи SBER в `feed.py`) и строить минутный режим-фильтр, зеркально бэктесту.
- История с 01.09 = 113 часовых свечей за ~0.3с.
- Пример утренней свечи: `07:00 open=44.73 → close=49.78, high=49.93, low=44.73` (всплеск к 50 пунктам утром 01.09).

---

## 2. IMOEX состав с долями

**Эндпоинт:** `https://iss.moex.com/iss/statistics/engines/stock/markets/index/analytics/IMOEX.json`
Ежедневная выгрузка; актуальна на предыдущий торговый день.

- **Всего 20 строк** — ровно 20 акций индекса.
- Колонки: `indexid, tradedate, ticker, shortnames, secids, weight, tradingsession, trade_session_date`

### Состав IMOEX (вес в %, отсортировано):

| Тикер | Название | Вес % |
|---|---|---|
| LKOH | ЛУКОЙЛ | 17.96 |
| GAZP | ГАЗПРОМ | 8.62 |
| GMKN | ГМКНорНик | 4.59 |
| HEAD | Хэдхантер | 1.19 |
| MOEX | МосБиржа | 1.08 |
| IRAO | ИнтерРАО | 0.95 |
| CBOM | МКБ | 0.87 |
| CHMF | СевСт | 0.86 |
| DOMRF | ДОМ.РФ | 0.67 |
| MAGN | ММК | 0.52 |
| AFLT | Аэрофлот | 0.45 |
| ENPG | ЭН+ГРУП | 0.45 |
| MDMG | MDMG | 0.41 |
| FLOT | Совкомфлот | 0.37 |
| ALRS | АЛРОСА | 0.36 |
| LENT | Лента | 0.34 |
| CNRU | Циан | 0.33 |
| BSPB | БСП | 0.32 |
| AFKS | Система | 0.26 |
| MSNG | МосЭнерго | 0.15 |

**Полезное:** альтернативный источник universe (20 акций «ровно по бирже», совместим с нашим top-20 по ATR%); даёт веса → можно фильтровать по минимальной доле. Обновляется раз в день (после закрытия).

---

## 3. TQBR котировки/обороты — весь основной режим одним запросом

**Эндпоинт:** `https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.only=marketdata`
**506 инструментов одной выгрузкой за ~0.4с.**

### Колонки marketdata на инструмент (SBER):

| Колонка | SBER | Смысл |
|---|---|---|
| `LAST` | 277.77 | Последняя сделка |
| `BID` / `OFFER` | 277.76 / 277.77 | Лучшие котировки (спред 0.01₽) |
| `BIDDEPTHT` / `OFFERDEPTHT` | 4.1М / 4.15М | Объёмы в лучших котировках (шт) |
| `OPEN` / `HIGH` / `LOW` | 278.95 / 279.98 / 275.5 | День: открытие/макс/мин |
| `WAPRICE` | 277.24 | Средневзвешенная цена дня |
| `NUMTRADES` | 83777 | Число сделок |
| `VOLTODAY` | 15 197 765 | Объём в штуках |
| `VALTODAY` | 4 213 353 025 | **Оборот в рублях** (главная метрика ликвидности) |
| `LASTCHANGEPRCNT` | 0 | Изменение за день в % |
| `TRADINGSTATUS` | T | Статус торгов (T = торгуется) |
| `UPDATETIME` | 17:17:59 | Время последнего обновления |
| `MARKETPRICE` | 279.39 | Индикативная рыночная цена |
| `WAPTOPREVWAPRICEPRCNT` | -0.74 | Динамика ср.-взвеш. цены к предыдущему дню |
| `ISSUECAPITALIZATION` | 6 трлн ₽ | Капитализация выпуска |
| `SPREAD` | 0.01 | Текущий спред |

### Топ-25 по обороту (17:20 МСК 2026-09-09):

```
LKOH  4.88 млрд ₽
GAZP  4.69 млрд ₽
SBER  4.23 млрд ₽
AKMM  3.73 млрд ₽
OZON  2.83 млрд ₽
T     2.80 млрд ₽
GMKN  1.97 млрд ₽
SBFR  1.90 млрд ₽
VTBR  1.81 млрд ₽
SBMM  1.68 млрд ₽
TATN  1.64 млрд ₽
YDEX  1.63 млрд ₽
NVTK  1.60 млрд ₽
SMLT  1.59 млрд ₽
LQDT  1.58 млрд ₽
MAGN  1.34 млрд ₽
SAFE  1.31 млрд ₽
PLZL  1.26 млрд ₽
ROSN  1.22 млрд ₽
ALRS  1.17 млрд ₽
CHMF  1.01 млрд ₽
MOEX  0.99 млрд ₽
SVCB  0.92 млрд ₽
TRNFP 0.77 млрд ₽
ASTR  0.77 млрд ₽
```

**Полезное:** один запрос даёт всю ликвидность рынка → автоматическое ежедневное ранжирование universe (пересборка «активных тикеров»). **Минус:** `LAST/BID/OFFER` идут с задержкой 15 минут, `WAPRICE/VALTODAY` агрегируются за день — для внутридневных решений точнее стрим T-Invest (как сейчас у бота).

---

## Остальные источники (проверено, отсечено)

### Опционы MOEX (`engines/futures/markets/options/`)
- SECID с привязкой к базе: `SP220CI6` (SBER), `AF1250BI6` (AFLT); SHORTNAME вида `AFLT-9.26M160926CA1250`
- marketdata: `BID/OFFER/LAST/SETTLEPRICE/OPENPOSITION/UPDATETIME` (+`OICHANGE`)
- **`marketdata_yields.IV` пуст** (`data: []`) — IV напрямую не отдаётся
- Свечи опционов пусты (`rows: 0`)
- **Вывод:** IV = расчёт через Black-76 из цены опциона. Много контрактов с `LAST=0` — нужен отбор ликвидных (по `OPENPOSITION`/`BID>0`).

### ALGOPACK (`apim.moex.com`, `moexalgo`)
- `HTTP 000` на всех путях с .3 (сеть блокирует), `iss.moex.com/iss/datashop/algopack.json` → 404
- Нужен токен `data.moex.com/personal-account` + возможно VPN. В текущей среде — мёртв.

### Finam Trade API (`trade-api.finam.ru`)
- `301 → tradeapi.finam.ru`, требует `X-Api-Key`, без ключа недоступен.

### CBR
- `DailyInfo.asmx` (SOAP) — работает (HTTP 200), но это ставки/валюты, не волатильность.
- `cbr-registry.ru/api/v1/dividends` — мёртв (HTTP 000).

---

## План интеграции (предложение)

1. **Модуль `volatility.py`** (backend/app/bot или services): забор RVI live + RVI 1м свечей, IMOEX состава, TQBR оборотов.
2. **Хранение в БД:** таблица для RVI (как свечи в `candles.ts`), дневной снимок IMOEX/TQBR в `universe` (новые колонки `imoex_weight`, `turnover_rub`).
3. **Режим-фильтр для бота:** RVI > 45 = high-vol → шире SL/TP; RVI < 30 = спокойный → уже. Логика как в `RegimeDetector`.
4. **Пересборка universe:** ежедневно обновлять топ по обороту из TQBR вместо/в дополнение к ATR%.

---

## Файлы проекта по теме

- `backend/app/bot/moex.py` (коммит `20a8ee0`) — пример free MOEX ISS 1-min candles (TQBR) + `sync_moex_sync`
- `backend/.venv313/.../ml4t/engineer/features/volatility/` — готовые фичи: realized_volatility, garman_klass_volatility, rogers_satchell_volatility, volatility_regime_probability, volatility_percentile_rank, volatility_of_volatility
- `backend/app/bot/universe.py` — `select_eligible_universe` (топ по ATR%)