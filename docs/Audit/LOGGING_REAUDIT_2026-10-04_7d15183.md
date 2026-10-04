# Новый аудит изменений — master `7d15183` — 2026-10-04

## Scope

- Репозиторий: `babichdenis/Deeptrading`
- Текущий `master`: `7d1518365b65dc6dc786f53cf1aadcd9bc2a6e0d`
- Предыдущая проверенная revision: `5bb7024a538a3a8f8f503c92424bdbb85cd649f1`
- Новые commits: `39773cf`, `7d15183`
- Diff: 12 файлов, +425/−850.

## Executive summary

Из четырёх конкретных дефектов предыдущего recheck три исправлены корректно на локальном уровне:

1. `request_id` теперь фиксируется при enqueue и доходит до DB payload;
2. unresolved order больше нельзя вытеснить новым order через `_submit_order` в пределах живого процесса;
3. stale `LiveBroker.degraded` marker очищается после успешного вызова.

`F821 ctx` также исправлен.

Но **P0 reconciliation всё ещё не завершён end-to-end**: pending state остаётся in-memory, нет broker lookup/reconciliation worker, terminal resolution и восстановления после restart. Новый guard закрывает обнаруженный overwrite-path только пока жив текущий runtime.

Кроме того, текущий `master` нельзя считать release-ready: GitHub Actions красный, локально воспроизводятся один failing test и четыре Ruff diagnostics для последнего push. Новый INFO-лог каждого gate rejection создаёт риск очень большого объёма логов в replay/live hot path.

**Вердикт:** исправления аудита полезны и в основном направлены верно, но статус — **частично исправлено; CI red; P0 reconciliation open**.

## Delta предыдущих замечаний

| Область | Новый статус | Проверка |
|---|---|---|
| `LiveBroker._log` | Исправлено ранее | Регрессии не обнаружено. |
| Overwrite `PENDING_RECONCILIATION` | **Исправлен локальный путь** | `_submit_order` теперь первым делом блокирует любой open/close по FIGI с unresolved order; добавлен тест. |
| Полная reconciliation/idempotency | **P0 open** | Нет consumer/worker для `PENDING_RECONCILIATION`, durable attempt ledger и восстановления pending state после process restart. |
| Structured `request_id` | **Исправлено для runtime bot_logs** | Persist queue стала 5-tuple `(level, source, msg, ts, rid)`; flusher использует сохранённый RID, а не ContextVar фоновой task. |
| Stale broker degraded marker | **Исправлено базово** | `_mark_ok(component)` вызывается после успешных margin/prices/status/session requests. |
| `F821 ctx` | Исправлено | `_filter_sparse_signals(..., ctx=None)` и call site обновлены; regression test проходит. |
| Persist double-requeue/counters | Исправлено ранее | Новые commits не меняют эту логику. |
| EventLog durability | **Open** | Fire-and-forget publish, silent failure/no retry и неатомарный sequence остались без изменений. |
| Redaction | **Open** | Единого sink-level enforcement по-прежнему нет. |
| Task supervision/readiness | **Open** | Нет restart/fail-runtime policy, current-vs-history semantics и очистки `_task_errors`. |
| CI | **Fail** | `ruff-changed` и `test` jobs текущего HEAD красные. |

## Findings

### P0-1. Reconciliation guard улучшен, но exactly-once после restart не обеспечен

Новая проверка в начале `_submit_order` правильно запрещает любой новый open/close, если `pending_orders[figi].status == "PENDING_RECONCILIATION"`. Это закрывает конкретную возможность вытеснить unresolved order присваиванием `self.pending_orders[figi] = order`.

Однако `pending_orders` остаётся in-memory. После рестарта процесса запись исчезнет, и новый runtime снова сможет отправить ордер, не установив результат предыдущей попытки. Поиск по коду не обнаружил отдельного consumer/worker, который:

- запрашивает состояние попытки у брокера по стабильному client order/idempotency key;
- разрешает `FILLED/REJECTED/CANCELLED/NOT_FOUND_AFTER_WINDOW`;
- восстанавливает unresolved attempts из БД;
- блокирует инструмент across restart.

Поэтому P0 следует считать не закрытым, а уменьшенным по поверхности риска.

**Рекомендация:** durable `order_attempts` table + unique idempotency key + broker reconciliation loop + startup recovery + timeout/escalation alert. Тестировать timeout → process crash → restart → broker says filled/not found.

