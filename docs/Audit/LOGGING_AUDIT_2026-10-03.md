# Deeptrading — аудит логирования и проглатывания ошибок

Дата: 2026-10-03  
Проверенный commit: `d70d6ab3b422f09cb838c37dbdc70e8c7bb5b6df`

Продуктовый код в ходе проверки не изменялся. Аудит выполнен поверх текущего `master` и дополняет `PROJECT_AUDIT_2026-10-03.md`.

## 1. Итог

Опасение подтверждается: при ряде инцидентов проблему действительно придётся восстанавливать по косвенным признакам.

В проекте уже есть полезные элементы наблюдаемости — стандартный `logging`, UI-кольцо `LogHub`, PostgreSQL `bot_logs`, runtime events, Signal Trace, reconnect-логи feed/streams. Но это не единый надёжный контур:

- runtime `_log()` попадает в UI/БД, но не в обычный process log;
- стандартный `logger.*` попадает в process log/UI, но не сохраняется в `bot_logs`;
- traceback почти всегда теряется;
- runtime events находятся только в памяти;
- при ошибке записи `bot_logs` batch теряется;
- при остановке нет финального drain логов;
- фоновые задачи почти нигде не контролируются supervisor'ом;
- health endpoint всегда отвечает `{"status":"ok"}` независимо от БД, feed, broker и task liveness.

Статический обзор production-кода `backend/app` дал:

- 584 `except` handlers;
- 477 broad `Exception`/bare handlers;
- 129 handlers с фактическим `pass`/`continue` без логирования;
- ещё 218 broad handlers с fallback/return/другим поведением без диагностической записи;
- 136 вызовов `_audit_swallow`, все в `runtime.py`;
- только 3 использования `logger.exception`/эквивалента во всём `backend/app`.

Эти числа — triage, а не утверждение, что каждый handler ошибочен: часть fallback'ов намеренная. Но масштаб показывает, что текущая диагностика строится на исключениях из правил, а не на общей политике.

Самая опасная находка относится уже не только к удобству логирования: `LiveBroker` в нескольких местах превращает техническую ошибку или неизвестный broker result в «нормальное» значение. Runtime после этого способен отметить ордер исполненным по цене свечи, хотя фактический результат API не разобран.

## 2. Что уже сделано хорошо

### 2.1. Есть централизованная точка настройки

`app/logging_setup.py` задаёт уровень, формат и подавляет шумные библиотеки. `HubHandler` подключает стандартный Python logging к UI-кольцу.

### 2.2. Feed логирует переключение stream → polling

`app/bot/feed.py` пишет attempt, uptime, факт получения свечи, timeout и тип gRPC-ошибки. Это хороший пример операционного контекста. Ошибки persist возвращают свечи в очередь.

### 2.3. StreamManager показывает reconnect lifecycle

Есть именованные задачи, backoff, номер попытки, subscription status и сообщения о достижении max reconnect.

### 2.4. Очередь Lab сохраняет terminal status

`test_queue.py` использует `logger.exception` на failure и переводит run в `FAILED`; это лучше, чем оставлять зависший `RUNNING`.

### 2.5. Для торговых событий есть доменные записи

Runtime создаёт `ORDER_SUBMITTED`, `ORDER_REJECTED`, `ORDER_FILLED`, `POSITION_OPENED/CLOSED`, circuit breaker и data-quality events. Signal Trace добавляет отдельный аналитический контур. Проблема не в полном отсутствии сигналов, а в их неполной долговечности и корреляции.

## 3. P0 — ошибки, которые могут скрыть фактическое состояние торговли

### P0.1. Неизвестный результат live-ордера превращается в исполнение

Файлы:

- `app/bot/live_broker.py::open_position`;
- `app/bot/live_broker.py::close_position`;
- `app/bot/runtime.py::_execute_pending`.

После успешного вызова `post_order` разбор `executed_order_price` обёрнут broad `except`:

- `open_position()` при ошибке разбора возвращает `None` без лога;
- runtime трактует `None` как допустимый результат, подставляет `c.open` и ставит `order.status = FILLED`;
- `close_position()` при ошибке разбора подставляет переданную candle price и создаёт локальный `PaperTrade`.

То есть «не удалось доказать execution» превращается в «execution состоялся по расчётной цене». При расхождении с брокером локальный held/PnL/SL state станет неверным, а исходная ошибка исчезнет.

**Что сделать:**

