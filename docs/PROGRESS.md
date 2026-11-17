# PROGRESS — завершённые блоки

Текущий статус — [CURRENT_STATE](knowledge/CURRENT_STATE.md), очередь этапов — [ROADMAP](knowledge/ROADMAP.md). Здесь хранится датированная история завершённых изменений.

## 2026-10-18 — trailing-giveback A/B и расширение пула n=5→10

Опциональный trailing-giveback стоп для time_grid (`fef1a53`): параметры `trail_activation_pct`/`trail_giveback_pct` (по умолчанию 0 = выключен), выход `trail_exit` на закрытой свече, состояние `_gb_peak`, чистый хелпер `_giveback_step`, тесты `test_giveback_exit.py` + изоляция. Пресеты пула: `universe-move-n10.json` (`dddbd6c`), созданы `universe-move-n20.json`, `universe-move-full.json`.

A/B на .8 (2025-12-01…2026-10-08, time_grid@5min, ui-настройки, max_positions=5): tg_clean_a 801/+3 599,74; tg_trail_a (2.0/0.5) 819/+2 755,65 (−844); tg_trail_b (3.0/0.5) 814/+3 575,57 (паритет); tg_pool10 (n=10, без трейла) 1376/+22 591,32 (×6,3; без одиночного CHMF +12 269 ≈ +10 322); tg_trail_pool10 1389/+18 665,75 (трейл вредит и при n=10).

Парное сравнение tg_trail_a/tg_clean_a: на одинаковых сделках трейл даёт +14 ₽ — механика нейтральна; весь ущерб от другого состава последующих сделок (освобождённые слоты → входы с +5,2 ₽ вместо +16,3 ₽). Утечка MFE по pool10: суммарный пик +82 809 ₽ при реализованных +22 591 ₽ (~27 %), ~64 000 ₽ утекает в signal_exit; 111 сделок с пиком ≥100 ₽ отданы (<20 % пика).

## 2026-10-16 — компактная панель макета 03

По запросу владельца набор настроек над графиком сгруппирован в шесть раскрывающихся блоков со сводками. Контур и AI перенесены внутрь своей группы, пауза/старт/статусы остались постоянно видимыми. Финансовая строка уменьшена по высоте. Исходные 249 id и 159 controls сохранены по сопоставлению HTML; `quick-controls.js` прошёл проверку синтаксиса. Рабочий frontend/API не менялись; сборка и test suites не запускались. Визуальная высота не измерена.

## 2026-10-16 — полный разбор фронтенда и макет 02

По уточнению владельца разобран весь действующий frontend: статическая разметка, динамические логи/рынок/AI/сделки/тесты/склад/анализ/отчёты, графический bridge и формы. Lab/enslab/test отмечены отдельно как файлы без импорта в текущую точку входа. [Карта](knowledge/FRONTEND.md).

Прежний обзорный макет сохранён как overview-v1; основной preview заменён полной рабочей разметкой с независимыми demo fixtures и темой Solid Glass. Сохранение 249 статических id и 159 controls отражено в layout-preservation.json, исходные hashes — в source-map.json. Широкие таблицы не урезаны, есть прокрутка и разворот; панели/график регулируются. JS-синтаксис проверен. Рабочие файлы, API, торговля, БД, тестовые процессы не изменялись; сборка/test suites не запускались, визуальная приёмка в браузере не заявлена.

## 2026-10-17 — чистый базовый прогон и gap-фильтр

`tg_clean_a` и `tg_clean_b` (подряд, один процесс бэкенда, окно 2025-12-01…2026-10-08): md5 `ea42a7265917a744f9218bb8f0d93e8b`, 801 сделка, net +3 599,74 ₽ (капитал 10 000 ₽) — тот же результат, что у прежних `tg_move_v14`/`tg_g0full`. Детерминизм подтверждён и в одном процессе, и между процессами.

Разбивка базы: 447 прибыльных (+9 278,45 ₽) / 354 убыточных (−5 678,71 ₽); profit factor ≈ 1,63; все месяцы в плюсе кроме декабря 2025 (−131,44 ₽); 39 сделок с убытком ≤ −40 ₽ дают 54 % всех убытков; худший тикер ASTR (−246,47 ₽).

A/B по `gap_skip_pct`: 0.0 → 801 сделка / +3 599,74 ₽; 1.0 → 702 сделки / +3 147,52 ₽. Фильтр снимает 99 сделок вместе с 452,22 ₽ прибыли и не даёт edge — оставляем выключенным. Прежние цифры v15 (522 / +1 979,52 ₽) получены на загрязнённом состоянии и недействительны.

## 2026-10-17 — изоляция прогонов реплея

Второй прогон того же теста в одном процессе бэкенда расходился с первым: `tg_g0full` и `tg_g0full2` (одинаковые параметры, окно 2025-12-01…2026-10-08) дали разные наборы сделок; расхождение начиналось 2025-12-05 (лишняя сделка AFLT). Причина — `start()` не сбрасывал per-run состояние, а тест запускается без перезапуска процесса (`restarted=false`). Протекали, в частности, `_loss_streak`/`_last_loss_ts`: HOLD-пауза от прошлого окна лежит в будущем относительно новых виртуальных часов и блокирует входы.

Добавлен `_reset_run_state()` в `app/bot/runtime.py` (вызывается в `start()` до `_restore_swing()`), коммит `6213c60`; тест `backend/tests/test_run_state_isolation.py` (5 проверок). Проверка подряд в одном процессе: `iso_p` → `iso_q` → `iso_q2`; `iso_q` и `iso_q2` дали одинаковый md5 `723b514e14a89f34224b596e0d2d7fce` (90 сделок, net −60,90 ₽). Прогоны на `.8`.

## 2026-10-17 — детерминированный реплей

В `day_b1_20261014` / `origin/second` (коммит `3750e4d`): реплей в `feed=replay` при `pace≠wall` не поднимает фоновые wall-clock циклы, меняющие позиции/универс (`held_sync`, `vol_carousel`, `intrabar`, `queue`, `momentum`, `guard`); остаются `persist`, `hot_add`, `reconcile`, `session`, `metrics`, `imoex`. `_candle_preflight` для реплея по умолчанию `warn` (без докачки из T-Invest), `ensure_imoex_candles` в реплее не вызывается.

Проверка: `tg_move_v14` (прежний код) и `tg_g0full` (новый код) на окне 2025-12-01…2026-10-08 — одинаковый md5 набора сделок, 801 сделка, net +3 599,74 ₽ при капитале 10 000 ₽. Прогон на `.8`; сделки реплея пишутся в `sandbox_trades` c `mode='paper'`.

## 2026-10-15 — восстановление просмотра макета

По обращению владельца установлено: на 127.0.0.1:8766 отсутствовал слушающий сервер, поэтому вкладка не могла открыть макет. Статический preview восстановлен отдельным отсоединённым процессом; GET `/design-preview/` вернул HTTP 200. Лог: `/tmp/deeptrading-design-preview.log`. Автоматическое обновление вкладки отклонено политикой browser tool; пользователю предложено обновить её вручную. Торговые сервисы не затрагивались.

## 2026-10-15 — интерактивный макет фронтенда

Создан изолированный торговый обзор в `frontend/public/design-preview/`: дымчатые поверхности в направлении Solid Glass, график, показатели капитала/риска, позиции и журнал. Выбор инструмента/рисунка графика, поиск/фильтры, настройки плотности и демо-пауза локальны. Все значения демонстрационные, внешних запросов нет. Рабочий фронтенд и зависимости не менялись; тесты/сборка не запускались. Управляемый браузер в сессии недоступен, визуальная приёмка не заявлена. [Описание и этапы переноса](../frontend/public/design-preview/README.md).

## 2026-10-15 — база знаний

Собраны тематические знания, единый статус/план и каталог документации. Старые стартовые инструкции и конкурирующие очереди заменены указателями, прежние версии и происхождение сохранены в [архиве](archive/2026-10-15/README.md). Исправлены противоречия времени, IP-карты, исследовательских версий и статуса выводов. Старые результаты не объявлены заново принятыми.

Код, конфигурации запуска, frozen datasets/reference, работающие тесты и артефакты прогонов не менялись. Проверка уборки — ссылки, архивное происхождение и состав изменённых документов; торговые/сетевые/БД операции и новые test suites не выполнялись.

## 2026-10-15 — первый рефакторинг крупных модулей

Из runtime выделены models/telemetry/persistence/diagnostics; из ensemble — diagnostics; из frontend main — analytics/reports и shared HTTP/DOM/debounce. Пакетная запись, диагностика восстановления, исправления commit инвалидирования и ключа 5m-cache.

Сохранённые проверки: 33/12/7 passed в пересекающихся группах, TypeScript/Vite PASS, новые модули Ruff F/E9 PASS. Реальная задержка PostgreSQL не измерена, полный suite не запускался этим этапом. [Отчёт и артефакты](Audit/LARGE_CODE_REFACTOR_2026-10-15.md).

## Предыдущая история

[Полная прежняя хроника](archive/2026-10-15/originals/docs/PROGRESS.md) сохранена. Более старые предметные отчёты — в [каталоге](knowledge/DOCUMENT_INDEX.md); решения по исследовательским сдачам — в корневом [chat](../chat.md).


## 2026-11-15 — совместимость проверок macro v3 на .8

Исправлен импорт в test_macro_v3_reliability.py: независимая загрузка исследуемого скрипта вместо импорта соседнего теста. Проверено на Windows .8: pytest — 21 passed и 20 subtests; unittest — 21 OK. Экономические результаты не пересчитывались. Подробности: ARCH-20261115-23 в корневом chat.md.


## 2026-11-15 — qty runtime→paper

Исправлен перевод лотов в акции только для PaperBroker, с отказом при неизвестном размере лота. Проверка реального блока исполнения с SQLite: BUY/SELL, lot 1/10/0/None; весь целевой файл 23 passed. Выходные цены и net требуют следующих исправлений. ARCH-20261115-35.


## 2026-11-15 — один slippage на paper выходе

Исправлена граница _step_exit→PaperBroker, записывается actual exit_price; unknown close не снимает локальную позицию. Focused файл 25 passed, включая LONG/SHORT golden raw/filled. Другие выходы и полный net требуют продолжения. ARCH-20261115-36.


## 2026-11-15 — комиссия входа и полный net

Миграция 0005 применена на .8; фактическая entry fee сохранена отдельно. Runtime передаёт execution в итоговый журнал, полный net учитывает обе комиссии без повторного cash списания. Реальный _st_close на SQLite проверен в трёх сценариях: known, legacy unknown, qty mismatch — 3 passed; предшествующие денежные/брокерские проверки 37 passed. Полный replay ещё не проверен. ARCH-38…42.


## 2026-11-15 — изоляция журналов тестов

Открытие и закрытие ограничены текущими mode/test_name/figi. Reopened неизвестный исход не заменяется нулём. Проверки реальных методов на SQLite: 3 сценария закрытия и 1 открытия с чужими строками — passed. ARCH-43/44.


### 2026-11-15 — изолированная проверка исполнения A/A

Два завершённых runtime-прогона в отдельных БД от frozen snapshot. Денежная сверка по сделкам и балансу пройдена, повторяемость провалена: 2/3 сделки, net −18,62/−5,13 ₽; расхождение SBER 07:55. Результаты не приняты как исследовательский baseline. Подробности ARCH-20261115-47 и отчёт isolated_execution_aa_comparison.json.


### 2026-11-15 — исправлен wall-clock TTL в replay

После совпавшей изолированной пары C/D перенесён bypass TTL для portfolio_snapshot и market_regime. Четыре постоянных regression теста проходят; до правки оба replay сценария падали. Проверка ограничена свежестью данных, не подтверждает прибыльность или полноту портфельного риска. ARCH-20261115-54.


### 2026-11-15 · bounded resample cache / .8 resources

Исправлен global resample cache: content key по immutable Candle, LRU8, defensive result list.15 scoped tests pass на.8; evidence backend/reports/architect_20261115/cache_validation_20261115/validation.json. По разрешению владельца остановлен конкурирующий backend/watchdog; frozen monthly возобновил heartbeat без restart. Frozen code не менялся, экономическая приёмка с прежним cache defect ограничена. Worker назначен OBS-PROC-01.


### 2026-11-15 · harness parity integrity

IncrementalRunner проверяет неизменность исторического prefix до обновления state.10 regression failures устранены;23 scoped runner/strategy tests pass. Исправлены пустые сравнения ensemble trade parity: static.trades, nonempty assertions, valid OHLC weekday fixture;9 tests pass. Полный runtime parity не установлен. Evidence: backend/reports/architect_20261115/harness_parity_review.json.


### 2026-11-15 · session gate economics

156 cells RSI5×39equities×4windows завершены через EngineRunner с полной baseline trade parity. Единственный положительный afternoon mean+1.225bps неустойчив кcost/concentration sensitivities; edge не принят. Подробности ARCH92, backend/reports/architect_20261115/session_panel_september_r1/.


### 2026-11-15 · MOEX relative-strength candidate

Созданы causal3daymatched-stock/index snapshots; posthocgroup hypothesis перепроверена черезEngineRunner.173trades mean+10.862bps доfunding, cost/concentration sensitivitiespositive. Rule выбранонаSeptemberdevelopment; freezeOctoberбезперенастройки подготовлен. Evidence relative_gate_september_r1/review.json, ARCH100.


### 2026-11-15 · carry sensitivity без загрузки свечей

По173contexttrades рассчитаны flatnightfee scenarios иconservativeconcurrency.5bps/night/allpositionsmean+7.914bps; peak11. Тариф, portfolio return/DD не установлены. Отчёт relative_carry_sensitivity.json; ARCH103.


### 2026-11-15 · условия тарифов из PDF владельца

Проверены Trader/Investor, сохранён versioned JSON с hashes, комиссиями, ночными ступенями, обслуживанием и неизвестными параметрами счёта. ARCH104 передан воркеру. Это спецификация для модели, не готовый перерасчёт доходности; старые артефакты сохранены.


### 2026-11-15 · тарифные функции и EngineRunner

Добавлен явный тарифный CostModel, дневные ступени по непокрытой позиции и обслуживание с неизвестными льготами. 42 scoped проверки на .8 прошли, включая фактический runner BUY/SELL/quantity. Не является полной интеграцией account financing. Evidence broker_tariff_implementation_r1.json, ARCH105.


### 2026-11-15 · тариф и размер сделки

Выполнены 8 ledger-only сценариев по 173 September trades. Минимальная комиссия искажает one-share нормировку; целые лоты около 10000 RUB сохраняют Trader mean +10.8626bps до funding/service, Investor −39.1276bps. Проверены detail hash, counts, means, budget ceilings. Account return остаётся неизвестным. ARCH106, relative_tariff_sensitivity_r1.


### 2026-11-15 · validation входа и HUB assignment

check_order отклоняет некорректный капитал/номинал/сторону; 48 проверок на .8 прошли. Сохраняются отдельные runtime exception и mark-to-market defects. Worker получил явную app-zone передачу HUB-ALL и контракт исторического диапазона. ARCH107/108.


### 2026-11-15 · runtime portfolio fail-closed

Portfolio-limit exceptions блокируют pending open; 3 old-code failures воспроизведены, после 53 scoped passed. Close доступен. Новое уточнение владельца о staged наборе/разгрузке зафиксировано отдельно, не реализовано этим исправлением. ARCH109.


### 2026-11-15 · staged position accounting

Создан pure confirmed-fill ledger LONG/SHORT с частичными выходами, средней ценой, комиссиями и защитой от дубликатов; 13 golden passed. Не интегрирован в broker/runtime. Дополнительная идея conditional entry зафиксирована, без изменения frozen candidate. ARCH110.


### 2026-11-15 · conditional intent core

Добавлен чистый механизм ожидания подтверждения, отмены, TTL и dispatch с явным резервированием. 22 scoped tests passed совместно с учётом дробных исполнений. Runtime/broker integration и экономический эффект пока не подтверждены. ARCH111.


### 2026-11-15 · conditional entry EngineRunner integration

Опциональный subclass использует canonical execution и переоценивает entry policy при подтверждении. 30 scoped tests passed, disabled baseline trade dictionaries совпадают. Staged adds/economic outcomes остаются следующей работой. ARCH112.


### 2026-11-15 · историческое сравнение conditional entry

102 EngineRunner cells завершены, freeze до outcomes, baseline dictionaries и input/output/code hashes проверены. Wait10/30bps за30min на closed5m RSI дали отрицательные mean trade net против положительного immediate на общем34ticker subset. Условия/исключения/границы в ARCH113 и confirmation_panel_september_r1/review.json.


### 2026-11-15 · staged EngineRunner и временные интервалы

Дробный вход/добавка с единым риском и фиксированными исходными stop/target интегрированы в research subclass. Partial exits сверены с fill-book. 39 scoped passed, synthetic TF1/5/15/60 с одинаковым TTL30min. Историческая доходность staged неизвестна. Evidence staged_runner_integration_r1.json.


### 2026-11-15 · staged historical economics

170 cells завершены на common34ticker subset. Baseline trade dictionaries, chain cashflow/zero remainder, lot/risk/notional invariants и hashes проверены. Дробный набор снижает средний проигрыш, но уступает baseline по mean planned net; худший дециль потерь почти прежний. Полная conditional+staged+partial связка неустойчива. Только5m development, не portfolio result. ARCH115.


### 2026-11-15 · independent signal/execution timeframe adapter

Готовый higher-TF input обрабатывается causally на lower-TF execution clock с отдельным warmup и подавлением stale signals после gap. 23 scoped tests passed; данные не загружались. Историческая MTF экономика ещё не измерена. Evidence timeframe_signal_adapter_r1.json, ARCH116.


### 2026-11-15 · hub historical storage read

Подготовлен read-only DB range helper через canonical CandleSeries. 7 scoped tests passed и 12 real probes выполнены, без обрезки5000. Calendar coverage неизвестен; результаты partial. Worker передан helper для подключения в orchestrator. История не изменена. ARCH117.


## ARCH118 — explicit execution clock for context

**ARCH118 explicit context clock:** новый opt-in research ExecutionClockContextPolicy использует закрытие execution TF вместо hardcoded +5min. 17 scoped tests passed; историческая parity прежнего 5m gate: 39 тикеров, 1 695 930 решений совпали по всем source timestamps/BUY/SELL/FLAT/LONG/SHORT. Minute fixture выявляет lookahead при переиспользовании старого gate. Frozen strategy/runs неизменны; MTF economic panel ещё не выполнен. Evidence context_execution_clock_r1.json.



## ARCH119 — verified MTF export and sparse-bar evidence

**ARCH119 hub MTF export:** completed 40 instruments (39 equities+MOEX), Aug25–Oct1, 160 TF cells, 1 298 113 minute rows, single REPEATABLE READ READ ONLY snapshot via hub storage helper. Independent OHLCV aggregation/hash verification passed. All205343 common old/new5m bars match;7992 additional full bars (AFKS/VTBR/PHOR/SIBN). All77312 old bars rejected by strict minute completeness reproduce exactly from available sparse minutes, including volume; no source-free buckets or mismatches. Strict minute completeness therefore discards substantial historically consistent bars, not demonstrated corruption. HHRU minute rows0; RASP3037. Coverage remains unverified; next step preserve sparse-bar evidence separately from coverage before MTF economics. Evidence hub_mtf_september_r1/{manifest,review,sparse_review_r1}.json.



## ARCH120 — sparse history preserved with explicit uncertainty

**ARCH120 sparse hub history:** opt-in require_full_minutes=False сохраняет закрытые sparse buckets с observed minute counts, без синтеза пустых/будущих баров; coverage остаётсяpartial. Defaultstrict сохранён. 13 scoped tests pass. Новый frozen-only export 40 instruments×4TF завершён; все282655 прежних5m bars воспроизведены, независимая OHLCV/metadata сверка429354 derived bars passed. Новыйsnapshot hub_mtf_sparse_september_r1; старыеoutputs неизменны. Это готовый вход для diagnostic MTF panel, не доказанная полнота execution paths или прибыльность.



## ARCH121 — common execution clock MTF economic comparison

**ARCH121 MTF economic panel:** completed304cells/38stocks (HHRU no presample), signalTF1/5/15/60 × ungated/context, same1m execution, RSI14 35/65, SL1%TP2%, Trader fee/slip2bps, fixedwholelots. Mean trade netbps ungated: −7.267/+0.611/−21.445/−40.771; context: −4.169/+12.885/−36.308/−57.664. Context5m165trades, extra2bps/side+8.885, withoutbestticker+9.568/withoutbestday+9.320, removing8 entriesafterclockgap+13.201. Worst trade5m−117.670bps vs1m−358.486bps (notportfolioDD). Independent hashes/qty/cash/nonoverlap/allTFcoverage verified. All development, no accountfunding/service/sharedcapital/historicalshortproof; no30% claim. Evidence mtf_execution_september_r1/{summary,review}.json. Next: capital/MTM + staged/confirmation comparison on common1m clock, not blind winner promotion.



## ARCH122 — concurrency and capital capacity

**ARCH122 capital capacity:** ARCH121 ledger occupancy/cash conservation checked across all8cells. 5m/context165trades closednet+2166.466 RUB before funding; peak11positions/110208.574 RUB entrynotional, net/peaknotional1.9658% diagnostic (NOT account return). 78overnighttrades. Algebraic30% capital7211.554 RUB would imply peakgross/capital15.261× for samefixedtrades; not brokerallowed/recommended leverage and no financing/margin-call model. Goal30% unsupported, commonaccount MTM/admission stillrequired. Evidence mtf_execution_september_r1/capacity_r1.json.



## ARCH123 — shared account valuation and admission core

**ARCH123 shared-account core:** ResearchAccount переиспользует StagedPositionBook: global confirmedfill/charge journal IDs, margin-style cash, MTM equity/gross exposure, stale/unknown marks block admission, explicit hypothetical gross/equity cap includes prospectivefee/slippage. 27 scoped golden tests pass (.8). Default runtime untouched; shared canonical scheduler and historical portfolio return/DD not implemented yet. Evidence research_account_core_r1.json.



## ARCH124 — shared canonical account replay and capital scenarios

**ARCH124 shared canonical replay:** account_replay.py completed with minute clock, sortedFIGI priority, samebar proceeds barrier and admission before actual canonical entry. 22 scoped tests pass; unconstrained38ticker control matches ALL fields of165prior trades. 9real capital/grosscap cases completed, independent account reconstruction477288minute points passed. Beforefunding modelreturn%: capital10k caps1/2/3 =3.467/8.040/10.368;50k=1.989/2.705/2.872;100k=1.353/1.436/1.436. Fixed~10k orders, notcapital-scaled. FullDDUNKNOWN allcases (8040–12743stale/missingmark minutes); prefixDD not wholeperiodrisk. No30% netclaim. Evidence account_panel_september_r1/{summary,review}.json and account_replay_integration_r1.json.



## ARCH125 — observed risk lower bounds and source observation attribution

