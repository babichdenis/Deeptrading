# MEMORY.md — единая память проекта Deeptrading

## Аудит крупных модулей — 2026-10-15

Выполнен первый этап рефакторинга по запросу владельца: из runtime выделены runtime_models, runtime_telemetry, runtime_persistence и runtime_diagnostics; из ensemble — ensemble_diagnostics; из frontend main — analytics/reports и shared HTTP/DOM/debounce. Runtime7399→6657 строк, main6097→5245, ensemble2205→1915. Методы и импортируемые имена совместимы; AST перенесённых dataclass/телеметрии/диагностик проверен.

Запись свечей переведена на пакетные execute с сохранением порядка; добавлен commit инвалидирования candle_integrity, ограниченная диагностика ошибок и восстановления. Исправлен ключ5m-cache при переходе через час; поиск отчётов debounce200мс. Проверки:33passed, затем12passed (группы пересекаются), после последней правки7passed; TypeScript/Vite PASS, новые модули Ruff F/E9 PASS. Реальная задержка Postgres не измерялась.

Отчёт: docs/Audit/LARGE_CODE_REFACTOR_2026-10-15.md; patch, hashes и verification в docs/Audit/artifacts/20261015/. Git на SMB возвращает SIGBUS, stage/commit/reset не делались. Исследовательские задания PARALLEL-DAY-1 и frozen inputs/common не изменялись. План дальнейшего разделения order/candle/startup/API/chart приведён в отчёте.


## Актуальный статус архитектора — PARALLEL-DAY-1

- 2026-10-14: поуказаниювладельца очередьперестроена в **PARALLEL-DAY-1**,3независимых24чпакета. B1 сочетанияфункций→сделки;B2 экономическийML→сделки;B3 дневнойконтекст→сделки. Frozenv3/snapshot/commonужеесть,чужиерезультатыненужны. common backend/research/daypack_20261014/ созданархитектором,6acceptancecasesPASS; source/hashmanifestсохранён. Полныепакетыопубликованывchat/docs/PARALLEL_DAY_1.md,readbackпроверен; ACKещёнет.

- 2026-10-14 v3: B1 ACCEPTED scoped (weekday/futurebucket исправлены, sourcehash совпал); B2 ACCEPTED scoped MODEL_READY (12bundle hashes совпали, повторныйretrain не нужен). B3 cash/accounting исправлены; ограниченный REWORK: точныеcomparators изB2 (logreg individualvotes=cash, HGB5m активен), genuineknownprefix отдельно отclosedcashflows, quorum2/pairedcalendar. Machine integration_contract.json иproof в architect/review_v3, полноеавтопродолжениевchat. B1/B2 текущаясдачазакрыта.

- Переписка: .chat-write.lock — persistent file; единый exclusivefcntl.flock, append+fsync, не удалять. B2 mkdir-publisher несовместим: исправление назначено.

- 2026-10-14: v2 сдачи всех троих прочитаны. Ранее исправленные label-index/t1/purge/NO_SELECTION/bundles/next-open/chronology/unknown-final повторно не назначены;12bundle hashes совпали. Новыйproof подтверждает B1 weekday mismatch иMATURE начастичномfuturebucket; B3 cash допуск безfees/slippage. B1/B3 REWORK; B2 перечисленные инфраструктурныеисправления приняты, новыйmarket-only modelrun послеDATA_READY. Полная автоматическаяветка опубликована вchat; evidence backend/reports/architect_20261013/ml_mtf_1/architect/review_v2/. «Все проигрываютcash» не подтверждено при finalNone/5m-cash1.0.

- 2026-10-12 следующая проверка: B1/B2 новых ответов/изменений кода нет; B3 DELIVERY на отозванных inputs — **REWORK**. Proof подтверждает SKIP занимаетслоты, слоты не переиспользуются, ошибка next-open +1m, fallback на последнююминуту вместо точногоcutoff, последнийизвестныйequity выдан заfinal. Полный единыйREWORK записан вchat, proof/REVIEW в architect/review_b3. B3 исправляетadapter параллельно B1/B2; economic rerun только на новыхinputs.

- 2026-10-12 приёмка B1/B2: **REWORK обоим**. Независимый proof: target сдвинут на13баров/65мин из-за enumerate вместо lr.index; label_end указан началом, labels пересекают ночь, stale votes395мин. B2 не использует label_end_ts для purge; NO_SELECTION fallback0.5, inference зависит отMATURE будущего, нет persisted model. Единый полный REWORK опубликован вchat; proof/REVIEW: backend/reports/architect_20261012/ml_mtf_1/architect/review_b1_b2/. Старые результаты непригодны для выводов об edge. B1 исправляет→новыйDATA_READY; B2 исправляет параллельно→автоматический retrain; B3 продолжает adapter и ждёт новыеinputs. Новых разрешений не требуется.

- 2026-10-12: шара доступна, последние B1/B3 V2 сообщения прочитаны. Направление владельца: обучаться сочетаниям существующих функций/голосов на нескольких ТФ по ML-разметке.
- Найдены label_votes.py, существующий label_votes.parquet, labels/ideal_trades storage, ml_meta и CandleHub/IndicatorHub. Различие upper-first в старом label_votes и stop-first в canonical labeling требует версионированного offline адаптера.
- Полные автономные ветки опубликованы в chat.md и docs/ML_MTF_1_ASSIGNMENTS.md: B1 dataset, B2 12-fit interaction matrix и frozen predictions, B3 chronological execution/comparators и единый DELIVERY. Зоны файлов разделены; DATA_READY → MODEL_READY → интеграция без отдельной команды владельца. B1/B2 сдали первую реализацию; приёмка REWORK, см. запись выше.
- Все elite31; features1/5/10/30/60m, primary canonical5m label; train до мая2026, val июнь–июль, eval август–08октября. Eval временно отделён от обучения, но не нетронутый holdout проекта. Coverage eligibility по будущему периоду исключён.
- Read-only БД, offline research/focused tests разрешены. Бот/orders/deploy/API/backfill/ручные исправления свечей не назначены. Прибыльность не установлена.


**Правило:** весь контекст, договорённости и статус — СЮДА. Файл читается первым.
Детали — по ссылкам. Не плодим новые md без нужды. Хроника сессий — в git-истории и
`docs/results/`.

## Активная работа — ARCH-20261007