1. Broker adapter должен возвращать typed result: `FILLED | REJECTED | ACCEPTED_PENDING | UNKNOWN` плюс `broker_order_id`, execution status, lots requested/filled и raw status code.
2. `UNKNOWN` никогда не превращать в `FILLED`; переводить order в `PENDING_RECONCILIATION`, блокировать повторный вход по инструменту и запрашивать `get_order_state`/trades stream.
3. На parse/protocol error писать `ERROR` с traceback, order id, figi, account contour, request quantity; секреты не логировать.
4. После любого order timeout/unknown запускать обязательный reconciliation и durable incident event.

### P0.2. Ошибка lot lookup молча меняет размер close order

`LiveBroker._get_lot()` при любой ошибке БД возвращает `1`. `close_position()` затем вычисляет количество лотов через этот fallback. Если broker position quantity выражена в штуках, а реальный lot больше 1, close request может оказаться многократно больше ожидаемого.

Это нельзя считать harmless fallback.

**Что сделать:** на close отсутствие подтверждённого lot — fail closed. Получать lot из instrument cache/API, сверять request против фактической позиции и логировать `CRITICAL/ERROR` с запретом отправки ордера. Fallback `1` допустим только если инструмент явно подтверждён как lot=1.

### P0.3. Потеря persistent logs при ошибке БД и shutdown

`runtime._flush_persist()` дренирует `_log_persist_queue` до вставки. Если INSERT в `bot_logs` падает, batch не возвращается в очередь. Сохраняется только новое сообщение `PERSIST_LOG_ERR`, а исходные записи потеряны.

При `stop()` runtime сначала выставляет `running=False`, затем отменяет `_persist_task`; финального drain очереди нет. Последние секунды логов перед остановкой/падением также могут исчезнуть — именно те записи, которые обычно нужны при расследовании.

**Что сделать:**

- при DB failure возвращать batch в bounded retry queue с attempt/backoff;
- перед shutdown выполнять shielded final flush с timeout;
- critical/error записи дополнительно писать синхронно в stderr или локальный append-only spool;
- хранить counters `log_queue_depth`, `log_persist_failures`, `log_dropped_total`, `last_log_flush_at`;
- при переполнении не молчать: оставить aggregate incident и сохранить ERROR/CRITICAL приоритетно.

## 4. P1 — системные пробелы наблюдаемости

### P1.1. Контур логирования фактически разделён на два

Текущий поток:

| Источник | Console/stderr | UI ring | PostgreSQL `bot_logs` |
|---|---:|---:|---:|
| `runtime._log()` | нет | да | да, если flusher работает |
| стандартный `logger.*` | да | да через `HubHandler` | нет |
| `runtime.events.log()` | нет | отдельное memory ring | нет |
| Signal Trace | нет/частично | нет | JSONL и опционально DB |

Из-за этого traceback из `logger.exception`, startup/migration/feed errors и task errors после рестарта обычно не найти в `/logs/history`. А основные runtime сообщения могут отсутствовать в container/journald log.

**Цель:** одна structured record должна fan-out'иться в console + durable sink + UI, без повторного форматирования и без рекурсивного логирования ошибок самого sink.

### P1.2. `_audit_swallow` скрывает повторные и полные ошибки

`runtime._audit_swallow()`:

- пишет только один раз на `where` за жизнь процесса;
- обрезает exception message до 120 символов;
- не сохраняет traceback;
- не считает число повторов;
- использует hardcoded labels со старыми номерами строк;
- разные реальные причины в одном месте после первого события больше не видны.

Например startup exception логируется под устаревшим `_load_bounded@L2643`, что направляет расследование не в тот участок.

**Замена:** rate-limited exception recorder по `(component, operation, exception_type, normalized_message)`:

- первая ошибка — полный traceback;
- повторы — counter и sampled summary, например на 10/100/1000;
- recovery event после восстановления;
- поля `first_seen`, `last_seen`, `count`, `degraded_since`;
- source location получать автоматически, operation задавать стабильным именем, без номера строки.

### P1.3. Критичные fallback'и неотличимы от корректного бизнес-результата

Примеры из `LiveBroker`:

- `margin_attributes()` возвращает `{}` при API error;
- `last_prices()` возвращает `{}`;
- `get_trading_status()` возвращает `""`;
- `main_session_active()` возвращает `False`;
- `free_funds()` тихо переключается на другую формулу;
- отдельные malformed last prices пропускаются через `continue`;
- `close()` глотает ошибку закрытия gRPC client.