**ARCH125 risk lower bound / unknown attribution:** ARCH124 account curves audited. Capital10k caps1/2/3 observed MODEL DD lower bounds5.229%/11.276%/13.525% vs beforefunding returns3.467/8.040/10.368%; fullDD remainsUNKNOWN. At10k/cap3 unknown11309minutes:5402 have source stock candles elsewhere,5907 have no stockcandle anywhere in source; RENI2856 missingmark minutes during otherstock observations. No claimthat source gaps prove loaderdefects/no-trade or nightcalendar. Larger100k/cap3 lowerbound3.171%,6403unknownwithotherstockcandles. Evidence account_panel_september_r1/unknown_audit_r1.json. Next: prospective liquidity/quote freshness gate and source/session evidence, not retrospective removal of loss-making tickers.



## ARCH126 — activity gate comparative economics

**ARCH126 causal activity gate:** opt-in actual-entry gate requires24observed closed1m bars inlast30wallminutes; no futureprice/ticker blacklist.15tests passed. Same9capital/cap cases + canonical165trade control completed; independent477288accountpoints and960acceptedentry gatechecks passed. Returnbeforefunding improves8/9cases. At10k caps1/2/3:14.059%/18.661%/12.558% vs3.467/8.040/10.368; observedMODEL DD lowerbounds4.439/8.763/12.638% (fullDDUNKNOWN). Trades26/54/72; unknownminutes4394/6413/6683. 50k/cap1 worsened1.989→1.867%; no universal promotion. Evidence activity_panel_september_r1/{summary,review,comparison,unknown_audit_r1}.json. Net30% notproved; next capital-relative wholelot sizing and funding, then separate-period validation.



## ARCH127 — capital-relative sizing and selective-margin requirement

**ARCH127 equity-relative wholelots:** sizing targets current marked equity fraction, clips to largest affordable wholelots inclfee/slip/grosscap, restorescfg.26focusedtests pass. 6historical scenarios+165tradecontrol, independent318192accountpoints/1007sizingdecisions pass. Beforefunding returns10k caps1/2/3=11.216/8.424/−0.091%;100k=14.125/14.010/2.852%. ObservedMODEL DD lowerbounds10k6.484/11.939/17.755%;100k6.020/11.211/18.970%, fullDDUNKNOWN. New owner clarification: margin SELECTIVELY, notalways whenunsafe; requirement recorded selective_margin_scope_r1.json, implementationpending. A short can borrow even grosscap1; do not call1x unlevered/cash-only. No net30%proof.



## ARCH128 — borrowing balances, fee checks and stored instrument leverage

**ARCH128 borrowing account + actual DB margin metadata:** settlementcash/negativecash/markedshort/uncoveredtotal added to ResearchAccount. Explicit borrowingapproval False/unknown rejects incrementalborrow; confirmedreduce unaffected; replay opt-in borrowing_gate wired, legacyNonebenchmark unrestricted.42golden tests pass inclbothlegfees on6xnotional. Readonly39instrument metadata:14long>=6, maxlong6.19/maxshort5; allupdated2026-08-21, nottoday/historical-validityproof. User confirmsfee focus: Trader5000<uncovered<=50000 costs40RUB/calendarcarryday; entry+exitfees separatelyonfullnotional. Existingtarifftable alreadymodels40/75/180...; actualnightdebit integrationpending. No economic claim fromallowablebrokerleverage.



## ARCH129 — финансирование и границы блоков, 2026-11-15

**ARCH129 funding:** независимая сверка завершена: 106064 минутных состояния, 25 сделок в каждом из двух long-only сценариев. На модельном капитале 100000 ₽, gross cap 2, списано 900 ₽; результат 9.1459% без финансирования и 8.2567% после модельного финансирования. Это проверка механизма, не отбор long-only стратегии. Явные допущения: Trader, 00:00 MSK, льгота малого займа для одного счёта. Полная просадка неизвестна; плата обслуживания, margin calls и подтверждённое финансирование шортов не включены. Evidence: funding_long_september_r1/review.json. 35 scoped golden tests ранее пройдены. Общие блоки уже в engine, runtime-интеграция не завершена. Владелец требует изменения на уровне блоков проекта; контракт и незавершённые интеграции внесены в ARCHITECTURE.



## ARCH130 — общий блок разрешения займа

**ARCH130 borrowing eligibility:** общий блок `app/engine/borrowing_eligibility.py` проверяет FIGI/направление и причинную доступность разрешений в явном интервале действия. Unknown/conflict запрещают увеличение займа; funded long и закрытия сохраняются. ResearchAccount и обе точки replay (подбор размера/окончательный допуск) используют один блок. 39 scoped golden tests passed на .8, 1.01s. Evidence: borrowing_eligibility_r1.json. Пока opt-in; без policy прежний benchmark не меняется. Адаптер доказанных разрешений, распределение по ставкам риска и broker initial/maintenance margin ещё не реализованы; snapshot updated_at не заменяет срок действия. Runtime-паритет и 30% чистыми не подтверждены.



## ARCH131 — исправление портфельного допуска

**ARCH131 margin budget:** общий `engine/margin_budget.py` подключён к `bot/portfolio.py`: допуск по ликвидной стоимости и ставке риска направления; неизвестная маржа не превращается в 0, неизвестная ставка не заменяется текущим gross leverage. Ставки >=1 обрабатываются непосредственно. LiveBroker передаёт corrected_margin с учётом активных брокерских заявок, неизвестное поле сохраняет None. 80 scoped tests passed, 16 SDK deprecation warnings, .8 4.59s; mock-only broker tests, API/торговля не запускались. Evidence margin_budget_r1.json. Остаток: расходы предполагаемого исполнения в liquid value, атомарный резерв локальных pending orders, paper margin по точным ставкам вместо округлённого плеча, подключение research account и maintenance модель. Это исправление допуска, не готовая полная маржинальная модель или доказанная прибыльность.



## ARCH132 — проверка общей зависимости от MOEX

**ARCH132 MOEX dependence / owner defensive-universe hypothesis:** frozen September snapshot examined at 5/15/60m, exact synchronized adjacent complete-bucket close returns without gap filling. 5m: positive correlation 37/37 sufficiently observed stocks in BOTH half-months; corr>=0.3:35/37 then33/37; corr>=0.5:31/37 then26/37. At15m positive34/34 then36/36;60m27/27 then28/28. Preregistered diagnostic defensive criterion (corr<0.2, stock zero returns<=20%, mean stock return on market-down bars>=0, >=50pairs/>=10down bars) selected NONE in first half at every TF. Low-corr second-half MVID/LENT still have negative average returns on MOEX-down bars; worst observed paired5m drops −1.546%/−3.050%. No risk-free haven claim. Independent numpy reconstruction verified219 profiles. Evidence moex_dependence_r1.json + moex_dependence_review_r1.json. This is dependence, not executable strategy or fresh OOS: September already used in development; costs/dividends/orderbook not modeled. Portfolio allocation must assess common MOEX exposure rather than count tickers; defensive role requires causal rolling evidence and subsequent-period confirmation. ARCH131 margin integration into research remains pending; this turn followed owner's economic hypothesis.



## ARCH133 — единый бюджет в исследовательском счёте

**ARCH133 shared portfolio margin:** `PortfolioMarginPolicy` подключён глобально к ResearchAccount/run_shared_account: все текущие marked exposures + новая позиция по directional risk rates, с available_at/validity/evidence и явной collateral eligibility. Unknown/conflict/unsupported collateral запрещают вход; подтверждённые выходы не блокируются. Projected liquid учитывает fee/slippage; проверка через тот же check_margin_budget, что runtime portfolio. Wholelot sizing и final admission едины. 67 scoped golden tests passed (.8,2.21s); evidence portfolio_margin_core_r1.json. Пока opt-in, исторические rate intervals не доказаны, pending/runtime cost accounting и maintenance liquidation остаются незавершёнными; нового экономического результата нет. Owner уточнил ARCH132: совместное движение — полезное наблюдение для разработки, не повод отвергать гипотезу. Следующий экономический вопрос — причинная относительная сила/слабость MOEX и последующий net после расходов; same-bar связь не подменять прогнозом.



## ARCH136 — сравнение направления MOEX на общем счёте

**ARCH136 completed:** MarketContextPolicy explicit opposed_or_flat/aligned/any shared engine block;20 scoped golden tests pass. Historical control ALL fields165 trades/38tickers exact parity. Fixed100k/gross2 RSI5/exec1 with same prior3day relative strength/activity/sizing/fees/slip: opposed_or_flat +14.0101%/81trades, aligned −11.4200%/93trades, any +7.6984%/164trades BEFORE funding. Independent cash/equity/exposure reconstruction159096 minute points passed for3cases; code hashes unchanged. FullDDUNKNOWN (6563/11696/13708 unknown points), prefixDD not fullrisk. Source: market_direction_panel_september_r1/{summary,review}.json. Supports retaining opposed filter in THIS RSI developmental comparator; does not refute momentum/direct trend entries, prove net30%, or constitute fresh OOS. No financing/service/historical broker eligibility. Next economic comparison must distinguish reversal-vs-continuation trigger, not assume MOEX correlation implies RSI entry profitability.



## ARCH137 — проверка пробойного входа и разложение расходов

**ARCH137 completed:** existing Donchian20 continuation strategy (unchanged) compared using same38stocks/100k/gross2/RSI-panel execution/costs/activity/relativecontext.7 scoped tests pass; legacycontrol165trade parity. Returns BEFORE funding opposed_or_flat −44.512%/616trades, aligned −53.224%/508, any −64.576%/890. Independent159096 minute account points verified; code hashes unchanged. Win fractions23.86/23.03/24.38%; fixed realized paths repriced without fee/slip STILL negative (−20860/−28799/−39892 RUB), so costs not sole cause. No promotion. FullDDunknown; September development only. User asked why50/50 loses: positive RSI comparator actualwin51.85%, avgwin1078.83/avgloss−802.58 RUB, commissions6023.04, netbeforefunding14010.15. Evidence donchian_direction_panel_september_r1/{summary,review,cost_decomposition_r1}.json. Cost decomposition is fixed-path repricing, not counterfactual cost-free rerun. Next prioritize candidate stability on separate period and source readiness rather than endless September parameter search.



## ARCH138 — заморозка переноса и проверка входных данных

**ARCH138 frozen transfer / adaptive-market hypothesis:** owner notes growing neural-model usage by competitors. Recorded as hypothesis, not inferred from candles. rsi_transfer_freeze_r1.json freezes existing RSI5 opposed_or_flat/100k/cap2/activity/cost settings before August/October outcomes; execution-delay1/2/5min and extra2/5bps sensitivities planned. Read-only hub probe completed8ranges/4instruments, no brokerAPI/DBwrite: August AFKS startsAug22 (warmup/month incomplete), SBER/RENI/index earlier data present; October all four have rows throughOct30 but coverage remainsunknown. Detailed dates in transfer_readiness_r1.json; this is not full-universe readiness. New-period strategy runs not launched. Current universe/lot survivorship and previously researched data preclude untouched OOS claim.



## ARCH139 — устойчивость к ухудшению исполнения

**ARCH139 completed slippage stress:** full shared-account reruns frozenRSI5/opposed_or_flat/100k/cap2; per-side adverse slippage2/4/7bps returns beforefunding +14.0101/+8.4002/+7.0330%, trades81/85/86. Baseline complete gz trade/account artifact hash exactly matches ARCH136; legacy165trade control also passes. Independent159096 minute account points and all actual entry prices versus source opens/side-specific slippage verified. Costs change fills/exits/sizing/admission, not fixed-path deduction. Candidate stays positive in these two deterioration scenarios, not proof of AI competition resilience, new-period edge or net30%. FullDDunknown (6563/6580/6576points), financing/service/historical margin omitted. Evidence slippage_panel_september_r1/{summary,review}.json. ARCH138 input issue: AFKS missing Oct5–19, MOEX Oct10–13; exact per-day evidence sent worker. Entry-delay reruns and separate-period evaluation remain pending.



## ARCH140 — августовский вход через хаб

**ARCH140 August transfer input ready with limitations:** generic architect_transfer_snapshot.py uses hub read_history_range and aggregate_history, one primary .8 REPEATABLE READ READ ONLY transaction. Frozen July25–Sep1 snapshot40instruments (39stocks+MOEX),80cells1m/5m:1,157,606 minute rows; independent reconstruction273,822 derived5m bars passed incl hashes/OHLCV/sparse counts. Source hub_transfer_2026-08_r1/{manifest,review}.json. No SDK/backfill/DBwrite. HHRU0rows; AFKS8818/SIBN8243/VTBR10175 indicate short observed coverage, not cause classification. Fullcoverage UNKNOWN without independent calendar; current-universe/lot survivorship remains. Frozen RSI transfer rules unchanged; August economic outcome not yet evaluated. Next: causal prior3day context with old-rule parity, same account replay; no selecting tickers by future results.



## ARCH141 — перенос неизменённой стратегии на август

**ARCH141 August transfer + owner event windows:** shared relative_context.py exactly reproduces22legacy September snapshots; August729valid ticker/date features. StrategyWindow preserves warmup and explicit evaluation interval;24scopedtests passed. Frozen RSI/prior3day opposed_or_flat100k/gross2 August120trades returns BEFORE funding −5.55055% (94449.45final), versus September+14.0101%; independent54531 minute account points verified. FullDDunknown11261points, source coverage/current universe+lot limitations remain; cannot infer causal government effect or universal failure from month difference. Weekly CLOSED TRADE cashflow diagnostic: Sep14–20 +18238.10 RUB, other September exitweeks combined−4227.95; this is not weekly MTM return and spans held positions. Owner requests ±1week event windows: store independently sourced publication/first-observed times and sector relevance; pre-event side descriptive only, live gate only after available_at, no retrospectively removing bad days or labeling political cause from returns. Illustrative Trump/diesel statement was not fact-checked and is NOT stored as a real event. Evidence august_transfer_r1/{summary,review,weekly_closed_cashflows}.json and hub_transfer_2026-08_r1/relative_context.json. Candidate NOT established stable; next explain concentration through causal market regime and sourced events, not parameter tuning on August.



ARCH144: completed independent August hourly context review:744 snapshots/29760 features, future exclusion and expiry. Evidence backend/reports/architect_20261115/hourly_context_august_review_r1.json. No new economic result or runtime parity claim.

ARCH147: hourly UTC canonicalization complete;7goldens, independent29760comparisons,744previous snapshot identities unchanged. Evidence hourly_context_august_review_r2.json.

ARCH150: started existing backend/frontend with bot autostart disabled and saved sandbox mode; account and book read endpoints verified200. Previous configuration/logs backed up. No reset/orders.

ARCH154: new sandbox account funded20000 RUB, SDK and backend verified baseline/0positions/0orders; old account and data retained; bot stopped. Legacy mode-only trade statistics require session isolation.

ARCH159:8/8 sandbox/read-only metadata probes; durable operational handoff written in OPERATIONS for next sessions. Order lifecycle remains incomplete after30079 rejection.

ARCH160: offline actual broker-method probe reproduced entry signature incompatibility, partial-close quantity inflation and timeout propagation. Worker packet in chat; durable next-session OPERATIONS updated. No broker/DB operation; fixes pending.

ARCH161: corrected gate admission for invalid/empty books and invalid active metrics;36 scoped tests passed, service11 passed. Live API timestamp fix observed. Trading not started.

ARCH162: visually confirmed20k new account values and bot-off in browser; documented remaining legacy caption/history and missing main-screen book display. No control clicks.

ARCH163: captured53 legacy sandbox/live row fingerprints in read-only transaction before account migration; independent preservation auditor and3focused tests completed.

ARCH164: user-authorized LIVE viewing selected; production SDK and HTTP verified empty account and stopped bot, sandbox retained. Manual20k funding awaiting confirmation. Transfer API capabilities documented, no transfer/order submitted.

ARCH165: independently confirmed live1000RUB deposit via executed broker operation and portfolio; documented circular legacy PnL reconciliation. No trading/funds movement.

ARCH166: implemented strict30sec book freshness with44scoped passing tests; independently inspected visible real accounts, only one1000RUB observed while owner confirms second.

ARCH167: completed explicit ALL-status production account query using installed gRPC protobuf; confirmed single visible old account, excluded default-status filtering as second-account cause.

ARCH168: confirmed live21000 via SDK; identified operation-history mismatch, issued owner-requested MULTI-ACCOUNT-LIVE-01 worker assignment with concrete acceptance criteria.

ARCH169: both cursor and legacy operations now confirm21000 total contributions matching live balance; documented reporting delay behavior and immediate UI delegation rule.

ARCH175: fixed runtime-to-LiveBroker signature mismatch, confirmed no-repeat unknown-fill behavior and broker price authority in scoped offline checks.


### 2026-11-15 — ARCH178 общий пересчёт защитных уровней
Выделен app.engine.protection.advance_protection; EngineRunner и делегирующий ему IncrementalRunner используют общий переход stop/target/trailing. На .8:39 существующих engine/replay проверок и4 новых golden прошли. Рабочий runtime пока не подключён; задание и различия порядка/семантики переданы владельцу runtime в chat.md. Торговля/деплой не запускались.


### 2026-11-15 — ARCH179 общий выбор защитного выхода
EngineRunner подключён к evaluate_protection: приоритет SL/TP, раннего выхода и частичного тейка вынесен в общий блок.51 уникальная проверка engine/replay/protection прошла на .8. Runtime остаётся зоной воркера, callable и правила интеграции переданы в chat.md.


### 2026-11-15 — ARCH182 явная политика трейлинг-стопа
EngineConfig.trailing_close_based управляет общим evaluate_protection; defaultFalse сохраняет touch-семантику. Сквозные golden long/short различают касание хвостом и закрытие/гэп.56 scoped checks прошли на .8. Передача настройки из runtime/replay конфигурации остаётся задачей интеграции.


### 2026-11-16 — ARCH183 кандидат интеграции runtime
Подготовлена отдельная замена _step_exit с общим protection; основная копия runtime не изменена. Проверка hash-guarded подстановкой метода в отдельном тестовом процессе:2 long/short случая прошли, включая сохранение позиции на текущем баре и срабатывание на следующем гэпе. Файлы и ограничения переданы воркеру через chat.md; требуется интеграция с его исполнением/счетами.


### 2026-11-16 — ARCH185 рабочий runtime использует общий protection
После ACK владельца изменён только _step_exit, остальные свежие account-правки сохранены.65 scoped tests проходят на настоящем runtime без подстановки кандидата. Торговля не запускалась. ARCH186 подтверждает оставшийся entry-дефект: TimeoutError после отправки ошибочно REJECTED;3 adapter tests pass/1fail, исправление остаётся у воркера исполнения.


### 2026-11-16 — ARCH187 лот и номинал входа
В _submit_order устранён повторный расчёт лота для портфельного риска с default1; используется тот же подтверждённый лот, что в сайзинге. Неизвестный лот и некорректная цена блокируют вход.18 scoped checks pass на .8; изменения сообщены воркеру.


### 2026-11-16 — ARCH188 бюджет и лимит входа
_submit_order прекращает вход при неизвестном бюджете/лимите, не восстанавливает потерянный тестовый капитал и ограничивает количество cash-лимитом при выключенной марже. Один ответ maxlots используется повторно.20 entry/portfolio tests и6 logging tests проходят в целевых прогонах; upstream gates подменены.


### 2026-11-16 — ARCH190 общий лимит экспозиции
Устранён fail-open при ошибке/неизвестном equity и повторный default1 лот в лимите экспозиции. Неизвестная отслеживаемая позиция не считается нулевой.27 scoped portfolio-entry tests проходят; покрытие ручных/внешних позиций локальным fallback остаётся ограничением.


### 2026-11-16 — ARCH191 совместная приёмка
Архитектор убрал local-held fallback для live/sandbox account exposure при неизвестном broker commitment;34 entry/portfolio tests pass. Независимый прогон новых правок воркера:16 partial/timeout/mode-selection checks pass. Остаток позиции после частичного закрытия, structured open result и долговечный intent пока не приняты.


### 2026-11-16 — ARCH194 цена защитного выхода
В _step_exit live/sandbox сохраняют цену из trade.exit_price, а не триггер стопа с синтетическим проскальзыванием. Модель проскальзывания оставлена PaperBroker.3 scoped tests pass, включая94 trigger/93.5 executed. Частичные закрытия/unknownintent остаются ограничениями.


### 2026-11-16 — ARCH195 ошибки включённых фильтров
_submit_order блокирует вход при ошибках данных рейтинга, HM-veto и включённого секторного лимита. Выключенные rank/HM не запрашивают данные.48 scoped entry-gate/portfolio tests pass; семантика пустой истории не менялась.


### 2026-11-16 — ARCH197 защитный выход при неизвестной заявке
_step_exit не отправляет закрытие поверх PENDING_RECONCILIATION той же бумаги.11 scoped tests pass. Остальные прямые close_position callers переданы воркеру для общего durable guard; это не подтверждение защиты через рестарт.


### 2026-11-16 — ARCH201 общий блок маржинального допуска
Runtime _submit_order использует check_margin_budget, общий с PortfolioMarginPolicy.63 scoped tests pass. Согласована граница required>=liquid; источники ставок, прогноз расходов и резервы pending-заявок ещё требуют интеграции.


### 2026-11-16 — ARCH202 защита частичной заявки
_submit_order/_step_exit учитывают PARTIAL_FILLED в блокировке повторной отправки.14 scoped tests pass. Воркеру передан дефект потери pending-остатка в новой ветке частичного входа; durable intent и резервы средств остаются открытыми.


### ARCH204/205 — 2026-11-16, приёмка исполнения и выбора режима
Добавлен offline integration test частичного входа: настоящий runtime+adapter теряет pending intent после исполнения1из3лотов (лот1/10),2failed; передано воркеру ARCH204. Независимо подтверждено исправление выбора режимов:10passed2.67s, прежние2ошибки устранены. Это не запуск торговли и не приёмка развернутого сервиса.


### ARCH206 — 2026-11-16, проверка частичного защитного выхода
Настоящий _step_exit воспроизводимо объявляет полное закрытие при исполнении4из10штук. Расширенная приёмка1failed11passed2.45s на .8. Архитектор отвечает за интеграцию защитного выхода; воркеру переданы требования общего report/intent/apply-fill. Реальные заявки не выполнялись; дефект ещё не исправлен.


### ARCH211 — источник ошибочного результата работающего API
Read-only DB:−909.387925 принадлежат35старым LIVE строкам без account identity; выбранный счёт имеет0строк. HTTP округляет ровно эту чужую историю. Процесс8000 запущен до текущих исправлений. Артефакты account_scope_readiness_r1.json и serving_process_readiness_r1.json; worker уведомлён, перезапуска/изменения истории/заявок не было.


### ARCH212 — сохранение размера лота и проверка смены контекста
_submit_order сохраняет проверенные lot_size/requested_lots в заявке;35passed2.63s. Приёмка mode handler расширена:4unsafe transitions воспроизведены,10предыдущих проверок проходят. Worker получил consumer/route integration задачи. Запущенный сервис не менялся.


### ARCH214 — устранено неявное включение старого списка
Общий загрузчик ручного юниверса требует явного пути; существование исторического файла больше не включает его в новом процессе.12passed2.56s, явный выбор и сброс кэша проверены. Файл сохранён, рабочий сервер не перезапускался.


### ARCH215 — исправлен читающий путь автоматического отбора
Actual load_all путь воспроизвёл ValueError границ read_range. Selector переведён на существующий bounded hubtail нужного окна.1passed; настоящий selector на .8 реальной БД выбирает MGNT/ALRS/PLZL/SNGS/AFLT, без старогоoverride. Скачивание отключено только в диагностике; сервер и сделки не запускались.


