# Тестирование контракта пайплайна (B3)

Сквозной контур: **источник → проверка/запись → PostgreSQL → CandleHub → A7/A24 → сводка**.
Цель — найденные классы ошибок ловятся автоматически, включая взаимодействие частей.

## Одна команда

```bash
cd backend
export PIPELINE_TEST_DATABASE_URL="postgresql+asyncpg://deeptrading:deeptrading@127.0.0.1:5432/deeptrading_test"
~/.venvs/deeptrading/bin/python scripts/check_pipeline_contract.py    # или backend/.venv/bin/python
```

Код возврата:

| код | значение |
|---|---|
| 0 | контракт выполнен (нет падений/skip/xfail, коллекция непустая) |
| 1 | есть падения |
| 2 | отсутствует/непригоден обязательный PostgreSQL-контур |
| 3 | нулевая коллекция |
| 4 | обязательный кейс пропущен (skip/xfail) — не зелёный результат |

## Подготовка изолированной тестовой БД (без секретов)

```bash
# Локально (PostgreSQL 16 уже поднят; рабочая БД deeptrading не трогается)
PGPASSWORD=deeptrading createdb -h 127.0.0.1 -U deeptrading deeptrading_test
```

Правила контура (fail-closed, `backend/tests/fixtures/pipeline_contract/db.py`):

- адрес тестовой БД берётся **только** из `PIPELINE_TEST_DATABASE_URL`; production `.env`
  для выбора тестовой БД не читается;
- имя БД обязано содержать `test` и **не** совпадать с рабочей (`deeptrading`);
- драйвер — PostgreSQL (asyncpg): настоящий PostgreSQL 16, не мок и не SQLite;
- host обязан входить в **явный allowlist** тестовых хостов (`127.0.0.1`, `localhost`,
  `::1` либо явно добавленный через `PIPELINE_TEST_DB_ALLOWED_HOST`); чужой host → отказ;
- адрес валидируется повторно на **границе destructive-операций** (`prepare_schema`,
  `truncate`): сессия, привязанная не к тестовой БД, не может создать/очистить таблицы;
- таблицы создаются/чистятся **только** в тестовой БД;
- нет контура → gate падает (код 2), а не пропускается зелёным.

## Что запускает gate

Единый гейт системной регрессии (источник → запись → CandleHub → исполнение):

- `tests/test_pipeline_contract_e2e.py` — сквозной контур (корректный пакет, повтор пакета,
  конфликт дублей, ошибка записи и отсутствие преждевременного hub update, провал invalidate,
  пустой источник, пропуск до входа/после входа/после завершённого выхода, невалидные числа,
  неизвестный net, причинность, повторяемость);
- `tests/test_ingestion_contract.py` — контракт границы записи (B1, юнит);
- `tests/test_ingestion_postgres.py` — реальные транзакции записи в тестовой БД (B1);
- `tests/test_pipeline_contract_e2e.py` (секция R2) — сквозные регрессии оркестрации:
  пустой кэш дыр с восстановлением, отказ чужой строке (figi/interval/окно), владение
  транзакцией при провале до commit, recovery hub из БД при недоступном источнике,
  `committed` = принятые уникальные строки;
- `tests/test_research_execution_contract.py` — контракт исполнения A7/A24 (B2);
- существующие golden: `tests/test_engine_golden.py`, `tests/test_candlehub.py`, `tests/test_candle_integrity.py`.

Внешняя сеть мокается (stub `fetch_candles`), собственная логика приложения не
дублируется тестовым алгоритмом. Торговый API, токены, счета и рабочая БД не используются.

Тест-файлы B1/B2 включены в гейт намеренно: это и есть контракт пайплайна.
Удаление/переименование любого из них роняет гейт (fail-closed), а не проходит молча.

## Как подключиться владельцу данных (B1)

```bash
export PIPELINE_TEST_DATABASE_URL="postgresql+asyncpg://deeptrading:deeptrading@127.0.0.1:5432/deeptrading_test"
backend/.venv/bin/python -m pytest -q -o addopts= tests/test_pipeline_contract_e2e.py   # напрямую
# либо целиком через gate:
backend/.venv/bin/python backend/scripts/check_pipeline_contract.py
```

Прямой `pytest` без `PIPELINE_CONTRACT=1` пропускает DB-тесты (гейт выставляет флаг сам) —
так существующий hermetic-suite (`-m "not artifact and not integration"`) не ломается.

## R2: регрессии оркестрации через реальный fill_gaps (B1)