### P1-1. `None` у PaperBroker имеет неоднозначную семантику

`PaperBroker.open_position` теперь возвращает фактическую цену — это исправляет ложный `PENDING_RECONCILIATION` при нормальном paper fill. Но `None` возвращается и при уже существующей позиции, и при отсутствующем account. Runtime трактует `None` как «результат неизвестен».

Для paper broker эти случаи детерминированы и должны быть `REJECTED/ERROR`, а не reconciliation. Универсальный контракт `float | None` смешивает:

- подтверждённый fill;
- бизнес-отказ;
- локальную ошибку состояния;
- неизвестный результат внешнего брокера.

**Рекомендация:** typed `OrderExecutionResult(status, fill_price, broker_order_id, error)`; `UNKNOWN` разрешать только адаптеру live broker после реально неоднозначного network outcome.

### P1-2. Текущий master имеет failing test

Локальный baseline:

```text
1 failed, 1099 passed, 77 deselected
```

Падает `tests/test_replay_analytics.py::test_mode_writes_sidecar_only_with_preset`. Тест всё ещё требует, чтобы запуск без preset возвращал `False` и не создавал sidecar, тогда как новая функциональность намеренно создаёт fact sidecar и возвращает `True`.

Это выглядит как не обновлённый контрактный тест, а не случайный flaky failure. До merge/release следует обновить тест одновременно с поведением и добавить проверки содержимого fact preset.

### P1-3. `ruff-changed` снова красный

Для файлов последнего commit `39773cf..7d15183` локально воспроизведены четыре diagnostics:

- `app/api/routes/bot.py:1788` — unused exception variable `e`;
- `app/bot/paper_broker.py:75` — unused `asyncio`;
- `app/bot/paper_broker.py:262` — unused `asyncio`;
- `scripts/signal_lab_hours_check.py:68` — unused `fams`.

Для полного diff `5bb7024..HEAD` остаётся восемь F401/F841 diagnostics. `F821 ctx` действительно устранён, то есть критическая ошибка исправлена, но required changed-files check остаётся красным.

### P1-4. INFO на каждый gate rejection может затопить hot path

`run_gate_chain` теперь пишет `GATE FAIL` на INFO для каждого первого отказа. Эта функция вызывается в торговом/replay hot path. На сотнях тысяч свечей это может дать огромный stdout/ring volume, дополнительную сериализацию и вытеснение более важных сообщений. Кроме того, runtime уже имеет `_reject_entry`/trace paths, поэтому возможна смысловая дубляция.

**Рекомендация:** structured counter по `(gate,key,side)`, sampled/rate-limited log, периодический summary; per-event logging оставить DEBUG и включать диагностически. Проверить redaction/high-cardinality для `detail`.

### P1-5. Fact sidecar формируется неатомарно с запуском

`_save_test_sidecar` пишет обычным `Path.write_text` без temp-file + atomic rename. Crash/конкурентный restart может оставить частичный JSON, после чего `load_sidecar` молча вернёт `None`. Ошибки ловятся только как `OSError`; serialization/type errors выйдут наружу.

Для audit provenance лучше писать `tmp`, `fsync` при необходимости и `os.replace`, а также сохранять schema version, config hash и immutable run ID.

### P1-6. `PaperBroker.reset` удаляет глобально все paper rows

Новый reset выполняет unscoped `DELETE PaperPosition` и `DELETE PaperTrade`. Сейчас код в основном использует `DEFAULT_ACCOUNT`, но схема содержит `account_id` и допускает несколько accounts. При параллельных тестах/runtimes один reset уничтожит состояние остальных. DB `ON DELETE CASCADE` уже связывает rows с account.

**Рекомендация:** удалять/reset только выбранный `account_id`/run scope; запретить concurrent reuse default account либо выделять account/run ID каждому тесту.

### P2-1. Удалены предыдущие audit documents

Из репозитория удалены `LOGGING_AUDIT_2026-10-03.md` и `PROJECT_AUDIT_2026-10-03.md`, вместо них добавлен recheck. Git сохраняет историю, но operational audit trail становится менее доступным на текущей ветке.

Лучше архивировать superseded reports с явной пометкой, а не удалять, либо поддерживать индекс аудитов с commit SHA и статусом каждого finding.