Владелец назначил Codex архитектором; два внешних builder в OpenCode получают задания и отчитываются через корневой `chat.md`. Цель — достоверный отбор роботов для полноценных краткосрочных сделок. Прибыльность не установлена; переход к live не входит в первый цикл.

- **Builder-1 / A1 и A3-C1 r3 — ACCEPTED (2026-10-08):** канон144925Z-1c756009; независимо24 passed (14.05s),3 manifest-хэша и8 секций совпали, findings/queries равны031556Z. Пустой/один/непрерывный/разреженный ряд проверены; остаточные формулировки исправлены. Origin NOT_VERIFIED — конечный допустимый вывод. Production/БД не исправлялись, прибыльность не доказана. Builder-1 DONE, ждёт отдельного назначения; повторные пересдачи не нужны.
- **Builder-2 / A2-C1 r4 — ACCEPTED (2026-10-08):** совместно 65 passed (49 WF+16 lineage, 63.74s), четыре manifest-хэша совпали. Контрольные offline-примеры unknown label, overflow производных, неполный robustness, unknown DD/invalid commission, перестановки сумм и безопасные worker errors прошли. Приёмка исследовательского отбора, не прибыльности; будущие эксперименты с одним явным exit до отдельной правки идентичности.
- **Builder-2 / A4 — ACCEPTED scoped (2026-10-08):** r4 hashes совпали; архитектор сам закрыл остаточные противоречия в §10. Канон175942Z-architect-accepted с исходной/принятой версиями. Приняты S1/S2,4конфига; факторы/RVI/голосование DEFERRED. Повторных пересдач A4 не нужно; Builder-2 переходит к A6/DeepSeek.
- **A4 дополнение владельца (2026-10-08):** Builder-2 исследует валюты/нефть/золото → секторный тренд; сравнение простого ценового тренда с добавлением внешнего фактора, разделение overnight/intraday. Документальное исследование в прежнем отчёте, shortlist≤3, без новых рыночных запросов/прогонов; контракт добавлен в chat.
- **Builder-1 / A5 r2 — ограниченный REWORK:** hashes и offline comparison184500Z_r2_run5 подтверждены; negative/duplicate исправлены. Два остатка: Future timeout не отменяет gRPC/поток; malformed T-Invest JSON и ISS body=[] вызывают необработанные исключения, ошибочныйcollect body не сохраняется. В chat конкретные решения и ручные проверки; новыхAPI не нужно.
- **Новости / A6 r2 — REWORK (2026-10-09):** все3code/9artifact hashes совпали. Независимо воспроизведена утечка будущего: исправление12:00 видно в snapshot09:00. Анализы перезаписываются, B/C не фильтруют ticker, частичные/пустые данные дают ложныйallow. В chat единый список исправлений1–5; DeepSeekNETWORK из-заproxy, успешного реального вызова нет. A5 новой сдачи нет.
- **Постановка C1 (указание владельца 2026-10-08):** в конце chat.md опубликованы единые актуальные контракты A2-C1/A3-C1, заменяющие разрозненные уточнения поведения. A2 включает политику обязательных/неизвестных/повреждённых данных, все фазы и матрицу приёмки; A3 — доказательства происхождения и явный критерий завершения при NOT_VERIFIED. Builders продолжают текущую работу, не начинают заново. Перед REVIEW — самостоятельная проверка всего контракта; архитектор возвращает сводный список, улучшения вне контракта выделяет отдельно.
- **Архитектор / A0 — IN_PROGRESS:** карта реального исполнения, проверка результатов A1/A2, затем формулировка ограниченного эксперимента. Детальные границы файлов и приёмка — в `chat.md`; последний статус брать из журнала ACK/REVIEW/ACCEPTED.
- В этом цикле `MEMORY.md` — состояние, `chat.md` — назначения/переписка. Старые My3/DeepSeek next_action_owner, задачи V2 и назначения в `BUILDER_CHANNEL.md` не управляют этой командой. Общие документы обновляет архитектор; builders меняют только выделенные им файлы.
- При первой проверке ответов корневой chat.md содержал только ACK A2 вместо заданий/журнала. Архитектор восстановил назначения из сессии, сохранил ACK, напомнил append-only + lock. Это историческая заметка первой проверки; актуальная приёмка A1/A2 указана выше.
- Подтверждена статическая разница контуров: Lab fixed моделирует отдельные сигналы на 1m-пути (`app/lab/fixed.py`); OSE sweep агрегирует исходные 1m до заданного TF и выполняет EngineRunner на этом TF (`scripts/ose_exit_matrix.py:362-380`); runtime отдельно исполняет pending, ведёт реальные позиции и обрабатывает выходы (`app/bot/runtime.py:5488`, `_step_exit`). Эти результаты нельзя объявлять взаимозаменяемыми без сверки конфигурации и сделок. Новые торговые прогоны в этом цикле ещё не запускались.

---

## ⚠️⚠️ БАЗА ДАННЫХ + IP-КАРТА — ЧИТАТЬ ПЕРВЫМ ДЕЛОМ (актуально с 2026-10-01)

**ВЕСЬ ПРОЕКТ — НА .7 (решение владельца 03.10.2026):** код, git-репо, backend,
frontend и БД живут на сервере .7. Если другой документ утверждает, что проект
(или его часть) на другой машине — документ устарел.

**После ребута роутера (01.10) DHCP сдвинул адреса — роли НЕ менялись, проверено:**
- **.6 = этот мак (opencode-сессии)** — код проекта здесь НЕ живёт; сессии правят
  код через SMB-шару с .7 (в совсем старых документах этот мак числился как .7);
- **.7 = сервер `MacBook-ProDen.local`** (бывший .3): **весь проект** — uvicorn :8000,
  vite :5173, git-репо, SMB-шара `Dev` (держится на mDNS-имени, поэтому шара
  пережила смену IP) — и **ЗДЕСЬ ЖЕ настоящая БД**: Docker Postgres 16,
  **`[REDACTED_USER]:[REDACTED_PASSWORD]@192.168.1.7:5432/deeptrading`**;
- **.8 = Windows-раннер** `nadts@192.168.1.8` (`DESKTOP-56NPC9C`, бывший .2);
- старые адреса .2/.3/.5 прежним ролям больше НЕ соответствуют (уточнить, если нужен .5).