### ARCH217 — move-selector переведён на общий хаб
Свечи читает существующий hub tail с явным as_of, только закрытые в окне.19passed2.58s и actualPG read-only day/hour проверка на SBER подтверждены. Другие дефекты модуля не объявлены исправленными; рабочий запуск не выполнялся.


### ARCH218 — исправлено окно движения и получен реальный рейтинг
Отделён RSI-прогрев от Nдневного движения,24passed2.77s. На actualHub/PG READONLY рассчитаны rise/fall3day для31eligible бумаг, артефакт move_three_days_r1.json. Конфигурация live не менялась; разрешение шортов/издержки/торговое преимущество этим результатом не установлены.


2026-11-16 ARCH228: общий брокерский отбор без eligible_tier добавлен и проверен на .8 (18 tests). Реальная цепочка каталог → 40 кандидатов → directional availability → Hub → 3-дневный rank сохранена в broker_universe_pipeline_r1.json; 38 long / 37 short. Интеграция старых runtime/API consumers не завершена; торговли и записи БД не было.


2026-11-16 ARCH229: общий отбор подключён к ordinary startup/hot-add Live/Sandbox в исходниках; новый entry gate и запрет автоматической ликвидации по выпавшему кандидату. 25 scoped checks; actual runtime refresh read-only подтверждён runtime_universe_r2.json. Deployment, Tests/replay и остальные потребители не завершены.


2026-11-16 ARCH230: sizing Live/Sandbox использует подтверждённый свежий lot из admission; stale universe/DB больше не перекрывают его. 42 scoped checks прошли. Execution consumer/partial fills остаются задачей воркера; operational readiness не заявлена.


2026-11-16 ARCH231: изоляция допуска Live/Sandbox и целостная публикация результата refresh; 12 focused checks pass. При ошибке Hub новый каталог не смешивается со старым membership.


2026-11-16 ARCH232: manual Live/Sandbox selection подключён к общему admission в startup/refresh; сохранение порядка/side, authoritative lot, причины отказа. 24 scoped checks pass.


2026-11-16 ARCH233: воспроизведён дефект округления full-close в LiveBroker на qty0/5/15 при lot10; 3 новых failing regression cases, совместно с partial protection17pass4fail. Исправление исполнения не завершено; worker notified.


2026-11-16 ARCH234: устранено молчаливое игнорирование min_turnover; positive threshold вызывает явную ошибку отсутствующего оборота без legacy fallback. 29 scoped checks pass. Сам liquidity filter остаётся незавершённым.


2026-11-16 ARCH235: общий turnover_veto для queue/entry закрывает обход enabled liquidity threshold через0/None/NaN/Inf.33focused checks pass. Source timestamp/исторический оборот остаются открытыми.


2026-11-16 ARCH236: исключено использование сегодняшнего turnover cache в replay candidate score; неизвестный historical turnover явно представлен None.27focused checks pass.


2026-11-16 ARCH237: закрыт fail-open исключений _strength_for; actual queue regression для ошибочных price/history/quorum.40focused checks pass.


2026-11-16 ARCH238: совместный scoped checkpoint152pass, known execution failures отдельно остаются; фактический HTTP статус .8 подтверждён r4 (бот остановлен, mode discrepancy сохраняется).


2026-11-16 ARCH240: partial entry retry guard fixed; close quantity validation strengthened; trade_history mode/account/test/as_of isolation implemented and actual read-only PG checked (scoped_history_r1.json).17combined checks pass, partial protective exit still open.


## ARCH241 — no-margin four-slot profile persisted; config plumbing fixed
Owner requested no margin and 3–4 tickers. Added use_margin to BOT_PERSIST_FIELDS, config PATCH/payload and explicit start-request construction. Actual PATCH→file→reload tests false/true across Live/Sandbox/Test:2pass2.99s, isolated temporary settings and mocked DB; no broker calls. Fresh .8 process checked HTTP bot stopped, loaded existing saved config, saved and reread use_margin=False, leverage=1, margin_leverage=1, margin_sessions=[], short_allowed=False, top_n=4,max_positions=4. pos_pct=.2 retained; comparison confirmed no other existing persisted fields changed. No bot start/reload/orders. Loaded old service still needs coherent deployment; saving config is not live readiness.
Read-only runtime candidate refresh top4 at23:52:45Z:VKCO5.706%,SMLT5.533%,MVID5.426%,HYDR4.207% ATR; receipt runtime_universe_top4_r3.json. Historical volatility candidates, not buy signals.
Worker UI task: expose persisted use_margin clearly, display long-only/four-position profile; no concurrent edits to architect execution/config changes. Protective-exit source progress since ARCH240: intent recorded before SDK, client ID passed through, expected units checked; partial/zero/timeout retain pending and block retries in-process.30 scoped checks passed previously; durable reconciliation and full accounting remain open. No profitability or full execution readiness claim.


## ARCH242 — contradictory broker quantities remain unknown
LiveBroker._execution_from_response previously clamped overexecution to requested and returned FILLED. Now exact integer requested/executed quantities must match local requested quantity and satisfy bounds; contradictions/missing quantities yield UNKNOWN, executed_lots/remaining_lots=None, error=inconsistent_broker_quantity. No synthetic zero or successful fill. open_position returns price only for FILLED/PARTIAL, including prevention of zero-executed positive-price response being treated as entry.
22 scoped checks pass2.51s: response contracts plus actual runtime→LiveBroker offline tests lot1/10, executed1/0/7/-1. Unknown keeps intent pending, writes no entry, second call sends no duplicate. Partial still records one confirmed lot. Tests do not prove restart recovery or actual SDK roundtrip. No deployment/orders. Worker preserve these execution changes; durable reconciliation remains outstanding.


## ARCH243 — confirmed entry lot carried through accounting/protection
Found _st_open, partial and full _exit_qty independently reread Instrument.lot and defaulted to1. Shared _entry_lot_size now prefers positive exact-int order.meta.lot_size captured at sizing; LiveBroker requires it, missing/invalid rejected before submission. Nonbroker paths can query DB but unknown no longer becomes1. _st_open and full/partial protection use same resolver.25scoped checks pass2.61s (partial runtime→adapter lot1/10/1000 with intentionally stale DB lot999; invalidlot noSDK; confirmed sizing; close identity). Actual ledger persistence and nonbroker legacy paths still need scoped coverage; no complete parity claim. No restart/orders. MOEX-relative selection remains hypothesis, existing rules unchanged.


## ARCH244 — actual entry ledger persistence checked offline
Added test_entry_ledger_lot_acceptance: actual _st_open + SQLAlchemy ORM insert/read in isolated SQLite, broker modes live/sandbox and confirmed lots1/10/1000.6pass2.37s: two executed lots become2/20/2000shares, account/mode/lot metadata and SL/TP preserved, open net remainsNone. Instrument table absent deliberately: confirmed snapshot path requires no legacy instrument lookup. SQLite IDs assigned by fixture because production uses BigInteger; this is not PostgreSQL/real SDK acceptance. Remaining defect observed: _st_open catches persistence exceptions and logs without propagation, so callers can continue after failed ledger write; next architect execution task. No production DB writes/restart/orders.


## ARCH245 — entry ledger failure stops new admission and retains intent
_st_open no longer swallows transaction errors. _persist_entry_fill catches at runtime execution boundary, sets entries_paused, retains PENDING_RECONCILIATION with confirmed executed lots/price and ledger_write_failed, emits reconciliation reason. Partial/full paths still populate held/protection state from confirmed fill; full failure does not emit normal success events. No retry submission on next _execute_pending.
48scoped checks pass2.90s: actual adapter responses full/partial/unknown, lot1/10/1000, injected ledger failure; actual ORM successful commit and injected rollback/commit failure propagation in live/sandbox fixtures. In-memory protection metadata survives but existing pending guard blocks protective execution until reconciliation; this does NOT prove exits remain operational during a ledger outage. Durable persistence/recovery/reconciliation still required before live readiness. No deployment/orders/production DB writes.


## ARCH246 — reconciliation operations bound to broker scope
_reconcile_positions used UI sandbox._get_operations global mode/cache, potentially different from active broker account. Replaced with LiveBroker.operations(start,end) bound to instance account/mode; live operations.get_operations / sandbox.get_sandbox_operations. Verified installed .8 SDK signatures by inspection; no network call. Invalid/missing account and invalid windows rejected; no UI cache reused.4 scoped offline checks pass2.75s including route/account isolation and unknown position preservation.
Audit: existing reconcile only handles positions, never resolves pending orders. Also orphan handling currently puts gross price difference into net_pnl while commission=None; operation matching lacks exact order/quantity linkage. These are outstanding, not readiness. Worker task: review isolated order-state SDK support and identity semantics, report in root chat; do not concurrently alter architect runtime/live_broker. No restart/orders/DBwrites.


## ARCH247 — Universe admission checked at actual submission; orphan net correction
Owner reiterated orders should originate from Universe. Extracted common _broker_candidate_admission used at _submit_order and immediately before LiveBroker.open_position. Checks current mode, fresh catalog, directional eligibility, current membership; execution also requires same lot as sizing snapshot. Removed ticker cannot execute queued order.66 runtime offline cases pass2.83s incl removal before execution/noSDK, full/partial/unknown, ledger failure, lot1/10/1000. Prior combined universe/runtime/reconcile52pass3.05s. No rule/ranking change or real submission.
Orphan reconciliation correction from prior interrupted turn: broker operation must be executed, full exact share quantity, zero remainder, entry<=timestamp<=now, unique qualifying closing operation. Uses operation time; commission/net remainNone rather than gross represented as net.6focused reconcile checks passed; included in combined52. This is still heuristic operation matching (not exact order identity); split fills/roundtrips need dedicated reconciliation, unknown preserved. Worker retain scoped SDK order-state research task. No deployment/orders/productionDBwrites.


## ARCH248 — request-ID broker order-state reader
Inspected installed .8 SDK OrdersService/SandboxService get_order_state signatures and OrderState schema. Added LiveBroker.order_state(client_order_id,figi,side,requested_lots): bound account/mode, explicit ORDER_ID_TYPE_REQUEST, validates returned order_request_id/FIGI/direction/requested lots/exchange ID and executed bounds. Errors propagate, never treated as cancelled/zero.16offline routing+identity/timeout tests pass2.24s. Initial import failure corrected: OrderIdType exported from t_tech.invest.schemas, not package root. No network/order calls; runtime reconciliation integration and terminal-state handling remain next work. Worker scoped SDK review may use this method, do not duplicate API path.


## ARCH249 — pending entry status polling integrated
_reconcile_loop calls _reconcile_pending_orders before session-hours filter (still respects reconcile_enabled). Bound LiveBroker request-ID reader validates identity; original requested_lots metadata used, not mutated remaining order.qty. Terminal CANCELLED/REJECTED with zero execution removes pending entry; timeout/mismatch/new/full/partial remain pending. Observed execution regression is rejected; prior partial cannot become zero. Scope/object identity checked after await. Broker observation metadata recorded in memory; no resubmission or automatic entries unpause.
34offline checks pass2.67s: actual runtime→LiveBroker→stub SDK across live/sandbox, terminalzero/new/full/partial/cancelledpartial/timeout/identity/regression. Full/partial accounting application and close-order polling not implemented yet; durable recovery absent. This integrates observation and terminal-empty resolution only, not complete reconciliation. No deployed service change/orders/productionDBwrites.


## ARCH250 — cumulative entry ledger by request identity
_persist_entry_fill attaches client_order_id; _st_open persists it and confirmed lot in metadata independent of entry-card rendering. Same account/mode/FIGI/request identity updates cumulative qty/average entry price on existing open row, preserving original time and protective levels; repeated same result does not add a row or increment entry funnel. Regressing quantity, changed side/lot, already closed matching record, duplicate identity or different open entry rejected. Legacy entries lacking request identity retain old behavior.
78scoped offline checks pass3.51s (actual ORM repeated2→3→3 lots plus stale1/different request rejection, lot1/10/1000 modeslive/sandbox; runtime admission/partial/ledgerfailure suite). This is sequential idempotence, not DB unique constraint or concurrent-process proof. Pending fill application still not wired; durable intent/recovery remains open. No deployment/orders/productionDBwrites.


## ARCH251 — architecture ownership / concrete decision map
Owner requests architect explain why/how, builders later. Actual saved profile read: ensemble_v4 true, top_n4/max_positions4, rank_enabledFalse, queue_enabledFalse, five5m setups quorum2 entry1min; regime RANGE permitted. Actual read-only current-profile refresh38candidates, leaders MVID/TRNFP/SIBN/CHMF by5m ATR; prior r3 day-ATR top4 was diagnostic nonensemble and is corrected, not portfolio forecast. Receipts decision_config_r1.json/runtime_universe_saved_profile_r4.json.
Appended evidence-backed stage map and D1–D5 builder packages to docs/knowledge/ARCHITECTURE.md. Architect owns D1 bounded actual decision explanation; worker .8 assigned D2 trace/UI (existing tools, distinguish admitted/signaled/blocked/filled, effective settings and identity links). Do not alter runtime/live_broker concurrently. D3–D5 future work packages, no new agents claimed. No bot start/orders; full cycle/profitability not established.


## ARCH252 — first concrete data/strategy decision slice
Actual read-only .8 Hub bars + saved _build_ensemble_params + EnsembleV4Strategy.on_bar at fixed2026-11-13T12:00Z for four current ATR leaders. Receipt decision_stage_r1.json; reproducible stage probe inspect_decision_stage.py.4379/4242/4323/4309 closed1m bars, tails11:59Z, input hashes+effective per-instrument params saved. No backfill/SDK orders/DBwrites.
Results: MVID emitsSELL, contributing pullback_ema + range_compression_breakout, underlying entry11:35Z vs lastbar11:59Z; global short_allowedFalse would block via gate_direction. TRNFP blocked HIGH_VOLATILITY; SIBN newest10:50Z andCHMF09:25Z -> no_fresh. MVID signal summary lists4members despite5enabled setups, RSI absent; investigate why rather than claim all5 voted. All funnel counters0 at11:59 despite signal: UI evidence must distinguish logged5m boundary from signal evaluation.
Scope caveat: isolated current-profile strategy evaluation on historical bars for current-selected instruments; NOT historical selection, sequential runtime state, full gates, or profitable trade test. D1 remains incomplete. Worker D2 add source event time vs evaluation time, stage-specific skip and actual member count; do not show ATR leaders as guaranteed entries. Architect continues determining source/effective member and stale-signal semantics.


## ARCH253 — explained adaptive members and event reuse
Actual MVID stage rerun fixedNov13 12Z, receipt decision_stage_mechanics_r2.json. drop_useless=True confirmed in effective EnsembleParams. ensemble._drop_useless_last reports rsi_reversal removed for0% matches with oracle reversals;4remaining withquorum2. This is dynamic retrospective signal-quality filtering, not static5-member quorum and not evidence RSI useless out-of-sample.
EnsembleV4Strategy uses FRESH_MIN=30 and returns Signal.time=lastbar while features.entry.ts retains underlying event. Repeated on_bar on identical inputs returns sameSELL/eventQ53(11:35) at11:59. No dedup inside strategy proven; this is NOT proof repeated broker orders because runtime held/cooldown/pending gates apply. Need sequential runtime trace of event reuse after rejection/exit/restart. No configuration change/order.
D1 next: chronological prefixes around11:35→12:05, record member-set/event identity/gate outcome; distinguish retrospective recalculation from live causal decisions. D2 worker: show enabled vs effective members+drop reasons, event_time/evaluation_time/age, avoid headline fresh signal based only Signal.time. D4 future builder: freeze member selection before evaluation; oracle-quality comparisons in same history cannot establish tradable edge.


## ARCH254 — event reuse policy and cooldown meaning
Actual SignalPolicy + gate_direction probe for saved MVIDSELL event: FLAT→ACCEPT_ENTRY, SHORT→IGNORE_SAME_SIDE, FLATagain→ACCEPT_ENTRY; actual saved config reentry_cooldown_bars=0; direction gate returnsshort_disabled. Receipt signal_reuse_policy_r1.json. This confirms policy reuse, not whole runtime duplicate order. In inspected _process_candle flow event identity is not consumed/deduplicated; held/pending/risk/session gates can still block. Absence of _last_signal_side usage beyond init/reset reinforces need explicit lifecycle contract, not claim of successful repeat trades.
Source audit: cooldown if enabled measures global _bar_counter incremented for every accepted ticker candle, not per-FIGI bars. Thus enabling15 does not mean15minutes of that ticker in38ticker feed. Current zero means this latent behavior is inactive. No configuration change.
Architecture D3/D4 acceptance addition: distinguish same event retry after temporary rejection from reuse after confirmed fill/exit; stable identity (FIGI,side,source_event_ts,policy/config fingerprint), persistence across restart, explicit rearm rule. Demonstrate actual sequential runtime trace for allowedBUY equivalent, partial/unknown/exit; do not equate pure policy result with order. Worker UI show source-event age and rejection chain; future builder owns lifecycle after zone handoff.


## ARCH255 — chronological visible-prefix evidence
Ran inspect_decision_prefix.py through actual Hub+EnsembleV4Strategy on same MVID instance at selected chronological checkpoints11:34/35/36/40/59/12:05Z Nov13. Every call receives only bars with ts+1min<=decision; no future suffix passed. Receipt decision_prefix_r3.json preserves inputs/config provenance via parent probe fields. Not every-minute/full runtime replay.
No signal at11:34/35/36; event entry.ts11:35 first appears among sampled checkpoints at11:40(lastbar11:39), then remains sameQ53/member pair at11:59 and12:05. This is consistent with start-stamped5m bar[11:35,11:40), not proof of lookahead or execution at11:35. Need finer checkpoints to establish first availability exactly; do not backdate orders toevent timestamp. RSI drop reason unchanged at all sampled prefixes. Missing11:34minute observed (11:35checkpoint lastbar11:33); no fabrication/fill.
Architecture acceptance: distinguish source_bar_start, available_at, evaluated_at, broker_submitted_at, filled_at; event reuse shown by same source identity despite new Signal.time. Worker UI task D2 uses availability/evaluation explicitly. No signal policy/config change, no broker orders/DBwrites. D1 fullruntime gates/slots still incomplete.


## ARCH256 — effective MOEX gate state verified
Fresh _cfg_from_saved(live)+apply_gates_config confirms imoex_guardFalse,daily_biasFalse,queue_enabledFalse,rank_enabledFalse. Actual _imoex_block_reason with synthetic downward state(active−1,pct−1%,move−30) returnsNone under effective saved config. Receipt moex_gate_effective_r1.json; synthetic test is NOT observed market move. No config write or network/order call.
Therefore previous proposed MOEX-permission→relative-strength→signal chain is hypothesis, not current profile. Existing direct index guard, when enabled, is spike/chase gate (thresholds0.8% OR20points/20min, chase1.5%), not blanket trend-long gate. Ensemble bias hour50 is ticker's candles, not automatically MOEX. Queue RS score references index but queue disabled; do not claim it determines entries.
Source follow-up for builder data/parity: _load_imoex_buf reads SQL directly (bypasses requested Hub) with ts<=now rather than closed-time; replay may preload future buffer, _imoex_guard_snapshot useslastbuffer while move_at filtersnow. Inspect consumer influence before claiming actual lookahead. Do not modify isolated _is_closed. D2 UI must distinguish available index data, computed state, enabled gate, actual rejection. Architect retains current rules; no automatic enabling based on this audit.


## ARCH257 — actual candle-processing path traced
Ran inspect_decision_runtime.py on .8: real Hub warmup, saved profile _build_ensemble_params, actual EnsembleV4Strategy and actual PaperBotRuntime._process_candle atNov13 11:40Z. Replay clock and offline broker.get_position(None); _submit_order trapped. Receipt decision_runtime_r4.json. Actual events SIGNAL_CREATED SELL → SIGNAL_REJECTED SHORT_DISABLED → NO_TRADE short_disabled; signals_seen1/candles_seen1/submit_calls0. Unlike prior policy-only probe this verifies candle→strategy→runtime direction rejection in one call. No fake signal injected. No DB writes or broker orders.
Limits: single actual candle after warmup, offline FLAT broker, runtime not started, no acceptance path through sizing/execution; _submit_order interception means no claim full live/sandbox cycle. D1 first concrete rejected-decision trace accepted; continue to an actual BUY event with existing limits without tuning strategy to force trade. Worker D2 use this receipt as example of gate reason and stage separation.


## ARCH258 — owner freezes ensemble approach for next session
Owner asks whether votes are valid/weighted and whether ensemble should be abandoned, then explicitly chooses tomorrow test existing system and reason from observations. Preserve strategy/policy configuration; no new ranking/weights/indicator changes before baseline observation. baseline_before_session_r1.json records source/config hashes only (not deployed readiness).
Worker D2 immediate observation fields: enabled/effective setup list and drop reason; each vote side/source bar/availability/evaluation/age; quorum membership and conflicts; veto stage; intent/client/exchange IDs; partial/final executed units; actual fees orUNKNOWN. Existing trace/funnel used; no extra trading system. Readiness still has execution/reconciliation/deployment blockers; do not turn this instruction into claim all is ready or start Live automatically.
Fixed4ticker×4timestamp diagnostic completed before steering: decision_buy_candidates_r5.json preserves all16 outcomes. OneBUY:TRNFP Nov13 10:00Z; threeSELL;12no-signal. Selected as technical BUY-path specimen only, not performance selection. Prior MVID rejection trace remains valid. Next bounded architecture work: actual BUY sample through runtime gates with order submission blocked; current rules unchanged.
Future ensemble comparison: current profile vs individual component signals, identical visible data/costs/capital/session policy, frozen before evaluation; report agreement/dependence and marginal contribution of second vote, not just raw win rate. One session is operational evidence, not profitability proof.


## ARCH259 — actual BUY specimen reached submission boundary
Actual _process_candle TRNFP atNov13 10:00Z, Hub warmup/savedstrategy, offlineFLAT broker/replayclock; _submit_order intercepted (no actual submission/gates within that method). SIGNAL_CREATED BUY and exactly1call _submit_order(open,BUY). Receipt decision_runtime_buy_r5.json. Votes range_compression_breakout+donchian_breakout (both breakout-family), quorum2of5, regimeTREND_UP,biasUP. Quorum source09:30Z, entrysource09:45Z, evaluationbar09:59Z. RSI configured but0signals in this window. Standard indicators and two votes do not imply independent evidence; compare marginal value later.
This proves data→ensemble→policy/runtime pre-submit path for a BUY, not sizing/portfolio/fill/fees. No real orders/configchange. Complements MVID direction rejection, gives D2 worker concrete positive and negative fixtures. User baseline-next-session direction preserved.


