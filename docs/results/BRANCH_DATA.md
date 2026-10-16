# BRANCH_DATA — ветка DATA (B1): пригодность данных и ограниченный recovery-пакет

**Builder-1 · DISPATCH-20261011-01 · 2026-10-10 · DATA_READY_FOR_APPROVAL.**
Каталог: `backend/reports/architect_20261010/b1/branch_data/20261010T221613Z/`.
Сеть НЕ вызывалась, production-БД/CSV/движок не тронуты, A5/A10/A12/A19/A23 — read-only,
`.env`/секреты в вывод не попадают. Ни один argv сейчас не executable (см. блокер A5).

Конечный результат ветки подготовки — три артефакта + честный executable/blocker статус +
bounded pilot без лишнего target. Разрешения на сеть нет; если потребуется — архитектор
запросит одно разрешение на конкретный pilot.

## 1. `pilot_request.json` — ровно 3 target-минуты VKCO

Пилот ограничен тремя target-минутами VKCO из A19 deep-dive:
`2026-07-10 10:43Z`, `2026-08-05 12:41Z`, `2026-08-06 12:43Z` (окна ±5min, half-open).
**Четвёртая минута `2026-08-05 08:56Z` (A23 pilot по датам захватывал 4 окна) явно исключена**
и помечена в `explicitly_excluded` — не включается автоматически.

Каждый случай: полные argv (offline + proposed collect), FIGI `TCS00A106YF0`, локальный
A10-хэш, `executable_now=false`. Бюджет pilot (раздельно от полного плана):
- **ISS: 3 логических запроса** (по distinct VKCO-день, `from=till=day` → весь день),
  ≤10 страниц/запрос, timeout 20с, retries 2 → worst-case 1800с;
- **T-Invest: 3 запроса** (по окну), timeout 20с, attempts 2 → worst-case 120с;
- **итого 6 логических запросов, ≤1920с (33 мин) worst-case.**

Полный 89-minute план (A23 `request_plan.json`, 729 мин worst-case) — **ссылкой, НЕ исполняется**
и не является разрешённым запуском. Выходы pilot — новые immutable каталоги, production не пишется.

## 2. `data_readiness.json` — пригодность данных по 5 тикерам

Факты raw/DQ/unknown с lineage (A10-хэш + A12 trades-хэш по обоим сценариям), 5/2bps:

| тикер | raw строк | filled | incomplete | DQ (по code) | пригодность экономики |
|---|---|---|---|---|---|
| SBER | 130027 | 118 | 3 | 1802 weekend | **NOT_FIT (KNOWN_BAD_INPUT)**, diagnostics FIT |
| GAZP | 121281 | 166 | **0** | 1802 weekend | **FIT (complete_observed)** |
| LKOH | 116978 | 166 | 5 (2.9%) | 1802 weekend | CONDITIONAL |
| VKCO | 107992 | 155 | 24 (13.4%) | 1794 weekend + 1 warmup_gap | CONDITIONAL (цель пилота) |
| SFIN | 92190 | 48 | **88 (64.7%)** | 1749 weekend + 3 warmup_gap | **NOT_FIT (непригодность наблюдений)** |

- DQ-ошибки — почти целиком `session_bars_weekend` (выходные, исключённые A7-сессией; **не дефект
  данных**, объяснено в A12) + `warmup_coverage_gap` (VKCO 1, SFIN 3 — прогрев частичный, skips
  сохраняются). **SBER known-bad явно** (economics_not_eligible, остаётся в атриции).
- **Неполный путь НЕ интерполируется**: полный эффект каждого INCOMPLETE — UNKNOWN; происхождение
  `NOT_VERIFIED` (SOURCE_ABSENT/LOCAL_ABSENT/UNKNOWN, причинные ярлыки не придумываются).

## 3. `recovery_overlay_contract.json` — новая версия данных, A10 не меняется

Как будущие проверенные ответы (после разрешённого pilot) породят **новую версию** исследовательских
данных: A10 остаётся immutable-базой, overlay — отдельный слой. Provenance каждой строки:
`a10_preserved` / `source_verified_present` / `source_confirmed_absent` (причина UNKNOWN) /
`source_conflict` / `source_unavailable`. Правило конфликта: **обе версии сохраняются, A10 не
перезаписывается**, разрешение — отдельное решение архитектора; affected путь до разрешения UNKNOWN.
Источник absent **не означает no-trade**. Пока документ + маленький `overlay_synthetic_example.json`
(3 строки: preserved / confirmed_absent / conflict); реальная история не правится.

## 4. Блокер A5 «collect захардкожен» → предложенный diff