**Настоящая БД цела:** `instruments=1922`, `instrument_info=502`, `candles≈20.09M`
(свежие до текущего дня), `sandbox_trades=600`, `bot_test_runs=52`, в ней идут тесты.
**Локальная dev-БД этого мака** (`127.0.0.1:5432`, Homebrew PG14) — песочница:
`instruments=0`, `instrument_info` нет → бот/реплей НЕ стартуют. `.env` мака оставлен
на 127.0.0.1 (решение владельца 01.10: боевых писать не надо); для боевых данных —
на `192.168.1.7:5432`. Отчёты аналитики (`report_runs`) перенесены в обе БД 01.10.

---

## 🚀 НАЧАЛО СЕССИИ — ОБЯЗАТЕЛЬНО ПРОЧИТАЙ (актуально 2026-09-09)

**Сводка последней сессии (все результаты, изменения кода, карта, токены, статус live-бота):**
👉 **`docs/results/SESSION_SUMMARY_2026-09-09.md`** — «карусель» SBER: стрим→polling-баклог→чириканье, гейты «закрытой/свежей» свечи, таймаут стрима 75с.

**Порядок входа агента:**
1. `MEMORY.md` (этот файл) → 2. `docs/results/SESSION_SUMMARY_2026-09-09.md` → 3. `AGENTS.md`
4. Нужные контракты в `docs/`, реестр скриптов `docs/roadmap/SCRIPTS_INDEX.md`

---

## Как устроены файлы (карта)

| Файл | Что это | Когда писать |
|------|---------|--------------|
| `MEMORY.md` | **главная память, этот файл** | при любом изменении статуса |
| `docs/adr/` | **канон архитектурных решений** (контекст→решение→альтернативы) | при принятом архитектурном решении |
| `docs/roadmap/PROJECT_AUDIT_2026-09-30.md` | разбор проекта: P0/P1, этапы A–D | при аудите; Этап A закрыт 01.10 |
| `ROADMAP.md` | план работ (обновляется в каждой сессии) | по ходу |
| `STATUS.md`, `PROJECT_STATE.md` | ⚠️ **АРХИВ** (handoff 28/27.08), не канон | не править |
| `docs/results/SESSION_SUMMARY_2026-09-09.md` | **сводка последней сессии** (карусель SBER, фиксы стрима/свечей) | в конце каждой сессии |
| `docs/results/*.txt` | золотые отчёты тестов (FINAL_COMPARE, report_*) | по тесту |
| `AGENTS.md` | кратко агенту: инфраструктура, ограничения | редко |
| `docs/roadmap/MTF_V4_2026.md` | роадмап МТФ + журнал результатов | по ходу серий |
| `docs/roadmap/SCRIPTS_INDEX.md` | реестр ВСЕХ скриптов (~180, что делает каждый) | при добавлении скрипта |
| `docs/roadmap/DEV_PLAN.md` | план развития: единый движок + ускорение 10-30× | при правках движка |
| `docs/roadmap/ENGINE_ARCHITECTURE.md` | полная архитектура движка + как писать тесты | при изменении движка |
| `docs/roadmap/Sreaming.md` | план перевода на стрим-окружение | при работе со стримами |
| `docs/streams_architecture.md` | архитектура стримов T-Invest | при работе со стримами |
| `instruments.md` | White/Gray/Black списки по Net/PF/WR (живой) | при расширении |
| `BUILDER_CHANNEL.md` | **канал Архитектор ↔ Билдер** (задачи/отчёты, роли) | по ходу работы с билдером |
| `chat.md` | активный канал Архитектор Codex ↔ Builder-1/Builder-2 (ARCH-20261007) | задания/ACK/вопросы/отчёты/приёмка; append-only |

---

## 📊 ОТЧЁТЫ ПО ТЕСТАМ — В КАКОМ ВИДЕ ПОЛУЧАТЬ

**Все тесты должны давать «золотой отчёт»** (полный, где каждую муху видно), НЕ одну строку net.

**Требуемый формат (пример — `docs/results/report_semi.txt`, отчёты S5):**
1. **PER-TICKER DETAILED**: Trades/Win/Loss/WR%/GrossW/GrossL/Comm/Net/PF/MFE/MAE/Bars/индикаторы
2. **PER-TICKER × REGIME** матрица: N / WR% / Net по ячейкам
3. **PER-REGIME**: WINS vs LOSSES с индикаторами (RSI/ATR%/EMA/ADX/Vol на вход/выход)
4. **EXIT REASONS** по режимам: signal_exit/target/stop_loss с N/WR/Net и SL_dist/TP_dist
5. **FLIP vs NON-FLIP** по режимам и total
6. **SL/TP BREAKDOWN**, **EXIT QUORUM VOTES** (кто закрывает)
7. **VOLUME RATIO** wins vs losses (если доступен)
8. **SESSIONS** (утро/день/вечер) разбивка — см. `FINAL_COMPARE.txt`
9. Сводная таблица конфигов в конце

**Инструменты генерации (все в `backend/scripts/`):**
- `analyze_full.py` — полный отчёт из jsonl-дампа сделок (per-ticker, per-regime, сессии, флипы)
- `final_compare.py` — единый файл сравнения нескольких конфигов (для A/B тестов)
- `session_compare.py` — разбивка утро/день/вечер по конфигам
- `s5_diagnostics.py` — золотой отчёт S5 (самый полный, шаблон для всех)
- `per_ticker_dump.py` + `portfolio_merge.py` — быстрый портфельный бэктест

**Золотые отчёты-примеры (что должно получаться):**
- `docs/results/FINAL_COMPARE.txt` — 4 конфига flip сравнение с сессиями/режимами
- `docs/results/report_full.txt`, `report_semi.txt`, `report_none.txt` — золотые отчёты

---

## 💰 ЗОЛОТЫЕ НАРАБОТКИ (что реально даёт доход)

| Конфиг/скрипт | Результат | Статус |
|---------------|-----------|--------|
| **Semi-flip** (в NEUTRAL закрыть без входа) | **+55% к baseline** (+77K vs +49K, WR 65.6%) | ✅ подтверждён |
| **Optuna per-ticker** (20 тикеров) | **+13.8% OOS** (w2+w3), +89.7% train | ✅ параметры в БД |
| **5m-вход + 1m-исполнение** (entry_tf=5min) | кратно лучше 1m (RUAL PF 11.6 vs 3.2) | ✅ проверено на неделе |
| **entry_session=main** | лучше all на 10/10 (2 месяца) | ✅ подтверждено |
| **Портфель 10K, 20%/позицию, semi-flip** | +101% (июль неделя) | ✅ портфельная модель |
| SL×4 (ATR) | оптимален (×3/×2 хуже) | ✅ |
| **Live-бот sandbox** | 20 тикеров, semi-flip, optuna | 🟢 работает :8000 |

