# Deeptrading — аудит актуального `master`

Дата: 2026-09-30  
Актуальный commit: `e2956d5021e9efe6774a59d33ab3bf56265b02bf`  
Предыдущая проверенная точка: `2a47fe6e17df59795513cba0eb24320ede1369f9`

## 1. Краткий вывод

Проект быстро развивается и за сутки получил несколько сильных улучшений: исправлена экономика выходов, OsEngine-индикаторы ускорены примерно в 36 раз, график вкладки «Бот» получил request generation/AbortController/режимы просмотра/READY-handshake, Universe разложен по слоям, добавлен Analytics-контур с тестами.

Главная проблема теперь не недостаток функций, а **слишком высокая скорость добавления функций при незакрытом системном долге**. В `master` одновременно находятся:

- рабочий live/runtime-контур;
- legacy compatibility-контур;
- новый Universe 2.0, пока не подключённый к live;
- исследовательские скрипты и результаты;
- тесты кода и тесты внешних исследовательских артефактов в одном suite;
- несколько больших монолитов, в которых новая функциональность продолжает накапливаться.

Рекомендация: на 1 короткий цикл заморозить новые фичи и сделать **stabilization/consolidation sprint**. Сначала восстановить обязательный CI и воспроизводимый baseline, затем убрать дубли и развести production/research/artifacts, после этого продолжать функциональное развитие.

## 2. Что изменилось с предыдущего аудита

Между `2a47fe6` и `e2956d5`:

- 24 новых коммита;
- 114 изменённых файлов;
- примерно `+24 795 / -719` строк;
- крупные блоки: Universe 2.0, Analytics, Exit Lab/Stop Oracle, новые OSE-роботы и индикаторы, исправления engine/runtime, стабилизация графика.

Сильные стороны нового кода:

1. **График:** реализованы `AbortController`, request generation, `chart-ready`, единый `focusBotChart`, отдельный overlay message, `TRADE_FOCUS`, сохранение viewport до мутации и частичный `series.update()`.
2. **Universe:** появились явные доменные типы и слои discovery/features/screener/selection/allocation/rebalance/policy.
3. **Analytics:** отдельные таблицы `report_*`, индексы, идемпотентный импорт по hash, серверная фильтрация и пагинация сделок.
4. **Engine/OSE:** есть reference/parity tests и реальное измеренное ускорение хвостовых индикаторов.
5. **Тесты:** в чистом окружении большая часть suite проходит; получено `883 passed`, несмотря на смешение тестов и недоступных артефактов.

## 3. P0 — блокирующие системные проблемы

### P0.1. `master` не защищён CI

`.github/workflows/ci.yml` запускается на push только для веток:

```yaml
branches: [second, audit-fixes-2026-09-18]
```

Push в `master` не запускает workflow. Именно поэтому в `master` попали дефекты, которые CI должен был остановить.

Что сделать:

- включить `master` в push trigger;
- сделать PR check обязательным;
- добавить branch protection: merge только после compile + unit tests + frontend build;
- убрать `continue-on-error` хотя бы для критичного поднабора Ruff (`F`, `E9`).

### P0.2. Python tree не компилируется

Команда CI:

```bash
python -m compileall -q app scripts
```

падает на:

`backend/scripts/trend_day_sim.py:358` — `SyntaxError: unterminated string literal`.

Это не просто «старый скрипт»: текущий CI сам заявляет, что компилирует `scripts`, поэтому актуальный `master` формально красный.

### P0.3. Зависимости неполны и установка невоспроизводима

Production-модуль `app/services/ml_ensemble_filter.py` на уровне import требует:

- `joblib`;
- `pandas`.

Их нет ни в `requirements.txt`, ни в `requirements-dev.txt`. Полный pytest сначала остановился на collection с `ModuleNotFoundError: joblib`; после ручной установки `joblib` и `pandas` suite продолжил работу.

Дополнительно CI устанавливает зависимости вручную и не использует `requirements-dev.txt`, поэтому requirements и CI уже расходятся.

Что сделать:

- добавить runtime-зависимости в `requirements.txt` либо вынести ML в optional extra и сделать lazy import;
- CI должен ставить ровно lock/requirements, а не собственный список;
- добавить constraints/lock с проверенной Python 3.11;
- `t-tech-investments` документировать как private-index dependency с понятной диагностикой сертификата.