## Позитивные изменения

- RID переносится на producer boundary — это правильная async correlation semantics.
- Guard расположен до тяжёлых gates и broker calls — блокировка быстрая.
- Для guard, degraded recovery и `ctx` добавлены targeted tests.
- `_mark_ok` вызывается только после успешного внешнего call.
- PaperBroker теперь возвращает реальный fill с учётом cost model.
- Replay исключает живой order book из historical gate, что устраняет look-ahead/current-data contamination.
- Валидация запрещает неявный fallback engine/timeframe при API-запуске без preset.

## Локальная верификация

Чистый clone HEAD `7d15183`, Python 3.13, зависимости из `requirements-dev.txt`:

```text
python -m compileall -q app scripts                         PASS
pytest -q tests/test_logging_audit_fixes.py                 6 passed
pytest -q -m "not artifact and not integration"            1 failed, 1099 passed, 77 deselected
ruff F/E9, latest commit files                             FAIL, 4 diagnostics
ruff F/E9, full 5bb7024..HEAD diff                         FAIL, 8 diagnostics
git diff --check 5bb7024..HEAD                              PASS
```

GitHub Actions run `37205612772`:

- `frontend`: success;
- `lint`: success;
- `ruff-changed`: failure;
- `test`: failure.

## Приоритетный план

### P0

1. Реализовать durable reconciliation across restart, а не только in-memory guard.
2. Ввести stable idempotency/client order key и startup recovery.
3. Добавить fault tests на lost response, crash/restart и concurrent signal.

### P1

1. Починить failing replay analytics test и четыре Ruff diagnostics; не выпускать с red CI.
2. Заменить `float | None` broker contract на typed execution result.
3. Rate-limit/aggregate `GATE FAIL`, per-event оставить DEBUG.
4. Сделать sidecar atomic/versioned и привязать к immutable run ID/config hash.
5. Scope `PaperBroker.reset` по account/run.
6. Завершить прежние open items: durable EventLog/outbox, atomic sequence, sink-level redaction, task supervision/readiness recovery.

### P2

1. Вернуть индекс и историю audit reports.
2. Добавить тест structured RID именно через enqueue → background flush → DB row.
3. Добавить observability для reconciliation age, unresolved count, oldest attempt и retry outcomes.

---

## Status 2026-10-04 (исполнение P1 этой сессией)

| Пункт | Статус | Что сделано |
|---|---|---|
| P1.1 Ruff diagnostics | done | bot.py (unused `e` → `_record`), paper_broker.py (2× unused asyncio), signal_lab_hours_check.py (unused fams/e/tf/period/hour → `_`); per-file сравнение с HEAD: diagnostics не хуже baseline |
| P1.1 failing replay analytics test | done | контрактный тест переписан под fact-пресет (sidecar теперь пишется всегда): `test_mode_writes_sidecar_fact_preset_without_preset`; test_replay_analytics 18 passed |
| P1.1 typed execution result | частично | `app/bot/execution.py`: `_open_long/_open_short` возвращают `ExecutionResult(ok, reason, price)` — цена входа берётся из факта PaperBroker/LiveBroker, а не intent; полный отказ от `float | None` в контрактах брокеров — в бэклоге |
| P1.3 GATE FAIL rate-limit | done | `app/bot/gates.py`: дедуп-окно `_GATE_LOG_DEDUP_TTL` (одно INFO на (gate, reason, side) в окно), счётчик suppressed → агрегат при восстановлении, per-event события — DEBUG |
| P1.4 sidecar atomic | done | `preset_tags.save_sidecar`: tmp-файл + `os.replace` (атомарно на той же ФС), tmp-левовер не остаётся при ошибке сериализации |
| P1.5 PaperBroker.reset scope | done | `reset` чистит счёт + позиции + сделки + order-id последовательность (раньше позиции/сделки копились между прогонами и ели маржу); вызов из `/bot/mode` — на каждый старт тест-прогона |

Верификация: `pytest tests/test_gates.py tests/test_execution_order_intent.py tests/test_logging_audit_fixes.py tests/test_replay_analytics.py -q` → 65 passed; `ruff check` по изменённым файлам — не хуже HEAD.

Остаются открытыми: typed-контракты брокеров целиком (P1.2), прежние durable-пункты (P0-блок) и P2-блок.
