# Deeptrading — повторный аудит `master`

Дата аудита: 2026-10-01

- Проверенный commit: `406710adc5e7f68ddb226306da8611b16c5fcfbd`
- Предыдущий аудит: `e2956d5021e9efe6774a59d33ab3bf56265b02bf`
- Изменения: 41 новый commit, 69 файлов, примерно `+4837 / -684` строк

## 1. Итог

Проект заметно улучшен после аудита 30 сентября: исправлен syntax error, CI подключён к `master`, зависимости `pandas/joblib` внесены в requirements, удалён duplicate `/heatmap`, добавлен route uniqueness guard, тесты начали делиться на hermetic/artifact/integration, временные файлы очищены, таймфреймы сведены к START-конвенции, появились reference run и контракт state snapshot.

Но P0 ещё не закрыт: **актуальный CI красный**, а заявленный hermetic baseline на чистом checkout всё ещё не hermetic. Кроме того, новый тестовый Universe v2 содержит три существенных риска: последовательную загрузку всей истории всех инструментов, потерю реального lot size и неоднозначную границу `as_of` при START-свечах.

Рекомендация: не расширять Universe v2 и replay analytics до устранения этих P0/P1. После этого продолжать controlled shadow/replay migration.

## 2. Статус находок предыдущего аудита

| Предыдущая находка | Статус | Комментарий |
|---|---|---|
| CI не запускается для `master` | Частично закрыто | Trigger исправлен, но текущий run #34 завершился failure |
| `trend_day_sim.py` не компилируется | Закрыто | `compileall app scripts` проходит |
| Нет `pandas/joblib` в requirements | Закрыто частично | Добавлены, но установка private dependency ломает CI |
| Artifact tests смешаны с baseline | Частично закрыто | Маркеры введены, но помечены не все файлы |
| Duplicate `/api/v1/bot/heatmap` | Закрыто | Дубль удалён, добавлен route uniqueness test |
| Temp/results смешаны с исходниками | Улучшено | `_tmp_sess_check.py`, `tmp_check_mtf.py`, `.old` удалены; старые `results_*.json` ещё tracked |
| Документы состояния конфликтуют | Улучшено | Добавлены ADR и архивные метки, ROADMAP упорядочен |
| Universe 2.0 не подключён | Частично закрыто | Подключён только к test/replay через `UNIVERSE_MODE` |
| Монолиты | Не закрыто | `runtime.py` и `main.ts` выросли ещё сильнее |
| График — residual refresh/performance | Не затронуто | Новые изменения в основном Analytics/Universe/engine |

## 3. Проверки

### Успешно

- `python -m compileall -q app scripts` — успешно.
- `npm ci && npm run build` — успешно.
- Frontend bundle:
  - JS: 341.00 KB, gzip 110.19 KB;
  - CSS: 63.24 KB, gzip 12.49 KB.
- Точечный новый набор route/aggregator/reference/state/preset/replay analytics:
  - `27 passed, 1 skipped`.
- Duplicate FastAPI route внутри одного path/method не обнаружен.

### Неуспешно

Локальный запуск той же pytest-команды, которая записана в CI:

```bash
pytest -q --tb=short -m "not artifact and not integration"
```

на чистом checkout дал:

- `904 passed`;
- `55 deselected`;
- `3 failed`;
- `16 errors`.

Ошибки в основном снова вызваны отсутствующими файлами `backend/reports`, потому что не все artifact-тесты получили marker.

Официальный GitHub Actions run #34 для commit `406710a` также красный. Lint/compile job зелёный, test job падает уже на шаге `Install deps`; тесты даже не запускаются.

## 4. P0 — что исправить немедленно

### P0.1. CI красный на установке private dependency

`requirements.txt` содержит private index:

```text
--extra-index-url https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple
...
t-tech-investments
```

На публичном PyPI пакета нет; на T-Bank index доступна версия `1.51.0`. В текущем GitHub Actions шаг `pip install -r requirements-dev.txt` завершается exit code 1. Локально индекс также требовал отдельной обработки доверия к сертификату.

Что сделать:

1. Зафиксировать версию `t-tech-investments==1.51.0`.
2. Исправить доверенную цепочку сертификатов private index, а не полагаться на `--trusted-host` как постоянное решение.
3. Разделить broker SDK и core dependencies:
   - `requirements-core.txt` — engine/API/test без реального broker connector;
   - `requirements-broker.txt` — T-Tech SDK;
   - production устанавливает оба;
   - hermetic CI либо устанавливает проверенный wheel, либо тестирует connector отдельным job.
4. Добавить lock/constraints с hashes, чтобы новый релиз private package не менял baseline без commit.

Пока test job не начинает запуск pytest, branch protection не даёт реальной защиты.

### P0.2. Hermetic marker migration не завершена

Без marker остались как минимум:

- `test_entry_exit_vote_behavior.py`;
- `test_exp002b.py`;
- `test_macro_regime_attribution.py`;
- `test_time_of_day_attribution.py`.

Они напрямую открывают отсутствующие файлы из `backend/reports`. Поэтому команда `-m "not artifact and not integration"` всё равно падает на чистом checkout.

Действие:

- пометить весь модуль `pytestmark = pytest.mark.artifact`;
- добавить статический guard-тест: если тестовый файл содержит путь `reports/`, он обязан иметь marker `artifact`, либо использовать versioned fixture;
- проверить baseline именно в fresh clone/container, где каталога пользовательских reports нет.

Также `test_indicator_state.py::test_ema_matches_batch` сравнивает float через exact equality и падает на разнице порядка `2e-14`. Для математического parity использовать `pytest.approx(..., abs=..., rel=...)` или унифицировать одно и то же округление в batch/incremental реализации.

### P0.3. Frontend build не входит в CI

Локально frontend зелёный, но `.github/workflows/ci.yml` содержит только Python lint/test jobs. Изменения `frontend/index.html`, `main.ts` и `style.css` могут попасть в `master` без `tsc`/Vite проверки.

Добавить обязательный job:

```bash
cd frontend
npm ci
npm run build
```

Опционально — cache npm и upload bundle stats.

### P0.4. Ruff пока только декоративный

Ruff job формально зелёный из-за `continue-on-error: true`. Текущий tree выдаёт около 4431 diagnostics. Это допустимо для legacy backlog, но не защищает новый код.

Правильная схема:

- полный Ruff оставить информационным;
- blocking Ruff запускать только на изменённых Python-файлах;
- минимум блокировать `F`, `E9`, duplicate definitions и syntax/import errors;
- новый код не должен увеличивать debt.

У новых файлов аудита обнаружено 12 diagnostics, в том числе `zip()` без `strict=` в `runtime_select.py` и `reference_run.py`.

## 5. P1 — Universe v2

### P1.1. `_all` загружает всю историю всех инструментов последовательно

`app/bot/universe/runtime_select.py::_load_snapshot_bars()`:

1. Находит все FIGI с минимум 30 строками 5m.
2. Для каждого FIGI отдельно вызывает `load_all_bars()`.
3. `load_all_bars()` извлекает **всю доступную историю** без временной границы.
4. Все ряды одновременно сохраняются в `bars_by`.
5. Feature layer использует только короткое окно, но до этого фильтрует весь ряд Python-списком.

При 500 инструментах и многолетней 5m-истории это означает:

- N+1 SQL queries;
- очень большой объём объектов SQLAlchemy/Python;
- высокий startup latency;
- риск исчерпания памяти;
- повторную загрузку одинаковой истории на каждый тест.

Безопасное решение:

- SQL должен выбирать только последние `FEATURE_WINDOW + reserve` баров с `ts < as_of` для каждого FIGI;
- использовать window function `row_number() over(partition by figi order by ts desc)`;
- одним bulk query вернуть, например, 64–100 баров на инструмент;
- не создавать ORM Candle objects — выбирать необходимые скаляры;
- добавить benchmark: 100/500 инструментов, query count, rows, wall time, peak RSS.

### P1.2. Lot size теряется