Некоторые fallback'и fail-safe, но оператор не видит, что система работает в degraded mode. Пустой настоящий результат и техническая ошибка имеют одинаковое представление.

**Что сделать:** возвращать value + freshness/source/degraded metadata или бросать domain exception на критической границе. Fallback обязан создавать sampled warning, counter и видимый health degradation.

### P1.4. Ошибки startup/config/migrations допускают запуск в неизвестной конфигурации

`app/main.py` молча пропускает:

- часть runtime `ALTER TABLE`;
- загрузку сохранённых bot settings;
- отдельные daily-bars downloads;
- DB error при определении stale daily bars (`_stale=False`, то есть remediation отключается);
- shutdown errors.

Alembic failure логируется только как короткий warning без traceback, после чего приложение продолжает стартовать. Сохранённые настройки могут не загрузиться, и бот продолжит с defaults без явного degraded status.

**Что сделать:**

- schema/config invariants — fail startup или readiness, а не `pass`;
- логировать effective config snapshot и его source/hash, без секретов;
- отмечать `CONFIG_FALLBACK`/`SCHEMA_DEGRADED` durable events;
- на partial daily sync показывать attempted/succeeded/failed и список sampled failures;
- migration errors писать с traceback и migration revision.

### P1.5. Фоновые задачи не имеют общего supervisor

В production-коде создаются десятки `asyncio.create_task`. Только несколько задач именованы, а `add_done_callback` в основном просто удаляет strong reference. Нет общего механизма, который:

- извлекает exception из завершившейся task;
- пишет task name + traceback;
- обновляет liveness/restart count;
- решает restart/backoff/fail-process policy;
- поднимает alert при permanent stop.

Runtime запускает persist, reconciliation, session, guard, metrics, momentum и другие циклы, но status не показывает liveness каждого worker. Задача может умереть, а бот останется `running=True`.

**Что сделать:** `TaskSupervisor`/`asyncio.TaskGroup` с именами, criticality и политикой restart. В `/ready` показывать `alive`, `last_success`, `last_error`, `restart_count` по каждой обязательной задаче.

### P1.6. Health endpoint не является health/readiness check

`GET /api/health` всегда возвращает `{"status":"ok"}`. Он не проверяет:

- DB connectivity/schema revision;
- bot main task и child tasks;
- freshness candles/feed;
- broker/stream connectivity;
- persist queues;
- Signal Trace drops;
- reconciliation freshness.

Нужны отдельные endpoints:

- `/live` — процесс/event loop жив;
- `/ready` — БД и обязательные зависимости доступны, schema совместима;
- `/health/details` — task/feed/broker/data freshness и degraded reasons.

HTTP status readiness должен быть 503 при критическом degradation.

### P1.7. Почти все exception logs теряют stack trace

Во всём `backend/app` только три явных `logger.exception`. Типичный код пишет `type(e)` и первые 80–200 символов сообщения. Этого недостаточно для определения call path, входного состояния и места ошибки.

Policy:

- ожидаемый business reject — без traceback, но со structured reason code;
- transient external error — traceback на первой ошибке fingerprint + sampled repeats;
- unexpected exception — `logger.exception`/`exc_info=True`;
- fatal state corruption — `CRITICAL`, stop/fail closed.

`HubHandler` сейчас при `exc_info` сохраняет только `ExceptionType: message`, но не stack. Durable sink должен иметь отдельное поле stack; UI может показывать stack в раскрываемой карточке.

### P1.8. Runtime EventLog называется audit, но живёт только в памяти

`app/bot/events.py::EventLog` — deque на 500 элементов. 61 call site пишет туда важные события, включая order/fill/position/circuit breaker. После crash/restart эти события исчезают. Это не audit log.

В проекте отдельно существует DB-backed `services/eventbus.py`, но runtime events туда не направляются. При этом persistent EventBus сам имеет проблемы: `delivered` не обновляется, QueueFull удаляет subscriber без warning, sequence вычисляется как `max+1` и не защищён от конкурентных publishers.

**Что сделать:** разделить:

- operational logs;
- immutable trading audit events;
- UI notifications.

Order/fill/position/risk/config-change события писать транзакционно в append-only audit table/outbox с уникальным DB sequence и correlation IDs. UI ring должен быть только projection.

### P1.9. Нет request/correlation IDs

