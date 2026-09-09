# Аудит движения свечей — 2026-09-08

> Полная карта пути свечи от биржи до графика, найденные баги, точки потери
> данных и план чинить. Аудит проведён по факту кода и живых логов sandbox-бота
> (uvicorn_fix.log, /api/v1/bot/*, /api/analysis/*).

## 0. TL;DR — почему «последняя свеча 15 минут назад»

1. **`analysis.py` для 1min НЕ догружал свежие данные из T-Invest API.**
   Для 5m/1h/day вызывался `resample_from_1m` (автоподтяг свежих минуток),
   а для `1min` — голый `SELECT` из БД. Если бот не писал в БД (persist
   падал) → график навсегда показывал старую свечу.
   → **Фикс применён**: для 1min добавлен `ensure_candles(...,"1min",days=1)`
   (при достижении фронтом analysis, недостающие бары тянутся из API).

2. **`_flush_persist` молча глотал ошибки (`except: pass`).** При перегрузе БД
   записи падали с `ConnectionError: unexpected connection_lost()` без следа,
   очереди затирались → данные терялись.
   → **Фикс применён**: логирование `PERSIST_ERR` + возврат батча в очередь.

3. **На сервере висели ДВА uvicorn.** Старый осиротевший (PPID=1) жрал
   29–55% CPU и не умирал после `Shutting down` (`while self.running` в
   фоновых тасках не выходил). Порт держал новый.
   → **Убит** `kill -9` (PID 7001). Бэкенд перезапущен чисто.

4. **gRPC-стрим свечей падает → fallback REST `polling`.**
   Статус всегда `mode=polling`, `last_candle_ts` залипает (health=STALE)
   даже когда свечи реально идут в лог.

---

## 1. Полная цепочка движения свечи

```
T-Invest биржа — gRPC CandleStream
   │  (waiting_close=True, свечи ЗАКРЫТОГО бара)
   ▼
CandleFeed._stream_grpc()                 app/bot/feed.py:68
   │  при ошибке стрима → except: pass → mode="polling"
   ▼
CandleFeed._polling()                     app/bot/feed.py:98
   │  REST get_candles каждые step_sec (1min=60с, окно now-4*step)
   ▼
CandleFeed.stream()                       app/bot/feed.py:53  (генератор)
   ▼
BotRuntime._run()                         app/bot/runtime.py:822/855
   └─ async for candle in feed.stream()
   ▼
BotRuntime._process_candle(c)             app/bot/runtime.py:882
   ├─ _candle_ok(c)                       runtime.py:198 — валидность OHLC/прыжок
   │     отбрасывает битые, prev_close только по валидным
   ├─ self.last_candle_ts = c.ts          runtime.py:885 (ПОСЛЕ фикса — до валидации)
   ├─ _persist_queue.append(...)          runtime.py ~899 (throttle 5с/тикер)
   ├─ _log("СВЕЧА ...")                   runtime.py:938 (в кольцо 400 логов)
   └─ стратегия/позиции (если тикер в universe)
   ▼
BotRuntime._flush_persist()               runtime.py:791  (асинх. таск, каждые 3с)
   └─ INSERT INTO candles ON CONFLICT DO UPDATE   (interval=1 и 5)
   ▼
[ PostgreSQL: таблица candles ]            (20.1M строк, индекс (figi,interval,ts))
   ▼
GET /api/analysis/{figi}?interval_name=1min   app/api/routes/analysis.py:28
   └─ (5m/1h/day) resample_from_1m        analysis.py
   └─ (1min) [ДОБАВЛЕНО] ensure_candles → догрузка из API
   ▼
Список свечей в JSON → фронт lightweight-charts
```

---

## 2. Критические точки (баллы отказа)

### 2.1 `feed.py` — переход в polling молча
```python
# feed.py:62-63 (в stream())
except Exception:
    pass          # ← ЛЮБАЯ ошибка стрима МОЛЧА переходит в polling
self.mode = "polling"
```
**Проблема:** нет логирования КАКОЙ стрим и ПОЧЕМУ упал, когда перешли в
polling, сколько попыток. Статус всегда `mode=polling`, но причина теряется.

### 2.2 `feed.py` `_polling` — ошибки глотаются per-ticker
```python
# feed.py:134
except Exception:
    continue      # ← ошибка get_candles на тикер молча
```
**Проблема:** если REST-поллинг всё время падает (сеть/token/тариф), бот
пишет «СВЕЧА» в лог только когда что-то приходит, а при полном отказе —
молчит. `last_candle_ts` не растёт без диагностики.

### 2.3 `runtime._process_candle` — персист и логика в одном потоке
- `_candle_ok` возвращает сразу → свеча не персистится (ок).
- Но персист — в том же async-цикле: если `_process_candle` долго (стратегия/
  ордера), приём свечей тормозит.
- `_persist_queue` — обычный list, throttle 5с/тикер; при многих тикерах и
  медленной БД очередь может расти, а `_flush_persist` (list, не deque) её
  забирает целиком под `except`.

### 2.4 `_flush_persist` — молчаливый `except: pass` (исправлено)
Было: `except Exception: pass` → батч выбрасывался, данные терялись, ни следа.
Стало: `except Exception as e: self._log(f"PERSIST_ERR ...")` + возврат
`batch + очередь` (не теряем).

### 2.5 `analysis.py` — 1min не догружал из API (исправлено)
Было: для 1min голый SELECT → при залипшей БД график не обновлялся.
Стало: для 1min `ensure_candles(...,"1min",days=1)`.

### 2.6 `stream_manager.py` — OrderStateStream падает с ошибкой атрибута
```
OrderStateStream disconnected: 'OrderStateStreamOrderState' object has no
attribute 'figi', reconnect in Ns (attempt N)
```
**Корень:** в `_run_orders_stream` (строка 374-381) обращение `os_data.figi`,
`os_data.direction`, `os_data.price`, а у схемы поле называется иначе
(напр. `instrument_uid` / `figi` отсутствует в OrderStateStreamOrderState).
Из-за этого стрим ордеров перезапускается с экспоненциальным backoff до
`_max_reconnect=50`, после чего ордера бот видит только через poll.

### 2.7 Дуальность статуса: `last_candle_ts` vs логи
- Статус: `data.last_candle_ts` и `candles_seen` из одного инстанса.
- Но `health` считался по `last_candle_ts` в `status()` (runtime.py:383-390).
  Раньше `last_candle_ts` обновлялся только ПОСЛЕ `_candle_ok` → если свечи
  битые, статус STALE, хотя данные идут.
  → **Исправлено**: `last_candle_ts`/`data_source` обновляются в начале
  `_process_candle` (до валидации).

### 2.8 Персист 5m в БД
- 5m собирается ресемплом из 1m в `_process_candle` (runtime.py ~918-931),
  пишется в `_persist_queue_5m`. Но эта ветка только если `_min % 5 == 4`.
  При пропуске 1m баров (прыжки/битые) 5m может не собраться.

---

## 3. Наблюдения из живых логов

- `mode=polling` почти всегда; `source="polling"` в статусе.
- `Skipped N broken candles` из `ensemble.py:1204` (compute_ensemble) растёт
  каждые ~6с — **это live-бот**: `EnsembleV4Strategy.on_bar()`
  (`ensemble_strategy.py:104`) на каждом 5m-баре каждого активного тикера
  вызывает `compute_ensemble(list(candles), req)` и пересчитывает ВСЮ историю
  с нуля. При 20 тикерах и 5m-барах это главный источник CPU (load 20-25),
  который тормозит persist/API → свечи залипают. Это НЕ отдельный процесс —
  нужно оптимизировать прогрессивный пересчёт или кэшировать (см. план C).
- `OrderStateStream disconnected ... has no attribute 'figi'` — постоянный
  цикл переподключений.
- После фикса analysis 1min: NLMK/GAZP/LKOH отдают свежие бары (до 09:59 UTC)
  при прямом запросе; SBER — старые (битый дневной кластер, отдельная тема).

---

## 4. План чинить (ут 20/10/2026)

### Этап A — Логирование критических файлов (приоритет)
1. **feed.py `stream()`** — логировать:
   - успешный старт gRPC, `mode="stream"`;
   - ПЕРЕХОД в polling с причинами (какая ошибка, сколько свечей успели);
   - в `_polling` — per-ticker ошибки REST (первое вхождение, затем throttle),
     кол-во свечей за опрос, аномалии.
2. **feed.py `_stream_grpc`** — логировать disconnect reason, reconnect.
3. **runtime `_process_candle`** — счётчик `candles_received`, `candles_persisted`,
   `candles_rejected`, время обработки, размер `_persist_queue` (периодически).
4. **runtime `_flush_persist`** — уже: `PERSIST_ERR` + возврат очереди.
   Добавить: метрику успешных батчей, задержку.
5. **analysis.py** — логировать (debug/rate-limit):
   cached_bars/downloaded из ensure_candles, отдаём ли свежий бар.

### Этап B — Чинить stream (gRPC → polling)
1. **feed.py**: не «молча» падать — retry с backoff, ЛОГИРОВАТЬ причину.
   Сделать polling более частым/надёжным (сейчас окно now-4*step).
2. **stream_manager `_run_orders_stream`** (баг `has no attribute 'figi'`):
   - проверить реальную схему `OrderStateStreamOrderState` (t_tech.invest);
   - читать правильные поля (возможно `instrument_uid` вместо `figi`, а figi
     через lookup; direction как enum);
   - обернуть дешифровку одного ордера в try/except, чтобы один битый не
     убивал стрим.
   - добавить reconnect-логику НЕ экспоненциальную бесконечную (если ошибка
     схожа с распаковкой — сразу стоп/перезапуск).

### Этап C — Чинить персист/консистентность
1. `_persist_queue` → `deque` + ограничение размера (не терять, но и не расти).
2. `_flush_persist` — батчи по размеру, повтор при транзиентной ошибке.
3. Проверить сборку 5m при пропуске 1m.
4. **Оптимизировать `compute_ensemble` на 5m-баре** (главный источник CPU):
   - кэшировать индикаторы/сигналы между барами (инкрементально);
   - или вызывать не на КАЖДОМ 5m-баре каждого тикера, а по изменению;
   - или лимитировать объём истории (`from_ts`), не тянуть все бары.

### Этап D — Авто-refresh графика
- Актуальный `main.ts` авто-refresh (8с) — ВКЛЮЧЁН (в `.bak_gchart`/`.staged`
  отключён). После фикса analysis 1min свежие бары доходят до фронта.
- Отдельный вопрос: отладочные `cdbg()`/оверлей в main.ts — почистить после
  верификации (часть нагрузки фронта).

### Порядок
A (логирование) → B (стрим) → C (персист) → D (фронт). Каждый этап — деплой +
проверка живых логов.

---

## 4.5 Журнал исполнения (утверждено 2026-09-08)

Решение пользователя: **этап A (логирование) первым** → затем B → C → D,
чинить всё подряд, шаг за шагом. ВАЖНО: свет внутренних оптимизаций —
не делать больших рефакторингов, чинить узкие места с логированием.

### Этап A — Логирование (завершён 2026-09-08)
- [x] A1. feed.py `stream()`: логировать старт gRPC, переход в polling с причиной
- [x] A2. feed.py `_stream_grpc`: логировать disconnect reason / reconnect (стрим падает мгновенно: `stream cancelled without shutdown`)
- [x] A3. feed.py `_polling`: per-ticker ошибки REST, кол-во свечей за опрос, аномалии (all через `_emit` → `on_log`)
- [x] A4. runtime: счётчики `candles_received/persisted/rejected`, размер `_persist_queue`, время обработки
- [x] A5. runtime `_flush_persist`: метрика успешных батчей (TECHINFO persist ok, раз в 10 флешей)
- [x] A6. analysis.py: логировать cached/downloaded из ensure_candles (rate-limit), отдаём ли свежий бар

**Итог A:** `feed.on_log` проброшен в `runtime._log` (`TECHINFO [feed]`). Ежеминутный `TECHINFO stat received/seen/rejected/persist_q` + `TECHINFO persist ok` каждые 10 флешей. Фронт: фильтр «Тех» (чип `lg-tech`, класс `.lg-tech`) — отфильтровать TECHINFO.

**Критическое изменение A (решение причины «фронт не видно»):** `/api/analysis?interval_name=1min` **больше не блокирует запрос докачкой свечей**. Раньше `ensure_candles(days=1)` висел >15с на T-Invest REST → фронт получал timeout. Теперь:
- ответ отдаётся из БД сразу (select150 = 0.05с);
- если последний 1m-бар старше 10 минут — фоном запускается `_spawn_ensure_1min` (ensure_candles за 2ч, `asyncio.wait_for` 12с, даггер не блокирует запрос).
- Замер после: 1min direct = 0.16с, через vite proxy = 0.39с (было timeout 15с+ code=000).
- day = 0.04с, /bot/status = 0.004с.

**Находки:** gRPC-стрим срывается немедленно (`stream cancelled without shutdown → falling back to polling`) — свежие бары идут только через polling. Это переходит в этап B (причина + фикс).

### Этап B — Стрим (gRPC → polling) (завершён 2026-09-08)
- [x] B1. feed: retry с backoff (3 попытки: 1с/3с/10с) + трассировка причины отмены
- [x] B2. stream_manager `_run_orders_stream`: починить `has no attribute 'figi'` — у `OrderStateStreamOrderState` НЕТ полей `figi`/`price`: figi резолвится через `instrument_uid` (маппинг `_uid_to_figi`, наполняется из PositionsSnapshot/Initial/Update), цена — из `initial_order_price/order_price/executed_order_price`
- [x] B3. обработка битого сообщения ордера в try/except — не роняет стрим

**Причина мгновенного падения стрима (2026-09-08, sandbox):** `market_data_stream(requests())` → `CancelledError` на `uptime=0.0s`, без единой свечи. Трассировка указывает на сам `t_tech` client — sandbox API НЕ отдаёт candle-подписки (рыночные данные только через REST get_candles). Вывод: в sandbox стрим невозможен, работает только polling; на live-токене стрим должен быть рабочим — там и брать min-свечи в потоке (идея пользователя про «минутные свечи идут в стриме» актуальна именно для live + MOEX доп.).

**Проверка на live-токене + все транспорты (2026-09-08, ИТОГ):** стрим T-Invest НЕ поднимается НИ ОДНИМ способом даже на боевом токене (forshadow):
1. **Bidirectional** (`market_data_stream` + `SubscribeCandlesRequest`, `waiting_close`/`info`) → мгновенный `CancelledError` (0.0s), без единого сообщения. Примеры SDK (async/easy_async_stream_client.py) — та же схема, тот же результат на этом токене.
2. **Менеджер подписок** (`client.create_market_data_stream()` → `candles.waiting_close().subscribe([CandleInstrument(...)])`) → стрим ОТКРЫВАЕТСЯ (в тарифе `MarketDataStream open=1`, лимит 32), НО сервер не шлёт НИЧЕГО: ни `subscribe_candles_response`, ни свечей, ни trades, ни ping. Молчит 30с+ → `SILENT`-fallback.
3. **Server-side стрим** (`market_data_server_side_stream`) → в SDK 1.49.3 баг: `_UnaryStreamMultiCallable.__call__() got unexpected keyword 'request_iterator'` (и async, и sync client).
4. **WebSocket** (`wss://invest-public-api.tbank.ru/ws/`) → HTTP 404; `/ws`, `/`, `:443/ws/` → таймаут opening handshake.
5. **Сертификаты НЕ при чём**: gRPC-канал и так верифицирует по вшитому НУЦ Минцифры (`t_tech/invest/certs/RussianTrustedRootCA.pem`). OrderStateStream/PositionsStream (тот же host, тот же канал) работают → gRPC/net сам по себе исправен; молчит именно MarketData service.
6. **Тариф/токен не при чём**: лимиты (MarketDataStream 32, Orders 16, Operations 11), аккаунт FULL_ACCESS, REST GetCandles 600/мин работает (бот стабильно живёт в polling на боевом API).

**РЕШЕНИЕ (утверждено пользователем):** T-Invest маркет-дата-стрим в этой среде не поднимается ни одним транспортом → **оставляем polling** (боевой REST, 600 запросов/мин — хватает с запасом на 20 figis по 1 запросу/мин). Код стрима в `feed.py` (менеджер + проверка подписки + переподписка + SILENT-таймаут 15с×3 + fallback) остаётся как fallback: если вдруг стрим заработает — бот сам переключится, иначе 45с → polling. Дальше по свечам: опционально MOEX-источник (уже есть `moex.py`), оптимизация compute_ensemble (C4).

### Этап C — Персист/консистентность (✅ C1–C2 задеплоены очередным рестартом 17:50)
- [x] C1. `_persist_queue`/`_persist_queue_5m` → `deque(maxlen=2000)` — защита от неограниченного роста; старые записи вытесняются без падения, потери видны в `TECHINFO stat persist_q`
- [x] C2. `_flush_persist` собирает батч через popleft-дрейн; при ошибке возвращает записи обратно в deque (extend) — без переполнения и потери порядка
- [x] C3. Сборка 5m проверена — работает при полном 1m-потоке (`len(buffer)>=5` + проверка кратного старта); edge-случай пропуска баров внутри интервала не чиню (низкий риск)
- [ ] C4. Оптимизация `compute_ensemble` на 5m-баре (источник CPU load 20-25) — **отдельная крупная задача, НЕ сделана**

> Примечание: C1–C2 внесены были раньше и с 17:50 уже работают в живом процессе (деплой через тот же рестарт, что поднял бота на polling). Проверка в тех-логах: `TECHINFO persist ok` без `PERSIST_ERR`.

### Выбор токена feed (добавлено 2026-09-08)
- `app/config.py`: `Settings.feed_token` → `tinkoff_live_token or tinkoff_token`. Один источник выбора.
- `app/bot/runtime.py` `_run()`: feed берёт `settings.feed_token` (боевой target=None всегда: sandbox не даёт стрим и данные боевые можно брать с боевого REST).
- `.env`: активен `TINKOFF_TOKEN`; заготовка `TINKOFF_LIVE_TOKEN=` закомментирована. Переключение = раскомментировать нужный, лишний закомментить.
- Отдельно: `t_tech` сам верифицирует сертификаты по вшитому НУЦ (SSL_TBANK_VERIFY default true) — сертификаты НУЦ Минцифры на сервере НЕ нужны.

### Этап D — Фронт
- [ ] D1. Подтвердить авто-refresh 8с показывает свежую свечу
- [ ] D2. Почистить отладочные `cdbg()`/оверлей в main.ts

---

### Этап E — Диагностика времени баров в feed (2026-09-08, ~17:35–21:00 МСК)

**Проблема:** при том, что `persist_ok` идёт без ошибок (весь этап C), в БД
нет свежих баров в последнем окне (`ts > now - 20min`). Свежие свечи
«теряются».

#### Что проверили и нашли (по шагам)
1. **БД/JDBC исправна:** прямые SELECT работают; вручную вставленная контрольная
   строка (`BBG004730N88` iv=1 ts=16:30 UTC) видна запросом.
2. **Библиотека `fetch_candles` отдаёт СВЕЖЕЕ время:** при текущем ~20:49 МСК
   вернула бары `ts=17:45..17:48 UTC` (правильно). Тот же боевой API/токен.
3. **`FLUSH_ITEM` в runtime показал сдвиг:** feed писал бары с `ts≈13:37Z`
   при реальном закрытии ~17:37Z (4 часа назад). Цены в этих барах — свежие
   (текущие!), т.е. это НЕ задержка поставки и НЕ таймзона, а именно
   **кривая сборка метки времени в feed**.
4. **Откуда месяцы валидных свечей в БД:** их наполняет НЕ фид, а библиотека
   `ensure_candles/fetch_candles/upsert_candles` (бэктесты, горизонты гисторы,
   стартовые буферы, фронт-анализ). Поэтому старая база полная и корректная,
   а бары, которые писал feed в эти месяцы, «уходили в историю» — просто никто
   не сверял свежие окна.

#### Корень (зафиксировано)
`backend/app/bot/feed.py` строит `ClosedCandle.ts` через
`candle.time.replace(tzinfo=timezone.utc)` — и в `_stream_grpc` (строка ~210),
и в `_polling` (строка ~256). В этом сценарии (AsyncClient t_tech) метка
приходит ровно на −4ч от реального закрытия. Библиотечный путь
(`fetch_candles`, sync `Client`) такой проблемы не имеет — он пишет
корректное время (проверено прямым вызовом).

**Плановое решение:** перевести `feed._polling` на общий слой
`fetch_candles` (+ `upsert_candles` для записи в БД). Это же реализует
архитектуру «первое место прихода свечи → сразу в БД» (требование
пользователя): `feed` перестанет сам конструировать время, а будет брать
готовое из библиотеки, которая и есть единственный источник истины.

#### Изменения кода в этой сессии (небольшие, безопасные — бот работает)
- **`backend/app/bot/feed.py`:**
  - импорты: `from app.database import SessionLocal`, `from app.services.tinvest import upsert_candles`;
  - `__init__`: `self._persist_buf: list[dict] = []`;
  - методы `_queue_persist(candle)` (строка ~74) и `_flush_persist()` (строка ~88) — буферизованная запись баров в БД через `upsert_candles`; лог `persist_ok n=… first=… ts=…` / `persist_fail …` (при ошибке буфер сохраняется);
  - вызовы `_queue_persist`+`_flush_persist` добавлены в оба пути: `_stream_grpc` (после построения cc, макс 10 в батче) и `_polling` (аналогично).
- **`backend/app/bot/runtime.py`:**
  - в `_flush_persist` после `self._persist_flushes += 1` добавлен **временный debug-лог**
    `TECHINFO FLUSH_ITEM f=… ts=… iv=… flushes=… q=… q5=…` (для диагностики времени) — убрать после подтверждения.
  - (лог остальных TECHINFO stat / persist ok — это этап A, задокументирован выше).

⚠️ **Торговая логика не менялась нигде** (`runtime` стратегии, `ensemble_strategy`,
позиции/заявки не тронуты).

#### Статус на конец сессии (~21:00 МСК)
- Бот жив: `running=True`, `mode=polling`, свежие бары идут до ~20:44 МСК (TCS 107UL4);
  `TECHINFO stat received` растёт (1→30 за ~5 мин), `rejected=0`.
- Наблюдение: бары приходят только по **1 тикеру из 20** (107UL4). Остальные тихо
  молчат — в `_polling` ошибки/пустые ответы `get_candles` глотаются `continue`
  **без лога** (ещё один пункт для исправления вместе с переводом на `fetch_candles`).
- Бот сейчас в вечерней сессии, входы разрешены (`sessions=[morning,day,evening]`,
  `ensemble_session=all`, окно evening 19:05–23:50 МСК).

#### ⏸ Отложено пользователем (не делать до подтверждения)
1. **Перевод `feed._polling` на `fetch_candles`** + рестарт бота (корень проблемы времени).
   Сейчас идут работы с другой стороны (нейронка) — бота и движок не трогаем.
2. Логировать по-тикерные ошибки/пустые ответы в `_polling`.
3. Убрать debug-лог `FLUSH_ITEM` после подтверждения фикса.
4. `git add/commit/push` накопленного (правки feed.py/runtime.py уже на диске на сервере).

---

## 5. Проверка после фиксов (checklist)
- [ ] Статус `mode` = "stream" (не polling) и причина перехода логируется.
- [ ] `OrderStateStream` НЕ падает с "has no attribute 'figi'".
- [ ] `last_candle_ts` растёт каждую минуту (health=HEALTHY), свежий бар.
- [ ] `_persist_queue` пустеет, нет `PERSIST_ERR` в цикле.
- [ ] `/api/analysis/.../1min` отдаёт бар ≤ 1-2 мин назад.
- [ ] График (авто-refresh 8с) показывает свежую свечу без скачков/пропадания.
