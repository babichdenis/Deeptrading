# Повторная проверка logging / failure semantics — 2026-10-04

## Scope

- Репозиторий: `babichdenis/Deeptrading`
- Проверенный `master`: `5bb7024a538a3a8f8f503c92424bdbb85cd649f1`
- Предыдущая контрольная revision: `450d7cc`
- Diff: 44 файла, +3705/−136; audit-правки находятся прежде всего в `5241edf`, `4f876e7`, `54c8fb3`, `5bb7024`.

## Итог

Логирование и observability **существенно улучшены, но исправление нельзя считать завершённым**. Устранён непосредственный crash из-за отсутствующего `LiveBroker._log`, исправлена прежняя двойная постановка persist batch в очередь и добавлены полезные счётчики. Однако P0-сценарий неизвестного результата ордера пока не имеет reconciliation-механизма, а его новый guard можно обойти последующей записью нового ордера в тот же `pending_orders[figi]`. Structured `request_id` теряется до DB flush. EventLog не предоставляет заявленную строгую durable/outbox semantics.

**Вердикт:** частично исправлено; перед production live trading остаются P0/P1 работы.

## Статус замечаний

| Область | Статус | Результат |
|---|---|---|
| `LiveBroker._log` | **Исправлено** | Метод появился; ветки unknown fill больше не падают с `AttributeError`. |
| Unknown-result duplicate submission | **Частично / P0 open** | `_execute_pending` блокирует повторное исполнение `PENDING_RECONCILIATION`, но автоматической сверки и terminal resolution нет. Более того, обработка свечи продолжает стратегию, а `_submit_order` защищает только `PENDING_APPROVAL`, поэтому новый сигнал может перезаписать unresolved order в `pending_orders[figi]`. |
| Persist double-requeue | **Исправлено** | Candle batch и log batch возвращаются ровно по одному разу. Добавлены `persist_failures`, `persist_dropped`, `persist_queue_depth`; overflow `deque(maxlen=...)` учитывается. |
| Persist no-loss guarantee | **Улучшено, но не строгое** | Потери при overflow теперь видимы, но всё ещё возможны по дизайну bounded in-memory queue и при process crash. |
| Correlation ID | **Частично / P1 open** | Middleware и префикс сообщения работают. Но `_log_persist_queue` хранит 4-tuple без RID, а flusher читает ContextVar фоновой task; поэтому структурное поле `bot_logs.request_id` обычно `NULL`. |
| Redaction | **Частично / P1 open** | Есть redaction в runtime `_log` и `HubHandler`, но нет обязательного sink-level redaction в `hub.push`, EventLog/event bus/outbox и иных прямых путях. |
| `source` width | **Исправлено** | ORM, runtime DDL, API limit/slicing и migration `0002` согласованы на 64; Alembic включён в core requirements. |
| Task supervision | **Частично** | Registry фиксирует exception и `TASK_DIED`, но не restart/controlled shutdown. Нормальное преждевременное завершение task не считается ошибкой. Main task и app-level retention task находятся вне этой политики. Старые `_task_errors` не очищаются. |
| Liveness/readiness | **Улучшено, но semantics неполные** | `/api/live` и `/api/ready` добавлены. Readiness может навсегда остаться 503 после transient task failure; исчезнувшая без exception task может не сделать readiness красным. |
| EventLog durability | **Не завершено / P1** | JSONL заменён DB publish, но publish запускается fire-and-forget. Callback только извлекает exception, не логирует и не повторяет. Без running loop событие остаётся лишь в RAM. Это не transactional/durable outbox. |
| Event sequence | **P1 open** | `MAX(sequence)+1` допускает гонку между concurrent publishers; нужен DB sequence/identity либо иной atomic allocator. |
| Retention | **Базово исправлено** | TTL 30 дней и cleanup каждые 6 часов добавлены. Один большой PostgreSQL-specific `DELETE` может создавать длительные locks/WAL; нет batch cleanup/partitioning и supervision метрик. |
| LiveBroker degraded metadata | **Улучшено, но stale** | Ошибки margin/prices/status/session видны в `/bot/status`. Маркеры не очищаются после успеха и не имеют TTL/`last_success`, поэтому историческая ошибка выглядит как текущее состояние. |
| CI | **Красный** | Test job успешен, но `ruff-changed` упал. Локально точный diff `450d7cc..HEAD` даёт 11 ошибок, включая `F821 Undefined name ctx` в `app/services/ensemble.py:175`. |

## Ключевые технические наблюдения

### 1. P0 reconciliation всё ещё не гарантирует exactly-once

`runtime.py:6551` возвращает unresolved order в `pending_orders` и не отправляет его повторно. Это правильная локальная защита. Но в `_process_candle` результат `_execute_pending` присваивается `_did_execute` и далее нигде не используется (`runtime.py:5457`). Обработка сигнала продолжается. В `_submit_order` дедуп проверяет только статус `PENDING_APPROVAL` (`runtime.py:6507`), после чего безусловно делает `self.pending_orders[figi] = order` (`runtime.py:6541`).