**Где золотые параметры:** `instruments.optuna_params` (БД, JSONB) — 20 тикеров с sl_mult/rr/quorum/vol/активные стратегии.

---

## 🧠 КЛЮЧЕВЫЕ ВЫВОДЫ (проверено, не переоткрывать)

1. **Semi-flip = менеджмент позиции, не фильтр.** В NEUTRAL: закрыть и ждать, не переворачиваться.
2. **Volume — единственный разделитель win/loss** (wins vol 2.05 vs losses 0.91), но как жёсткий порог НЕ работает (Optuna выбрала vol=0 для 16/20).
3. **RSI ~50 для win и loss** — бесполезен как фильтр.
4. **Вечерняя сессия — слив** (WR 40-50%) во всех конфигах. День — золото (net/t +48...+58).
5. **Pullback_ema закрывает 35% сделок** — стержень ансамбля.
6. **Все 7 стратегий ВСЕГДА в quorum.** `inc_<sid>` в Optuna = лишь кому тюнить параметры.
7. **Валидация свечей обязательна**: 22.08.2026 был сбой T-Invest (мерцание цен AFLT/MVID/NLMK). Битый день = 21% ложной прибыли.
8. **GMKN прошёл сплит ~24x 24.08.2026** (2900→122₽) — реальное событие, не баг.
9. **T задвоен**: TCS80A107UL4 (новый, данные) vs BBG000BSJK37 (старая, пусто).
10. **Маржа в бэктесте**: и LONG и SHORT замораживают обеспечение; полный реинвест × плечо = нереалистичная лавина.
11. **«Карусель» SBER = падение стрима в polling + перемотка баклога.** Стрим на `waiting_close()` отдаёт бар только при закрытии минуты → таймаут первой свечи должен быть >60с (сейчас 75с, `feed.py`). При рестарте проверять `mode` в логах: `switching to polling` + большой `rejected` = баклог, чириканье вернётся.
12. **Гейты свечей в runtime.py**: `_is_closed` (незакрытая свеча → не в БД и не в стратегию) + гейт свежести по wall-clock (~строки 1166-1180, старый баклог пишем в БД, но не торгуем).
13. **Стрим T-Invest работоспособен** (отменяет вывод из 09-08 «не поднимается никак» — это был 15с-таймаут первой свечи). Следить за mode.

---

## 🏗️ ДВИЖОК / СТРИМЫ — НОВЫЕ РАЗРАБОТКИ

**Архитектура:** `docs/roadmap/ENGINE_ARCHITECTURE.md` (полный аудит, как писать тесты, баги).
**План ускорения:** `docs/roadmap/DEV_PLAN.md` — профиль показал 71с/месяц/тикер, из них:
- `_signal_quality` 54% (13M lambda min/abs) — кэш/векторизация
- ATR в plan_entry O(N²) на каждый вход — кэш
- 20M `total_seconds()` на datetime — int-таймстампы
- **Потенциал 10-30× БЕЗ Rust.** Rust/стриминг — после ускорения.

**Стримы:** `docs/roadmap/Sreaming.md` (план), `docs/streams_architecture.md` (архитектура).
Уже реализовано: StreamManager (positions/trades/orders), CandleFeed gRPC+fallback,
reconcile loop. НЕ реализовано: StreamingEnsemble (инкрементальный compute_ensemble).

⚠️ **Стрим живёт, но капризен к рестартам**: `waiting_close()` отдаёт бар только при
закрытии минутного интервала, поэтому таймаут первой свечи = **75с** (`feed.py`,
`first_candle_timeout`). Если при рестарте стрим не успел — падает в polling и
начинает перематывать исторический баклог → «карусель». После каждого рестарта
проверять: `curl /api/v1/bot/logs?limit=50` → в TECHINFO должно быть `mode=stream`,
иначе включена карусель (фикс 2026-09-09, см. `SESSION_SUMMARY_2026-09-09.md`).

**Единый движок (цель):** runtime.py должен гонять тот же конвейер, что compute_ensemble
(backtest_v2.py уже делает это через реальный runtime + FakeDatetime).

### 🕐 КАНОН ТФ-АГРЕГАЦИИ — START (2026-10-01, обязательно)

Метка ТФ-бара = **НАЧАЛО бакета** (эпоха/floor) — как в T-Invest (доказано: 10m 102/102,
1h 18/18), в БД (`date_bin`), UI и replay. Любая агрегация 1m→ТФ обязана совпадать с
`marketdata.Resampler`; заморозка — `backend/tests/test_aggregators_parity.py`.
Новые агрегаторы и END/ceil-семантику (метка по закрытию) не заводить.
Все 6 реализаций сведены к канону 01.10 (`6ac09ed`, `b367bcc`); детали —
`docs/roadmap/ARCHITECTURE_STANDARDIZATION_2026-09-30.md`.

**Следствие — граница видимости (решение владельца 01.10, `1364705`):** так как
метка = НАЧАЛО бакета, бар с меткой T описывает [T, T+TF) и в момент T ещё НЕ
закрыт. Видимость везде строго **по закрытию: `bar.ts + TF <= as_of`**
(включительно на самом закрытии). Фильтр `ts <= as_of` — неверен: включал
незакрытый бар, т.е. заглядывал в минуты, которых рантайм ещё не видел.
Канон в `bars.bar_is_visible` / `bar_close_ts`; заморозка —
`tests/test_universe_v2_as_of_boundary.py` (13 тестов, включая look-ahead с
баром `ts == as_of`). Распространять ту же границу на новые слои.

---

## 🔧 ОПЕРАЦИОННЫЕ ЗАМЕТКИ (актуально)

### Инфраструктура — что где есть (адреса проверены 01.10, после ребута роутера)

