# Deeptrading — дифференциальный аудит `master`

Дата аудита: 2026-10-03

- Проверенный commit: `d70d6ab3b422f09cb838c37dbdc70e8c7bb5b6df`
- База сравнения / предыдущий аудит: `406710adc5e7f68ddb226306da8611b16c5fcfbd`
- На момент завершения аудита GitHub `master` всё ещё указывает на `d70d6ab3b422f09cb838c37dbdc70e8c7bb5b6df`
- Дифф: 26 commits, 117 файлов, `+76 989 / -1 133` строк; значительная доля добавлений — generated research JSON/fixtures
- Продуктовый код в ходе аудита не изменялся

## 1. Итог

Обновление существенно лучше состояния от 1 октября. Предыдущие P0 практически закрыты: официальный GitHub Actions зелёный, clean-checkout baseline воспроизводится, frontend реально собирается в CI, broker dependency разделена и зафиксирована, marker migration и float-parity исправлены. Все четыре главных риска Universe v2 из прошлого отчёта также закрыты кодом и тестами: bounded bulk query, реальный lot, close-time `as_of`, runtime bridge и ADR.

Локально получен стабильный baseline:

```text
1084 passed, 77 deselected, 66 warnings in 17.49s
```

Однако зелёный CI сейчас даёт ложную гарантию для changed-files lint: на прямом push в `master` job сравнивает HEAD с уже обновлённым `origin/master`, получает пустой diff и ничего не проверяет. Ручной запуск правила на реальном диапазоне `406710a..d70d6ab` обнаружил 18 `F/E9` diagnostics. Это новый P0 уровня release protection.

В новых подсистемах найдены P1 correctness-риски:

1. Candle watchdog обнаруживает `DEFICIT_INTRADAY`, но ни repair, ни runtime strict-mode не считают его проблемой.
2. Replay помечается `finished` даже при cancellation/exception и очищает локальный held-state независимо от результата закрытия.
3. Weekend-flat пытается закрывать только в момент/после конца сессии, а не до него; swing-позиции исключены.
4. Signal Trace не связывает ORDER/FILL с `signal_id`, пропускает ранние entry rejections и поэтому не даёт заявленного сквозного lifecycle.
5. Signal Trace outcomes имеют off-by-one по START-барам: горизонт `h` включает бар с `ts == anchor+h`, то есть ещё одну минуту будущего.
6. Signal Lab не включает dataset fingerprint и эффективный ticker override в run identity; повтор после ремонта свечей может смешать старые и новые строки.
7. Новые схемы по-прежнему вводятся через `create_all` и runtime `ALTER TABLE`, а не версионированными миграциями.

Рекомендация: **не считать новый research/trace/watchdog контур production-grade до закрытия P0 и первых шести P1**. Universe v2 при этом уже можно продолжать испытывать в test/replay-контуре: прежние блокеры в нём устранены.

## 2. Воспроизводимость и CI

### 2.1. Подтверждённые проверки

Проверки выполнены в отдельном чистом detached worktree на audited commit.

| Проверка | Результат |
|---|---|
| Remote `master` | `d70d6ab3b422f09cb838c37dbdc70e8c7bb5b6df` |
| GitHub Actions | run #57 / id `37086392356`, conclusion `success` |
| `python -m compileall -q app scripts` | успешно |
| Exact CI pytest: `pytest -q --tb=short -m "not artifact and not integration"` | `1084 passed, 77 deselected` |
| `npm ci && npm run build` | успешно |
| FastAPI duplicate path/method scan | дублей нет |
| `git diff --check` | чисто |

Локальный Python — 3.13, GitHub CI — 3.11. Официальный run зелёный, поэтому разница версий не блокирует вывод, но локальная проверка не заменяет CI 3.11.

Frontend bundle:

- JS: 348.64 kB, gzip 112.54 kB;
- CSS: 64.07 kB, gzip 12.64 kB;
- HTML: 46.58 kB, gzip 10.61 kB.

Относительно 1 октября JS вырос примерно на 7.6 kB raw / 2.35 kB gzip — рост пока умеренный.

### 2.2. Полный Ruff

Полный Ruff остаётся информационным и выдаёт 4 459 diagnostics. Это ожидаемый legacy backlog, но его нельзя путать с blocking changed-files gate.

## 3. Статус находок аудита 2026-10-01