`a5_adaptation_proposed.diff` (r1): минимальная параметризация collect через CLI
(`--collect-ticker/figi/board/iss-day/window-start/window-end/window-interval`), **все дефолты =
текущие константы A5** → прежний collect ведёт себя идентично, регрессии нет. Локальный dry-run
построения argv (без исполнения A5) — OK: argv согласованы с diff, ни один не executable сейчас.
**Сам A5 не изменён** (hash зафиксирован в manifest); diff — предложение для owner/architect.

**Найден и исправлен дефект r1 → r2 (`a5_adaptation_proposed_r2.diff`):** r1 переименовывал
**запись** raw в `tinvest_{ticker}_1m.json`, но `load_raw`/`build_comparison` при **чтении** были
захардкожены на `tinvest_sber_1m.json` → для не-SBER пилота (VKCO) offline-сверка упала бы на
`MissingRawInputs`. r2 прогоняет ticker/interval через `load_raw`+`build_comparison` и вводит
единый хелпер `tinvest_raw_name()` (запись и чтение — одно имя; при дефолтах
`tinvest_raw_name('SBER','1min')=='tinvest_sber_1m.json'` — без регрессии). Dry-run подтверждён.
r1 сохранён immutable; A5 по-прежнему не изменён (hash `33891c24…` = manifest).

## 5. Что ещё потребует сети/приёмки адаптации (честно)

- **Сеть**: pilot-сбор (6 логических запросов) — только после одного разрешения архитектора;
  авторизации нет.
- **Приёмка адаптации A5**: collect-argv исполним только после применения diff (owner A5) —
  до тех пор collect невозможен, offline-intake (`consume`) работает уже сейчас.
- Далее в той же ветке: после ответа источника — source comparison (не ждать новой микрозадачи).

## 6. Addendum: overlay исполним и проверен (offline)

`recovery_overlay_contract.json` усилен до исполнимой form: `recovery_overlay.py` (режим `build`)
читает consume-выход + read-only A10 raw, строит overlay-слой с provenance каждой строки
(`a10_preserved`/`source_conflict`/`source_verified_present`/`source_confirmed_absent`/
`source_unavailable`) **и `new_version.csv`** (новая версия = база A10 preserved + добавки
`source_verified_present`; конфликт/absent/unavailable не добавляют строк), **A10 не мутирует**,
конфликт не перезаписывает A10. Синтетический тест `run_overlay_tests.py` — **5/5 `ALL_OK=true`**
(классификация всех 5 row_kind, `a10_mutated=false`). Каталог `.../branch_data/20261010T223233Z/`.

**Интеграционный разрыв — ЗАКРЫТ:** `consume` теперь эмитит `source_bar` (OHLCV) в `status=exists`
(ревизия `prepare_gap_recovery.py`, новый hash `71efd162…5405`). End-to-end тест
`run_e2e_consume_overlay.py` **5/5 `ALL_OK=true`** (все row_kind через реальный consume-выход):
consume (с `source_bar`) → `recovery_overlay build` → `a10_preserved` / `source_conflict` (детект
по `source_bar`, A10-версия сохранена) / `source_confirmed_absent` / `source_unavailable` /
`source_verified_present` (ядро recovery — источник вернул свечу, в A10 её нет).
`new_version.csv`: 3 строки (2 preserved + 1 добавка `source_verified_present`), absent исключён,
конфликт не добавляет строк. `a10_immutable=true`. consume-тесты 6/6 не сломались.
Вся offline-половина пайплайна (plan→consume→overlay→new_version) работает целиком; при
разрешении сети остаётся только шаг `collect`.

## 7. Ограничения

`source_truth=NOT_VERIFIED` (свечи не сверены — пилот ещё не выполнялся), `phase=development_unvalidated`,
`holdout=NOT_ASSIGNED`, прибыльность UNKNOWN, OOS/WF/PASS нет. Сеть/production/suites не запускались.
Хэши всех артефактов — в `manifest.json`.

## 8. REWORK (архитектор 2026-10-11): строгий offline intake + pilot collector

Архитектор вернул **REWORK по DATA intake** (4 пункта). Collect/network пока НЕ разрешён.
Новая ревизия в **новом** каталоге `.../branch_data/20261011T034047Z/` (старый `223233Z` не
перезаписан, hash прежний). `prepare_gap_recovery.py` = `f9571638…0a97`.

**1. Явный статус/полнота/hash (п.1):** `fetch_status`/`pagination_complete` больше НЕ
дефолтятся в `ok`/`True`; отсутствие → `invalid_input`, не `absent`. Declared hash проверяется
по каждой ISS-странице (`page_N_declared_hash_missing`/`_hash_mismatch`) и payload T-Invest;
hash файла-обёртки не заменяет проверку полноты страниц.