HTTP middleware проверяет только write token. Нет request id, latency/status logging и связи API action с bot run/order/event. `uvicorn.access` принудительно поднят до WARNING и дополнительно отбрасывается `HubHandler`, поэтому обычного request trail нет.

Минимальные IDs:

- `request_id` для HTTP/WebSocket command;
- `run_id/test_name`;
- `signal_id`, `decision_id`, `order_id`, `broker_order_id`;
- `figi/ticker`;
- `task_name`;
- `mode/contour`.

Контекст нужно проводить через `contextvars`/`LoggerAdapter`, а не вручную вставлять в текст.

### P1.10. Нет автоматических alert sinks

Поиск не обнаружил Sentry/OpenTelemetry/Prometheus integration. Ошибки доступны только если оператор открыл console/UI. Для live trading нужны минимум alerts на:

- unknown/rejected broker order;
- reconciliation mismatch;
- bot main/critical child task death;
- no fresh candles;
- DB/persist failure;
- repeated gRPC reconnect / permanent stop;
- watchdog deficit;
- log/trace drops;
- circuit breaker.

Alert должен содержать ссылку/ключ расследования, но не токены и персональные данные.

## 5. P2 — качество, хранение и безопасность логов

### P2.1. Поля слишком бедные и source обрезается до 16 символов

`LogRecord`/`bot_logs` хранят только `ts, level, source, msg`; source принудительно обрезается до 16 символов. Полные logger names могут столкнуться после truncation. Structured context находится внутри русского текста, поэтому его трудно фильтровать и агрегировать.

Рекомендуемая запись:

```json
{
  "ts": "UTC ISO-8601",
  "level": "ERROR",
  "service": "backend",
  "component": "live_broker",
  "event": "broker_order_result_unknown",
  "message": "Не удалось подтвердить исполнение ордера",
  "mode": "live",
  "run_id": "...",
  "request_id": "...",
  "signal_id": "...",
  "order_id": "...",
  "broker_order_id": "...",
  "figi": "...",
  "task": "runtime-main",
  "attempt": 1,
  "degraded": true,
  "error": {"type": "...", "message": "...", "stack": "..."}
}
```

Не все поля должны быть отдельными SQL columns: базовые индексируемые поля + JSONB context достаточно.

### P2.2. Нет retention/partition policy

`bot_logs` очищается только вручную целиком. TTL, partitioning и scheduled cleanup не найдены. Таблица будет расти; history filters по `ILIKE msg`/source со временем станут дорогими.

Нужны retention tiers, например:

- DEBUG 3–7 дней;
- INFO 14–30 дней;
- WARN/ERROR 90–180 дней;
- trading audit — согласно бизнес-требованиям, отдельно и дольше.

Добавить индекс по `(level, ts)`, `(component/event, ts)` и partition/cleanup job.

### P2.3. Collapse работает только в UI, но не в persistent queue

`LogHub.push()` объединяет повторения в `rep`, но `_log()` всё равно добавляет каждую исходную запись в `_log_persist_queue`. Поэтому БД может получить весь spam, тогда как UI показывает одну строку ×N. Persisted aggregation/sampling policy должна быть явной.

### P2.4. Нет redaction policy, а GET logs не защищены write token

Явного логирования токена в просмотренных местах не найдено. Но общий redaction filter для `Authorization`, tokens, passwords, DSN и account identifiers отсутствует. Generic exception/raw response со временем может принести чувствительные данные.

Текущий API token middleware защищает только mutating methods. `/api/v1/bot/logs`, `/logs/history`, `/state` и health details — GET и могут оставаться открытыми.

Нужно:

- централизованное redaction до всех sinks;
- запрет raw request headers/body по умолчанию;
- отдельная read/admin auth для diagnostic endpoints;
- аудит доступа к логам;
- тесты, что canary secrets не попадают в console/UI/DB/alerts.

### P2.5. Тестового контракта логирования почти нет

Не найдено целевого набора tests для:

- persist retry/no-loss;
- final shutdown flush;
- traceback preservation;
- task crash supervision;
- redaction;
- correlation propagation;
- rate limiting/recovery event;
- readiness degradation;
- runtime audit durability.

## 6. Места для исправления в первую очередь

### Trading/broker boundary