| Машина | IP | ОС | Роль | Что есть |
|--------|-----|-----|------|----------|
| **.6 (этот мак, opencode)** | 192.168.1.6 | macOS | opencode-сессии (проект — на .7) | правим код через SMB-шару с .7 (mDNS), dev-PG14 на `127.0.0.1` (песочница) |
| **.7 (сервер, бывший .3)** | 192.168.1.7 | macOS | **backend + frontend + git-репо + БД** | uvicorn (:8000), vite (:5173), **Docker Postgres 16 :5432** |
| **.8 (Windows-раннер, бывший .2)** | 192.168.1.8 | Windows | живой бот/таски (`uvicorn_test`, `vitebot`) | ssh `nadts`/445 ок; **:8000 после ребута не поднимался** |
| **.5 (вычисления)** | 192.168.1.5 | Windows 10 | бэктесты/Optuna | ⚠️ после ребута не подтверждён: ping отвечает, портов (22/445/8000) нет |

**SSH (проверено 01.10):**
- сервер: `sshpass -p [REDACTED] ssh Denis@192.168.1.7` → `MacBook-ProDen.local` (иногда таймаутит — повторить)
- раннер: `sshpass -p [REDACTED] ssh nadts@192.168.1.8` → `DESKTOP-56NPC9C`. SSH ставит NLS_LANG=cp866 — кириллица в выводе cmd битая, не пугаться.
- старые `Denis@192.168.1.3` / `nadts@192.168.1.2` / `nadts@192.168.1.5` — НЕ работают (это уже другие хосты/ничего).

**Фоновые процессы на .5 (Windows):**
- `nohup`/`&` НЕ работают (процесс умирает при закрытии ssh). Использовать:
  - `wmic process call create "cmd /c cd /d C:\Users\nadts\Dev\backend && python scripts\foo.py > C:\out.log 2>&1"` — вернёт PID
  - ИЛИ bat-файл: `scp run.bat` → `wmic process call create "C:\path\run.bat"` (надёжнее для длинных команд)
- Убить: `taskkill /F /PID <pid>` (wmic-обёртка может дать другой PID, искать в `tasklist | findstr python`)
- ⚠️ Не гнать параллельно тяжёлые скрипты на .5 (перегружает и такую, и свою копию данных).


**Код (где правка):**
- Проект = git-репо на сервере **.7**: `/Users/Denis/Dev/Deeptrading`. С этого мака (.6)
  виден как SMB-шара `/Volumes/Dev/Deeptrading` (монтируется по mDNS-имени `MacBook Pro Den._smb._tcp.local` — переживает смену IP).
- Правим с .6 через шару — изменения сразу на .7. На Windows — ОТДЕЛЬНАЯ копия
  `C:\Users\nadts\Dev\backend`, синхронизируется вручную (scp), когда нужна для расчётов.
- git-операции делаем на .7 (там .git), НЕ на Windows.

**База данных (как цепляться):**
- **Настоящая БД — Docker Postgres 16 НА СЕРВЕРЕ .7**: `[REDACTED_USER]:[REDACTED_PASSWORD]@192.168.1.7:5432/deeptrading`
  (ssh: `Denis@192.168.1.7` → `docker exec`). Слушает все интерфейсы → доступна со всех машин сети.
- **Локальная dev-БД этого мака (.6)** — `127.0.0.1:5432`, Homebrew PG14: **песочница**
  (`instruments=0`, `instrument_info` нет → бот/реплей тут НЕ стартуют).
  `.env` мака оставлен на 127.0.0.1 — **решение владельца 01.10: в боевую БД из этой
  сессии ничего не писать**. Для боевых данных подключаться к `.7` точечно
  (например, `--engine-url postgresql://…@192.168.1.7:5432/deeptrading` в скриптах).
- Прямой SQL: `postgresql://[REDACTED_USER]:[REDACTED_PASSWORD]@192.168.1.7:5432/deeptrading` (боевая);
  локальная — `…@127.0.0.1:5432/…`. В сессии работать через `asyncpg`/python.
- ⚠️ На `.8` (Windows) в `database.py` нужен `connect_args={'ssl': False}` (asyncpg/Windows), НЕ удалять.

**Сервисы на сервере .7 (живые сейчас):**
- Backend: `cd ~/Dev/Deeptrading/backend && nohup .venv/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/uvicorn.log 2>&1 &`
- Frontend: vite :5173. **⚠️ управляется launchd `com.denys.vite-frontend` (KeepAlive). НЕ убивать kill!**
  - Перезапуск: `launchctl kickstart -k gui/$(id -u)/com.denys.vite-frontend`
  - Остановка: `launchctl bootout gui/$(id -u)/com.denys.vite-frontend`
- Проверка бота: `curl -s http://127.0.0.1:8000/api/v1/bot/status`
- Не убивать чужие uvicorn (могут быть параллельные процессы пользователя).

**Секреты:**
- Реальные токены ТОЛЬКО в `.env` (на сервере .7 и в Windows-копии .8, НЕ в git) и `live_broker.py` (в .gitignore). Шаблон без токенов: `live_broker.py.example`.
- Sandbox-токен/аккаунт — см. `SESSION_SUMMARY_2026-09-07.md`. Боевой live-токен (`TINKOFF_TOKEN` в .env) не смешивать с sandbox.

**Прочее:**
- На этом маке (.6) проекта НЕТ: `~/Documents/Default Project` — только заметки/артефакты сессии, НЕ код (проект — на .7).
- Тесты движка: `cd ~/Dev/Deeptrading/backend && .venv/bin/python3 -m pytest tests/ -q` (~942 зелёных; 4 флака `test_research_pack.py` зависят от живых данных БД .7 — свечи/MTM, не чинить).
- БД: часовой пояс ВСЁ по Москве (UTC+3). ts в БД = UTC, метка бара = закрытие.

---

## 🧱 СЛОЙ UNIVERSE 2.0 (2026-09-30) — measure/screener/selection рядом с legacy

Параллельный чистый слой в `backend/app/bot/universe/` (legacy НЕ рефакторится,
работает как эталон; удаление — отдельным этапом). Подробности и семантика —
`docs/architecture/UNIVERSE_SCREENER_SELECTION_REBALANCE.md` §16.

- **Universe = полный допустимый рынок** (доступность), без ATR/Top-N/тренда.
- **VolatilityMeasure** (`volatility.py`): `compute_volatility_features`, ATR —
  canonical из `app.engine.indicatorhub._atr` (единый источник), ATR%/realized/range.
- **TrendMeasure** (`trend.py`): `compute_trend_features` — slope/ATR,
  direction UP/DOWN/FLAT (`FLAT_THRESHOLD=0.0`, фикс −0.0), strength.