### P0.4. Тестовый suite смешивает unit/integration и проверки локальных артефактов

Результат полного запуска после установки недостающих import dependencies:

- `883 passed`;
- `12 failed`;
- `52 errors`.

Большая часть errors — отсутствие локальных файлов в `backend/reports/...`: `entry_confluence.json`, `entry_quality.json`, `vote_attribution.json`, `imoex_*`, manifests и т. п. Эти файлы не входят в checkout, но тесты требуют их без skip/marker/fixture generation.

Это делает число «весь suite прошёл» зависимым от конкретной машины и её каталога reports.

Нужны отдельные контуры:

- `tests/unit` — полностью hermetic, обязательный CI;
- `tests/integration` — PostgreSQL/SQLite, обязательный CI;
- `tests/artifact_contract` — запускается только с явно указанным artifact bundle;
- `tests/e2e` — браузер/живые сервисы, отдельный workflow.

Для artifact tests: marker `@pytest.mark.artifact`, проверка precondition и `pytest.skip`, либо маленькие versioned fixtures в `tests/fixtures`.

### P0.5. Дублирован endpoint `/api/v1/bot/heatmap`

В `backend/app/api/routes/bot.py` дважды определены:

- `_tz_utc`;
- `_dt_now_utc`;
- `_bias_from_daily`;
- `_hm_compute_meta`;
- `bot_heatmap`;
- один и тот же `@router.get("/heatmap")`.

Блоки начинаются примерно с линий `1305/1388` и повторяются с `2379/2475`. Ruff подтверждает `F811`, а FastAPI регистрирует одинаковый method/path дважды. В зависимости от порядка маршрутов одна версия фактически становится недостижимой, а разработчик может исправлять не ту реализацию.

Действие: выбрать одну каноническую реализацию, добавить route uniqueness test и удалить повторный блок.

## 4. P1 — архитектурные «висяки»

### P1.1. Universe 2.0 пока параллельный, а не рабочий контур

Это честно записано в `app/bot/universe/__init__.py`: live runtime использует только compat-функции `select_eligible_universe` / `select_volatile_universe`; Selection/Allocation/Rebalance/Policy — research API и ордеров не создают.

Поиск production usages показывает, что `StrategyScreener`, `EqualWeightAllocation`, `RebalancePlanner` и новые domain contracts вне пакета практически не используются. Runtime в `app/bot/runtime.py:3106+` импортирует legacy-compatible API.

Риск: две модели отбора будут расходиться, а тесты нового слоя не гарантируют поведение реального бота.

Безопасный cutover:

1. Старый путь остаётся source of execution.
2. Новый pipeline работает в shadow и пишет diff: universe set, rank, target size, rejected reason.
3. На исторических/реплейных сессиях установить parity threshold.
4. Переключить только selection.
5. Отдельно переключать allocation, затем rebalance/policy.
6. После периода parity удалить compat, а не держать обе реализации бессрочно.

### P1.2. Критические монолиты

Крупнейшие production-файлы:

- `app/bot/runtime.py` — 6323 строки;
- `frontend/src/main.ts` — 5165 строк;
- `app/api/routes/bot.py` — 2533 строки;
- `app/services/ensemble.py` — 2205 строк;
- `app/api/routes/sandbox.py` — 1650 строк;
- `frontend/src/api.ts` — 1348 строк.

Крупнейшие функции:

- `ensemble._run_pipeline` — около 1008 строк;
- `runtime._submit_order` — около 542;
- `runtime._process_candle` — около 451;
- `bot_config_patch` — около 339.

Это уже влияет не только на читаемость: появляются дубли endpoint-ов, локальные imports, широкие exception handlers, трудно изолируемые state transitions и медленное ревью.

Целевое разбиение:

- `BotRuntime` оставить orchestration facade;
- вынести `StartupCoordinator`, `CandleProcessor`, `OrderService`, `PositionService`, `ReplayLifecycle`, `RuntimeState`;
- `routes/bot.py` разнести на config/control/orders/positions/telemetry/analytics;
- `frontend/main.ts` разнести на `bot/`, `chart/`, `analytics/`, `warehouse/`, `router/`;
- `_run_pipeline` превратить в последовательность pure stages с typed stage results.

Делать механически, под characterization tests, без одновременного изменения торговой логики.

