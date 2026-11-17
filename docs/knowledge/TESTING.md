# Проверки и CI

Этот раздел описывает существующие контуры. Во время документальной уборки новые прогоны не запускались: владелец сообщил, что тесты уже идут.

## Существующие контуры

| Контур | Назначение и условия |
|---|---|
| Hermetic | Основные тесты без `artifact` и `integration`; воспроизводимая база CI |
| Artifact | Требуют конкретных локальных frozen reports; отсутствие входа явно отражается |
| Integration | Требуют определённого окружения/данных; нельзя направлять на рабочую БД по умолчанию |
| Pipeline contract | Обязательный gate реального PostgreSQL в изолированной test-БД, без broker API |
| Engine golden | Закрепляет тайминг, исполнение и консервативные правила |
| Frontend | TypeScript и Vite build; успешная сборка не заменяет UI-проверку |

Источник: [ADR-0001](../adr/0001-hermetic-baseline-ci.md), `backend/pytest.ini`, `.github/workflows/ci.yml`. CI также проверяет compile, F/E9 на изменённых Python-файлах и `check_pipeline_contract.py`. Полный Ruff в существующем workflow информационный; не называть его блокирующим чистым lint.

## Справочные команды

Из backend, в рамках разрешённого scope:

```bash
~/.venvs/deeptrading/bin/python -m pytest -q -m "not artifact and not integration"
~/.venvs/deeptrading/bin/python -m pytest tests/test_engine_golden.py -q
~/.venvs/deeptrading/bin/python scripts/check_pipeline_contract.py
```

Последняя команда требует `PIPELINE_TEST_DATABASE_URL` для изолированной PostgreSQL-БД и проходит fail-closed проверки имени/хоста/драйвера. Подготовка или очистка БД не является частью чтения документа. Полные условия и коды возврата: [PIPELINE_TESTING](../PIPELINE_TESTING.md).

## Как понимать результат

Указывать версию кода, точную команду, входы, passed/failed/skipped, длительность, окружение и ограничения. Не суммировать пересекающиеся группы и не считать старые «900/988 passed» текущим результатом. Обязательный pipeline-кейс с skip/xfail или нулевая коллекция — не успешная приёмка. Ошибку реализации не списывать на историческую запись «это ожидаемый флак».

Последний ограниченный refactor-run: 33 passed, затем 12 passed, после последней правки 7 passed; группы пересекаются. TypeScript/Vite и проверки новых модулей прошли по [сохранённому отчёту](../Audit/LARGE_CODE_REFACTOR_2026-10-15.md). Полный suite и реальная задержка БД не проверялись этим этапом.

Новый код engine требует golden-покрытия. Не добавлять и не запускать дополнительные тесты вне запроса/назначения; текущая уборка документации не меняет тестовый scope.


### Isolated PostgreSQL broker journal (ARCH369)
`tests/test_broker_order_journal_postgres.py` launches its own temporary PostgreSQL cluster when `DEEPTRADING_TEST_PG_BIN` explicitly points to PostgreSQL binaries; absent setting skips. No application DB URL accepted. On .8: `set DEEPTRADING_TEST_PG_BIN=C:\pgsql\bin` then `.venv\Scripts\python.exe -m pytest tests/test_broker_order_journal_postgres.py -q --tb=short`. Five tests cover real0006 guard/application, concurrent scoped reservation, cleanserverrestart, cumulative observation monotonicity. Not crash-recovery/atomicfill-accounting/runtime integration acceptance. Temporary instance stopped in fixture cleanup. Windows server output must go to a file, not inherited capture pipes.


### Close adapter with real isolated PostgreSQL (ARCH383)
`tests/test_live_close_postgres_acceptance.py` uses the same explicit DEEPTRADING_TEST_PG_BIN fixture as journal tests. Runs actualcloseadapter/journal against actual0006 tables, withfake SDKresponse only. Independentconnection verifies committedintentbeforepost;7cases coverfull/partial/NEW/wrongrequest,timeoutrecreation,atomicwritefailure andunknownentrytime. No productionDB URL accepted. Not realbroker/currentportfolio or fullruntime acceptance.