| Находка 01.10 | Статус 03.10 | Проверка |
|---|---|---|
| P0.1 CI падает на private broker SDK | **Закрыто** | requirements разделены; `t-tech-investments==1.51.0`; run #57 зелёный |
| P0.2 Hermetic markers/EMA equality | **Закрыто** | exact CI baseline: 1084 passed, 77 deselected |
| P0.3 Нет frontend CI | **Закрыто** | отдельный обязательный frontend job и локальный build зелёные |
| P0.4 Нет blocking lint на новый код | **Реализовано неверно** | job добавлен, но no-op на прямом push; см. P0.1 ниже |
| P1.1 Universe N+1/full-history | **Закрыто** | один PostgreSQL LATERAL bounded query |
| P1.2 Потеря lot | **Закрыто** | lot проходит через runtime bridge, есть tests |
| P1.3 `as_of` видит незакрытый START-бар | **Закрыто** | `bar.ts + TF <= as_of`, boundary tests |
| P1.4 Нет bridge tests | **Закрыто** | добавлены bridge/bulk/boundary/lot tests |
| P1.5 CandleHub docs противоречат START | **Закрыто** | docstring исправлен; ADR 0002 фиксирует availability contract |
| P1.6 State snapshot только схема | **Не закрыто** | runtime restore/adapters не добавлены |
| P1.7 Reference comparison недостаточно строгий | **Не закрыто** | затронутых production-изменений не найдено |
| Replay slices/sidecar residual risks | **В основном не закрыто** | новый `run_state.json` пишется атомарно, но это не устраняет весь прежний список |
| Декомпозиция монолитов | **Ухудшилось** | `runtime.py` и `main.ts` снова выросли |

## 4. P0 — release protection

### P0.1. `ruff-changed` не проверяет прямые push в `master`

Файл: `.github/workflows/ci.yml`, job `ruff-changed`.

Сейчас base определяется так:

```bash
BASE=$(git rev-parse --verify origin/master 2>/dev/null || git rev-parse HEAD~1)
```

После `actions/checkout` на push в `master` remote-tracking ref уже указывает на текущий commit. Получается `BASE == HEAD`, diff пустой, job зелёный без проверки файлов.

Ручной запуск правила на реальном диапазоне `406710a..d70d6ab` дал 18 diagnostics. Примеры:

- `app/api/routes/bot.py`: unused/redefined imports, unused `trade`;
- `app/lab/persist.py`: unused `now`;
- `app/models/__init__.py`: новые модели импортированы, но не все экспортированы;
- `app/services/candle_integrity.py`: unused `date`;
- `scripts/signal_trace_outcomes.py`: unused `timezone`.

Большинство ошибок не ломает runtime, но обязательная гарантия «новый код не растит F debt» фактически отсутствует.

**Безопасное исправление:**

- для `push`: base = `${{ github.event.before }}`; для initial/force push предусмотреть merge-base/fallback;
- для `pull_request`: base = `${{ github.event.pull_request.base.sha }}` или fetch + merge-base с `${{ github.base_ref }}`;
- не передавать список через небезопасный word splitting; использовать NUL-delimited filenames или `xargs -0`;
- добавить workflow test/shell fixture для push и PR событий;
- после исправления убрать 18 текущих F diagnostics отдельным механическим commit без изменения логики.

**Критерий:** тестовый commit с намеренным `F821` в изменённом Python-файле обязан красить job и на PR, и на прямом push.

## 5. P1 — correctness новых подсистем

### P1.1. Candle watchdog видит внутридневные дыры, но не ремонтирует и не блокирует их

Файлы:

- `app/services/candle_integrity.py`;
- `app/bot/runtime.py::_candle_preflight`.

`classify_status()` корректно возвращает `DEFICIT_INTRADAY`, когда breadth показывает отсутствующие активные минуты. Но далее:

- `repair()` выбирает только `DEFICIT_DAY` и `MISMATCH`;
- `preflight_universe()` запускает repair только для тех же двух статусов;
- `_candle_preflight()` строит `bad` только из `DEFICIT_DAY`/`MISMATCH`;
- поэтому даже `CANDLE_PREFLIGHT=strict` пропускает `DEFICIT_INTRADAY`.

Это именно тот класс повреждения, ради которого нужен minute-level watchdog. Дополнительно breadth threshold вычисляется как `max(3, len(figis)//5)`: при universe из 1–2 инструментов активных минут не будет вообще, а при 3 инструментах минута считается активной только если присутствует у всех трёх — отсутствие одного бара само скрывает дыру.

Также `auto_baseline=True` создаёт baseline из текущего состояния при первой проверке. Это допустимо как operational snapshot, но не является независимым доказательством корректности исходного датасета.

**Исправление:**