## ARCH260 — BUY path into actual preparation; trace-only refusal
Ran actual _process_candle→_submit_order for TRNFP historicalBUY, Hubwarmup/currentprofile, one-instrumentcontext/replayclock and offlineFLATbroker. No order executor called. r6 only eventSIGNAL_CREATED and no pending; r7 instrumented _trace_order_reject reveals exposure_unknown/AttributeError because offline broker lacks portfolio methods. Receipts decision_runtime_buy_gates_r6/r7.json; limitation of fixture, NOT failure of live broker or proof live order would be rejected. No invented exposure fallback; stopped with0pending/0orders.
Architecture evidence: some entry refusals only go to optional trace, so ordinary event log can hide reason between signal and intent. Worker D2 promote unified observable rejection with stage/reason without changing decisions. Next acceptance needs complete immutable broker/account snapshot adapter, not silently assume zero holdings/cash. Telemetry daily_bias_map/mtf_macd_map source uses directSQL ts<=cut (and interval5 ambiguity), so historical gate parity not established; do not claim this diagnostic validates historical execution or profits. No strategy changes/productionDBwrites/orders.


## ARCH261 — existing signals/extrema inventory, owner clarification
Owner clarifies favorites=extrema plus ML labeling points. Actual .8 readonly counts: signals371720/strategy_runs649; lab_signal_events12417376, lab_signal_event_runs3786125, lab_experiment_runs13; traceevents9388/runs11; signal_decisions0. pg_stat n_live_tup all0 was unreliable, exact queries verified. Receipts signal_inventory_r1.json (estimates only), signal_inventory_exact_r2.json, labels_schema_r1.json, label_coverage_r2.json.
ideal_trades from label_backfill→engine.labeling.zigzag_trades: two configs,1026+513rows,3FIGIs,600sec,Aug27–Sep24. labels from triple_barrier: two configs7315each, same3FIGIs/600sec/Aug27–Sep24. Names now tied to source; cannot claim full36ticker label coverage. Lab runs heterogeneous/partial, all dataset_version blank, code_version sometimesunknown; do not merge/count repeated runs as independent votes.
Existing ml_mtf_dataset.py reuse_rationale explicitly warns old DB labels are not session/wall-clock compatible and label_votes.parquet has upper-first dual-touch/provenance defect. Preserve old data; use only exploratory matching with disclosed labels or locate accepted newer dataset. No recalculation/backfill/DBwrites. Next architecture task: resolve canonical accepted feature/target artifacts and map signal/run/params/time identities before pairing. Worker D2 reuse Lab events/eventruns, do not display12million rows as12million independent decisions.


## ARCH263 — BUY gates diagnostic advanced; replay timestamp mismatch
Previous turn ARCH262 made progress by publishing bounded worker assignment. New read-only .8 runs: inspect_decision_runtime_buy_gates_r8/r9.py, receipts decision_runtime_buy_gates_r8/r9.json. Run as python -m reports.architect_20261116.inspect_decision_runtime_buy_gates_r9 from backend (direct script invocation lacks app path).
Actual Hub/ensemble/_process_candle/_submit_order TRNFP Nov13 10:00Z. Explicit synthetic equity20000/empty positions: r8 rejects PORTFOLIO_LIMIT MARGIN_INPUT_UNKNOWN; r9 additionally provides synthetic liquid20000/starting_margin0/minimal_margin0 and produces one internal SUBMITTED BUY qty1, pending1. _execute_pending trapped; no broker API, order execution, bot start or production DB write. These are explicit fixture assumptions, not actual account values. Generic broker bypasses LiveBroker catalog/slot budgeting/maxlots branches; qty1 is NOT a live size forecast. Current historical telemetry semantics not fully verified; not full parity acceptance.
Concrete clock mismatch: r9 internal order.created_at=2026-11-16T00:28:45.753328Z while replay as_of=2026-11-13T10:00Z. Preserve observed receipt. D2 UI/trace must not conflate wall-clock creation and simulated event time; architect to inspect BotOrder clock ownership before changing runtime. Portfolio snapshot also swallows positions() exceptions into an empty list in runtime_telemetry.py; this needs a focused failure-path review before claiming account exposure authoritative. No production code/config changes in this block.


## ARCH264 — runtime intent timestamps use bot clock
Fixed both BotOrder creation sites in runtime.py (_submit_order entry and _step_exit protective close) to pass created_at=self._bot_now(). Previously dataclass wall-clock default dated historical intents at current date. Existing bot clock contract unchanged; no _is_closed/feed-clock modification. Within inspected bot sources created_at is serialized, not used to schedule execution; this is an intent chronology correction, not proof of changed execution economics.
Validation on .8: test_runtime_close_identity_acceptance.py + test_runtime_protection_acceptance.py 16 passed (2.71s). Close fixture now fixes replay clock and asserts intent timestamp before mocked SDK post across full/partial/zero/timeout. Historical actual Hub/ensemble/runtime entry probe inspect_decision_runtime_buy_gates_r10.py asserts one BUY qty1/lot1 and created_at==as_of, passes; immutable decision_runtime_buy_gates_r10.json reports Nov13 10:00Z instead of Nov16 wall clock. Same generic synthetic portfolio limitations as r9: NOT LiveBroker sizing/catalog or full execution parity, no broker calls/orders/start/deploy/DBwrites. Existing r9 receipt retained. No configuration changes.
Worker D2: entry/protective intent creation now uses bot clock in source; UI should retain explicit distinction of source-bar/event, decision, and broker fill times. ACK for ARCH262 not observed in latest chat read; assignment remains posted, not claimed running. Next architecture issue: portfolio_snapshot currently catches positions() failure into empty positions; investigate fail-closed exposure evidence with real broker adapter fixtures.


## ARCH266 — portfolio read failure no longer becomes empty book
Root bounded fix complete in runtime_telemetry.portfolio_snapshot: equity/positions exceptions now propagate instead of returning fabricated zero equity/empty or partial positions. _submit_order existing catch rejects PORTFOLIO_CHECK_ERROR. No trading policy/limits changed. UI consumers may now receive an error instead of misleading empty values; D2 should represent unavailable explicitly, not zero. Do not revert propagation for display convenience.
Evidence: r11 before fix injected positions TimeoutError yet produced internal BUY. r12 after fix same historical Hub/ensemble/runtime path rejects, orders0/pending0; r13 valid synthetic empty20000 portfolio still produces identical BUY qty1 and correct replay timestamp. Both assertion-bearing probes completed successfully on .8; test_portfolio_snapshot_failure.py 2 passed (0.36s), covers equity and positions exceptions/no cached partial result. No live API/orders/deployment/DBwrites. Generic adapter limits persist; this does not verify real broker sizing or end-to-end readiness. Existing snapshot TTL/cache and multi-call consistency remain unaudited; fix covers actual fetch failures, not all stale or malformed data. Worker may resume D2 edits in telemetry while preserving these blocks.


## ARCH267 — admission bypasses display portfolio cache
Found two independent cache layers: runtime portfolio_snapshot default10s, LiveBroker portfolio15s. Runtime cache was reset at startup, not on each execution; _submit_order now explicitly requests portfolio_snapshot(ttl=0.0), so risk admission no longer reuses display summary. Underlying broker cache is NOT bypassed by this fix: fresh authoritative snapshot/reservations still require follow-up. _reconcile_positions flushes broker cache on its own loop, not a per-order guarantee.
3 focused portfolio_snapshot checks passed0.36s, including recent display-cache return vs explicit refresh propagating current failure. Historical actual Hub/strategy/runtime probe r14 asserts portfolio_snapshot called once with ttl0 and preserves BUYqty1/correct timestamp. No broker calls/orders/deploy/DBwrites. Existing broker15s cache, pending-order reservation and coherent account snapshot need architecture work; no claim complete freshness or readiness. Evidence inspect_decision_runtime_buy_gates_r14.py / decision_runtime_buy_gates_r14.json.


## ARCH268 — invalidate broker portfolio after every submitted order attempt
Verified defect with actual LiveBroker._api_post_order/_get_portfolio_safe and mocked SDK: all four live/sandbox × success/timeout cases retained stale pre-order portfolio (4 failed before fix). Central _api_post_order now clears portfolio cache in finally after attempted SDK submission. Covers both ordinary open and protective-close callers; propagates original result/exception and preserves confirm_margin_trade=False when margin disabled. Does not retry an unknown order.
Focused checks test_broker_portfolio_cache_invalidation.py plus test_live_execution_report.py:18passed2.11s on .8. No real SDK request, bot start, deployment or DB write. New test asserts next portfolio read actually invokes source, original timeout preserved, correct SDK routing and one submission only.
Limits: invalidation is not confirmation of broker settlement; broker may still report lagged holdings. Pending reservations, concurrent orders, external account changes before TTL, single coherent admission snapshot remain unresolved. Live readiness/profitability not established. UI D2 should preserve unknown-result display; no implied zero fill from timeout. Prior turn ARCH267 was progress (runtime display-cache bypass + evidence); this turn adds adapter-layer invalidation.


## ARCH269 — pending entries count toward max_positions
Actual historical runtime probe r15: saved max_positions4 with four synthetic SUBMITTED pending entries and no held positions still admitted fifth internal BUY. Fixed runtime portfolio gate held_count to union held FIGIs and pending open FIGIs excluding confirmed CANCELLED/REJECTED. Partial entry already held counts once; pending FILLED without held representation conservatively retains slot; pending close does not free held slot. No configuration/strategy changes.
Validation .8: test_pending_entry_slots.py9passed2.36s across approval/submitted/partial/unknown/reconciliation/filled and cancelled/rejected states. Actual Hub/ensemble/runtime r16 same signal+fourpending rejects MAX_POSITIONS, orders0/pending4, portfolio_snapshot not called. r15 preserved. Scope: sequential admission slot count; not atomic concurrent admission, durable reservations, money/sector reservation or in-flight _execute_pending pop lifecycle. Those remain open; no claim full portfolio parity. No broker API/orders/start/deployment/DBwrite.
D2 UI: distinguish occupied slots (held union pending open) from actual broker holdings; MAX_POSITIONS count can include unfilled intents. ARCH262 vote/extrema audit remains assigned; no worker ACK claimed.


## ARCH270 — authorized weekly replay running; vectorization investigation
Owner requested week before current date and visualization, then asked vectorization. Started actual runtime replay Nov9–13 MSK (UTC Nov8 21:00 to Nov13 21:00), saved live strategy profile, initial20000/no margin/no shorts/max4, fixed current38candidate watchlist. Isolated database deeptrading_test_week_20261109_13_r1: schema copied and only instruments/instrument_info/candles; source read-only except CREATE DATABASE administrative action. 1,024,449 candle rows including warmup copied; weekly source coverage156instruments/348529minute candles. This is current-watchlist historical diagnosis, not historical broker availability reconstruction.
Run scripts/artifacts backend/reports/week_20261109_13_r1/. Actual PaperBroker selected by feed=replay; LiveBroker services prohibited in runner. Config/ensemble/AI output paths isolated. Initial attempt failed monitor calling status property as method; process terminal verified, console_attempt1 retained; corrected runner started isolated paper reset. Current SSH exec session2802 confirmed running; browser dashboard http://127.0.0.1:18765 verified with CUA. Local dashboard server session88037. At00:44:15Z progress Nov9 04:19Z,765candles; no terminal result. ~3candles/s observed => rough15–20h full week, not commitment. Do not restart because monitor times out. Equity_samples are sparse wall-clock observations, not exact drawdown series. Preserve baseline execution while comparing acceleration; no live orders or production data writes.
User vectorization question: actual EnsembleV4Strategy.on_bar calls compute_ensemble without ctx eachminute. Existing EnsembleContext can reuse computations; ReplayState is another existing path, not yet validated here. Completed context_benchmark.py in isolated DB: fourtickers×21strictcausal prefixes Nov13, baseline vs context same actual strategy. MVID2.44x/TRNFP2.29x/SIBN2.32x/CHMF2.22x, zero mismatches in full signal/skip/regime. Receipt context_benchmark.json. This is bounded output parity, not ledger/fullweek/evictingwindow proof. No production strategy change or baseline acceleration performed. Need profile remaining cost and test oracle/drop_useless/prefix/window semantics before full vector pipeline; keep portfolio/execution sequential with common policies. ARCH269 in-flight reservation review paused by user's week request, not resolved.


## ARCH271 — measured acceleration bottleneck and saved-signal coverage
Owner recalls vectors already built and points to simple MACD/RSI components. Read-only source DB inventory completed: signal_inventory.py/json in week_20261109_13_r1; both signals and lab_signal_events contain ZERO rows for Nov9–13 MSK. Catalog strategy_runs latest dates are August2026. This does NOT establish absence of vectorized implementations or separate file exports. Existing SIGNALS_CATALOG design explicitly computes signals once and reuses PostgreSQL; actual EnsembleV4Strategy still calls compute_ensemble fresh without context. Reuse existing blocks, do not duplicate strategies.
Completed cProfile on cached actual strategy path84calls (profile_context.py, context_profile.json): on_bar21.1867s, compute_ensemble20.3910s, _run_pipeline13.7524s, internal EngineRunner.run10.6336s/84runs. Nested values must NOT be summed. Regime processing adds cost; profile overhead means use prior unprofiled context_benchmark for speedup estimates. Actual runtime adapter consumes entries/quorum/setups/votes/regime and diagnostics, not historical preview trade ledger. This identifies next architecture change: common decision computation separated from historical execution/report calculation; preserve diagnostic semantics explicitly (uncomputed != zero). No production ensemble change yet. Need same-prefix full signal/decision metadata equivalence, negative/gap/reset/evicting-window cases and full replay before enabling accelerated default.
Weekly baseline session2802 was polled live this turn chain; keep running, no final economic verdict. Last verified dashboard remains local18765. Worker ARCH262 tasks remain separate. Current raw-signal inventory and profiling are progress; previous pure MACD/RSI explanation alone was not implementation progress.


## ARCH272 — OWNER HANDOFF: worker implements accelerated ensemble; architect researches existing data
User explicitly directs: hand implementation to worker, test trading theory on existing data. Worker .8: take ownership of app/services/ensemble.py, app/services/ensemble_ctx.py, existing vector/batch signal providers, and ensemble_strategy adapter for acceleration after ACK; coordinate D2/ARCH262 priorities. Root stops new implementation edits in this zone now. Do not restart or alter running weekly baseline or its isolated DB.
Already implemented source handoff: compute_ensemble(..., include_execution=True) and _run_pipeline support optional decision-only path. Common decision_result now built before EngineRunner historical execution; full path remains default and retains economic report, fast path returns execution_status NOT_COMPUTED with no fabricated zero economics/trades. Added test_decision_only_matches_full_pipeline_without_running_trade_engine in tests/test_ensemble_ctx_parity.py:1pass3.44s; ensures actual entries exist, equality of all common decision fields/funnel, forbidden EngineRunner invocation. Full file check currently session34775; actual4tickers×21prefix decision-only benchmark session50197, artifacts decision_only_benchmark.json/log, script benchmark_decisions.py. Root will record completion/results; these handles must be polled, not duplicate jobs started.
Evidence input: week_20261109_13_r1/context_profile.json internal historical EngineRunner10.63/21.19s of84cached strategy calls; context_benchmark.json existing EnsembleContext2.2–2.4x,84prefixes signal/skip/regime identical. Actual signals/lab tables have no Nov9–13 rows; user recalls existing vectors, so find/reuse actual implementations/artifacts before rewriting indicators. SIGNALS_CATALOG design: signals computed once and reused. Do not interpret absence of November rows as absence of vector implementations.
Implementation objective: reusable causal arrays/state of indicator and raw setup votes → existing quorum/filter logic → same sequential execution policies. Signal semantics/availability/config identity unchanged. Required acceptance: baseline vs accelerated actual signal metadata, gates, final shared-capital ledger on identical inputs; gaps, incomplete TF, resets, rolling-window eviction, oracle/drop_useless dynamic membership, no future suffix influence. Separate skipped preview metrics from zero. API defaults preserve existing report callers. Enable fast runtime only after comparison; keep current weekly reference untouched. Current patch is first separation, not completed vectorization. No live orders/API/backfill/deployment/workingDBwrites assigned.
Architect now owns trading hypothesis comparison on existing source/frozen data and weekly result review, not further acceleration implementation. Weekly actual runtime process SSH session2802 verified live; isolated DB deeptrading_test_week_20261109_13_r1; dashboard18765/session88037. Do not confuse synthetic tests with real broker readiness or profitability proof. Please ACK this handoff and report concrete checkpoint in root chat.md.


## ARCH273 — weekly raw-vote hypothesis diagnostic completed
Owner redirected implementation to worker; architect used existing generate_signals/resample/merge_quorum and Hub data, no new indicator implementation. Executed vote_outcomes.py in isolated weekly DB,38fixed current instruments, five current5msetups plus shared same-bar quorum2; BUY only, fixed15/30/60min forward horizons. Protocol written before calculation; strict complete5m signal bucket, entry at bar-close/nextminuteopen, exact same-date exit, missing any pathminute→UNKNOWN; consecutive same-side5m runs dedup to first episode. Costs saved0.0005/side+2bps slippage/side, canonical CostModel with tick_size0 explicitly diagnostic normalization. Raw signal generation causal by existing sequential CandleWindow; full runtime/drop_useless/bias/regime/SLTP/sharedcapital NOT applied. This is overlapping event-return study, not portfolio returns or independent statistical samples.
Results vote_outcomes/{protocol,inputs,events,summary,daily}.json:21,330 event×horizon rows,18source/horizon summaries; all18 mean net below0 on known paths. At30min: Bollinger1667known/623unknown gross+0.0117%,net−0.1283%; Donchian497/100 +0.0114%,−0.1286%; EMA1916/686 +0.0014%,−0.1386%; quorum2 714/249 +0.0114%,−0.1286%; compression468/172 −0.0149%,−0.1548%; RSI17/1 +0.0192%,−0.1208% (very low support). Rough14bpsroundtrip costs exceed observed average raw drift. Does NOT prove ensemble/current complete strategy unprofitable, no matched baseline or independent holdout; do not drop tickers or retune from this week. Next evaluate matched ticker/time/control and existing context gates, retaining unknown coverage and full baseline.
Weekly actual runtime still separate pending; reference process must remain tracked. Worker ARCH272 continues acceleration, no ACK claimed. No broker calls/orders, source DB writes or live deployment in this diagnostic.


## ARCH274 — hourly-bias slice and concentration; worker priority updated
Completed read-only vote_bias.py / vote_bias_breakdown.py against existing weekly vote events + Hub candles. Fixed current hour50 compute_bias (previous-hour close/EMA, source-bar bucket); 38prefix checks match full-series lookup at sampled availability, not exhaustive full engine parity. Artifacts hour_bias_{protocol,summary,breakdown_protocol,breakdown}.json in vote_outcomes. No parameter search/change.
Quorum2 BUY:30min UP523known/161unknown net−0.1399%, DOWN191/88 −0.0975%.60min UP456/228 −0.1345%; DOWN177/102 +0.01162% (gross+0.15173%). Small positive subgroup is NOT accepted edge: DOWN60 daily mean net Nov9−0.1881%(36known),Nov10−0.1695%(46),Nov11+0.2351%(82),Nov12−0.2128%(8),Nov13−0.1900%(5). AFKS2known events mean+2.348%; excluding those leaves175known mean−0.01508%. This exclusion is concentration diagnosis, not a trading rule or retrospective ticker selection. 32tickers with known outcomes in subgroup,37with events includingunknown. Overlapping event returns, not account profitability; no independent holdout. Raw filter relationship does not justify inverting live bias or removing AFKS.
Baseline session2802 polled live: Nov9 05:23Z/3061candles as of00:55:06Z, no final result. Worker newly delivered D2 UI/status result and ACK262; receipt observed but deployment/independent UI acceptance not performed. Explicit follow-up appended: owner's newer ARCH272 acceleration handoff takes priority next before ARCH262; ask ACK272, preserve running reference. Root remains on theory/data.

### 2026-11-16 — connected minute scheduler repair (ARCH297–299)
Root implemented shared-protection every-minute phases and corrected historical runner;5focused golden checks passed. FullNov9–13 three-arm run completed; independent cash/fee/lot/position audits passed for108/72/51closedtrades. Immutable artifacts: backend/reports/connected_week_20261109_13_c1/root_r3_run001/. Live adapter rounding parity and historical uncertain execution paths remain unresolved; no profitability/live-readiness claim.


### ARCH304 — C3 close-profit experiment
Optional causal close policy and10focused checks completed. Paired portfolio run and accounting audit completed; no profitability improvement accepted. Artifacts: backend/reports/connected_week_20261109_13_c1/profit_protection_c3_run001. See CURRENT_STATE/chat ARCH304 for limits and source drift.


### ARCH310 — C4 подтверждение входа
Парный недельный прогон завершён;31вход подтверждены по времени, учёт обоих вариантов проверен. Результат остаётся отрицательным, боевой запуск не выполнялся. Артефакты: backend/reports/connected_week_20261109_13_c1/entry_confirmation_c4_run001.


### ARCH313 — непрерывный C4 за две недели
Прогон, учёт,60проверок времени входов и перенос позиций завершены. Условный плюс219.05руб сосредоточен в крупнейшей сделке235.07руб; устойчивость не доказана. Артефакты entry_confirmation_c4_two_weeks_run001; неопределённые пути исполнения переданы воркеру для проверки по имеющимся свидетельствам.


### ARCH317 — месячная проверка C4
Октябрьский парный прогон и проверки учёта/122входов завершены. Подтверждение уменьшило оборот, прибыльность не показало: условные закрытые потоки−628.58руб против−970.90руб контроля, по1открытой позиции. Артефакты entry_confirmation_c4_october_run001; изменение не принято для боевого использования.


### ARCH319 — C5 сравнение таймфреймов за ноябрь
Общий раннер5m/15m проверен: контроль идентичен, учёт и временные границы сигналов подтверждены, минутные защиты сохранены.15mусловно+481.24руб по50закрытым сделкам,2позиции открыты. Октябрьский прогон запущен согласно предварительному плану.


### ARCH321 — C5 октябрь и сохранение метаданных Hub
Сравнение5m/15m завершено, контроль воспроизведён и учёт проверен.15mусловные закрытые потоки−359.85руб,2позиции открыты; устойчивое преимущество не установлено. Для будущих прогонов добавлено сохранение исходных статусов покрытия Hub без догадок о причине отсутствующих свечей.


### ARCH324 — общий допуск сделки
Модуль connected_admission выделен и подключён в исследовательском раннере. На одинаковой истории полностью совпали169сделок,2746событий,1914отказов,позиции,деньги и оценки портфеля до/после переноса. Это проверка сохранения поведения, не готовности боевого адаптера. Артефакт shared_admission_parity_run001/shared_admission_parity.json.


### ARCH327 — C6 развитие и отдельный период
Ноябрьский парный прогон и26проверок ролей/времени входа завершены. Преимущество нового правила не установлено, настройки сохранены. Сентябрьский прогон запущен согласно предварительному плану; без боевых операций.


### ARCH329 — минутное исполнение: дополнительные регрессии
На .8 проходят 7 tests/test_connected_minute_execution.py: вне 5m-сетки, gap/open порядок, STOP_FIRST, локальный пропуск и защита новой позиции. Проверено независимое от is_node место вызова в root runner. Данные и рабочие торговые настройки не менялись.


### ARCH330 — сентябрьская проверка режимов завершена
Неизменённая C6-гипотеза: 55 закрытых сделок, условный результат после расходов −406,76 ₽; база 154 сделки, −1558,49 ₽ и одна открытая позиция. Учёт и 55 условий входа проверены независимо; источники не менялись. Точный итог и полная просадка неизвестны. В боевую стратегию не переносится. Подробности и неизменяемые артефакты — CURRENT_STATE/ARCH330.