Следствие: unresolved order может быть вытеснен новым order object; после этого guard больше не видит исходный `PENDING_RECONCILIATION`. Риск duplicate exposure остаётся.

Нужны:

1. durable order-attempt ledger с стабильным idempotency key;
2. broker lookup/reconciliation по `order_id`/client key после timeout;
3. терминальные состояния `FILLED`, `REJECTED`, `CANCELLED`, `NOT_FOUND_AFTER_WINDOW`, а не вечный pending;
4. запрет любого нового open/close по инструменту, пока unresolved attempt не разрешён;
5. отдельные timeout/escalation metrics и alert;
6. crash/restart tests, включая timeout → restart → reconciliation.

### 2. Structured request ID теряется

На enqueue `_log` читает RID для префикса, но складывает `(level, source, msg, ts)` (`runtime.py:1894`). Позже `_flush_persist_once` читает `_request_id_ctx` уже внутри фоновой persist task (`runtime.py:5079`), чей context обычно не соответствует task, создавшей запись.

Исправление: enqueue immutable record `(level, source, msg, ts, request_id)` и не читать ContextVar повторно во flusher. Аналогично переносить correlation/trace IDs в event payload на producer boundary.

### 3. EventLog пока не durable outbox

`EventLog._persist` создаёт background task и callback `lambda t: t.exception()`; ошибка извлекается, но теряется. При отсутствии event loop метод молча возвращает. Сначала нет durable commit, а значит процесс может завершиться между append в RAM и DB commit.

Рекомендуется один из вариантов:

- синхронно/await записывать событие и business mutation в одной DB transaction, затем отдельный dispatcher доставляет undispatched rows;
- либо bounded queue с owned worker, retry/backoff, drain-on-shutdown, overflow counter и readiness/degraded policy.

Для sequence использовать PostgreSQL sequence/identity/unique constraint с retry, но не `SELECT MAX()+1`.

### 4. Централизовать sanitization

Redaction должна выполняться на последней общей границе каждого sink, а producer-side redaction — только defense in depth. Ввести общий `sanitize_log_record()` и применять перед:

- ring/WebSocket hub;
- `bot_logs` insert;
- EventLog/event bus persistence;
- console/file handlers;
- serialization exception payloads и degraded metadata.

Добавить table-driven tests для token/password/Authorization/DSN/query-string, mixed case и nested dict/list payload.

### 5. Supervision и health

Task registry должен хранить desired state, считать ошибкой как exception, так и неожиданное нормальное завершение, и иметь явную policy: restart с bounded backoff либо fail runtime. Readiness должна вычисляться из текущего состояния (`required tasks alive`, DB/broker freshness, queue lag), а history ошибок публиковаться отдельно. После успешного recovery error/degraded state следует закрывать/очищать с `last_failure`, `last_success`, count.

## Проверки

В чистом clone на Python 3.13 выполнено:

```text
python -m compileall -q app scripts                         PASS
pytest -q tests/test_logging_audit_fixes.py                 3 passed
pytest -q -m "not artifact and not integration"            1097 passed, 77 deselected
ruff F/E9 по файлам последнего commit cffe223..5bb7024      PASS
ruff F/E9 по push diff 450d7cc..5bb7024                    FAIL: 11 errors
git diff --check 450d7cc..HEAD                              PASS
```

Targeted tests подтверждают unknown-fill/unknown-lot и присвоение `PENDING_RECONCILIATION`, но **не** покрывают: реальную reconciliation, overwrite unresolved order новым сигналом, persist fault injection/overflow, структурный RID во время background flush, EventLog publish failure, restart semantics и recovery readiness.

CI run `37193252625` на `5bb7024`: основной `test` job прошёл; `ruff-changed` завершился exit 123 (`xargs` агрегирует ненулевой exit Ruff). Локальная проверка полного push diff воспроизвела 11 diagnostics: 7 unused imports/variables и, важнее, `F821 ctx`, плюс остальные F841/F401. Сам механизм changed-files CI теперь действительно блокирует ошибки, то есть прежняя проблема пустого diff исправлена.

## Приоритетный план

### P0 — до live production

1. Закрыть order reconciliation end-to-end и устранить overwrite unresolved attempts.
2. Сделать попытки ордера durable и idempotent across restart.
3. Добавить integration/fault tests timeout-before-response, response-loss, restart и concurrent signal.
4. Исправить CI, прежде всего `F821 ctx`; не merge/deploy с красным required check.

### P1 — следующий короткий цикл

1. Переносить `request_id` в persist record при enqueue.
2. Реализовать настоящий owned durable event pipeline и атомарный sequence.
3. Централизовать redaction на sink boundaries.
4. Довести task supervision и readiness до current-state semantics; очищать stale errors/degraded markers после подтверждённого recovery.
5. Добавить тест persist failure: ровно один requeue, ordering, overflow accounting, success after retry.

### P2 — эксплуатационная устойчивость

1. Batch/partition retention, duration/deleted/error metrics и alerting.
2. Queue age/oldest-item, retry count, last successful flush и saturation alerts, а не только depth/count.
3. Явно разделить current degradation и incident history в API.
4. Обновить GitHub Actions versions из-за предупреждения Node.js 20 deprecation.