1. Ввести единый `REPAIRABLE/BLOCKING_STATUSES`, включив `DEFICIT_INTRADAY`.
2. В strict-mode считать проблемой любой unresolved deficit/mismatch; `NO_BASELINE` обрабатывать явной политикой.
3. Для малых universe использовать threshold не больше `len(figis)` и добавить независимый market calendar/reference universe.
4. Repair делать по конкретным `(figi, day)`/окнам, затем повторно проверять; не перезаписывать доказательство до post-check.
5. Добавить PostgreSQL integration tests: one-minute hole, 1/2/3-symbol universe, first baseline, failed fetch, repeat check.

### P1.2. Replay cancellation/exception ошибочно превращается в успешное завершение

Файл: `app/bot/runtime.py::_run`, `_finalize_replay`.

`_finalize_replay()` вызывается в `finally` для любого исхода replay:

- normal stream exhaustion;
- ручная остановка / `CancelledError`;
- исключение processing loop.

После этого безусловно выставляются `_replay_finished = True` и `finished_at = now()`. В UI и `bot_test_runs` аварийный или прерванный прогон выглядит завершённым. Кроме того, `_held.clear()` и `_exit_plans.clear()` выполняются даже если отдельные `close_position()` вернули `None` или бросили исключение.

**Исправление:**

- ввести terminal status: `COMPLETED`, `CANCELLED`, `FAILED`, `PARTIAL_CLOSE`;
- передавать `exited` в finalizer;
- `finished_at`/`finished=true` использовать только для normal exhaustion и успешной финализации;
- хранить `remaining_positions`, `close_errors`, last processed timestamp;
- не очищать held-state для незакрытых позиций;
- добавить tests на normal/cancel/exception/close failure и повторный idempotent finalize.

### P1.3. Weekend-flat срабатывает слишком поздно и не является действительно flat

Файлы:

- `app/engine/sessions.py::weekend_close_due`;
- `app/bot/runtime.py::_portfolio_guard_loop`, `_process_candle`.

Для пятницы due становится true только при `mins >= last_session_end`: 18:45 для day или 23:50 для evening. Live guard после этого отправляет закрывающий ордер уже в момент/после закрытия рынка. Такой market order может быть отвергнут, а следующие retries происходят уже вне ликвидной сессии. Это противоречит комментарию «закрыто перед выходными».

Кроме того, и timer path, и candle path исключают `self._swing`. При включённом `weekend_flat=True` часть позиций поэтому сознательно переживает выходные.

**Исправление:**

- использовать lead window, например `last_end - eod_close_min_before`, на пятнице;
- запретить новые entries в том же cutoff window;
- формально определить, включает ли `weekend_flat` swing; если нет — переименовать/добавить отдельную настройку и показать её в UI;
- проверять факт исполнения, а не только вызов close;
- добавить runtime integration test с broker reject/retry и Friday 23:39/23:40/23:49/23:50.

### P1.4. Signal Trace не образует сквозную цепочку signal → order → fill

Файлы:

- `app/bot/runtime.py::_process_candle`;
- `_submit_order`;
- `_trace_order_reject`;
- `_reject_entry`.

`signal_id` создаётся локально в `_process_candle` и присутствует у RAW/DECISION. Но он не передаётся в `_submit_order`, `BotOrder` или `_trace_order_reject`. ORDER/FILL/EXIT поэтому остаются orphan events и не могут быть надёжно присоединены к исходному сигналу.

Ранние отказы после DECISION (`already_held`, session, pause, direction, regime, risk и др.) идут через `_reject_entry`, который пишет старый event log, но вообще не пишет Signal Trace rejection. Получается разрыв lifecycle именно в важных gate outcomes.

**Исправление:**

- передавать immutable trace context (`run_id`, `signal_id`, decision id) через order meta/model;
- сделать один trace-aware reject helper для всех gates;
- различать `DECISION/REJECTED`, `ORDER/REJECTED`, `FILL/REJECTED`, а не обозначать любой pre-fill отказ как FILL;
- добавить invariant tests: каждый RAW signal имеет terminal decision; каждый ORDER/FILL ссылается на signal; orphan count = 0, кроме явно описанных manual/AI orders.

### P1.5. Outcomes смотрят на одну минуту дальше заявленного горизонта

Файл: `scripts/signal_trace_outcomes.py`.

Для START timestamps код делает:

```python
i1 = bisect.bisect_right(bts, anchor_ts + timedelta(minutes=h))
win = bars[i0:i1]
```

Бар с `ts == anchor+h` покрывает `[anchor+h, anchor+h+1m)`. Он включается в MFE/MAE и его close становится `future_return`. Поэтому horizon=1 фактически использует путь до конца второй минуты после anchor.