Добавлены сквозные тесты (реальный `fill_gaps_detailed` / `upsert_candles_detailed`, а не
изолированный `validate_row`), фиксирующие 4 класса дефектов R2. На коде после R1 они
**падают** — это подтверждение дефекта; адресованы B1. После CODE_READY_R2 B1 обязаны
проходить без ослабления теста:

| тест | что доказывает |
|---|---|
| `test_late_candle_recovered_after_empty_source` | пустой ответ источника не блокирует поздно пришедшую свечу навсегда (нет бессрочного `_empty_gap_cache`); восстановление на том же экземпляре |
| `test_foreign_source_row_rejected_by_real_fill_gaps[other_figi\|wrong_interval\|outside_window]` | строка чужого figi / не того interval / вне окна запроса отклоняется: без записи в БД и без публикации чужой свечи в hub X |
| `test_failed_upsert_does_not_persist_when_session_reused` | при провале до commit (ошибка invalidate после INSERT) неуспешный пакет не сохраняется при переиспользовании сессии; тест НЕ откатывает транзакцию «за код» |
| `test_hub_recovered_from_db_when_source_unavailable` | после провала публикации при недоступном источнике hub восстанавливается из БД (committed-данные), а не ждёт нового ответа API |
| `test_committed_counts_accepted_unique_rows` | `committed` = принятые уникальные строки, а не `len(raw rows)` |

Guard контура (герметично, без БД): `test_contour_fail_closed`, `test_contour_allowed_host_env`,
`test_prepare_schema_validates_url_before_connect`, `test_truncate_guard_rejects_non_test_session`
— отказ чужому host и destructive-операции на неподходящей сессии. Реальные негативные
подключения/удаления не выполняются: валидация идёт до открытия соединения.

Закрытие R2 (B1): проверка принадлежности — `cic.assert_request_scope` до записи/публикации
(структурированный отказ в `scope_rejections`); дедуп пустых дыр — внутри одной операции;
`upsert_candles_detailed` владеет rollback; `_recover_hub_from_db` на повторе; `committed`
по принятым уникальным строкам. Координированный прогон R2: **219 passed, exit 0**
(герметичный suite — см. ниже).

## CI

Job `pipeline-contract` в `.github/workflows/ci.yml`: сервис `postgres:16`,
отдельная БД `deeptrading_test`, `PIPELINE_TEST_DATABASE_URL` в env, запуск
`python scripts/check_pipeline_contract.py`. Без `continue-on-error` и без
исключения новых тестов через `artifact`/`integration`.

## Интегрированный прогон (после CODE_READY B1/B2, раунд R1)

- общий гейт: `203 passed`, exit 0 (тест-БД `deeptrading_test`);
- герметичный suite `-m "not artifact and not integration"`: `1311 passed`,
  `38 skipped`, `77 deselected`; единственное падение —
  `tests/test_news_observer.py::test_shadow_c_coverage_and_veto` (файл **untracked** —
  дефект продукта, чинит B2 в R2; untracked ≠ отсутствие дефекта, из поставки тест не исключается).

Координатный прогон R2 (после CODE_READY_R2 обоих): единый гейт — **219 passed,
0 failed, 0 skipped, exit 0**; герметичный suite `-m "not artifact and not integration"` —
**1318 passed, 51 skipped, 77 deselected, 0 failed, exit 0** (красный news-тест закрыт B2).

## История дефектов пути записи (закрыто владельцем B1)

Контрактные тесты фиксировали дефекты пути записи (файлы зоны B1:
`app/services/tinvest.py`, `app/services/candle_hub_orchestrator.py`). Все четыре
класса закрыты B1 (CODE_READY), проверено на реальном PostgreSQL:

1. `upsert_candles` писал OHLCV без проверки — NaN/Inf, отрицательные цены/объём и
   `high < low` попадали в БД; теперь отказ до записи
   (`test_invalid_numeric_rejected_at_write_boundary`, 4/4 green).
2. Конфликтующие дубли в пакете принимались молча (last-write); теперь явный отказ
   (`test_conflicting_duplicate_rejected_explicitly`).
3. Ошибка `candle_integrity.invalidate` подавлялась (`except Exception: pass`); теперь
   не подавляется (`test_invalidate_failure_is_not_swallowed`).
4. `fill_gaps` публиковал в CandleHub **до** сохранения в БД; теперь публикация —
   только после успешного сохранения (`test_write_error_does_not_publish_to_hub`).