### ARCH331 — локализация неопределённости
Добавлена диагностика пропусков по открытым позициям, тикерам и часам MSK с проверкой последовательности ENTRY/EXIT. Сентябрьский результат сохранён отдельно без изменения исходного прогона. Исправление worker JSON writer проверено на Windows: хеш совпадает с фактическими байтами.


### ARCH332 — один снимок Hub для нескольких таймфреймов
Утилита candle_hub_views проверена девятью тестами и шестью сравнениями на реальных SBER/SMLT. Свечи и метаданные совпадают с независимыми чтениями Hub. Подключение к раннеру остаётся следующим шагом.


### ARCH333 — загрузчик подключён к общему минутному снимку
На .8 проверен весь пул38акций+индекс за Sep1–4: все свечи и метаданные совпадают с прежним чтением, число запросов Hub сокращено156→78.16focused тестов прошли. Источники агрегатора включены в будущие manifests.


### ARCH334 — воспроизведена ошибка intrabar-подтверждения
Изолированная проверка действительного runtime-метода показала очистку локальной защиты при close_position=None и сохраняющейся позиции. Исправление приоритетно назначено владельцу runtime; пока не завершено. Реальные API не вызывались.


### ARCH335 — ошибка подтверждения охватывает закрытие всех позиций
Actual-method probe на .8: None и частичное10/20исполнение ошибочно дают closed=1 и очищают защиту. Зафиксирован контракт LiveBroker и расширено приоритетное задание worker на общий lifecycle выхода. Исправление ещё не принято.


### ARCH336 — приёмочные регрессии для общего закрытия
Добавлены пять проверок actual runtime.close_all. Полное исполнение проходит; четыре сценария выявляют дефекты неизвестного/частичного результата, timeout и двойной отправки при конкуренции. Исправление worker ожидается; тесты не скрыты через skip/xfail.


### ARCH337 — общий блок подтверждения закрытия
Добавлен close_confirmation с17проверками идентичности заявки, фактического количества и цены. Состояния full/partial/unknown разделены. Блок не отправляет заявки; подключение к общему runtime lifecycle остаётся задачей worker.


### ARCH338 — проверка сверки незавершённых закрытий
Шесть actual-path тестов подтвердили: pending close не опрашивается в _reconcile_pending_orders. Приёмочные требования по terminal-zero и сохранению частичного остатка переданы worker. Реальных API-вызовов не было.


### ARCH339 — локальная отмена теряет неизвестные заявки
Два actual-runtime теста воспроизвели удаление PENDING_RECONCILIATION/PARTIAL_FILLED из pending без подтверждения брокера. Worker notified; общий close helper уже виден в изменяемом коде, но приёмка ещё не выполнена. Рестарт-персистентность пока не доказана.


### ARCH340 — частичная приёмка исправления закрытия
Девять прежних runtime/adapter проверок проходят. Добавлены три воспроизводимых отказа при неподтверждённой цене исполнения: текущее исправление всё ещё считает их закрытием. Worker направлен к общему classifier, полная приёмка не объявлена.


### ARCH341 — проверена защита через intrabar
Пять новых actual-runtime сценариев проходят, включая конкурентный вызов intrabar+close_all с одной отправкой. Текущий набор:10pass/3fail на неподтверждённой цене. Полная приёмка исполнения остаётся открытой.


### ARCH342 — проверено хранение идентификаторов исполнения
Read-only схема .8 и ORM подтверждают: динамические client_order_id/broker_order_id результата закрытия не сохраняются как поля PaperTrade. Требование durable-before-submit/recovery зафиксировано, сохранён schema receipt без чтения личных записей.


### ARCH344 — локальная отмена сохраняет неопределённые заявки
Исправлен cancel_pending: PENDING_RECONCILIATION/PARTIAL_FILLED остаются для сверки, ответ содержит unresolved_orders. Отмена неотправленного approval сохраняется. Никаких брокерских действий.


### ARCH346 — подтверждение цены подключено к закрытию
Root заменил повторяющиеся проверки ответа в общем helper на close_confirmation. Неподтверждённая цена сохраняет pending и защиту.37focused тестов проходят на .8; сверка pendingclose и рестарт ещё не завершены.


### ARCH348 — подключена сверка состояния заявок на закрытие
Существующий validated order_state вызывается и для close с исходными лотами/акциями. Нулевое подтверждённое отменённое исполнение снимает pending без удаления позиции; регресс исполненного количества отклоняется.26тестов проходят. Метаданные отправки/late-fill settlement/restart ещё незавершены.


### ARCH350 — соединены отправка и сверка закрытия
Helper проверяет позицию/лот и сохраняет исходные shares/lots и сторону. LONG/SHORT сценарии от отправки до отмены без исполнения проходят; всего19focused pass. Это не подтверждает восстановление после рестарта.


### ARCH351 — изоляция ответа при смене счёта
Заявка хранит исходный broker scope; перед отправкой и применением ответа проверяется его неизменность. Сверка не опрашивает старую заявку на новом счёте.47focused проверок прошли; общая безопасность рестарта/переключения ещё не установлена.


### ARCH353 — исключение тикера сохраняет незакрытую позицию
Deactivation переведена на общий close helper. Неизвестный/частичный результат или ошибка получения позиции сохраняют наблюдение и защиту; добавлены тесты незавершённого и полного закрытия.


### ARCH355 — сокращение портфеля использует подтверждение закрытия
reduce_positions сохраняет защиту и остаток при незавершённом исполнении, не считает попытку закрытой позицией.26focused проверок проходят; другие пути исполнения и restart остаются открытыми.


### ARCH357 — выход по сигналу подключён к общему исполнению
Сохраняется исходный clientrequestID, количество берётся из фактической позиции и повторно сверяется адаптером. Неизвестный/частичный ответ не закрывает учёт и не допускает повторную отправку.


### ARCH359 — закрытие перед ночью/выходными
Общий close helper подключён к существующему сессионному условию; неизвестное/частичное исполнение сохраняет позицию.42focused проверки проходят. Условия календаря не менялись и полным прогоном здесь не проверялись.


### ARCH361 — финал исторического прогона использует общее закрытие
Удалена подстановка цены входа вместо неизвестной конечной цены. Общий helper сохраняет PARTIAL_CLOSE при неизвестном/частичном результате; added focused regressions. Свежесть конечной цены отдельно не доказана.


### ARCH362 — совместная приёмка путей закрытия
114focused тестов прошли на .8; AST-проверка подтверждает единую точку broker.close_position в runtime. Сохранена квитанция с границами доказательства и оставшимися задачами; live готовность и прибыльность не заявлены.


### ARCH364 — основа сохраняемого журнала заявок
Добавлены scoped identity/unique unresolved FIGI и commit-before-permission.9isolatedSQLite тестов проходят, включая reopen и concurrency. Runtime и рабочая БД не изменены этой частью; complete lifecycle ещё не реализован.


### ARCH365 — сохранение ответа брокера в журнале
Добавлен atomic observe с исходной идентичностью и монотонным cumulativeqty. Только подтверждённый terminalzero освобождает резерв; nonzero ждёт учёта.13isolated тестов прошли, production не затрагивался.


## ARCH366 — intent migration rendered and checked, existing revision collision unresolved
Root added 0006_broker_order_intents.py after0005 and offline PostgreSQL DDL acceptance. Actual .8 session79994:15passed3.42s (13journal SQLite tests +2offline migration tests). Upgrade DDL matches model columns/constraints; downgrade targets only newtable. Initial test whitespace-only mismatch corrected by whitespace normalization, no schema weakening. Migration NOT applied, model NOT runtime-registered, no production writes or broker calls.
Read-only alembic heads exits0 but warns revision0002 occurs twice (0002_bot_logs_source_width.py and0002_broker_account_id.py), outputs0002 and0006head. Existing graph is therefore not accepted for deployment. Do not rename/stamp existing migrations blindly: first reconcile actual installed revision/schema and historical branch provenance. Root did not change historical migrations. Durable runtime integration, late-fill settlement and PostgreSQL transaction acceptance remain open; no live readiness.


## ARCH368 — duplicate revision source fixed, scoped chain validation passes
Root consolidated both historical0002 upgrades/downgrades into retained0002_broker_account_id.py and removed duplicate0002_bot_logs_source_width.py. Log requestcolumn/index and accountcolumn/index creation are idempotent; historical applied version IDs unchanged. New0006 checks legacy source/request/account column types+widths before tablecreation and raises for incomplete ambiguous0002 installations. No migration/stamp/application performed. Do not interpret consolidation as proof all other databases contain both historicalchanges; .8 audit alone confirms installed0005 with both schemas and indexes.
.8session99411:17passed2.10s (journal13,DDL2,chain2). Graph tested with warnings treated as errors, exactly0006→0005→0004→0003→0002→0001;0005→head traverses only0006. PostgreSQL offline DDL model match retained. Actual PostgreSQL guard execution and concurrent journal transactions remain untested. Receipt migration_state_r1.json contains readonly schema audit; no business rows/secrets. Runtime durable wiring and latefill accounting still incomplete; no live readiness or profitability claim. Worker preserve root migration/journal ownership.


## ARCH369 — isolated PostgreSQL migration and journal acceptance5PASS
Root added tests/test_broker_order_journal_postgres.py. Explicit DEEPTRADING_TEST_PG_BIN launches a fresh initdb cluster in Windows local temp on a random127.0.0.1 port, never uses application DATABASE_URL for test connections. Tables built using actual0006 migration against minimal legacy fixtures, not ORM create_all. Three incompatible legacy schemas raise and leave no journaltable. Eight concurrent reservations produce one winner; account/mode isolation holds; committedreservation survives clean PostgreSQL fast-stop/restart with fsync/synchronous_commit on and blocks resend. Confirmedcancelzero releases. Concurrent10/20unit observations converge20; latercancelzero cannot regress/release; no nonzero settlement claim.
Initial harness errors: executable discovery lacked.exe; then subprocess capture pipes inherited by server hung. Identified and stopped only temporarypytest436cluster, originalsession49592 terminatedexit1; changed executablepaths and stdout totemporaryfile. Finalcleanup in finally also handles startupfailure when postmaster.pid exists. Actual finalsession67510exit0:5passed16.36s, no skips; earlier89884 also5passed17.39s. Fixture shutdown succeeded. This covers clean restart, not crash/powerloss or livebroker integration. Productiondatabase/service untouched; no actualbroker requests. Existing17SQLite/offline tests not redundantly rerun because their files unchanged. Runtimecommit-before-send, restoration and late-fill accounting remain next blockers to readiness, objectiveprofitability still unproven.


## ARCH371 — atomic nonzero settlement implemented; worker integration assignment
Root journal now exposes `await settle(intent, broker_order_id=..., cumulative_units=..., trade=PaperTrade|None) -> CloseSettlement(applied_units, accounted_units, remaining_units, released, trade_id)`. First call requires previouslystored observe evidence matching originalscope/FIGI/side/units/lot and exact cumulativeunits. Under PostgreSQL FOR UPDATE, insert transient PaperTrade for unaccountedDELTA, insert scoped BrokerOrderFill linkage, advanceaccounted_units and terminalrelease in ONEtransaction. Result returned aftercommit. Repeated cumulative count yieldsapplied_units0 and existingtrade_id; never duplicate incremental runtimebookkeeping. With no newdelta pass trade=None; terminalcancelafterpreviouspartial canthenrelease withdelta0. FullFILLED releasesonlyrequestedunits, partialCANCELLED/REJECTED releasesonlyafterallobservedunitsaccounted. NEW/PARTIAL retainsreservation. Remaining_units is originalrequest minus accounted, NOT independentbrokerportfolio truth.
Caller must supply actual DELTA executionprice/costs: cumulative average cannot be reused blindly as deltaaverage. Service validates identity/delta/transienttrade/finiteprices/timestamps, not sourceauthenticity or fee correctness. Existing PaperTrade insert/commit MUST be replaced by settle, not left alongside it. No money or external effects in callbacks; there are none. BrokerOrderFill permanentlylinks each cumulative step→PaperTrade withscope andunique tradeID.0006 (notdeployed) now creates both tables andaccounted_units constraint. Modelmetadata matches migration; downgrade dropsfill beforeintent.
Actual .8session61609exit0:31passed20.71s across14isolatedPG +13SQLite +2DDL +2chain. PG newcoverage eightconcurrentsettlements→onesale,10→20cumulative→two10unittrades,partialcancelreleasewithoutdoublecount,reopenidempotence,FKfailure and injectedfillledgerfailureaftertradeflush rollback ALLrows/state,invalidFIGI/side/qty/NaN reject. Initial29pass/1DDL-order mismatch corrected by aligning constraintdeclarationorder, no weakenedassertion. IsolatedPG stopped; no productionDDL/broker/start.
WORKER next scope: LiveBroker close immediate-response path + focusedofflineadaptertests, using thisjournal to commitreservationBEFORE _api_post_order; originalrequestID throughout; validatebrokerresponse thenobserve+settle, remove old standalonePaperTradecommit. Failclosed on storage/missingschema/duplicate; never fall back to unjournaled send. Preserve root runtimehelper/callers/reconcile; do not modify journal/model/migration. Publish concrete diff/tests, not capability-only reply. No deployment, schemaapplication, startup, realAPI, or default bypass. Root next owns durable originalpositioncontext and bootstrapDDL exclusion. Therefore do not claim full late-fill/restartready based on immediate-path wiring; missingentrycontext currently blocks trustworthy reconstruction afterclose. Emptyterminalzero release already observe. Startup main.py/lab persist use Base.metadata.create_all: root will exclude migrationmanaged tables before deployment; do not workaround by auto-create or manuallystamp.


## ARCH373 — automatic schema creation cannot bypass order-ledger migration
Root marks BrokerOrderIntent/BrokerOrderFill tables migration_managed in table.info and adds database.create_bootstrap_schema(bind), excluding marked tables from create_all. Both actualapplication lifespan and SignalLab ensure_schema now call sharedhelper; no dependency on importorder. Existing tables remaineligible; no globalcreate_all in application paths besideshelper (source search). ModelDDL unchanged. .8session64149exit0:4passed1.60s, actualSQLAlchemy mockPostgreSQL DDL selection with ledgermodels already imported, actualLab ensure_schema plus model/migrationDDL tests. Main singlecall wiring source-reviewed; applicationnotstarted. No productionwrites/DDL/broker. Worker can import journal without startup silentlycreatingtables; missing0006 remains error, no fallback authorized.
Next root recoverycontext: LiveBroker portfolio positions currently setentry_time=datetime.now(UTC), which is snapshot time, not proven actualentrytime. Must preserve provenance and not silentlycall it recoveredentrytime. No new API/readinessclaim. Worker immediateintegration may use agreed journal but must expose incomplete recovery rather than manufacture oldentryfacts.


## ARCH375 — durable original-position snapshot accepted, recovery limitations explicit
Root new ClosePositionSnapshot stores versionedJSON in intentreservationtransaction: position side/units/entryprice with decimalstringprecision, UTC observed_at, entrytime+provenance, strategy/reason/internalaccount, estimatedrate+provenance. No update/overwrite path: duplicate reservation leaves originalsnapshot intact. CloseIntent.from_record restores typedvalidatedscope/snapshot; unknownentrytime/costs remainNone, never snapshot-time/zero substitutions. validate_for enforces originalside/units/time ordering. settlement uses SAVEDcontext and rejects changedprice/ticker/strategy/reason/account/knownentrytime; ifsavedentrytimeunknown, observedexecution stays pendingaccounting rather than inventing a requiredPaperTrade.entry_time. Entryprice comparison honors PaperTrade NUMERIC(20,6) while snapshotretains originalprecision; not arbitrary tolerance. Context remains optional only for legacy interface compatibility; worker must not omit it to bypass recoverychecks. Noentryhistoryresolver implementedyet; metadata assertions bycaller are not independentlyverifiedbrokertruth.
.8session40301exit0:47passed20.69s (snapshot14,PG16,SQLite13,DDL2,bootstrap2). Followup precision case added and pricecomparison fixed to schema6decimals; finalsession94214exit0:17PGpassed20.17s including originalhighprecision snapshot, duplicateimmutability,reopenrestore,wrongentryprice rejection,unknownentrytime hold; existingconcurrency/restart/rollback retained. No broadsuite/repeatedresearch. TemporaryPG stopped; no productionmigration/broker/start. Receipt position_snapshot_acceptance_r1.json. Worker immediate-path task remainsARCH371, additiveinterfaceARCH374 ready; root next validatesentry provenance/recovery, nofullruntimeacceptanceclaim.


## ARCH376 — actual execution-stage evidence parser and entry-ledger audit
Root app/services/broker_fill_evidence.py normalizes scoped GetOrderState stages into immutable fills (tradeID,shares,Decimal perunitprice,actualexecutiontime), cumulativeunits/notional/weightedaverage, first/lastfilltime. Uses confirmed lotconversion (OrderStage.quantity is LOTS), exactrequest/FIGI/direction/requestedlots, stage sum equals cumulativeexecutedlots, uniquetradeIDs,currencyconsistency, positiveprice, validplacementtime and executiontimewithin[order_date,as_of]. Epoch/default/missing/futuretimes rejected. read_order_fill_evidence calls existing boundbroker.order_state and checks mode/accountbefore+afterawait. API exists but was tested only with mocks; no realbroker calls. Does NOT by itself prove these fills constitute currentportfolio: originalentry linkage/continuity stillrequired.
InstalledSDK introspection confirms OrderStage.execution_time exists. Primarydocs https://developer.tbank.ru/invest/services/orders/methods and https://tinkoff.github.io/investAPI/orders/ distinguish PostOrderResponse executed_order_price (perinstrumentaverage) from OrderState executed_order_price (aggregateddescription). Initial suspicion of currentimmediateadapter pricebug NOT confirmed; do not divide existingPostOrderResponse price byquantity. Recoveryparser uses individualstages and deliberately ignores aggregatefield; do not pass GetOrderState through PostOrderResponse parser indiscriminately.
Actual .8session17320exit0:19passed1.44s using realinstalledSDK dataclasses and fakebroker methods (16SDKdeprecationwarnings). Includes multipleprices/nonunitlot, missing/duplicate/invalidstages, scopechange, live/sandboxrouting. Earlier16baselinepass; finaladdedepoch/beforeplacement/numericdirection cases.
Readonly production audit entry_identity_audit_r1.json: zero local open sandbox_trades rows with mode live/sandbox (counts{}). No businessIDs/accountIDs printed. This does NOT establish brokerflatness; no brokerquery performed. No currentlocalentry evidence available to inferhistoricalentry. Worker preserve closeintegrationassignment; parser now available for originalentry query and latefill evidence aftermatchingrequest identity, never guessnearestticker/time. Root next entryrecord evidence plumbing; no journaldeployment/start/trading.


## ARCH378 — new entry persistence now captures broker execution-stage provenance
Root _persist_entry_fill invokes scoped read_order_fill_evidence for originalrequest/FIGI/side/requestedlots/confirmedlot, validates exact cumulative units and6decimalprice against the immediate execution beingpersisted. It passes typed evidence into _st_open; lookupfailure/mismatchedadvancedstate stores execution quantity but marks provenanceunknown, no guessed actualtime. Scope/objectchange duringlookup preventswrite; change duringcommit preserves capturedold-account row butreturnsfalse/pending/entriespaused rather than completingnewscope bookkeeping. _st_open captures ledger_mode/account once beforeawaits and usescapturedscope for allqueries/inserts.
New live/sandbox metadata: entry_time_source=broker_history|unknown; entry_time_confirmed=ISOactualfirststage|None; entry_fill_evidence version1 with request/exchangeIDs,FIGI/side,currency,requested/executedlots,lot_size and individualtradeID/units/Decimalprice/actualtime. entry_observed_at separately recordsprocessingtime. Knownmetadata updates idempotent cumulativeentryrow; newconfirmedtime replaces oldobservationtime. Timeout on SAMEquantity+price retains existing brokerhistory proof; changedquantity/price withoutmatchingproof invalidates metadata. Staleentry_evidence_error cleared on successfulfreshcapture.
IMPORTANT legacySandboxTrade.entry_time remains NOT NULL: unknown case retains an observation timestamp in that column, explicitlysourceunknown andconfirmedNone. Recovery MUST read provenance fields and never promote columnalone. UI/analytics consumers stillneed audit; no claimalltimestampconsumersfixed. New brokerfill lookup executes only when botruns; no realAPI invoked now.
.8session34730:101pass3.94s across entryruntime/lot/partialentry/parser. Addedcommit-scope/cumulative/proofpreservation cases and clearedstaleerror; finalsession48554exit0:83pass3.42s across3entryacceptancefiles,16SDKdeprecationwarnings. Tests actual _persist_entry_fill+_st_open with temporarySQLite and realSDKstate objects/fakeorder_state; partialentryfakebroker explicitlyhas mode andofflineunavailablehistory. No actualDB/broker/start/deploy. Worker closeintegration mayconsume confirmedmetadata only withscopedoriginalentryidentity; preserve root entrymethods. Runtime pendingorder persistence/closeintegration/positioncontinuity stillnotfullyaccepted.


## ARCH380 — broker status mapping corrected and actual duplicate-send regression passes
Root _close_evidence_status now uses installedSDK OrderExecutionReportStatus enum names, preserves CANCELLED evenwithpartialfill, rejectsunknown/noninteger/boolstatus and status-quantitycontradictions instead of inferringFILLED fromquantity. NEW4 staysNEW; REJECTED2 staysREJECTED. _journal_observe catches invalidstatus beforewritingevidence and retainsreservation. Worker do not overwrite these two methods.
Actual .8session30801exit0:15passed2.65s,16SDKdeprecationwarnings. Includes12status/quantitychecks +actualLiveBrokerclose/tempSQLite NEWzero andunknownzero followedby newclientID:onlyonepost, unresolvedreservationretained,notrades; partialCANCELLED records10units thenreleases. Initial12methodchecks confirmed9fail/3pass beforefix; initialactualtestimport corrected to projecttests package. These3integrationcases reuse worker offlinefactory withlegacycontextNone andSQLite-only PK adaptation; they do NOTaccept missingpositioncontext,scopeguard,responseidentity or actualPGadapterwiring.
Worker scope corrections ARCH379 remain open: populateverifiedposition_snapshot, capturebroker/account/mode acrossawaits andsendcapturedaccount, validate responseidentity and exacttypes before observe/settle, removefallbackinternalaccount1. Your partialfixture status2 meansREJECTED; change intendedpartial to SDK PARTIALLYFILL5, do not loosenstatusvalidator. Currentfullintegration NOTaccepted, no realbroker/start/migration/deploy. Root will continue independentacceptance/recoverywork; notifywhen correctionsready.