- `app/bot/live_broker.py`: silent parse/defaults, lot fallback, degraded API reads;
- `app/bot/runtime.py::_execute_pending`: не считать unknown broker result filled;
- `app/bot/runtime.py::_run/_startup`: полный traceback и terminal reason;
- reconciliation paths: durable mismatch/recovery events.

### Persistence and lifecycle

- `app/bot/runtime.py::_flush_persist`, `stop`;
- `app/services/loghub.py`, `app/logging_setup.py`;
- `app/bot/events.py` и `app/services/eventbus.py`;
- `app/main.py` startup/migrations/keepalive/shutdown.

### Background workers

- runtime child tasks;
- `ensemble_queue.py`: dispatcher broad exception сейчас просто sleep без лога;
- screener refresh worker: exception `pass`;
- analysis/screener/warehouse background tasks: supervisor/done callback должен извлекать exception;
- stream manager permanent-stop должен отражаться в readiness/alert.

### API

- request/correlation middleware;
- centralized unexpected exception handler;
- защищённые diagnostic endpoints;
- readiness/details вместо постоянного `ok`.

## 7. Безопасный план внедрения

### Шаг 0 — немедленно: broker truth

1. Убрать silent `None`/candle-price fallback для неизвестного исполнения.
2. Запретить close при неподтверждённом lot.
3. Ввести `PENDING_RECONCILIATION/UNKNOWN` и reconciliation по broker order id.
4. Добавить integration tests на malformed response, timeout, accepted-not-filled, partial fill и lot DB failure.

**Критерий:** ни одна transport/parse ошибка не создаёт локальный FILLED/Trade без подтверждения брокера.

### Шаг 1 — no-loss logs

1. Объединить runtime и standard logging через один structured pipeline.
2. Requeue/spool при DB failure.
3. Final flush на shutdown.
4. Счётчики queue/drop/failure и alert на потери.
5. Полный traceback для unexpected exceptions.

**Критерий:** fault-injection DB outage + restart не теряет ERROR/CRITICAL records; runtime сообщения видны и в stderr, и в durable history.

### Шаг 2 — task/health supervision

1. Все tasks именовать и регистрировать в supervisor.
2. Зафиксировать critical/restartable/best-effort policy.
3. Добавить liveness/readiness/details.
4. Alert на permanent task death и stale dependency.

**Критерий:** намеренное падение persist/reconcile/feed task переводит readiness в 503 и создаёт один actionable incident с traceback.

### Шаг 3 — durable trading audit

1. Append-only audit/outbox для order/fill/position/risk/config changes.
2. Сквозные IDs request→signal→decision→order→broker order→fill.
3. Устранить race sequence в EventBus и реализовать delivery semantics.
4. UI строить как projection, а не источник истины.

**Критерий:** после kill -9 можно восстановить полную последовательность торговых решений и фактических broker outcomes.

### Шаг 4 — hygiene/security/retention

1. Redaction filter и canary-secret tests.
2. Read/admin auth для logs/state/health details.
3. Retention/partition/index policy.
4. CI guard на новые broad silent handlers: allowlist только с явным комментарием и fallback metric.

## 8. Рекомендуемая политика обработки ошибок

| Ситуация | Поведение | Лог |
|---|---|---|
| Ожидаемый gate/business reject | продолжить | INFO/WARN, reason code, без stack |
| Временная network/API ошибка с безопасным fallback | degraded + retry | WARN, stack на первом fingerprint, counters/recovery |
| Неожиданная ошибка вычисления | изолировать операцию | ERROR с traceback и input identity |
| Неизвестный broker order result | fail closed + reconcile | ERROR/CRITICAL, durable incident |
| Нарушение state/schema/config invariant | не стартовать/остановить affected contour | CRITICAL с traceback |
| Ошибка best-effort UI decoration | fallback | DEBUG/WARN sampled, counter |
| Потеря observability sink | secondary spool + alert | CRITICAL вне сломанного sink |

## 9. Финальная оценка

Проект логирует много сообщений, но большое количество строк не равно диагностируемости. Сейчас основные слабости — это потеря exception chain, разные недолговечные sinks, отсутствие task/readiness supervision и смешение «нет данных» с «не удалось получить данные».

Самое важное — сначала исправить broker boundary и гарантировать, что ошибка наблюдаемости не меняет торговый факт. После этого строить единый no-loss structured logging pipeline. Массово заменять каждый `except` на `logger.exception` не следует: это создаст шум. Нужна классификация границ, stable event codes, корреляция, rate limiting, counters и явный degraded state.