В `_load_snapshot_bars()` lot собирается в `entries_by_figi`, но затем `InstrumentRef` не несёт lot, а `select_screened_universe()` всегда возвращает:

```python
"lot_size": 10
```

Для бумаг с lot != 10 тестовая экономика и ограничения портфеля будут неверными. Это делает сравнение legacy vs Universe v2 ненадёжным.

Исправление: хранить metadata по FIGI отдельно и возвращать реальный `Instrument.lot`/`UniverseEntry.lot`; fallback 10 должен сопровождаться явным diagnostic reason, а не быть нормой.

### P1.3. Граница `as_of` потенциально включает будущую свечу

После перехода на START timestamp старший бар с `ts=T` представляет интервал `[T, T+TF)`. Feature functions используют условие:

```python
b.ts <= as_of
```

Если replay начинается ровно в `T`, историческая закрытая 5m-свеча с меткой `T` уже содержит минуты после `T`, которых runtime в этот момент ещё не видел. Контракт `EngineStateSnapshot` при этом прямо говорит: состояние должно быть **перед первым входящим баром as_of**.

Для START-свечей безопасная видимость обычно:

```text
bar.ts + timeframe <= as_of
```

либо минимум `bar.ts < as_of` при гарантированно закрытых бакетах и согласованной семантике входного времени.

Нужен отдельный boundary test:

- replay start ровно на 5m/10m/hour границе;
- добавить в БД бар `ts == as_of`, содержащий экстремальные будущие значения;
- результат Universe/state до `as_of` не должен измениться.

### P1.4. Runtime bridge не имеет собственных тестов

Большой набор unit-тестов покрывает отдельные Universe-компоненты, но поиск не обнаружил тестов для `runtime_select.py`, `UNIVERSE_MODE`, `_all`, FIGI mapping, lot propagation и подключения в `_startup`.

Нужны тесты:

- legacy mode не изменён;
- v2 разрешён только в `mode=test`;
- `_all` использует только историю до as_of;
- TCS→BBG mapping;
- реальный lot;
- stream universe совпадает с selected universe;
- hot-add отключён;
- пустой/частично битый рынок диагностируется детерминированно.

## 6. P1 — канон таймфреймов и state/reference contracts

### Что хорошо

- START-конвенция закреплена parity-тестами.
- `CandleHub`, `Resampler`, ensemble resample сведены в один лагерь.
- Добавлен DB-TF integration invariant.
- Появился `EngineStateSnapshot` с JSON/fingerprint.
- Появился reference run и runtime comparison.

### P1.5. Документация CandleHub противоречит новому коду

Верхний docstring `app/engine/candlehub.py` всё ещё утверждает:

- `ts` — время закрытия;
- свеча покрывает `(T-tf, T]`;
- бакет закрывается на правой границе.

Ниже код и новые комментарии утверждают START/floor:

- `ts` — начало `[T, T+tf)`;
- закрытие при первой минуте следующего бакета.

`finalize()` также всё ещё говорит о «метке закрытия своего бакета». Это опасный висяк: разработчик, ориентирующийся на модульный контракт, реализует новый consumer по старой семантике.

Исправить docstrings одновременно с ADR `TIMEFRAME_SEMANTICS`, где явно определить:

- что означает timestamp 1m и старшего TF;
- когда бар доступен стратегии;
- что такое partial;
- поведение на EOD/gap/flush;
- as_of boundary.

### P1.6. State snapshot пока только схема

`EngineStateSnapshot` не собирается и не восстанавливается runtime/replay; это честно указано в docstring. Поэтому Stage B пока нельзя считать завершённым в функциональном смысле.

Следующий шаг:

1. Snapshot adapters для каждого incremental indicator.
2. Strategy snapshot adapters.
3. Cold→snapshot→restore→continue parity.
4. Fingerprint состояния перед каждым контрольным баром.
5. Версионирование и отказ от restore при несовместимой версии.

### P1.7. Reference comparison недостаточно строгий

`compare_runtime()` проверяет count, side, entry time, entry/exit price, но не проверяет:

- `exit_time`;
- `exit_reason`;
- signal/decision identity;
- порядок/ключ сделки при вставке одной лишней сделки.