- **MarketFeatures** (`features.py`): `compute_market_features[_many]` = vol+trend, `.valid`.
- **StrategyScreener** (`screener.py`): `TrendStrengthScreener` /
  `MeanReversionScreener`; принимают готовые MarketFeatures, не считают внутри.
- **Selection** (`selection.py`): `rank_candidates`/`select_top_n` на уже
  отфильтрованных, внешний score, детерминизм (score DESC, ticker ASC).
- **SectorMembership** (`sectors.py`): metadata/группировка, не фильтр.
- Все measure принимают `as_of` и видят только ЗАКРЫТЫЕ бары
  `bar.time + TF <= as_of` — строгая граница вместо `ts <= as_of`, см. канон
  ТФ-агрегации выше (look-ahead дисциплина), без БД/брокера/HTTP.
- Тесты: `tests/test_universe_v2_*.py` + ATR parity
  (`test_atr_canon_parity.py`). Hermetic-база 01.10: **988 passed / 0 failed**
  (было 911 на P0-коммите `7333a7e`). Legacy-контур зелёный.
- **Live-DB parity** new==legacy eligible universe отложена (нет автотеста с
  живой БД); 🔜 миграция: runtime, бэктест-скрипты, `vol_carousel`,
  `select_volatile_universe`, raw-SQL select, legacy Universe/top_n.

---

## 📕 ПЛЕЙБУК (16.09.2026) — читать перед изменениями бота

**`docs/roadmap/PLAYBOOK.md`** — единый документ: живой контур (сигналы/AI-гейт/риск),
кейс SMLT (гейт отклонил в 07:24 и одобрил в 11:47 — так и должно быть), стратегия
«следуем за IMOEX» (проверена: 240д +13%, PF 1.49, K=10-11, entry 0.19%/exit +0.05),
рабочий процесс тестирования (честное исполнение, лоты, train/valid, плато) и
список грабель (orphan_cleanup, опаздывающие AI-вердикты, EOD по свече, мусорный
пик equity, look-ahead в реплее, риск-ставка вместо GetMaxLots).

**Скрипты тестов:** `scripts/test_moex_follow.py`, `scripts/optuna_moex_follow.py`,
`scripts/test_mtf_filters.py`, `scripts/analyze_filters.py`.

---

## 🤝 ДОГОВОРЁННОСТИ КОМАНДЫ

- **Единая рабочая папка — проект на .7:** `/Users/Denis/Dev/Deeptrading` (с .6 видна как `/Volumes/Dev/Deeptrading`). Не создавать копий. Временное — в `~/Documents/Default Project`.
- Каждый правит свои файлы; перед правкой `git status`, чтобы не затереть чужое.
- Договорённости и статус — в этот файл (chat.md не единственный носитель).
- **ЕДИНЫЙ ДВИЖОК для тестов и живой торговли** — требование владельца (тест = бот).

- **Вход по bias+режим (heatmap-логика, перенос из bt_flip2 в живой бот, 23.09.2026):** торгуем только в TREND_UP (BUY при bias 30m = +1), TREND_DOWN (SELL при bias 30m = −1) и HIGH_VOLATILITY (направление от bias 10m: +1→BUY, −1→SELL). NEUTRAL/RANGE в UI включаются и видны, но входа не дают. Выход — EOD (конец дня). PF-фильтр акций (ночной пересчёт всех + отбор по PF 3-дневного окна, оптимум порог ≥3) — внедрять позже, после основной проверки.

## ✅ HERMETIC BASELINE CI (2026-10-01, владелец подтвердил) — не откатывать

Закрыт Этап A аудита `docs/roadmap/PROJECT_AUDIT_2026-09-30.md` (все P0).

- **Разделение тестов на 3 контура** (маркеры в `backend/pytest.ini`):
  - hermetic/unit — основной, обязательный CI;
  - `artifact` — требуют локальные research-файлы `backend/reports/` (не в git) → 7 файлов;
  - `integration` — требуют живых данных БД .2 → `test_research_pack.py`.
- **CI гоняет только hermetic:** `pytest -q -m "not artifact and not integration"`.
  **Инвариант: 0 failed.** Число растёт по мере добавления тестов
  (01.10: 900 → 923 passed, 54 deselected) — судить надо по «0 failed»,
  а не по точному счётy.
- **Локально полный прогон:** `~/.venvs/deeptrading/bin/python -m pytest tests -q`
  (hermetic + ожидаемые флаки по данным БД .2).
- **Heatmap — ЕДИНСТВЕННАЯ каноническая версия 30m** (режим по 30m-барам, bias 30m —
  правило владельца). Дубль H1-версии удалён 01.10: FastAPI молча отдавал устаревшую
  (первая регистрация выигрывала, вторая была недостижимой). **Правило: на (path, method) —
  ровно один обработчик.**
- **Route uniqueness guard:** `tests/test_route_uniqueness.py` — падает, если в `app`
  два одинаковых (method, path). Держит регрессию P0.5.
- **CI включён на `master`/`main`** (был только `second`, `audit-fixes-2026-09-18` — потому
  P0 и доехали до master). CI ставит `requirements-dev.txt`, а не свой список.
- `joblib`+`pandas` в `requirements.txt` (нужны `app/services/ml_ensemble_filter.py`,
  который тянет `ensemble.py` на уровне импорта).
- Мусор (`tmp_*.py`, `_tmp_*.py`, `*.jsonl.old`) вынесен из git и добавлен в `.gitignore`;
  файлы на диске не удалять — только не отслеживать.
- `python -m compileall -q app scripts` обязан быть зелёным (был красный:
  `trend_day_sim.py:358`, unterminated f-string).

## 🔎 АУДИТ ПРОЕКТА — актуализация 2026-10-07

Ревизия: `docs/roadmap/PROJECT_AUDIT_2026-09-30.md` → раздел «Актуализация аудита — 2026-10-07».

- **P1: API_TOKEN в checkout не задан;** при пустом значении middleware отключает auth для всех мутирующих `/api` запросов. `POST /bot/mode` может включить `live` и перезапустить runtime; чтение чувствительных данных не защищено даже когда токен задан. До live — fail-closed auth, защита read API, отдельное подтверждение live, ограниченный CORS.
- **P1: миграции не fail-closed.** `create_all()` не меняет существующие таблицы; Alembic выключен по умолчанию, а сбой upgrade только логируется. Проверять/применять схему как обязательный deployment gate до запуска runtime.
- CI на `master`/`main`, frontend build, объединение requirements и уникальность route статически подтверждены в текущем checkout; в аудите 07.10 CI/тесты не запускались.