### P1.3. Документация состояния конфликтует сама с собой

`STATUS.md` начинается с handoff от 2026-08-28/29, тогда как `docs/PROGRESS.md` содержит состояние от 2026-09-30. Одновременно существуют `STATUS.md`, `PROJECT_STATE.md`, `MEMORY.md`, `ROADMAP.md`, `docs/PROGRESS.md`, `main_plan.md`, несколько chat/handoff файлов.

Нужен один канон:

- `docs/STATUS.md` — текущее состояние, генерируется/обновляется;
- `docs/ROADMAP.md` — только будущая очередь;
- `docs/adr/` — принятые архитектурные решения;
- `docs/archive/` — исторические handoff/эксперименты.

У каждого пункта: owner, status, evidence, commit, next action. Старые документы не должны выглядеть актуальными.

### P1.4. Research artifacts и исходники смешаны

В git отслеживаются временные/результатные файлы: `_tmp_sess_check.py`, `tmp_check_mtf.py`, `ai_trader_cycles.jsonl.old`, несколько `results_*.json`, отдельный `backend/reports/*.json`.

Рекомендуемая структура:

```text
backend/app/          production
backend/research/     воспроизводимые research pipelines
backend/tools/        эксплуатационные утилиты
artifacts/            immutable run outputs, обычно вне git/LFS/object storage
configs/experiments/  versioned configs
```

В git хранить config + manifest + короткий summary; большие результаты — по content hash во внешнем artifact store.

## 5. Производительность

### 5.1. Уже хорошо

- OSE tail indicators измеренно ускорены с ~555 до ~15 секунд на прогон.
- График перестал всегда делать полный `setData()` при каждом 8-секундном refresh.
- Analytics использует агрегирующие SQL-запросы и индексы по `run_id/strategy/ticker/entry_time`.
- Startup инструментов уже имеет bounded concurrency через semaphore.

### 5.2. Heatmap делает N+1 запросы

`bot_heatmap` проходит по каждому инструменту и выполняет отдельный SQL; при `meta=1` добавляется отдельный расчёт metadata на инструмент. Это масштабируется как 1–2 запроса × число бумаг.

Ускорение:

- один set-based SQL по `figi = ANY(:figis)`;
- group by `figi, date_trunc(...)`;
- bias/regime также считать bulk либо предварительно материализовать;
- кэшировать результат по `(universe_revision, interval, from, meta)`.

### 5.3. Startup открывает много коротких DB sessions и последовательно готовит данные внутри каждого task

В `_startup` каждый `_load_one` создаёт несколько `SessionLocal`, а `ensure_*_candles` и чтение выполняются per instrument при semaphore=3.

Ускорение без риска rate-limit:

- bulk-load instrument params и существующие candle tails одним/несколькими запросами;
- отдельно выполнить network backfill с собственным rate limiter;
- после backfill одним batch получить tails;
- измерить semaphore 3/6/10, а не увеличивать вслепую;
- записывать phase timings: discovery, backfill, DB load, strategy init.

### 5.4. Инкрементальный график всё ещё делает полный rebuild на каждом новом баре при скользящем окне

Инкремент разрешён только если первый timestamp нового snapshot равен прежнему. Endpoint возвращает последние 2000 свечей; когда появляется новый бар, окно сдвигается и первый timestamp меняется. Поэтому обновление текущей свечи идёт через `update()`, но новый минутный бар обычно вызывает полный `setData()` всех серий.

Лучше:

- отдельный tail endpoint `after_ts`/WebSocket;
- `series.update()` для current/new bar;
- full snapshot только при gap, symbol/TF change или checksum mismatch;
- периодический reconciliation snapshot, например раз в 5–10 минут.

### 5.5. Refresh графика не single-flight

`refreshEmbedChart()` не использует свой AbortController/in-flight flag. Если запрос длится больше 8 секунд, два refresh одного generation могут выполняться одновременно и примениться не в порядке запуска. Generation защищает от смены focus, но не от out-of-order responses одного focus.

Добавить `_refreshAbort` или `_refreshInFlight`, отдельную refresh revision и передать `signal` в `fetchAnalysis`.

### 5.6. Price lines всё ещё могут влиять на autoscale