**2. Привязка response→план (п.2/п.3):** сверяются `window_id`/`ticker`/`figi`/`board`(ISS)/
`interval`/UTC-окно. Бар другого инструмента/окна → `invalid_input`; неизвестный window_id →
`invalid_input`; `interval≠1min` → `not_comparable`.

**3. Дубликаты и потери (п.4):** идентичные дубликаты → `exists` + `duplicate_note`;
конфликт OHLCV одной минуты → `invalid_input(conflicting_duplicate_bars)` с обеими версиями,
**НЕ** `exists`/`verified_present`. Любая нормализационная потеря строк (`bad_begin` и т.п.)
→ `invalid_input(normalization_loss_absence_unconfirmed)`, **НЕ** `absent`. `absent` — только
явно полный ПУСТОЙ ответ (причина UNKNOWN, не доказательство no-trade).

**Manual synthetic случаи (13, не suite):** `run_readiness_rework_cases.py` — **13/13
`ALL_OK=true`**; `absent` только у явного пустого happy-path; конфликт → invalid.

**4. Pilot collector вместо псевдо-diff A5 (п.4):** `pilot_collector.py` — параметризованный
read-only сбор одного окна (ticker/figi/board/interval/window/iss-day через CLI), ЯВНЫЙ
request→worker→metadata тракет, bounded timeout, **offline dry-run без сети** (4 режима
ok_present/ok_empty/incomplete/error, `network_executed=false`). **A5 не изменён.**
Wiring `run_wiring_collector_consume.py` — **4/4 `ALL_OK=true`**: формат коллектора принимается
строгим intake; error/incomplete → `unavailable`, не `absent`.

`manifest.json` = `e613c3f3…5e4e`. Сеть/БД/секреты не тронуты; A5/A10/A19 read-only;
`phase=development_unvalidated`, `holdout=NOT_ASSIGNED`. Collect остаётся НЕ разрешённым —
ждём решение архитектора по отдельному разрешению pilot.

## 9. Pilot collector с реальным launcher (immutable, offline)

Архитектор (2026-10-11) закрыл остаточный репро `pages=[]` (`prepare_gap_recovery.py` =
`98910ec7…056a`, `unavailable/no_raw_pages_absence_unproven`) и указал блокер: collect_one звал
mock_worker напрямую, без транспорта/subprocess/timeout. Эта ревизия (`.../20261011T042445Z/`)
закрывает блокер одной сдачей.

**Общий launcher для mock и real:** `launch_worker` — `subprocess.Popen(start_new_session)` +
`communicate(timeout)` → `killpg(SIGTERM)` → grace → `killpg(SIGKILL)` → reap. Тот же механизм
для обоих путей.

**Timeout smoke (ручные, `run_timeout_smoke.py`, 7/7 `ALL_OK`):** hang (mock спит 3600s) обрывается
launcher'ом за 3.01s (`terminated_sigterm`) → `unavailable(timeout)`; error → `unavailable`;
real transport gated → `network_not_allowed` (сеть не тронута); hang-control через общий
`launch_worker`; реальный transport идёт через launcher(subprocess), не in-process.

**Явные params→worker:** figi/ticker/board/interval/window/iss-day передаются worker через
request.params и строят response (не только request_echo).

**Единая формула бюджетов:** iss `logical*pages*attempts_per_page*http_timeout`,
tinvest `logical*attempts*timeout`; attempts T-Invest = `TINVEST_ATTEMPTS` (2), расхождение «+1»
устранено; различие logical requests / HTTP pages / attempts явное.

**Wiring (`run_wiring_collector_consume.py`, 4/4 `ALL_OK`):** collector(mock, через launcher) →
строгий intake: ok_present→exists, ok_empty→absent, incomplete/error→unavailable (не absent).

**Ревизия r2 (`.../20261011T052609Z/`), self-review:** исходный `042445Z` объявлен в чате
(immutable). Self-review после сдачи нашёл дефект launcher-timeout: дефолт ISS был 60s (worst
одной страницы), но один logical ISS request = до 10 страниц → 600s; launcher убил бы легитимную
пагинацию. Исправлено: дефолт `--iss-timeout` = `ISS_MAX_PAGES*ISS_ATTEMPTS_PER_PAGE*HTTP_TIMEOUT_S`
= **600s** (tinvest 40s уже корректен). Добавлен consistency-check в smoke (`launcher-timeout >=
worst одного logical request`). Timeout smoke теперь **8/8 `ALL_OK`** (`iss 600>=600, tinvest
40>=40`). `pilot_collector.py` = `1c44f037…b02f`, `run_timeout_smoke.py` = `d7ca956e…22e9`,
wiring 4/4 без изменений. Правка архитектора `pages=[]` (`98910ec7…`) сохранена. `042445Z`
superseded этой ревизией.