## ARCH382 — close scope and dispatch guards accepted in30focusedchecks
Root captures mode/account beforefirstpositionawait, validates explicitscope/side, checksunchangedscope afterposition/lot/internalaccount/reservation, insidepostthread and afterresponse. Send usescapturedaccount and expected_scope. _api_post_order bindsroutingmode before service lookup, checks expectedmode/account bothbefore/afterlookup and rejects mismatched accountarg; expected_scope consumedlocally, neverSDKargument. Latecontextchange cannotroute sandboxintenttolive or reportnewscopecompletion; unresolvedoriginalreservation retained forreconcile. Aftersettlement scopechange suppresses returnedtrade, durableoldscopeaccounting retained forlaterrestore. Fullmodeswitch orchestration stillnotcertified.
Observedworker concurrentcorrections capturedschema/internalaccount/responseidentity/contextinterface; root didnotoverwrite these, tookguardregions ARCH381. Independentrootfixtures nowexplicitinternalaccount,matchingrequestidentity,knownentrytime; oldincompletefactory alone no longerexercisesclose. Rootstatusacceptance updatedto currentcontextcontract ratherthan weakeningchecks.
Actual .8session97175exit0:30passed3.83s (15status,11scope,4cache),16SDKdeprecationwarnings. Scope tests: beforeposition/lot, duringreservation(account+mode), aftersend, unchangedlive/sandboxpositive paths, actual_api_post_order service-routing guard with mockedservices; no realAPI. New/unknownzero stillonepost afterretry,partialcancelaccountedbefore release. TempSQLiteonlyadapter acceptance; earlierjournalPG acceptance separate, no fullPGadapterclaim. Worker remainingresponsibility: responseidentity/type/fullclosefixtures and runtimeverifiedentrycontext plumbing; publish finalscope/testreceipts, do not overwrite rootstatus/guardregions. No migration/start/deploy/actualbroker, profitabilityunproven.


## ARCH383 — actual close adapter + isolated PostgreSQL acceptance7PASS
Root added tests/test_live_close_postgres_acceptance.py using existing freshclusterfixture andactual0006 migration, actualLiveBroker.close_position + realjournal/model, realinstalledSDK PostOrderResponse objects; ONLYSDKpost isfake. Internalaccount explicitlyseeded inisolatedschema. Independentasyncpg connection frompostthread confirms committedreservation+snapshot visible BEFORE fakebrokerpost returns. No productionURL/networkbrokerused.
.8session80456exit0:7passed16.26s,16SDKdeprecationwarnings. Full20shares/2lots settlesonce; partial10shares/1lot staysunresolved; NEWzero andwrongrequest retainreservation/notrades; disposedpool+newadapter rejectsrepeat(originalID afterfull, newIDwhilepending). Timeout afterpost survivesadapterrecreation noresend. Injectedfillledgerfailureaftertradeflush leavesobserved20/accounted0, noPaperTrade, noresend. Unknownentrytime fullresponse remainsobserved/unaccounted, noinventedtrade. RealPG tableFK/constraints used, noSQLitePKadaptation. Clusterteardown passed. This is adapter/journal/DB integration withfakeSDK, NOTruntimecaller/realbroker/positioncontinuity or profitacceptance.
Worker immediateadaptercore nowhas independentpositivePG evidence forthisscope; remainingruntime verifiedentrymetadata forwarding, latefill recovery, brokerinventory continuity, open-intentdurability and UIconsumers stillunaccepted. Root tests do not excuse unchangedoldfixtures orroot guardoverwrite. No applicationstartup/schemaapplication/realorders. Receipt live_close_postgres_acceptance_r1.json.


## ARCH385 — scoped close restoration wired before streams/startup
Root _restore_close_intents reads durableunresolved rows for explicitbroker mode/account, validates originalidentity/wholelots/cumulativeaccounting/snapshot, constructs pendingcloseorders with originalrequestID/side/lot/requestedunits and observed/accountedmetadata. Doesnotinventheldpositions/prices orsendanything. Restoredqty=requestedminusaccounted is requestarithmetic, not freshbrokerposition. Conflictingexistingpending orchangedbroker/scope aborts withoutpartialinstall. Missingjournal/errors pauseentries andraise. Anyrestoredclose pausesnewentries; emptyresult preservesexistingpause; repeatedrestore retains samependingobject.
start now awaitsrestore immediatelyafterLiveBroker construction, beforeStreamManager creation/start and beforestartup/run tasks. Removedlater entries_paused=False thatoverwrote alreadyloaded userflags. Fullstart/network path NOTexecuted; callorder/sourcewiring reviewed. Paper/replay unaffected byrestorecallbranch. Missing0006 intentionallypreventsbrokerstart; do not auto-create/bypass. No productionmigrationapplied.
Actual .8session18743exit0:7passed15.19s,16SDKdeprecationwarnings, freshPostgreSQL+actualruntime methods. Persist/reconnect→restore→_execute_pending/_submit_close_reconciled neverresends; account/modeisolation,partialaccounting,missingtable,scopechange,existingconflict,emptymanualpause covered. BrokerSDK methods assertnotcalled. Earlier5casespasssession28405. Temporaryclusterstopped. Journal→runtime recovery is nowimplemented; downstreamreconciliation stillneedsdurableobserve/latefillsettlement/resumeintegration, so this is NOTcompleteautomaticrecovery. Current_reconcile_pending_orders canclearlegacyinmemoryterminalzero without updatingjournal; root next addresses commonDB/memoryreconciliation, no claimresolvedyet.
Worker preserve rootrestore/start/entrymethods andscopeguards; closeentrymetadataforwarding stillneeded because LivePosition currentlydoesnotpopulate confirmedfields. No realbroker/API/start/deploy/profitclaim. Receipt runtime_close_recovery_r1.json.


## ARCH387 — durable close observations precede runtime cleanup,90checksPASS
Root _reconcile_pending_orders now persists everyclose observation through BrokerOrderJournal.observe using originalscope/request/FIGI/side/units/lot/created_at and SDKstatusvalidator before terminalzero memorycleanup. No restored-only bypass. Invalid/missing/conflictingstorage retains pending/protection, flags durable_observation_pending andpausesentries. Confirmedcumulativeunits retained inmemory evenwhenpersistencefails, preventinglaterzero regression. Scope/pendingidentity recheckedafterDBawait. Nonzeroobservations persisted but remainaccountingpending; latefill settlement/UI/position updates notimplementedbythisstep.
Journal.observe now idempotentlyacknowledges EXACTrepeat of an alreadycommittedemptyterminal record (allidentityfields, brokerID/status, observed/accountedzero, releasedslot). Doesnotcreateanotherreservation orpermitresend; differingidentity/status rejected. Fixes crashwindow DBterminalcommitted/runtimependingstillpresent. ExistingSQLite tests updatedtoassertduplicateack/wrongIDreject. Earlierdocs sayingallreleasedobservationsFalse superseded onlyforthisexactzero case.
Actual .8session56883exit0:90passed20.08s across realPG recovery/reconcile13,SQLitejournal13,closepending8,closecaller38,openreconcile18;16SDKdeprecationwarnings. RealPGcases terminalcancel/rejectzero release+memorycleanup+repeat, NEW/partial/fullquantity evidence persistsandholds, injectedstoragefailure retainsbothstates thenretryack succeeds. No protectiondeletion orbrokerresend. Unit-onlylegacyclose fixtures now explicitlymockjournalcommit acknowledgement; realPGcases independentlyteststore behavior, no hiddenproductionDB fallback. Openreconcileunchanged and18tests retained.
Worker preserve commonreconcile branch; root next latefill settlement integration. No actualAPI/networkstartup/productionmigration/deploy. Receipt durable_close_observation_r1.json.


## ARCH388 — contradictory execution history rejected
Root found broker_fill_evidence accepted positive executions with NEW/REJECTED and a fully executed order marked PARTIALLYFILL. Five new cases failed before the fix (session 36829: 5 failed, 26 passed). Parser now rejects these contradictions, while accepting partial/full CANCELLED executions. This aligns history validation with the existing close adapter status contract. Runtime integration confirms contradictory evidence remains unknown rather than acquiring confirmed entry provenance.
Updated cumulative-entry fixture to transition PARTIALLYFILL to FILL when all requested lots execute; added separate inconsistent-status runtime case. Final .8 session 45214: 52 passed in 3.61s, 16 SDK deprecation warnings, covering parser, actual entry persistence with temporary SQLite, and close status acceptance. No real SDK calls, production writes, migration or startup. Late close settlement remains incomplete; this fixes a prerequisite used by current entry persistence and future recovery. Worker preserve broker_fill_evidence and these tests.


## ARCH389 — position repair respects pending execution
Root found _reconcile_positions could rebuild, orphan-close or deduplicate a ticker while its order remained pending accounting. It now excludes pending open/close and in-flight close tickers, preserves unresolved status, and continues processing other tickers. Dynamic checks before row changes and a pre-commit check roll back repair changes when a pending execution appears during an awaited read. This is an in-process guard, NOT a cross-process transaction lock or proof against changes during commit. Startup durable restoration remains required; late fill settlement is still incomplete.
Four regression cases failed before fix (.8 session 56330: 4 failed, 6 passed). Final session 3962: 38 passed in 3.01s, 16 SDK deprecation warnings: position-repair 12, pending-open reconciliation 18, pending-close 8. Tests invoke runtime methods with mocked broker and session; rollback test verifies invocation, not real-PG rollback. Covers orphan/rebuild/distinct duplicate/inflight exclusion, unaffected second ticker and late pending rollback. No real broker calls, production writes, migrations or startup.
Worker preserve root _reconcile_positions pending-execution guards. Next recovery work must settle broker-confirmed close deltas and integrate scoped runtime/local accounting, not use orphan repair as a substitute.


## ARCH391 — stage-based close delta accounting implemented and tested on isolated PostgreSQL
Root added close_fill_accounting.build_close_delta and optional evidence path in BrokerOrderJournal.settle. Uses original durable position snapshot and validated OrderFillEvidence. Under the same FOR UPDATE transaction as accounting, loads prior fill records, compares every previously accounted stage ID/quantity/price/time with current cumulative broker history, and constructs PaperTrade only from unseen stages. New delta is priced independently of cumulative average. Stores exact stage evidence and explicit configured_estimate commission basis in nullable BrokerOrderFill.execution_evidence JSON; model and UNAPPLIED 0006 migration updated. Existing immediate transient-trade path remains supported, with null stage evidence. Prior partial accounting without stage identities deliberately cannot be silently reconstructed from rounded average prices. Original unknown entry time/account/cost basis blocks new accounting; zero configured rate remains zero.
Tests: session 38816, 38 passed 31.79s (initial 8 new PG + 17 prior PG journal + 13 SQLite journal). Final session 33423, 18 passed 30.52s (11 stage-accounting PG + 7 actual adapter/PG acceptance), 16 SDK deprecation warnings. Fresh temporary PostgreSQL only, fake SDK responses. Cases partial 102 then remaining 108 (not cumulative 105), disposal/reconnect, repeated response, eight concurrent settlements one trade, cancelled partial release, altered prior stage rejection, unknown context rejection, SHORT accounting and zero commission, legacy partial proof gap. Fees are estimates, not actual broker charge evidence. No full-suite, production migration, real API or startup.
This is journal integration, NOT completed runtime recovery. _reconcile_pending_orders does not yet invoke the new evidence path. Before wiring it, retain a durable runtime/bookkeeping acknowledgement so journal settlement cannot release the FIGI slot and disappear from recovery before local position/accounting effects complete. Existing immediate-path release also needs that audit. Do not declare live readiness or profitability.
Worker: observed new _entry_provenance currently takes first recent scoped row with confirmed metadata; this is insufficient identity evidence. Please harden your entry-provenance integration against wrong position cycle: verify side, full original quantity, entry price, lot, original entry request and stage payload, not only FIGI/mode/account/time-source. Preserve root stage-accounting files and pending-reconciliation method. Report focused evidence in root chat.md. Root owns runtime settlement acknowledgement/integration next.


## ARCH392 — runtime now records late fills with durable local-accounting checkpoint
Root added nullable runtime_accounted_units to BrokerOrderIntent and unapplied migration 0006, constrained 0..accounted_units when present. settle(defer_runtime=True) activates the checkpoint on the first settlement; later calls cannot bypass it by omitting the flag. Terminal fill keeps unresolved FIGI reserved until local accounting is acknowledged. Existing accounted execution without a checkpoint cannot be guessed as unprocessed (explicit rejection); legacy immediate adapter path is unchanged and still needs integration/audit.
BrokerOrderJournal.apply_runtime locks the same intent and supplies only unacknowledged durable trades to an async database handler. Handler must use the supplied session, must not commit or perform external effects. Handler writes and checkpoint update commit together. Repeated/concurrent calls skip already applied trades. Error after an effect rolls both back. Full or cancelled-partial broker-terminal state releases the slot only after all observed/accounted units are applied. This API is implemented/tested; production SandboxTrade projection handler is not yet connected.
_reconcile_pending_orders now parses actual OrderState stages and calls settlement with defer_runtime=True for nonzero close observations. Persists the trade, keeps pending/protection, marks runtime_accounting_pending, pauses entries. _restore_close_intents validates/restores runtime counter and pending flag; fully executed qty=0 pending order still blocks resend. SDK post never invoked by recovery tests. Old unknown snapshots/legacy partial evidence remain held, not fabricated.
Validation: session 98710 initially 26 pass/1 fail because fixture stages were in the future relative to actual .8 clock (04:21 UTC vs 11:01 fixture); production future-evidence guard correctly rejected. Fixed test clock only. Session 4168: 53 passed in 50.15s (3 delivery PG +13 recovery PG +11 stage PG +8 close-pending +18 open-pending). Added rejection of adopting old accounting without runtime checkpoint; final session 8748: 4 delivery PG passed in 16.83s. Both had16SDKdeprecationwarnings. Isolated PostgreSQL, fake broker responses, no production writes/migration/API/start. Delivery test callback adjusts a synthetic test PaperAccount only; it is not real broker capital/profit evidence.
Next root scope: actual scoped SandboxTrade projection in the same journal transaction, then idempotent memory/protection reconciliation and acknowledged-state cleanup; immediate adapter must adopt equivalent checkpoint before end-to-end readiness. Do not release/purge pending requests merely because PaperTrade exists. Worker preserve journal/models/migration/reconcile/restore ownership; entry provenance hardening task from ARCH391 remains.


## ARCH394 — actual local position projection implemented under journal transaction
Root added close_position_projection.project_close_fills, a real SandboxTrade database handler for BrokerOrderJournal.apply_runtime. Requires explicit paired local_position_id and entry_client_order_id in immutable ClosePositionSnapshot (new optional validated/serialized fields). Locks original row by ID and checks mode/account/FIGI/side/remaining quantity/entry time/price plus original entry-request/time provenance. No latest-row selection. Missing/mismatched context keeps acknowledgement pending.
Partial projection copies a closed allocation row for exactly the durable PaperTrade delta and decreases original open quantity; residual stop/target and entry metadata are retained. Final allocation closes original row. Multiple unacknowledged trades apply in order in one transaction. Exit metadata links original row, entry/close requests and PaperTrade ID, and labels configured cost estimate. No invented times or prices. This handler is implemented and tested through actual apply_runtime; _reconcile_pending_orders does NOT yet call it. Before wiring, complete original-link capture and memory/protection cleanup including already-acknowledged retry path. Current full settlement release semantics must not cause a stuck in-memory pending request after successful projection.
Tests on .8: session11152,15passed28.59s (11 real PG projection +4 delivery),16SDKdeprecationwarnings. Session47903,19snapshot checks passed2.02s. Projection covers partial→reconnect→full, exact repeat, two unapplied deltas, wrong account/mode/request/side/quantity/price/closed/missing row rejection, and injected failure after projection flush rolling back both split rows and acknowledgement before successful retry. Preserved residual protection verified. Test money is synthetic; no profitability evidence. No production writes, migration, actual API/start/deploy.
Worker ARCH393 task still stands: wire verified local row/request IDs into snapshot. For future close after a partially cancelled order, original entry stage quantity exceeds residual position quantity; provenance validation must account for durable prior close allocations, not rewrite original stage evidence or invent a new entry. Do not relax identity checks to bypass this case. Root owns final projection/runtime integration.


## ARCH395 — late-close runtime projects local portfolio and finishes memory after broker inventory check
Root wired _finish_close_runtime into nonzero pending-close reconciliation. Loads original scoped journal row, flushes broker portfolio cache, checks actual inventory: full close requires no position, partial requires exact FIGI/remaining units/side/entry price. Rechecks broker object, mode/account, runtime mode and pending identity across awaits. Calls apply_runtime(project_close_fills), then updates residual _exit_qty/held state or clears full-close exit protection/held/opposite/entry-bar state. Terminal orders removed; partial active orders remain. Entry pause is retained intentionally; automatic resume not implemented.
Added journal.load with original identity validation and an already-SETTLED retry path: if DB projection/ack committed but memory cleanup failed, a repeated reconciliation uses that durable acknowledgement, verifies inventory, and completes memory without another trade. Does not run legacy _st_close again. Errors/scope changes/mismatched or unavailable inventory preserve pending/protection. Ledger observation/settlement may exist while local projection remains pending; no invented flat inventory.
Evidence: .8session37282,27passed50.59s (10 new runtime projection PG +4 delivery PG +13 recovery PG),16SDKdeprecationwarnings. Actual runtime+actual journal+actual local projection+real isolated PostgreSQL, broker calls mocked. Full, partial→full, partial→cancelled residual, wrong qty/side/price, still-present full position, timeout, account switch, and failure after DB commit before cleanup all covered. No SDK post. Final caller/provenance regression session86226:82passed7.65s (8 pending-close,18 pending-open,38 close callers,18 worker provenance). Earlier same set session72493 passed with an unawaited Connection._cancel warning; found old close-caller fixture could enter global SessionLocal during new provenance lookup. Explicit mocked unknown provenance now isolates that fixture; final warning gone (only16SDKdeprecations). Dedicated provenance tests still exercise worker implementation with temporarySQLite. No claim prior fixture could not attempt a SELECT.
Worker ARCH393 linkage/side-normalization changes observed in current source and their18 acceptance cases independently rerun. Root PG test starts with persisted link; adapter→journal→projection full integration still needs an immediate-path redesign. Immediate adapter still records/releases legacy transient trades and old callers still use _st_close, so complete live/sandbox lifecycle parity NOT established. Legacy partial with no stage IDs cannot silently upgrade. Production migration/start/API/deploy not done, profitability unproven.
Next root: unify immediate adapter completion with stage-evidence/journal projection flow, avoid duplicate legacy _st_close, and distinguish recovery pause from manual pause before automatic resume. Worker separate task: implement verified entry provenance for a residual after partially cancelled close using durable linked close allocations; preserve original entry stages. Tests must reject foreign/missing/duplicated allocation links and accept a valid remaining quantity. Stay in entry provenance helper/tests; root owns close submission and completion flow now.


## ARCH398 — immediate and late close completion share stage accounting; manual UI bypass removed
LiveBroker.close_position now returns immutable CloseSubmission after committed reservation and validated durable observation. Removed immediate PaperTrade creation, response-price PnL calculation and synthetic exit time. No adapter settlement/release of nonzero fill. Zero configured commission now remains zero in the saved snapshot. _submit_close_reconciled validates receipt scope/identity and invokes reconciliation for this FIGI immediately; same stage evidence, deferred journal checkpoint, local projection and broker-inventory verification as timeout recovery. Successful projected close returns reconciled_closed. All runtime close callers handle it without legacy _st_close: close_all, reduction, intrabar, session, signal, universe removal, protective exit and replay finalization. PaperBroker legacy close accounting is preserved; full cross-mode strategy parity is not established by this change.
Manual /positions/close now calls the same runtime close_all filtered to the explicit FIGI with manual_close reason. Removed direct adapter invocation and automatic sandbox fallback. close_all captures mode/account/broker across portfolio await and before each submission. A changed scope returns pending instead of trading another account. Manual endpoint returns closed=False for pending/not_closed; command summaries may carry price/net_pnl=None plus order_id rather than fabricating a price from a candle. Economic details are in the ledger/local portfolio.
Tests on .8: session76668,51passed37.59s (initial3 real PG full adapter→runtime cases +10 late runtime PG +38 prior callers). Session33720,70passed5.14s (44 caller cases incl6 projected no-double-accounting branches +15status +11scope). Session16533,11passed32.60s (4 unified PG, incl zero fee, +7 actual adapter PG observation/reservation cases). After manual route/scope guards, session22100,75passed6.26s; final unified PG session61977,4passed14.24s. All SDK transport mocked; temporary isolated PostgreSQL. Full, partial→later full and timeout→later full each submit once; price comes from stages (105 versus post response99); no legacy _st_close; zero fee verified. Independent connection still proves reservation committed before fake post.16SDKdeprecationwarnings each. No full suite/profit claim, production migration/start/API/deploy.
Old adapter test expectations intentionally changed from instant trades/release to observation-only receipts. Root updated owned status/scope/PG tests. Worker receipt/provenance fixture updates observed but not all independently rerun while that scope is evolving; explicit no-SDK fixture request ARCH397 remains. App/bot/execution.py is the separate documented paper research adapter; no live app caller found in search. UI should interpret closed=False/pending, not assume POST means success.
Remaining: separate manual/risk/recovery pause reasons and resume only recovery-owned pause after reconciliation; currently entries remain paused after a reconciled fill. Durable open-intent recovery, stale/legacy entry provenance migration, residual-cycle proof, actual broker fee reconciliation, UI display and full strategy/live-sandbox-test parity remain unproven. No ready-for-live claim. Worker can continue residual-provenance and receipt-test task; root owns pause state next.


## ARCH399 — independent pause reasons and scoped risk verification before recovery resume
Root introduced EntryPause: manual flag plus keyed automatic blockers. entries_paused remains the effective compatibility property; assigning it changes manual intent only. Close blockers are keyed by mode/account/request, startup recovery failure by scope, entry ledger failure by scope/request. A successful terminal reconciliation removes only its own blocker; partial/unresolved work stays paused. Risk is freshly checked after local projection and before memory cleanup/resume. Manual pause arriving during broker await survives. Failed risk read keeps close pending, so the already-acknowledged retry path can finish after facts become available.
Found/fixed refresh_daily_pnl mixing accounts within one mode. Live/sandbox query now uses captured broker sessions, explicit mode/account and MSK trading date window up to as_of; excludes duplicate_cleanup (non-economic bookkeeping), rejects unknown/nonfinite outcomes rather than treating them as0, verifies scope after await before caching. Circuit-breaker reason is registered even when manual/recovery pause already exists, and only refreshed known risk can clear it. Manual resume refreshes risk and cannot override active blockers; save_bot_flags stores manual request only. Startup now calls the circuit-breaker check instead of merely refreshing PnL (full startup not executed). Status/manual pause result expose entry_pause_reasons without account IDs.
Evidence: .8session11053,37passed62.36s (initial4 reason tests +6 scoped risk PG +10 close PG +4 unified submission PG +13 recovery PG). Session45137,60passed4.04s (5 reason/manual-resume tests +6 entry evidence +44 close callers +5 manual route). Final session12813,22passed29.85s (7 scoped risk PG +4 unified PG +5 reason tests +6 entry evidence).16SDKdeprecationwarnings. Checks manual/recovery/risk coexistence, another account/mode losses excluded, unknown prior PnL holds then resolves, scoped refresh account change after DB read retains pending and does not cache wrong scope, successful retry resumes; real PostgreSQL isolation and mocked broker only. Test clocks advanced coherently between submission and risk observation; no production API/start/migration/deploy.
Limits: this proves resume on the covered close-completion paths, not all risk management. daily_pnl_cached still has old TTL→0 fallback; root will address that next. Open-intent recovery and risk-cache scope/date invalidation still need audit. Known configured fee estimates are not actual broker fee reconciliation, profitability unproven.
Worker UI task: display entry_pause_reasons as readable reasons (manual, awaiting close reconciliation, entry accounting pending, journal recovery failure, daily loss limit, risk facts unavailable). Manual resume can return entries_paused=True with blockers; do not show successful resume simply because the request returned200. Also preserve closed=False/pending handling from ARCH398. Keep root pause/risk/reconciliation code untouched. Residual provenance acceptance reported by worker remains to be independently reviewed by root.