SL/TP/trailing создаются как price lines на candle series. При далёком stop/target они могут расширять вертикальный диапазон и визуально «сжимать» свечи. Следует проверить в браузере и, если подтверждается, рисовать линии primitive/overlay, исключённым из autoscale, либо явно управлять autoscale provider.

### 5.7. Сначала benchmark, потом оптимизация

Добавить стабильный performance suite:

- engine: candles/sec, strategy × symbols × bars;
- runtime startup: total и фазы;
- `_process_candle`: p50/p95/p99;
- DB: query count + time на API endpoint;
- frontend chart: data apply time и dropped frames;
- report import: files/sec, trades/sec.

Baseline хранить как JSON в CI artifact; fail только при существенной регрессии, например >15–20% на стабильном benchmark.

## 6. Остаток по графику «Бот»

Большая часть ранее предложенного P0 уже реализована в новом `master`:

- исторический `TRADE_FOCUS` не заменяется latest snapshot;
- stale focus requests отменяются/игнорируются;
- parent focus стал canonical;
- есть READY handshake;
- overlay отделён от data fetch;
- viewport снимается до обновления;
- учитывается OHLCV signature последней свечи.

Перед признанием задачи закрытой нужны браузерные e2e-сценарии:

1. быстро кликнуть A→B→C при искусственной задержке API — остаётся C;
2. открыть старую сделку и ждать >30 секунд — график остаётся у сделки;
3. вручную отмотать live-график и ждать refresh — viewport не двигается;
4. нажать «К текущему» — follow включается явно;
5. trailing SL обновляется без reload свечей и без изменения масштаба;
6. переключить TF во время request — старый ответ не применяется;
7. открыть iframe до/после parent focus — выбор одинаковый.

## 7. Рекомендуемый план работ

### Этап A — 1–2 дня, стабилизация baseline

- включить CI для `master`;
- исправить `trend_day_sim.py`;
- починить requirements/CI install;
- разделить artifact tests markers;
- удалить duplicate `/heatmap`;
- зафиксировать зелёный результат unit+integration и frontend build.

Критерий готовности: чистый clone + одна команда дают одинаковый зелёный baseline.

### Этап B — 3–5 дней, систематизация

- канонизировать STATUS/ROADMAP/ADR;
- переместить tmp/results/artifacts;
- добавить route uniqueness test;
- разбить `routes/bot.py` и frontend `main.ts` по feature modules;
- ввести typed application services между routes и runtime.

### Этап C — 3–7 дней, измеримое ускорение

- benchmark harness;
- bulk heatmap query;
- batch runtime startup;
- chart tail updates/single-flight;
- профилировать `_run_pipeline`, затем разбить его на stages и кэшировать общие indicator contexts.

### Этап D — controlled migration Universe 2.0

- shadow diff;
- replay parity;
- staged cutover selection → allocation → rebalance;
- удалить compat после подтверждения, не поддерживать два вечных production пути.

## 8. Что не следует делать сейчас

- Не переписывать весь runtime «с нуля».
- Не подключать Allocation/Rebalance к live одним большим переключением.
- Не продолжать добавлять новые роботы до восстановления обязательного CI.
- Не исправлять все 4700 Ruff diagnostics одной массовой auto-fix операцией; сначала включить блокирующий минимум на изменённых файлах.
- Не смешивать архитектурный refactor с изменением торговой экономики в одном commit.

## 9. Проверки аудита

- `npm ci && npm run build` — успешно; production bundle около 330 KB JS, 107 KB gzip.
- `python -m compileall -q app scripts` — ошибка в `trend_day_sim.py:358`.
- pytest до добавления незаявленных зависимостей — collection error (`joblib`).
- pytest после ручной установки `pandas/joblib` — `883 passed, 12 failed, 52 errors`; большинство errors связано с отсутствующими локальными research artifacts.
- два точечных реальных расхождения: каталог допускает `10min`, старый тест — нет; EMA state отличается от batch на ~2e-14 из-за строгого exact equality.
- Ruff по всему дереву: 4700 diagnostics; использовать как backlog, а не массовый blocker. Критичные сейчас: syntax/F811/unused-redefinitions/duplicate routes.

Итог: техническая база сильная, но проекту нужен короткий этап консолидации. Самая высокая отдача сейчас — не ещё одна стратегия, а воспроизводимый CI, один production path на домен, удаление дублей и измеримые performance budgets.