`manifest.json` = `28f4bb81…7da1`. `pilot_collector.py` = `4858cd0a…e1f4`. Правка архитектора
`98910ec7…` сохранена (не откатывал). Сеть/секреты/production DB не тронуты; overlay не расширялся.

## 10. Ревизия r3: реальный транспорт ISS/T-Invest (закрытие REWORK, offline)

Архитектор (2026-10-11) принял `042445Z` по структуре launcher/bounded smoke, но transport оставил
REWORK: `RealTransport.fetch` после `allow_network` ВСЕГДА делал `raise NetworkNotAllowed` —
успешного пути `allow_network=True` в коде не было (URL/params строились, HTTP/SDK-отправка — нет).
Эта ревизия (`.../20261011T053815Z/`) закрывает блок ОДНОЙ сдачей; launcher/overlay/budget НЕ
переписывались (приняты), `052609Z` (launcher-timeout 600s fix) встроен.

**Реальная ветка ISS (`fetch_iss`):** пагинация `start += n_rows` до пустой страницы; лимит
`ISS_MAX_PAGES` → `INCOMPLETE`/`PageLimit` (полнота не доказана → intake unavailable, не absent);
retries `ISS_ATTEMPTS_PER_PAGE` на страницу; каждая страница сохраняет raw `body` + `sha256` +
`n_rows`. Нижняя точка I/O — `http_get(url, timeout)` (инжектируемая; реальная = urllib).

**Реальная ветка T-Invest (`fetch_tinvest` → `_tinvest_get_candles`):** реальный SDK-путь —
`t_tech.invest.Client` + `market_data.get_candles(figi, interval, from_, to)` из `request.params`;
retries `TINVEST_ATTEMPTS`; секрет `get_settings().tinkoff_token` из env, НИКОГДА не печатается;
ошибка SDK → `fetch_status=error` (НЕ маскируется под пустые данные). Нижняя точка —
`tinvest_call(...)` (инжектируемая; реальная = SDK Client).

**Gate/default сохранены:** `--transport` default `mock`; `allow_network=False` → `NetworkNotAllowed`
до I/O; `--allow-network` явный и по-прежнему НЕ разрешён pilot. `network_executed` теперь
**вычисляется** из фактических ответов worker (поле появляется у RealTransport только когда реальный
I/O реально выполнялся) — не зашито `False`; в этом пакете всегда False (mock/gated/fake), для
будущего real-вызова станет True автоматически.

**Бюджет согласован:** `--iss-timeout` default 600s = `ISS_MAX_PAGES*ISS_ATTEMPTS_PER_PAGE*HTTP_TIMEOUT_S`
(внешний timeout с grace 5s); `--tinvest-timeout` 40s = `TINVEST_ATTEMPTS*TINVEST_TIMEOUT_S`;
attempts T-Invest = `TINVEST_ATTEMPTS` (2, не +1).

**Тесты (все offline, сеть НЕ вызвана):**
- `run_transport_smoke.py` **7/7 `ALL_OK`** — fake I/O инжектирован на НИЖНЕМ уровне
  (`http_get`/`tinvest_call`), реальный код `fetch_*` запущен целиком: ISS 2 страницы + завершение
  (start-цепочка 0→2→3, sha256/params), лимит 10 страниц → incomplete, retry после ошибки; SDK
  success (figi/from/to/interval переданы, payload+sha), SDK error (retries → error); реальная
  SDK-обвязка `_tinvest_get_candles` через fake `Client` в `sys.modules` (Client(token) построен,
  get_candles вызван с явными params, serialized=1); gate → `NetworkNotAllowed`.
- `run_timeout_smoke.py` **8/8 `ALL_OK`** (hang/error/gated/launcher-каркас/budget/consistency);
  кейсы real-transport переведены на gated `allow_network=False` — сеть не трогается.
- `run_wiring_collector_consume.py` **4/4 `ALL_OK`** (incomplete/error → unavailable, не absent).

`manifest.json` = `a81fb01a…6fe9`. `pilot_collector.py` = `eef32b71…ea6d`. `run_transport_smoke.py`
= `50ffe964…3f60`. Правка архитектора `pages=[]` (`98910ec7…`) сохранена. Сеть/секреты/production DB
не тронуты; A5/A10/A19 read-only; overlay не расширялся. `042445Z` и `052609Z` superseded r3.