**Исправление:** использовать полуинтервал `[anchor, anchor+h)`, то есть `bisect_left` для правой границы, и явно тестировать h=1 на двух свечах с экстремумом во второй. В outcome config/version записать исправленную семантику, чтобы старые и новые labels не смешивались.

### P1.6. Signal Lab run identity не гарантирует воспроизводимость

Файлы:

- `app/lab/config.py`;
- `app/lab/runner.py`;
- `app/lab/persist.py`;
- `app/models/signal_lab.py`.

Проблемы:

1. `dataset_version` есть в модели, но runner его не вычисляет и не записывает.
2. `config_hash` не включает fingerprint свечей. Repair/upsert данных на том же commit оставляет тот же run id.
3. CLI `tickers` влияет на реально обработанный universe, но не изменяет `cfg.universe` и, следовательно, hash.
4. Повторный `upsert_run` сбрасывает status, а `insert_signals(... ON CONFLICT DO NOTHING)` сохраняет старые строки. Если данные изменили набор сигналов, run может стать объединением старого и нового результата.
5. Эффективный universe после `resolve_universe` может отличаться от запрошенного из-за отсутствующих тикеров, но identity этого не отражает.

**Исправление:**

- вычислять immutable dataset manifest: источник, диапазон, figi, count/min/max и content checksum/day hashes;
- hash строить после resolution и после CLI overrides;
- сохранять requested и resolved universe отдельно;
- делать run immutable; для нового dataset hash создавать новый run;
- либо при явном resume валидировать manifest и очищать/пересчитывать фазу транзакционно;
- добавить tests на ticker override, repaired candle, missing ticker и partial rerun.

### P1.7. Schema evolution не контролируется миграциями

Новые `signal_trace_*`, `lab_*` и `candle_integrity` создаются через `Base.metadata.create_all`. `bot_test_runs` и `sandbox_trades` меняются runtime-командами `ALTER TABLE ADD COLUMN IF NOT EXISTS`. В Alembic остаётся одна старая migration.

Риски:

- fresh schema и upgraded schema не имеют проверяемой одинаковой версии;
- runtime instance должен иметь DDL privileges;
- несколько процессов могут одновременно выполнять DDL;
- нет rollback, migration ordering и release audit trail;
- несовместимое изменение модели `create_all` не применит к существующей таблице.

**Исправление:** создать последовательные Alembic revisions, убрать DDL из request/runtime paths, проверять `alembic upgrade head` на пустой и на production-like old schema в CI. `create_all` оставить только тестовым fixture при необходимости.

## 6. P2 — research/performance/architecture

### P2.1. Regime v2 теряет custom hysteresis parameters при `evaluate()`

Файл: `app/services/regime_v2/hysteresis.py::RegimeV2State.clone`.

`clone()` создаёт `RegimeV2State(self.cp)` и восстанавливает snapshot, но не переносит исходные `HysteresisParams`. Проверка показала:

```text
original HysteresisParams(min_tenure=7, confirm_bars=9)
clone    HysteresisParams(min_tenure=1, confirm_bars=2)
```

При custom tuning `evaluate()` может вернуть не то, что вернул бы non-mutating update с теми же параметрами. Сейчас provider по умолчанию legacy и v2 не является trading gate, поэтому severity P2, но до production promotion это обязательный fix.

### P2.2. Signal Trace DB writer делает по одному round-trip на событие

`SqlTraceWriter.write()` выполняет `await db.execute(...)` в цикле до 256 событий. При активном trace это снижает throughput фонового consumer, ускоряет заполнение bounded queue и рост `dropped`. Использовать executemany/bulk insert одним statement на batch; добавить load test и метрики queue depth/flush latency/DB failures.

DB failure сейчас записывает batch в JSONL, но не повторяет DB write. Это допустимо только если JSONL объявлен source of truth и есть replay/import utility; иначе DB trace будет молча неполным.

### P2.3. Signal Lab финализирует partial trailing TF bucket

`build_tf()` всегда вызывает `Resampler.flush()`. Если `period_to` не выровнен по границе TF, последний неполный bucket превращается в исследовательский бар с формальным `bar_close_ts = start+TF`, хотя данные загружены лишь до `period_to`. Нужна политика `drop_partial` по умолчанию и тесты произвольной правой границы согласно ADR 0002.

### P2.4. Монолиты продолжают расти

Текущие размеры:

- `backend/app/bot/runtime.py` — 7 060 строк (`+622` к прошлому аудиту);
- `backend/app/api/routes/bot.py` — 2 759;
- `backend/app/services/ensemble.py` — 2 203;
- `frontend/src/main.ts` — 5 979 (`+375`);
- `frontend/src/api.ts` — 1 348.

Крупные функции:

- `_run_pipeline` — 1 008 строк;
- `_submit_order` — 567;
- `_process_candle` — 554;
- `_startup` — 265.

Новые trace/watchdog/replay функции снова интегрируются напрямую в runtime. Это повышает вероятность частичных lifecycle hooks — уже видимую в Signal Trace и replay finalization.

## 7. Что сделано хорошо

### Universe v2

- bars загружаются одним bounded PostgreSQL LATERAL query;
- выбираются только видимые закрытые бары по `ts + TF <= as_of`;
- реальный lot доходит до runtime;
- test/replay bridge ограничен флагом/контуром;
- добавлены boundary, bridge, bulk и lot tests;
- ADR 0002 согласует START timestamp, availability и `as_of`.

### Regime v2

- production default остаётся `legacy`;
- v2 не подключён как trading gate до parity/validation;
- measurements/classifier/hysteresis/provider разделены;
- есть reference fixture и unit tests;
- stateful hysteresis имеет snapshot/restore и prefix parity для default config.

### Run/replay UX

- виртуальные replay clocks после нормального финала больше не остаются active;
- появился frozen summary;
- `run_state.json` пишется атомарно через temp + `os.replace`;
- источник boot state логируется, что снижает риск тихого возврата к другому engine/TF.

## 8. Приоритетный безопасный план

### Шаг 0 — немедленно, восстановить честный release gate

1. Исправить base SHA в `ruff-changed` для push/PR.
2. Добавить self-test workflow semantics.
3. Убрать 18 changed-file F diagnostics отдельным mechanical commit.
4. Повторить exact CI и frontend build.

**Критерий:** намеренный F821 блокирует merge/push; обычный commit зелёный.

### Шаг 1 — correctness replay/watchdog/weekend

1. Сделать replay terminal statuses и failure-aware finalization.
2. Включить `DEFICIT_INTRADAY` в repair/strict и исправить small-universe breadth.
3. Перенести Friday close в pre-close cutoff, определить swing policy.
4. Добавить PostgreSQL/runtime integration tests для failure paths.

**Критерий:** cancelled/failed replay нельзя принять за completed; minute hole либо восстановлен, либо блокирует strict start; weekend close инициируется до закрытия рынка и подтверждает исполнение.

### Шаг 2 — сделать Signal Trace аналитически надёжным

1. Пронести `signal_id` через decision/order/fill/exit.
2. Покрыть все ранние gate rejections.
3. Исправить horizon boundary и version outcomes.
4. Перевести DB writer на bulk; добавить importer JSONL→DB и completeness metrics.

**Критерий:** lifecycle invariant tests не находят orphan/gap; golden outcome h=1 не видит второй будущий бар.

### Шаг 3 — зафиксировать Signal Lab и schema contracts

1. Dataset manifest/fingerprint и effective config hash.
2. Immutable run semantics и transactional resume policy.
3. Drop partial TF bucket.
4. Alembic migrations для всех новых таблиц/колонок; CI upgrade tests.

**Критерий:** repair одной свечи создаёт новую identity; ticker override не смешивается с полным run; fresh/upgrade schemas совпадают.

### Шаг 4 — research hardening и декомпозиция

1. Исправить Regime v2 clone custom params и добавить test.
2. Закрыть прежние snapshot/reference parity gaps.
3. Механически вынести runtime trace/replay/orders/watchdog в отдельные модули без изменения поведения.
4. Разбить frontend `main.ts` по feature modules.

## 9. Финальная оценка

По сравнению с 1 октября проект сделал реальный шаг вперёд: CI и baseline теперь воспроизводимы, а Universe v2 исправлен по всем четырём ключевым пунктам прошлого аудита. Это не косметические изменения.

Но новый слой observability/research пока опережает свои контракты. Главный риск теперь не в базовой сборке, а в **ложноположительном “всё хорошо”**:

- lint job зелёный, хотя не проверял diff;
- strict candle preflight зелёный при intraday holes;
- replay finished при cancellation/error;
- weekend-flat объявлен, но close стартует после доступного торгового окна;
- Signal Trace выглядит полным, но terminal events не связаны с сигналом;
- Signal Lab имеет run id, который не идентифицирует фактический dataset.

Следующий цикл должен быть посвящён не добавлению новых подсистем, а превращению этих статусов и идентификаторов в исполняемые, проверяемые гарантии.