## Поиск торгового преимущества — 2026-10-07

- Уточнение владельца: задача — найти роботов/функции для полноценных краткосрочных сделок; «скальпинг» был условным обозначением. Размер капитала сейчас не главный вопрос.
- Прочитаны существующие отчёты, новые торговые прогоны не запускались. В `backend/reports/signal_lab/report/a336abe5-c7ce-4f6b-affb-42366774a419_cells.csv` (профиль sl1_tp2) 19094 ячейки; PF>1 при n>=10 у 348, при n>=30 у 47, при n>=100 у 2. Это результаты отбора на тех же данных, НЕ доказательство OOS-преимущества и НЕ независимые сделки реального портфеля.
- Две ячейки n>=100: `momentum_1bar / 5min / VKCO / SELL / RANGE / morning`: n=108, PF=1.16, avg_net=0.0423%; `momentum_1bar / 5min / SFIN / BUY / NEUTRAL / day`: n=110, PF=1.10, avg_net=0.0411%. Кандидаты для проверки; запас к дополнительным издержкам мал.
- В `backend/scripts/bt_ose_sweep.py:787` WF при пустом passed ранжирует всех (`passed or list(by_label)`). В `WF_wf_mtf_rsi_20260930_0638.json` действительно выбраны убыточные IS-конфигурации при min_net_pnl=0. Для допуска к торговле нужен исход «никто не прошёл»; исследовательский fallback не считать допуском.
- Предлагаемое направление (ещё не утверждённый запуск): несколько простых семейств сигналов → сравнение будущего движения с контрольными входами и издержками → отбор только на прошлом, проверка на последующих окнах → исполнимый портфель через тот же runtime. Фиксировать отказы и не переоткрывать отвергнутые гипотезы без нового основания.

## 🏗️ CANDLEHUB V0.1 — ИСТОРИЧЕСКИЕ ЗАМЕТКИ (2026-09-28; временная конвенция superseded 2026-10-01)

- **CandleHub — собственный market data layer**, не буквальный порт OsEngine. Event-driven модель, разделение ответственности, плавная миграция через compatibility adapter.
- **Старый feed.py не удалять сразу** — сначала compatibility adapter, затем parity-тесты, затем удаление.
- **CandleHub не знает, какая стратегия торгует** — только управляет жизненным циклом свечи.
- **IndicatorHub с кэшем** — один индикатор на (figi, tf, indicator, params), считается один раз.
- **Две фазы индикатора** — preview(forming) и commit(closed).
- ⚠️ Старое утверждение `UTC-grid ceil bucketing` superseded: актуальный канон от 2026-10-01 — метка начала бакета (floor) + видимость только после закрытия, `bar.ts + TF <= as_of` (см. раздел «КАНОН ТФ-АГРЕГАЦИИ» выше).
- **Полный набор ТФ** — 1min, 5min, 10min, 15min, 30min, hour, 2h, 4h, day предсоздаются автоматически.
- **Логирование с префиксом [CandleHub]** — для фильтрации в production.

## 🏗️ OSENGINE PORT — СТАТУС (2026-09-28)

- **Решение**: полный порт OsEngine «по кусочкам» (clean-room). Тесты всегда на .8,
  после зелёной приёмки — поэтапная установка на Windows-раннер .8 (бывший .2; там живой прототип, не ломать).
- **Готово**: `app/engine/ose/` — indicators.py (sma/rsi/stochastic/bollinger/envelops/
  price_channel, семантика OsEngine), robots.py (5 роботов + TesterTab: слоты стоп/тейк
  на позиции, OCO, TryReloadStop/Profit, трейлинг на стоп-слоте, срабатывание между
  барами по касанию), strategy.py (6 стратегий в STRATEGY_CATALOG, wave 5).
  Тесты зелёные на Mac и .8 (test_ose_indicators/robots/strategy).
- **Документы** (`docs/osengine/`): PORT_NOTES_EXITS.md — механика выходов OsEngine
  (позиция владеет стоп- и тейк-слотом, Reload перезаряжает, трейлинг тянет стоп);
  PORTING_MAP.md — скан всех 201 роботов → волны порта; robots_registry.tsv;
  scan_robots.py (перегенерация реестра).
- **Волны порта**: A выходы (готово), B OnScriptIndicators (13), C Trend+CounterTrend
  (10), D Patterns+Monitors (11), E PositionsMicromanagement (8), F айсберг+фьючерсы
  (10), G остатки (6), H SPECIAL (40). Гриды/арбитраж/мм — вне плана. **Далее: волна B.**
- Для волны B нужны индикаторы: macd, cci, rvi, bulls/bears power — добавлять в
  ose/indicators.py в стиле OsEngine (сознательные отличия от наших indicators.py
  сохраняем: совместимость с тестером).


---

## СТАНДАРТ ОТЧЁТОВ ПО ТЕСТАМ (обязательно, требование владельца)

Любой тестовый прогон (Signal Lab / реплей / бэктест) обязан выдавать полный отчёт:

- **Разрезы:** движок × ТФ × сторона × **каждый тикер** × **режим (V2)** × **сессия**.
- **Метрики на ячейку:** число сделок; win / loss **в штуках**; winrate %; **PF**;
  средний выигрыш и средний проигрыш **в ₽ и в единицах (R/ATR)**; суммы в ₽.
- **Чистый PnL без разрезов — НЕ отчёт.** Разрешён только как одна из колонок.
- **Выходные и ночь не торгуются** — в общую корзину не складывать; отдельной
  таблицей можно, в агрегат — нет.
- **Тикеры не смешивать:** дорогой тикер с плохим днём не должен гасить дешёвые.
  Агрегаты — только после разбивки по тикерам.
- **Формат:** машинные CSV + краткий markdown. Ориентир таблиц — вкладка «Анализ» в UI.
- Обязательные стабильностные колонки: разбивка по месяцам (half-year+) и
  first/second half — без них результат не считается подтверждённым.

**По умолчанию и обязательно:** любой harness/replay-прогон (Signal Lab, /bot/mode test,
replay-аналитика) автоматически формирует полный отчёт по этому стандарту —
фаза `report` в lab-раннере и дефолт для реплеев. Без отчёта прогон не считается
завершённым. Срезы: движок × ТФ × сторона × тикер × режим × сессия (weekend/night —
отдельно, в агрегат не входят). Метрики: n, win/loss (шт), winrate, PF, avg win/loss
(₽ и единицы/R), суммы ₽, помесячная и half/half стабильность.