## ARCH400 — unknown daily risk no longer becomes zero; queued entries recheck risk
Root replaced daily_pnl_cached TTL-to-zero fallback with nullable evidence. Cache is bound to runtime mode, broker object/account (paper run identity), aware clock, MSK date and strict 30s freshness; backward time invalidates. Getter never overwrites loss or turnover state. RiskSnapshot UNKNOWN blocks entries, nullable status and gate messages supported. Async refresh serialized per runtime; signal acceptance, order time gates and queued open execution refresh before permission. Queued entry checks actual risk, not merely manual pause. Close paths do not get the entry gate.
Paper risk reads bound broker sessions and explicit default PaperAccount, missing account fails; foreign PaperTrade accounts excluded. Live/sandbox captured scope verified after query as before. UI worker: risk.daily_pnl may be null and risk.state UNKNOWN; display unavailable, never zero. No edits to root risk files needed.
Evidence .8: session58579 110 passed7.93s (cache, reasons, sessionrisk, gates, partialentry). Session92012 94 passed28.03s (expanded cache and queued-entry no-post checks, partialentry, scoped recovery PG, unified close PG). Session60742 paper risk PG 3passed13.14s. Initial paper unknown fixture attempted SQL NULL prohibited by schema; corrected to PostgreSQL Numeric NaN, final3pass. Actual isolated PostgreSQL, fake SDK only,16deprecationwarnings. No working DB writes/API/start/deploy/migration. Production readiness and profitability unproven; open-intent durability still outstanding. Refresh failures currently retry at next entry check; no retry backoff added.


## ARCH401 — opening submission uncertainty retains original scope instead of false rejection
Root audited open path before durable recovery. LiveBroker.open_position previously read mutable account in its thread and let non-timeout post/response exceptions become runtime REJECTED. It now captures valid live/sandbox/account, validates positive integer lots and direction, passes expected_scope to dispatcher, preserves original request id and clears stale last_execution. All post/response-processing exceptions become EntrySubmissionUnknown; post-send scope change likewise. Runtime captures broker object and original scope/requested lots into pending metadata before send, checks object/mode/account after await, and handles EntrySubmissionUnknown as PENDING_RECONCILIATION, not REJECTED. Existing scoped reconciliation refuses the wrong account; pending execution refuses a second post.
Evidence .8session62913:123passed3.95s (8 adapter scope/transport tests,71 partial-entry/risk/unknown-path checks,44 close-caller checks),16 SDK deprecation warnings. Fake services only. Includes connection loss after simulated acceptance, account switch after post, runtime broker replacement, service-lookup switch with zero dispatches, malformed response parsing, unchanged positive path. Initial test's exception object identity failed for TimeoutError crossing asyncio thread bridge; fixed assertion to original type and args, not object identity. No real API/DB/start/deploy.
Limits: process-local pending protection only. Open-intent durable reservation before submission and startup restoration still missing. This does not establish crash safety, full engine parity, live readiness or profitability. Explicit broker rejects conservatively remain pending until an authoritative order-state response; no exception text is treated as execution evidence. Root retains ownership of entry submission/durable journal work; worker UI/provenance assignments unchanged.


## ARCH402 — typed opening reservations share the durable instrument lock
Root added common OrderIntent plus separate CloseIntent/OpenIntent. Shared journal/schema now store action (close default) and opening plan (version1 ticker/strategy/exact decimal stop/target); validated positive whole lots, aware original time, strict payload decoding and directional protection ordering. Open/close share the existing unique unresolved FIGI per mode/account and original request primary key. reserve/load/observe are action-aware; unresolved defaults to close for existing recovery, explicit action=open reads opening reservations. Close restoration/settlement/runtime accounting reject opening intents rather than treating BUY/SELL as sufficient to infer action. Empty confirmed cancellation releases instrument but never permits reuse of original request; partial cancellation keeps reservation until accounting.
Migration0006 source expanded, not applied to working DB. No adapter/startup opening integration yet: this is the persistence primitive required for it, not a claim of process-crash safety in live bot. Opening stage accounting and local position projection/ack must be added before activating this path; otherwise successful entries would leave durable reservations forever and block protection closes. Existing close recovery and submission remain tested.
Evidence: .8session41720 existing journal17passed; new4 initially failed because chosen fixture had already reserved a close for same FIGI/request, correctly excluding open. Replaced fixture with fresh migrated isolated DB (did not weaken exclusion). Final session20681 22passed51.54s:5opening PG +13close recovery PG +4unified close PG. Session60231 32passed1.91s:13opening contract/invalid recovery +19close snapshot. PG proves committed restoration after pool disposal, concurrent open/close exactly one winner, partial/cancel monotonic observation and no close-path accounting of open. It does not simulate full process kill. Fake SDK,16deprecationwarnings; no working DB/API/start/deploy/migration.
Worker stays on UI/provenance; root owns next opening accounting/recovery integration. No profitability or live readiness established.


## ARCH403 — opening stage accounting and local position commit together
Root added services/open_fill_accounting.py and entry_accounting JSON checkpoint on BrokerOrderIntent/migration0006 source. settle_open locks original typed opening reservation, requires exact observed broker ID/cumulative quantity/status and scoped RUB stage evidence; validates positive whole-lot units, unique stage IDs, finite prices, actual aware execution times between original intent and as_of. Initial accounting refuses to adopt any pre-existing unlinked local open position. Creates SandboxTrade with actual first stage time, cumulative weighted price, confirmed shares and saved protection; commission/net remain unknown. Stored entry metadata uses existing broker_history/stage-proof contract.
Subsequent partial→full accounting validates all previous stages unchanged, exact linked local position/mode/account/side/quantity/price/time/protection and proof metadata; updates same position, never creates another allocation for opening. Local insert/update and accounted_units/checkpoint commit in one transaction. Repeated/concurrent same proof returns delta0. No release yet: runtime_accounted_units remains0 and unresolved FIGI retained until future broker-inventory/runtime completion. Partial cancellation records only filled units and likewise stays pending. Model is currently unleveraged; margin and additional opening policy metadata not wired.
Evidence .8session35872 10passed31.16s (initial5 accountingPG +5 reservationPG). Finalsession57978 13passed29.55s (9 accountingPG +4 unified closePG),16SDKdeprecationwarnings. Covers partial→full weighted103 from102/104, repeat,4concurrent settlement calls one local position, pool disposal between stages, injected failure after INSERT rolls back position+checkpoint and retry succeeds, changed old stage rejected, local price/qty/account drift rejected, foreign account proof rejected, partialcancel. All actual isolated PostgreSQL, fake SDK; no production API/DB/start/deploy/migration.
Still incomplete: opening adapter reserve-before-send, startup restoration, broker inventory verification + runtime acknowledgement/release and replacement of legacy entry persistence have not been wired. This is a tested transaction component, not full live opening recovery. Root continues integration; worker assignments unchanged. No profitability or live readiness claim.


## ARCH404 — startup opening restoration and durable reconciliation wired
Root added _restore_open_intents immediately after close recovery in LiveBroker startup, before stream setup. Loads only opening rows for captured mode/account, validates typed plan and whole-lot counters, restores original request ID/creation/quantity/lot/protection payload as PENDING_RECONCILIATION, never resubmits, preserves existing matching pending object. Errors/scope changes/missing storage fail startup with entry_recovery_failed; each restored order owns an entry_reconciliation blocker. Separate open/close loader action filters avoid misclassification.
Durable-open pending reconciliation now observes broker state into journal before terminal cleanup. Exact empty cancellation/rejection removes only its own pause; observation failure retains it. Nonzero stages call settle_open and store local position/accounted checkpoint in order metadata. Repeated stage accounting stays idempotent. It intentionally retains unresolved order and pause pending broker-inventory/runtime completion, which is not yet implemented. New adapter submissions still use legacy path (reserve-before-send not activated). Full startup/network not executed, only actual loader/reconciliation methods with actual journal/isolatedPG and mocked SDK.
Evidence .8session82355 20passed28.18s (7opening runtimePG +13close recoveryPG). Finalsession87354 expanded9opening runtimePG passed15.99s,16SDKdeprecationwarnings. Cases unaccounted/accounted partial restart, foreign mode/account excluded, no second post, pending object stable, empty cancel/manual pause ownership, partial/full actual local stage projection once, failed observation retains pending, scope switch while loading installs nothing, missing storage fails closed. No working DB/API/start/deploy/migration.
Outstanding before enabling opens: capture exact exit-policy parameters (levels alone cannot restore ATR/trailing semantics from changed runtime config), broker inventory/runtime acknowledgement/release, adapter durable reservation and replacement of legacy persistence; startup's older generic position-restoration query/policy reconstruction also needs scoped audit. Worker new independent artifact research task posted separately; acknowledgement not received, do not claim it is running. Profitability/live readiness unproven.
UI worker: add readable entry_recovery_failed reason (opening journal recovery unavailable); entry_reconciliation also covers recovered opening intent.


## ARCH405 — exact versioned exit-policy snapshot retained with opening evidence
Root added ExitPolicySnapshot for existing FixedSlTpPolicy, AtrStopPolicy and AtrTrailingPolicy. Captures every constructor parameter and policy version, excludes transient _atr_cache, restores exact supported implementation with no current-config defaults. Rejects missing/extra parameters, unsupported/versioned policy, malformed identity, boolean/nonfinite numeric fields, unavailable required values and invalid period. Snapshot immutable tuple parameters; JSON payload detached.
OpenIntent payloadv2 includes optional typed exit_policy; v1 remains readable as policy unknown, never inferred. settle_open persists full original entry_plan alongside stage proof in SandboxTrade metadata. Existing runtime opening path captures actual selected exit_policy into order.meta.entry_exit_policy before calling broker, forwards it in plan_meta, so legacy _st_open also has reproducible policy parameters. Snapshotting does not change policy algorithms or substitute a new strategy.
Evidence .8session88507 116passed39.06s (policy goldens, opening contract,71partial-entry/risk/unknown tests,9opening runtimePG,9opening accountingPG); finalsession43498 28passed3.56s after malformed identity guard. Six golden cases LONG/SHORT ×3policies compare fixed expected initial stops/targets and original/restored stepwise trailing outputs; ATR cache warm→serialized→fresh restored agrees. ActualPG confirms snapshot retained across reservation reload/recovered pending and cumulative position accounting. Initial test used exact110 comparison against110.00000000000001, corrected numeric golden tolerance; equality original/restored remains exact.16SDKdeprecationwarnings in runtime set. No working DB/API/start/deploy/migration.
Remaining: startup's generic existing-position loader still rebuilds policy from config and has an unscoped row query; must replace for evidenced positions, not claim snapshot capture alone fixes runtime recovery. Opening inventory/memory completion and ack/release still not wired, new entry submissions still not durable. Root owns this integration. Worker must preserve entry_exit_policy and entry_plan metadata when inspecting provenance; research ACK still not received. No profitability/live readiness claim.


## ARCH406 — durable opening completion restores protection and acknowledges exact broker inventory
Root wired _finish_open_runtime after durable opening settlement and added acknowledge_open transaction. Loads original journal/checkpoint and exact linked local position, requires saved exit policy, flushes portfolio and reads captured broker position. Requires exact FIGI/side/whole units and entry price at local precision; scope/object/pending checked across awaits. Installs original policy/SL/TP, held quantity/entry price and initial runtime protection state, then freshly checks daily risk. Acknowledgement locks intent and local position, verifies unchanged accounted/observed quantity/link, stage proof and entry price/time/plan, marks runtime_accounted_units; only full or cancelled remainder releases shared FIGI. Partial live remainder stays pending/paused; its actual filled position gets protection. Manual/risk pauses survive terminal completion.
Repeat protection installation is keyed by process-local broker/scope/request ownership, not a persisted metadata flag. Partial→full updates actual quantity/average without resetting already installed trailing state. If ack committed but subsequent pending cleanup failed, already-SETTLED observation retry verifies broker/local state and finishes memory without duplicate position. _reset_run_state clears ownership map. Scope switch/failure/mismatch preserves pending and pause. Broker inventory is an observed snapshot, not an atomic cross-broker transaction.
Evidence .8session16036 18passed30.41s (9initial completionPG +9recoveryPG). Finalsession82271 16passed33.75s (12completionPG +4unified closePG),16SDKdeprecationwarnings. Actual runtime+journal+isolatedPG; fake SDK. Positive full, partial→full, partialcancel; wrong/missing qty/side/price, account switch while reading inventory, manual pause, failed risk read keeping installed protection and pending, injected post-ack-commit failure followed by idempotent completion, forged protection_installed metadata cannot skip protection. No working DB/API/start/deploy/migration.
Still outstanding before activating durable opening submission: adapter reserve-before-send/receipt path and legacy persistence replacement; scoped generic startup restoration for already-completed positions (old loader can rebuild config or mix rows); active partial remainder cancellation before a protective close; interaction between legitimate persisted trailing updates and current strict opening local-drift guard. Full process-kill/startup acceptance not done. Root continues integration; profitability/live readiness unproven.


## ARCH407 — scoped startup recovery for completed live/sandbox positions
Root replaced live/sandbox branch of old generic startup position restoration with _restore_live_position_exits and recover_position_exit. Uses captured broker.sessions and explicit mode/account for local open rows; rechecks scope after inventory and DB awaits. No choosing latest duplicate row. Requires actual broker FIGI/side/positive whole units/entry price matching a unique scoped local position, saved supported policy (nested entry_plan or legacy entry_exit_policy; conflicting snapshots rejected), valid saved protection/leverage. Restores exact policy and current saved stop/trailing state; explicit absent TP remains absent, stale target removed. Current config does not recreate evidenced protection. Pending journal-owned instruments are left to pending reconciliation.
Per-position failure does not prevent restoration of other valid positions. Adds position_recovery_failed blocker scoped to account/FIGI; global read failure preserves previous held state and adds scoped blocker, never treats failure as flat. Confirmed absence in both successful broker inventory and local open rows clears only that recovery blocker. Manual pause untouched. _ensure_exit_state refuses to bypass pending/recovery failure with current-config protection. Legacy positions without saved policy remain unresolved/paused rather than receiving guessed original rules. Paper startup keeps its prior branch for now (not proof of three-mode recovery parity).
Evidence .8session9508 initial17passed31.44s (5positionPG+12opening completionPG); that invocation had an unawaited-coroutine assertion in test fixture, so it did not prove lazy fallback blocking. Corrected await/argument order and guard. Finalsession89945 51passed16.38s (7positionPG+44close callers), RuntimeWarning elevated to error;16SDKdeprecationwarnings only. Covers saved ATR policy despite changed fixed config, persisted tightened stop and disabled TP, foreign account/mode exclusion, bad one vs good second ticker, inventory failure retaining held, account-switch abort, duplicate-row refusal, recovery-blocker release after both inventories empty. Actual isolated PG and mocked broker, not full startup or process-kill test. No working DB/API/start/deploy/migration.
Outstanding: adapter durable opening reservation/receipt wiring, active partial remainder cancellation before protection close, strict opening drift guard vs legitimate trailing updates, paper recovery parity. Overall profitability/live readiness not established. Worker UI: position_recovery_failed means position protection cannot yet be restored from verified data; show actual pause reason, no success message based only on started=True. Root owns runtime/journal; independent research review task still awaiting ACK.


## ARCH408 — actual opening submission now reserves durably before post and completes through stages
Root switched LiveBroker.open_position to receipt-only OpenSubmission. Requires original request ID, exact supported exit policy, confirmed unchanged lot and unleveraged policy; flush/read preflight refuses existing broker position (no averaging). Captures scope, commits shared OpenIntent before post, passes same request/account into scoped dispatcher. Duplicate request never posts again; another unresolved FIGI reservation rejects submission. Post identity/quantity/status and observation must be stored before returning receipt. Post/observation/scope failures stay EntrySubmissionUnknown; no PostOrderResponse-price local accounting, no fabricated fill. Current path explicitly rejects leverage !=1/use_margin to match authorized no-margin phase.
Runtime sets original scoped PENDING_RECONCILIATION and its pause BEFORE awaiting adapter, so task cancellation while thread is posting retains uncertainty. Known pre-submission rejection clears only its own blocker. Valid receipt routes through _reconcile_pending_orders → staged atomic position accounting → broker inventory → original policy/protection → risk → acknowledgement. No legacy _st_open on live/sandbox opening. Paper still returns price through its existing path. Only app opening call sites besides runtime are in documented PaperBroker research execution adapter; no additional live caller found by source search.
Evidence .8session36357 initial3unifiedPG passed14.43s. Session22034 123focused caller/scope checks passed5.84s. Finalsession39601 113passed19.29s:9unified openingPG (full/partial/timeout,storage/held/lot/observation failure,duplicate+new-runtime recovery,task cancellation after post started), parser/id tests, adapter/risk/quantity guards, logging fixtures. Independent asyncpg connection inside fake SDK post asserts committed original opening reservation BEFORE post; planned/Post price99 vs actual stage105 proves stage-based position accounting.16SDKdeprecationwarnings only.
Additional close/paper check session8430:4unified closePG pass;7 old paper fixture failures (AST namespace missed LiveBroker/new policy inputs, removed close-price AST pattern, missing lot resolver on fake runtime). Updated fixtures: actual runtime paper-close helper replaces removed AST shape, quantity fixture includes real classes/policy, ledger fake explicitly resolves lot. Finalsession72622 all36paper checks passed7.21s. Earlier logging fake lacked risk evidence and was corrected with explicit known-risk stub; risk is separately exercised by actual runtime tests. Removed legacy assertions of immediate adapter-price booking; parser tests remain direct parser tests. No whole-suite run.
Receipt: backend/reports/connected_week_20261109_13_c1/ROOT/durable_open_submission_r1.json with source hashes and test history. No working DB/API/start/deploy/migration. Full process kill and full startup not executed; new runtime restoration/isolated journal covered.
Outstanding before operational acceptance: active partial remainder must be cancelled and confirmed before protective close; strict opening local drift check must accommodate legitimate persisted trailing updates without masking mutation; paper recovery parity; actual fee reconciliation. Generic live completed-position restoration now uses ARCH407 scoped policy path. Root continues; worker UI/research assignments remain, research ACK not yet received. 30% monthly target and live readiness unproven.


## ARCH409 — root verifies worker research review and freezes one candidate
Correction: worker research ACK/review was already present above ARCH405; root missed it while reading only journal tail. Prior no-ACK statements (including ARCH408) were wrong. Root now read the review and independently recomputed C5 October/November raw trades from result.json: gross - fees - slippage agrees with closed_net summaries within1e-6. Oct5m -970.9007508 (249 trades) vs15m -359.8528988 (93); Nov5m -53.7431625 (119) vs15m +481.2415575 (50). Nov15m without largest +308.5642297; October15m without largest -479.7533352. Both exact marked equity fields are null; no return/monthly edge claim. Read-only source receipt/hashes: ROOT/research_worker_review_verified_r1.json. Worker's other findings (correlated setup votes and coverage breadth) remain reviewed claims, not root-independent certification.
Root froze ONE hypothesis from C5+C4 without retuning: ROOT/C7_frozen_hypothesis_r1.json, SHA256 26441349454fc181abbeacb0933068b94489862547cbaf185c744fc6fe6c2c8f. 15m BUY quorum2, then exactly next closed5m close above prior15m high with low>=prior15m low; contemporaneous5m SELL-majority quorum veto; next1m open, unchanged admission/risk/costs. Explicit timing T at15m close, confirmation interval[T,T+5m), execution openT+5m. Paired control original15m. Proposed full Jan-Aug2026 historical comparison, all months reported; NOT an untouched-holdout claim and NOT cleared to run without coverage/source acceptance. No portfolio profit extrapolation from closed cashflows.
ROOT→WORKER: research ACK accepted. Implement the frozen C7 composition in a NEW offline research file/isolated output area; do not modify old/frozen runners/artifacts or runtime/journal owned by root. First deliver small golden timing checks: 15m bar not visible before close, exact next5m confirm, missing/late confirm cancellation, SELL veto, next1m execution and unchanged control. Produce implementation source hash and a read-only Hub/DB input coverage manifest for frozen38pool +warmup over requested dates; never source-download/backfill or write working DB. Do not launch evaluation before root reviews timing/parity and manifest. Existing UI work may finish first; report actual files/results through chat. No real API/trading/start/deploy. Once evidence is accepted root will run/coordinate paired comparison; do not select thresholds against outputs.
Execution work remains incomplete: source review confirms _close_pending_block currently blocks a protective close while an open remainder is pending and LiveBroker has no cancellation method yet. No cancellation/real API executed this turn; next root implementation must cancel original scoped broker order and verify terminal state before releasing intent/closing actual filled quantity. Overall goal unchanged, profitability unproven.


## ARCH410 — cancellation primitives and minute execution regression acceptance
Root verified existing runner uses stamps=sorted(by_ts), execute_minute outside is_node; only signal on_open is gated to its timeframe. Added parameterized golden checks across all five minute offsets, with and without another missing ticker. Actual .8 session4897:17passed1.16s. This confirms the scoped research scheduler, not full live engine parity; no database revalidation or synthesis.
Prepared LiveBroker.request_cancel_order bound to original mode/account/request ID using installed SDK ORDER_ID_TYPE_REQUEST; scope checked before lookup, before dispatch and after await. Cancel response is not terminal proof; timeout propagates. Durable CloseAfterOpen request stored on original open journal row, serialized under row lock, first committed reason retained, survives pool restart and opening acknowledgement. New nullable close_request in model and UNAPPLIED migration0006 source. Runtime cancellation/handoff/startup discovery NOT wired yet. No protection-exit completion claim.
Session69039:22passed28.84s,16SDK deprecation warnings. Nine cancellation scope/routing/timeout tests, four close-request PostgreSQL tests, nine unified opening PostgreSQL tests. Fresh isolated PostgreSQL and fake broker only. No real API, working DB write, bot start, deploy or production migration.
Next root work: durable cancellation -> authoritative terminal state/stages -> inventory -> close reservation, recover request after restart even when opening slot is released; preserve tightened protection during later opening fills. Worker C7 assignment unchanged; do not run evaluation before manifest/timing review. Profitability and live readiness unproven.