Для эталонного parity это оставляет слепую зону: выход может произойти на другом баре по другой причине, но при близкой цене comparison пройдёт.

Добавить canonical trade key и сравнивать минимум decision_ts, entry_ts, exit_ts, side, entry/exit reason и цены с явно заданными tolerance.

## 7. P1/P2 — Replay Analytics и presets

### Хорошо

- API списка использует SQL aggregation, а не N+1 по тестам.
- Есть фильтрация и пагинация сделок.
- Новый контур имеет крупный тестовый набор.
- Sidecar отделяет конфигурацию теста от торговых таблиц.

### Остаточные риски

1. `/replays/{name}/slices` загружает все закрытые сделки теста в Python и строит все dimensions циклом. Для крупных replay это станет latency/memory bottleneck. Канонические dimensions лучше агрегировать SQL или материализовать после завершения прогона.
2. Sidecar write не атомарен. Использовать temp file + `replace`, как уже сделано для OSE cache.
3. Удаление теста удаляет sidecar до `db.commit()`. Если commit упадёт, DB останется, а metadata исчезнет. Сначала commit DB, затем удалить файл и вернуть warning при file cleanup failure.
4. `reports/presets` — файловое состояние одного узла. При нескольких backend instances нужна БД/object storage либо чётко зафиксирован single-node contract.

## 8. Размер и структура

Монолиты продолжают расти:

- `backend/app/bot/runtime.py` — 6438 строк;
- `frontend/src/main.ts` — 5604;
- `backend/app/api/routes/bot.py` — 2580;
- `backend/app/services/ensemble.py` — 2203;
- `frontend/src/api.ts` — 1348.

Крупнейшая функция `_run_pipeline` остаётся около 1008 строк. Новый функционал Analytics/Universe снова добавляется в существующие фасады.

После закрытия P0 рекомендован отдельный механический refactor без изменения торговой логики:

- `runtime/startup.py`;
- `runtime/candle_processor.py`;
- `runtime/orders.py`;
- `runtime/replay.py`;
- `api/bot_control.py`, `api/bot_tests.py`, `api/bot_telemetry.py`;
- frontend feature modules `chart/`, `bot/`, `analytics/`, `presets/`.

## 9. Приоритетный план

### Шаг 1 — сегодня, вернуть зелёный CI

1. Исправить установку T-Tech SDK в GitHub Actions.
2. Домаркировать четыре artifact test modules.
3. Исправить exact-float EMA test.
4. Добавить frontend build job.
5. Перезапустить CI на чистом commit и закрепить branch protection.

Критерий: GitHub Actions `lint`, `test`, `frontend` — зелёные; pytest реально запускался.

### Шаг 2 — Universe v2 correctness до новых прогонов

1. Bulk bounded query `ts < as_of`.
2. Реальный lot propagation.
3. START/as_of boundary test.
4. Runtime bridge tests.
5. Измерить startup time/RSS/query count.

Критерий: v2 test universe детерминирован, не видит будущее и не искажает сайзинг.

### Шаг 3 — закрепить time/state contracts

1. Обновить CandleHub docstrings и ADR.
2. Усилить reference comparison.
3. Реализовать snapshot adapters и restore parity.

### Шаг 4 — оптимизация и декомпозиция

1. SQL/materialized replay slices.
2. Atomic sidecars.
3. Разбить runtime/routes/frontend монолиты.
4. Возобновить shadow migration Universe selection → allocation → rebalance.

## 10. Общая оценка

Направление правильное: предыдущий аудит не просто задокументирован, а значительная часть замечаний реально реализована. Особенно полезны route uniqueness guard, START parity, reference run, hermetic markers и test-only включение Universe v2.

Однако текущая скорость изменений снова опередила защитные механизмы: CI включили, но он красный; hermetic baseline объявили, но чистый checkout падает; Universe v2 подключили к runtime до тестирования самого bridge и до ограничения выборки истории.

Главная цель следующего цикла: **не добавить ещё один слой, а довести уже введённые контракты до исполняемых и зелёных гарантий**.