### 2026-10-09 — Архитектор: A5 r3 / A6 r3
- A5 r3: hash скрипта/7 raw/comparison подтверждены, comparison воспроизведён. Subprocess timeout принят (ручной hang 0.5s → 0.52s, child reaped). Остаток REWORK: norm_iss_rows падает на columns с вложенным list; валидировать строковые уникальные имена; обновить отчёт, который остался r2. Точный контракт и evidence в chat.md / backend/reports/architect_20261008/b1/a5/20261009_architect_r3_review/review.json. Сеть и автоматический suite архитектор не запускал.
- A6: Builder-2 явно подтверждено немедленно продолжать ранее назначенный r3 П.1–П.5, ожидание отдельного одобрения ACK не требуется. Только оговорённые ручные offline-сценарии, новых API нет. A6 не принята.


### 2026-10-09 — A5 ACCEPTED / A7 DISPATCH
A5 r4 принята: hash скрипта/выходов/7raw и воспроизведение подтверждены; malformed columns list/dict/duplicate безопасно INCOMPLETE. Evidence: backend/reports/architect_20261008/b1/a5/20261009_architect_r4_accepted/. Builder-1 назначена A7: автономный offline S1/S2 core/replay, 4 фиксированных конфигурации A4 §10, только локальные синтетические ручные примеры, без сети/БД/исторического перебора/автотестов/live. Полный контракт в chat.md. Builder-2 продолжает A6 r3. Прибыльность UNKNOWN, holdout NOT_ASSIGNED.


### 2026-10-09 — A7 r1 REWORK / A6 r3 REVIEW
A7 проверена статически и ручными offline-примерами:19 manifest code hash совпали. Блокеры: пропуски после входа игнорируются; ATR первый TR без previous close; точная граница theta даёт вход; tick округление нарушает net по fill; невалидные производные costs. В chat дан единый список1–5 для r2 с примерами и уточнениями DQ/manifest. Evidence backend/reports/architect_20261009/b1/a7/architect_r1_review/review.json. A5 ACCEPTED, A7 не принята. A6 r3 сдана вторым, ещё не проверена.


### 2026-10-09 — A6 r3 проверена: REWORK r4
3code/7artifact/scenario hashes совпали; исходная утечка будущего заголовка закрыта. Ручные offline-примеры: пустой архив+ok и ok+error дают ложныйallow; модель может приписать GAZP-новости SBER и вызвать veto с чужимversionID; received+03:00 неверно сравнивается с UTC. Единый остаток1–5 в chat: coverage, доказанная привязка, нормализация/версии, запрет in-place migration, сдача/отклонения. Evidence backend/reports/architect_20261009/b2/a6/architect_r3_review/review.json. Builder-1 A7 r2 REWORK, Builder-2 A6 r4 REWORK, A5 ACCEPTED. Сеть/live/production не менялись.


### 2026-10-09 — A7 ACCEPTED / A8 DISPATCH / A6 r4 REWORK
A7 r2:30manifest code/output hashes совпали, ручные кейсы исправлений подтверждены. Архитектор добавил finite trade/summary guard, strictJSON и CLI safe numerical rejection; канон backend/reports/architect_20261009/b1/a7/architect_r2_accepted/. A7 принята только research. Builder-1 A8: read-only ограниченная инвентаризация1min БД.7 для5тикеров/120дней без PnL/инжеста/ботов, контракт chat.md. A6 r4 hashes совпали; осталось3: C ложноallow безанализа при отсутствии keywords, legacy миграция подставляет позднийtitle к раннемуfirst_seen, description/sourcepayload не сохраняется. Builder-2 r5 REWORK, evidence architect_r4_review. A5 ACCEPTED; прибыльность UNKNOWN/holdout NOT_ASSIGNED.


### 2026-10-09 — Builder-3 / A9 DISPATCH
Владелец добавляет третьего внешнего worker. A9: причинный S1/S2 Strategy-adapter к существующему EngineRunner и построчное offline сравнение с A7 на синтетике; точные API-блокеры допустимый результат, общий engine/runtime не менять. Зона новые scripts/day_engine_adapter.py, day_engine_compare.py, docs/results/ARCH_20261009_B3_A9.md, reports/architect_20261009/b3/a9. Без сети/БД/live/автотестов. Полный контракт chat.md. Builder-1 A8 данные, Builder-2 A6 r5 новости; задачи независимы.


### 2026-10-09 — A6 r5: два блока закрыты, один остаток
Hash code/9outputs совпали. Релевантная новость безанализа UNKNOWN и legacy миграция безвыдуманнойистории подтверждены вручную. Остался lossless payload: описание обрезано4000 доhash, изменение после4100 теряется (duplicate). Builder-2 назначен r6 только rawpayload/parser→archive и корректная команда в новыйout. Evidence reports/architect_20261009/b2/a6/architect_r5_review. A8/A9 пока RUNNING, новых REVIEW нет.


### 2026-10-09 — A8 ACCEPTED_SCOPED / A10 DISPATCH
A8 code/outputs hashes,585CSVстрок и112/105/96/68/30 ключевыхнаборов подтверждены. Архитектор §11 поправил определения/выводы безпересдачи: отсутствуют последние4дняGAZP/LKOH/VKCO,3SFIN;526минут включают18:45 внеA7; причиныпропусковUNKNOWN; неотбиратьдни по будущемуDQ. Evidence b1/a8/architect_accepted. Builder-1 A10 immutable rawCSV5тикеров Jun8–Oct9 иofflineDQ, read-only ограниченныйэкспорт, безPnL/инжеста. Builder-2 A6r6, Builder-3 A9 RUNNING.


### 2026-10-09 — A6 ACCEPTED_SCOPED / A11 DISPATCH
A6r6: hashes кода/11files/сценария совпали; ручныеRSS/MOEX parser→archive4101chars A→B иas-of подтверждены. Закрытаofflineчасть, DeepSeekrealUNVERIFIED/нетlive/нетдоказаннойполезностиPnL; evidence b2/a6/architect_r6_accepted. Builder-2 A11 новыйofflineотчётчик A7 trades: ticker/config/side/month, costs/PF/R/INCOMPLETE, безновыхрыночныхпрогонов. Builder-1 A10 иBuilder-3 A9 RUNNING.