## ARCH411 — protective-close trigger persists while opening is pending
Root connected request_close_after_open to _submit_close_reconciled's durable-pending-open branch. Original scope/request/FIGI must match unresolved opening reservation; first committed exit reason persists before returning blocked_existing. No cancellation or close dispatch yet; failures remain blocked and report reconciliation required. Scope changes cannot attach a trigger to another account. This removes loss of the trigger from that caller but does not complete operational protective closing.
Added load_close_after_open: explicit live/sandbox/account scope, action=open, strict payload validation; includes released/SETTLED opening rows, skips SQL/JSON null only, malformed non-null payload fails. PendingCloseAfterOpen derives stable UUID5 closing request ID from versioned unambiguous mode/account/original request identity. Future handoff must use this ID and validate close-journal completion before retiring trigger; loader currently includes all saved requests, not only unfulfilled ones. No startup consumer wired yet.
Evidence .8session43816:7PG checks passed16.42s; finalsession54747:13passed32.60s (9close-request/recovery/runtimePG +4unified closePG),16SDK deprecationwarnings. Covers post-ack/pool-restart recovery, scope exclusion, null vs malformed payload, stable distinct closing identity, actual runtime blocked close persists original trigger, foreign account refusal. Isolated PostgreSQL/fake SDK; no working DB/API/start/deploy/migration.
Worker C7 implementation/coverage assignment remains current; no new response found this turn. Root next: cancel and confirm original remainder, reconcile final fills, crash-safe handoff to closing reservation and startup recovery, plus tightened trailing preservation. Goal remains active; profitability unproven.


## ARCH412 — scoped cancellation and post-cancel opening reconciliation
Root connected saved protective-close trigger to request_cancel_order in blocked durable-open path. Per-FIGI inflight guard wraps cancellation/reconciliation; original broker/mode/account rechecked after request persistence and cancel await. Cancellation exceptions are unknown outcomes, followed by authoritative order_state reconciliation if scope unchanged. Success response alone never releases reservation. Actual stages and inventory determine final opening quantity, including additional fills racing cancellation. Separate close_after_open blocker survives opening acknowledgement; startup now calls _restore_close_after_open_requests after open/close recovery and restores this blocker even for released opening rows. Recovery errors fail startup with scoped close_after_open_recovery_failed. No closing handoff/trigger retirement yet, so these requests remain paused pending that implementation.
Evidence .8session26115:12PG passed28.38s. Session67313:26passed41.48s (13requestPG+4unifiedclosePG+9cancel scope); runtime-mode post-save guard was tightened while that run was in flight, so final source rechecked separately. Finalsession90502:14requestPGpassed22.15s,16SDKdeprecationwarnings, including new mode-switch-before-cancel test. Checks cover cancellation ACK with stillPARTIAL remains reserved, cancel success/timeout racing full20shares (caller had10) accounts20 and preserves close blocker, restart after opening release restores close blocker, mode switch after persistence sends nothing. Isolated PG/fake SDK only; no working DB/API/start/deploy/migration.
Worker received readable UI pause-label task for close_after_open/recovery_failed; C7 implementation and read-only coverage remain worker research priority. No new C7 deliverable observed this turn.
Next root: closing handoff with stable saved closing ID and exact current position after terminal opening; completion proof/trigger retirement and startup resumption without duplicate close. Current helper restores pause but does not resume cancellation or send close at startup. Tightened trailing vs late opening fills still outstanding. Goal active, profitability/live readiness unproven.


## ARCH413 — exit completion proof and scoped pause retirement
Root added close_after_open_completed to recovery loader. Empty terminal opening (confirmed CANCELLED/REJECTED, zero observed/accounted and no runtime amount) needs no closing sale. Nonempty opening requires released SETTLED fully acknowledged opening and a closing journal under deterministic close ID, opposite side, exact quantity/lot/scope/FIGI, and snapshot linked to original entry request plus local position. Only fully FILLED/SETTLED closing with observed=accounted=runtime units and released slot fulfills the request. Partial/cancelled close or pending runtime acknowledgement stays active; wrong identity/link raises. Original close_request audit payload remains unchanged, not erased.
Successful runtime recovery clears only close_after_open blockers in the queried scope absent from remaining requests; manual/foreign/risk blockers untouched. No cancellation/handoff resume loop added yet. Completion predicate is journal-level proof, not independent fresh broker verification; operational handoff remains outstanding.
Evidence .8session36307 initial20PGpassed33.33s; finalsession43785 21PGpassed23.83s,16SDKdeprecationwarnings. New completion tests explicitly seed closing journal states to verify predicate, not claim end-to-end actual closing submission. Existing runtime cancellation tests still run actual journal with fake broker. No production DB/API/start/migration.
Worker C7 files now exist (not yet a chat delivery): worker_c7_r1/c7_timing.py, coverage_manifest.py/json, tests/test_c7_frozen_timing_golden.py. Preliminary read of manifest reports38withminutes but only10withwarmup_presence; no independent coverage certification or evaluation authorization. Next root should review worker handoff/source/timing/manifest and clarify date-dependent eligibility without dropping failed months or pretending absent minutes mean no trades. Worker must still deliver through chat. Root execution next: stable close-ID handoff, recovery resume and full pipeline proof; late-fill trailing preservation remains. Profitability/live readiness unproven.


## ARCH414 — root independent C7 review; corrections required before evaluation
Root inspected worker_c7_r1/c7_timing.py, coverage_manifest.py/json and timing goldens. .8session41255:23passed0.79s. Independent synthetic probes saved immutable ROOT/C7_root_review_r1.json against timing SHA256 af771d3674766d4a4f049a14d81574b1a20901f9a2f29fe1ec54190b5391c87b. No DB query/runner/API by root in this review.
ROOT→WORKER actionable findings in your owned files:
1. Missing same-label confirmation then late arrival changes CANCEL_MISSING_CONFIRMATION -> CONFIRM. Existing test_cancelled_decision_is_stable_when_a_late_bucket_appears calls confirm_at with [] twice, never supplies late. Pure lookup cannot prove expiry/no-revival without caller state. Implement explicit as_of/expiry and retained decision state in composition/caller, then golden-test early T+4 (not readable), exactT+5, late same-label T+6 (cannot revive). evaluate_15m_boundary currently returns future C7 decision alongside T control with no scheduler evidence; acceptable only as offline proposal with tested delayed event application. Show integration gating, not only timestamp labels.
2. Numeric validation accepts confirmation close=+infinity (finite high/low) and execution open=-1. Independent probes both CONFIRM. Validate finite positive OHLC-consistent confirmation and finite positive execution open for both arms; retain incomplete execution minute high/low invisibility (only open known then).
3. Manifest per_month month_utc boundaries are 22:00 UTC previous day, while frozen MSK month is21:00 UTC. SQL date_trunc(month/day,ts) depends on session TimeZone. Make month/day grouping explicit Europe/Moscow and export UTC boundaries; pin transaction timezone if necessary. Do not modify existing manifest; emit revision with corrected code/source hashes and recorded DB timezone. Remove embedded DB connection fallback; use project configured environment without printing credentials.
4. 10/38 warmup presence is start-of-window readiness, not a reason to remove28 tickers for all8months. Deliver per-FIGI earliest causal eligible boundary after120 closed15m +required60m trend history, using same aggregation as paired engine; unavailable periods explicitly ineligible. Preserve Jan-Aug reporting and pool. No future-based selection. Current manifest hashes code/config but not actual candle values; add deterministic input fingerprint in ordered stream/snapshot used by eventual run. Presence counts alone do not freeze input. Explain Hub provenance/read-only snapshot consistency; coverage_known=false remains honest, do not relabel it certified.
No hypothesis threshold change and no evaluation approval yet. Root review is evidence-based despite current23 green tests; worker keeps ownership, root did not edit your files. Deliver fixes/source hashes/parity timing and corrected manifest through chat. Root continues runtime protective-close handoff separately. Profitability remains unproven.


## ARCH415 — late opening fills preserve tightened trailing protection
Root changed settle_open local-position drift check: unchanged initial levels remain valid; changed protection is accepted only when persisted trailing_active=True, stop is finite positive Decimal and at least as tight as original stop for original BUY/SELL side, TP=None. Missing/looser/unmarked/changed-target changes reject. Original entry_plan must equal durable intent payload; stage/quantity/price/scope checks remain. Later fills update quantity/weighted price/evidence but preserve stored stop/TP/trailing and additional metadata instead of replacing all metadata. This is a contract for marked trailing relative to original baseline, not an authenticated history of every intermediate stop revision or support for arbitrary manual level edits while opening remains pending.
Evidence .8session17956:28passed36.20s (16opening accountingPG+12runtime completionPG),16SDKdeprecationwarnings. New BUY/SELL trailing preservation and five invalid mutation cases. Additional finalsession22234:1passed13.74s actual runtime partial fill -> _st_update_sl persisted101/active/TPNone -> full fill updates20/105, same runtime101/trailing/TPNone, journalack20/released. This uses isolatedPG, mocked broker, SessionLocal explicitly routed to isolated session. No working DB/API/start/deploy/migration.
Protective closing stable-ID handoff/resume is still outstanding. _st_update_sl legacy persistence selects latest scoped row; this test does not certify concurrency/duplicate-row semantics or all manual override paths. Worker C7 corrections ARCH414 remain current; no new chat delivery observed. Goal active, profitability/live readiness unproven.


## ARCH416 — saved protective exit hands off to stable closing request
Root added prepare_close_after_open: requires unchanged saved request, terminal/released opening with observed=accounted=runtime units, exact linked open SandboxTrade scope/FIGI/side/qty, original plan/stage proof/weighted price/time. Existing closing journal prevents any resend. Returns typed handoff with original local row, entry request/time/price and actual quantity.
Runtime _resume_close_after_open_requests wired after normal pending reconciliation in existing reconciliation loop. Restores saved scoped exit pauses; pending original opening resumes cancellation/reconciliation; subsequent iteration with completed opening verifies fresh broker position quantity/side/average and submits stable closing UUID through existing _submit_close_reconciled. Explicit typed proof carries exact entry identities into adapter snapshot without rediscovering latest row. Pending close stays with existing reconciliation path; fulfilled journal proof clears only own exit pause. Repeated handoff does not resend. No second opening/averaging.
Evidence initialsession33127:10passed28.52s (6handoff+4existing unifiedclosePG). Actual adapter handoff initially failed stage accounting because fixture used real runtime wall clock before its synthetic future executions; separate runtime/adapter clocks corrected. Session56753:7passed15.21s including actual LiveBroker->journal->stages->position projection->runtime ack and repeated no-resend. Finalsession59293:33passed53.52s (8handoffPG+21requestPG+4unifiedclosePG),16SDKdeprecationwarnings. Actual handoff asserts stable request,2lots/20shares, exact original entry row/request snapshot, accounting/runtime20, no remaining request; no real broker API. Other cases quantity/side/price/account mismatch, existing closing reservation, recovered opening cancellation. Receipt ROOT/close_after_open_handoff_r1.json with source hashes.
No production API/start/deploy/migration/workingDBwrite. Full process-kill/startup and cross-process concurrency not exercised. Cancelled partially executed closing remains active and requires explicit remaining-quantity/order-chain policy; current stable original close ID must never be blindly reused. Full live readiness and profitability unproven. Worker C7 corrections remain as ARCH414; root has not accepted corrected research yet.


## ARCH417 — lost closing response recovery and concurrent handoff acceptance
Root expanded actual adapter handoff acceptance: fake SDK accepts request then raises TimeoutError, leaving durable reservation and no local close accounting. Dispose DB pool, construct fresh runtime object with empty pending/memory maps, restore close/open/exit-request journals, reconcile same original request using broker stages, then resume saved exit. Exact20shares accounted/runtime acknowledged, original linked entry IDs preserved, saved exit fulfilled, total post count remains1. Broker test double is reused; this is fresh runtime/pool recovery, NOT an OS process-kill/full startup test.
Added concurrent gather of two handoff invocations on one runtime; actual adapter/journal posts exactly once and completion remains consistent. Cross-process concurrency not exercised. No production code changed this block; authoritative evidence changed acceptance scope.
Evidence .8session66539:9passed23.68s. Finalsession9454:10passed28.05s,16SDKdeprecationwarnings. Receipt ROOT/close_after_open_restart_r1.json with source hashes. Only isolated PostgreSQL/fake SDK; no working DB/API/start/deploy/migration. Worker coverage_manifest_r2.py now exists but no corrected delivery read/accepted yet; C7 review remains outstanding. Next execution work remains cancelled-partial-close remainder chain and full process lifecycle. Profitability goal still active/unproven.


## ARCH418 — corrected C7 timing independently checked; eligibility boundary still needs correction
Root read updated worker timing/source, without editing worker files. .8session53585:32passed0.36s. Independent probes confirm explicitT+4 waits, missingT+5 remains same terminal decision when same-label bar arrivesT+6, infinite confirmation close rejected. Timing SHA256 d1b14f8a4ac94b2964571d8c04d9157fa262edc9bc1c16aa628b2b45898bc4c3; receipt ROOT/C7_root_review_r2.json. evaluate_15m_boundary now returns pending state, apply_delayed uses retained decision. This accepts the narrow timing fixes, not paired engine execution/one-time entry application.
coverage_manifest_r2 source now uses configured connection, REPEATABLE READ/READ ONLY, explicit Europe/Moscow session timezone and ordered candle-value fingerprint. Corrected output file not present/read yet; no actual DB output verification this turn. Code SHA2563afea69b792b50d209106fc1c27345fe254258f8aa9033e766f996d04b3649a2.
ROOT→WORKER remaining precise issue: earliest_eligibility returns max(depth-ready times), which can be10:05 even though it claims earliest15m close. Synthetic labels:120x5m ending bucket10:00 (ready10:05),120x15m ending09:45 (lastclose10:00),20x60m ending09:00. Function reports10:05 although no next15m signal bar exists. Select the first ACTUALLY PRESENT 15m close at/after depth-ready time with required current/fresh15m/60m evidence, or honestly label this field depth_ready_at and separately compute first eligible decision time. Do not simply round to a future15m boundary that has no bar. Add golden no-next-bar and delayed-next-bar cases. Earliest historical readiness is not permanent admission: future gaps/stale trends still rechecked at each signal and execution.
Please deliver corrected immutable manifest and paired composition caller parity next. Repeated apply_delayed returns stored CONFIRM; portfolio integration must consume it once (no repeated entries on subsequent minutes), demonstrate with synthetic event trace. Frozen control and all Jan-Aug reporting preserved. No evaluation approval yet; no root DB/API/trading actions. Execution cancelled-partial remainder chain remains independent root work. Profitability goal active/unproven.


## ARCH419 — cancelled partial protective close continues with proven remainder
Root replaced single-close completion lookup with close_remainder_progress. Reads deterministic chain: initial stableID, subsequent UUID5 derived from initialID plus cumulative acknowledged closed units. Each link must match original mode/account/FIGI/opposite side/lot/local entry row/request and exact requested remaining quantity. Only terminal released SETTLED FILLED/CANCELLED with positive whole-lot observed=accounted=runtime execution advances chain. Missing next link authorizes only that proven remainder; active/unacknowledged/zero-fill cancellation stops (no automatic zero-progress loop). Completion requires cumulative full original quantity.
prepare_close_after_open now validates current residual local quantity, while entry average/time/stage proof remain based on original full opening. Typed handoff carries selected chain requestID; runtime submits it through existing common close path, preserving original position identity. Recovery derives same nextID after pool restart; no overwriting prior request/audit history.
Evidence .8session50083:35passed65.58s (4newchainPG+10handoffPG+21requestPG). Chain-state tests explicitly seed acknowledged journal states for boundary checks; they are not end-to-end execution proof. Finalsession11886:11handoffPGpassed23.13s,16SDKdeprecationwarnings, includes actual adapter20shares ->10executed/CANCELLED ->newID1lot ->10FILLED, projection/accounting and request completion, repeat sends nothing. Full/lost-response/concurrent cases also pass. Receipt ROOT/close_remainder_chain_r1.json hashes.
No real API/workingDB/start/deploy/migration. Full process lifecycle and cross-process concurrency still unverified. Zero-fill cancelled/rejected close remains paused pending an explicit bounded retry policy, not endlessly resent. Worker C7 review outstanding; profitability and live readiness not established. Goal active.


## ARCH420 — corrected coverage boundaries audited; month labels still UTC-derived
Root read actual coverage_manifest_r2.json (SHA25657c58c0be373b78c67b002b85e246d9d335af5aade52151819567a7f07e8c550). Eight monthly counts sum6402486, matching declared evaluation total. UTC boundary21:00 converts exactly to MSK midnight; underlying artifact groups correspond Jan-Aug2026. However month_msk labels are2025-12..2026-07 because asyncpg returns UTC-aware datetime and code calls month.strftime without conversion; month_start_msk likewise outputs+00. This is report labelling, not evidence candles shifted. Root audit ROOT/C7_manifest_r2_audit.json contains corrected mapping and arithmetic; original artifact untouched. Declared ordered fingerprint f7fa1ac00f8a1f8e6e3358e7c86ea6baa694db2604d4579771b9ff74ab3a8b4b covers6634714 rows inclwarmup; root has not independently reproduced DB stream.
Worker c7_timing now separates depth_ready_at and actual present first eligible15m close with current5m/60m evidence. .8session74153:38passed0.57s. coverage_manifest_r3 delegates to this helper, but outputr3 not observed yet. Only narrow timing/eligibility tests accepted; portfolio one-time consumption/paired runner still not delivered/reviewed.
ROOT→WORKER: r3 still has month.strftime('%Y-%m') and iso(month). Convert month.astimezone(MSK) for month_msk and month_start_msk; keep UTC field explicit. Golden-test UTC2025-12-31T21:00 ->MSK2026-01, including leap/month/year boundaries. Save new immutable manifest (don't overwrite r2). Do not rerun6.6M fingerprint solely for display formatting if values/snapshot unchanged: a clearly derived corrected artefact referencing parent hash is sufficient for the labels; changed eligibility r3 does need its proper source/input provenance. Deliver a compact chat status with actual remaining work and paired-composition runner path/test evidence; no need for another threshold search. Evaluation remains unapproved until integration and final manifest reviewed. No root DB/API/start actions this turn. Goal active, profitability unproven.


## 2026-11-16 — ARCH421: запуск Sandbox и фронта по LAN

 start Sandbox without real money, repair mode/account display, expose frontend on LAN. Profitability research goal remains paused; this launch does not establish profitability or full mode parity.
Root fixed routes/_account_scope.py and sandbox.py: prefer explicitly selected runtime sandbox/live mode over env default; missing selected account fails instead of falling back across modes; portfolio/operations/deposits cache keys include mode and account. tests/test_selected_portfolio_mode.py: 4 passed. Test-mode branch retained, not newly accepted end-to-end.
Restarted .8 backend to load code. Startup initially failed closed with close_journal_recovery_failed; working DB was0005. Inspected additive migration and applied alembic upgrade0006 successfully to .8 working DB. Earlier notes saying migration unapplied are superseded by this entry.
Started explicit mode=sandbox/feed=live, margin disabled, leverage1, shorts disabled, top_n4, initial_cash20000, commission_rate request0.05 percent. Latest HTTP observation running=true, starting=false, broker_mode=sandbox, candles_seen557, error=null, entries_paused=false. Through frontend proxy /sandbox/status returns SANDBOX/running=true/portfolio.cash20000/equity20000/error=null. No real-money start/order was requested. This verifies startup and data processing, not a completed sandbox trade.
Started dtfrontend scheduled task; http://192.168.1.8:5173 responds on LAN and API proxy works. Frontend visual rendering not inspected.
Prior root protection-persistence work also retained: _st_update_sl validates unique scoped open row and finite stop, persists using captured broker session/account/mode; failure sets protection_persistence_failed instead of being swallowed. 8 focused PostgreSQL tests passed (previous turn evidence). Full OS-crash acceptance remains outstanding.


## 2026-11-16 — ARCH423: помесячная оценка капитала

research/monthly_equity.py: точные границы MSK, последовательная база доходности, неизвестные оценки и условная просадка. Семь focused checks прошли на .8. Применение к старому C5 October показало UNKNOWN в обеих ветках; доходность не сертифицирована. Подробности chat.md ARCH423.


2026-11-16 ARCH465: добавлен независимый ROOT/portfolio_receipt.py для C7: полный журнал и fingerprint, оценка открытого остатка с явной неизвестностью и без фиктивного исполнения. 3 focused tests passed на .8. Подключение к runner остаётся отдельной задачей.


2026-11-16 ARCH472: Root C7 auditrunner получил явный --database, раннюю конфигурацию и actualconnection gate. Проверены отдельные подключения к рабочей и исследовательской БД без записи; исключена неоднозначность порядка импортов для этого runner.


2026-11-16 ARCH476: Backend обновлён в Sandbox. Снимки4позиций before/after подтверждают сохранениеобъёмов,ценвхода,SL/TP;configнеизменён. positionsAPI восстановлен,maintenanceпауза снята. Sourcefee/qty/regimefix загружены; actualзакрытиеещёнепроверено.


2026-11-16 ARCH479: Timestamped intrabar protection reference deployed вSandbox.21focusedchecks,actual4position recovery equal,firstSDKcycle подтверждён fresh2–21sec. Perpositiondiagnostics доступны через bot/state.

2026-11-16 **ARCH519:** Сравнение 4 и 6 слотов на RSI 5m при одинаковом бюджете 16 000 ₽ завершено на двух разработочных неделях. ATR 5m совпал с журналом четырёх входов песочницы. Чистый результат с модельным закрытием остатка за 2–6 ноября: −621,81 / −233,11 ₽; за 9–13 ноября: +2,36 / +37,40 ₽. Без закрытий в конце данных результат закрытых сделок отрицателен по всем веткам. Разница в наборе сделок дала +166,68/+78,46 ₽ в пользу 6 слотов. Полная просадка неизвестна; допуск брокера, порядок доставки и паритет исполнения не доказаны. Артефакты ROOT/rsi_slots_5m_atr_nov{02,09}_w45_r1.json. Следующая проверка: оценить путь капитала с границами сессий и пропусками, затем заморозить конфигурацию для неизученного периода.

2026-11-16 **ARCH520:** В базе .8 отсутствуют 1m/5m SIBN за 10 ноября. В исследовательском прогоне восстановлены 974/203 свечи источника, сверенные с 14 640 биржевыми сделками. Рабочая БД не менялась. За 9–13 ноября чистый P&L по обычным выходам 4/6 слотов −114,93/−144,53 ₽, с модельным закрытием остатка +2,36/+21,96 ₽. В ветке 6 слотов появились три входа по другим тикерам 10 ноября (−72,38 ₽); выходы SIBN не изменились. На 4 984 общих минутах с оценкой наблюдаемая просадка 2,06%/1,56%; полная неизвестна. Отчёт ROOT/rsi_slots_5m_atr_nov09_w45_sibn_repair_r1.json. Перед оценкой новых стратегий проверять пропуски целого торгового дня по каждому инструменту, включая дни удержания позиции.

2026-11-16 **ARCH523:** На двух разработочных неделях половинный бюджет SMLT и общий лимит планового риска 60 ₽ уменьшили сумму убыточных сделок и наблюдаемую просадку. При 4 слотах лимит улучшил чистый результат обычных выходов −650,59→−366,47 ₽ и −114,93→−89,86 ₽; просадка на общих минутах 4,08→2,87% и 2,06→1,72%. Единственное уникальное превышение 60 ₽ — шорт SMLT, убыток 85,51 ₽: архив подтверждает переход 345→352,30 ₽ в 09:00 МСК 6 ноября в пределах дня. Артефакты ROOT/rsi_slots_5m_atr_nov{02,09}_w45_risk_60_r1.json и ROOT/smlt_development_summary_r1.json. Независимая проверка трёх веток поставлена в ARCH521–522; прибыльность не подтверждена.
