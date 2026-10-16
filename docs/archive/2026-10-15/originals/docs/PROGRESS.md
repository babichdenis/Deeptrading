# PROGRESS — ход работ

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


> Обновлять при завершении каждого блока. Формат: [статус] блок — дата — заметки.
> Статусы: ✅ готово | 🔄 в работе | ⏳ план | ❄️ заморожено

## ARCH — проверка A6r2, 2026-10-09

- 🔄 A6r2: хэши подтверждены; воспроизведено подглядывание будущего заголовка. Версионирование/анализы/релевантность/UNKNOWN требуют исправлений; DeepSeek покаNETWORK.
- 🔄 A5r3: новой сдачи нет, действуют два прежних замечания.

## ARCH — A5r2 и стартA6, 2026-10-08

- 🔄 A5r2 воспроизведена; два остаточных блока: реальный timeout и malformed-input/raw-error сохранение. Результат SBER подтверждён.
- 🔄 Builder-2 подтвердил RUNNING A6 (новости/DeepSeek); сдачи пока нет.

## ARCH — A4 принята, A5 кейс подтверждён, 2026-10-08

- ✅ A4: S1/S2 и4конфигурации приняты с редакцией архитектора (§10); факторы отложены, Builder-2 переходит к A6.
- 🔄 A5: независимо подтверждены хэши и offline-воспроизводимость,40OHLC двухисточников совпали; спорная12:47 отличается от сохранённойБД. Четыре исправления инструмента в chat, без нового сбора данных.
- ⏳ A6: сдачи/реализации пока нет, уточнение DeepSeek действует.

## ARCH — DeepSeek для A6, 2026-10-08

- ⏳ В A6 добавлен адаптер существующего DeepSeek и один ограниченный реальный вызов. Подтверждён код провайдера в ai_trader/ai_approval_worker; доступность API пока не проверялась. Торговое применение выключено.

## ARCH — новости N1 / A6-C1, 2026-10-08

- ⏳ Builder-2 назначен автономный архив новостей и теневой журнал решений после сдачи A4r4. Время фактического получения/версии, схема событий, сравнение A/B/C. Архив/сборщик ещё не реализованы и не запущены; production-фильтр не меняется.

## ARCH — сверка источников и A4 r3, 2026-10-08

- 🔄 A4 r3: хэши подтверждены, пять конкретных исправлений в chat; исполнение/holdout и факторная гипотеза пока не приняты.
- ⏳ A5-C1: Builder-1 назначен автономный инструмент тройной сверки SBER (T-Invest/MOEX/сохранённая БД), с ограниченным read-only collect. Рабочая БД и CandleHub не изменяются.

## ARCH — A3-C1 r3 принята, 2026-10-08

- ✅ Builder-1: аудит происхождения аномальной свечи завершён. Канон144925Z; независимо24 passed (14.05s),3 хэша manifest и8 секций подтверждены. Остатки(a)/(f) закрыты; origin NOT_VERIFIED. Production/данные не менялись, новые рыночные запросы не выполнялись.

## ARCH — проверка A3 134640Z, 2026-10-08

- 🔄 Полный offline-перенос подтверждён:3 хэша manifest,8 секций и исходные findings/queries совпали. Приёмка A3 ожидает исправления вывода на пустом входе и оставшихся категоричных формулировок; два пункта в chat. БД/API не вызывались.

## ARCH — факторно-секторная гипотеза, 2026-10-08

- 🔄 По предложению владельца Builder-2 получил дополнение A4: валюты, нефть, золото и секторный тренд; проверить добавочную пользу к простому тренду и соответствие горизонта удержания. Только исследование/спецификация.

## ARCH — приёмка A4, 2026-10-08

- 🔄 Builder-2 / A4-C1: REWORK. Два SHA256 подтверждены; источники проверены выборочно по издателю/NBER. Требуются воспроизводимые правила, корректное отделение публикаций от адаптаций и независимая финальная проверка. В chat единый список из 6 пунктов. Новых тестов, торговых прогонов и обращений к рыночному API архитектор не выполнял.

## ARCH — приёмка 2026-10-08, A2 r4

- ✅ A2-C1: исследовательский WF-отбор принят. Подтверждены отказ при неполноте/повреждениях, unknown вместо ложного нуля, сохранение результатов и прежние контрольные примеры. Четыре хэша r4 совпали; объединённые целевые тесты — 65 passed (63.74s).
- 🔄 A3-C1: полный перенос доказательств и согласование отчёта не закончены; приёмки нет.
- ⏳ A4-C1: Builder-2 назначено исследование новых кандидатов и альтернативной логики с первичными источниками; реализация/торговые прогоны не начаты.

## ARCH-20261007 — приёмка 2026-10-08

- ✅ A1: аудит трёх ячеек Signal Lab принят в границах арифметики и сохранённого снимка. Независимо проверены SHA256 и офлайн-пересчёт 108/110/266 строк. Прибыльность стратегии не доказана.
- 🔄 A2: честный WF-отбор — REWORK после REVIEW; оставшиеся случаи проверки полноты/обязательных метрик зафиксированы в chat.md.
- 🔄 A3: Builder-1 назначен ограниченный аудит происхождения аномальной свечи SBER; production не менять.
- Проверка: два целевых набора, 41 passed (9.53s); это не покрывает найденные архитектором дополнительные случаи A2.

## Этап 0 — Фундамент ✅

- ✅ Скелет FastAPI + PostgreSQL (docker на .3) — 2026-08-21
- ✅ Загрузка инструментов: POST /api/instruments/sync → 1922 акции — 2026-08-21
- ✅ История свечей: POST /api/candles/{figi}/sync, интервалы 1min…month — 2026-08-21
- ✅ Frontend: Vite+TS график (свечи+SMA20+MACD), тултип OHLC, тумблеры,
  растягиваемый по высоте (localStorage), авто-синк пустых ТФ — 2026-08-22
- ✅ Запуск в проде проекта: бэк+фронт живут на .3, код правится с .7 — 2026-08-22
- ✅ GET /api/analysis/{figi} — свечи+SMA20+MACD одним запросом
- ✅ Кеш свечей: services/candle_cache.ensure_candles — докачка только недостающих
  диапазонов (голова/хвост), POST /api/candles/{figi}/sync = ensure, кэш-hit = 0 запросов
  к бирже; GET /api/candles/{figi}/coverage — покрытие по всем ТФ — 2026-08-22
- ✅ Фронт: каждый ТФ при открытии делает ensure → пустых графиков больше нет;
  история по умолчанию 3-4 мес (1min=14d … day=120d, см. ENSURE_DAYS в main.ts)

## Этап 1 — Canonical Engine v1 🔄 (текущий)

- ✅ engine/models.py: Candle/Signal/Position/Trade/audit enums — 2026-08-22
- ✅ engine/costs.py: CostModel (комиссия 0.05%, slippage 2bps, тик) — 2026-08-22
- ✅ engine/exits.py: FixedSlTpPolicy, AtrStopPolicy, STOP_LOSS_FIRST, гэп→open — 2026-08-22
- ✅ engine/policies.py: SignalPolicy(ignore_same_side/min_hold), Strategy protocol — 2026-08-22
- ✅ engine/strategies.py: MacdCrossStrategy + реестр — 2026-08-22
- ✅ engine/ledger.py: сделки+audit+fingerprint (детерминизм) — 2026-08-22
- ✅ engine/runner.py: canonical loop next_open — 2026-08-22
- ✅ Golden-тесты: 12 passed (target/stop/gap/conflict/same-side/min_hold/
  short/costs/replay/macd-smoke) — 2026-08-22

### Осталось в этапе 1
- ⏳ ATR-экзиты на реальных данных (smoke через API)

## Этап 1 дополнено — 2026-08-22 (вечер)

- ✅ engine/sessions.py: SessionPolicy moex_intraday_v1
  (MSK 10:00–18:45, entry_cutoff_bars, weekend block, overnight=False →
  принудительное закрытие на первом баре новой сессии)
- ✅ Интеграция в EngineRunner: TF<день — сессии активны; дневки+выше игнорируют;
  REJECT_SESSION_CUTOFF пишется в audit
- ✅ DonchianBreakoutStrategy(period) + реестр стратегий [macd_cross, donchian_breakout]
- ✅ Golden-тесты сессий и Donchian: всего 19 passed

## Этап 2 — Metrics & Warehouse ⏳

- ⏳ metrics/: PF, expectancy, winrate, maxDD, equity, per-FIGI, per-day, H1/H2
- ⏳ Alembic миграции (schema эволюция)
- ⏳ Dataset API: датасет = снимок фильтров над candle-таблицей + hash

## Этап 3 — Lab API ⏳

- ⏳ POST /api/v1/experiments (+jobs table, asyncio worker без Redis)
- ⏳ Артефакты: summary.json/md, trades.csv/json, equity.csv
- ⏳ Вердикты: technical/physical/execution/trading + research status

## Этап 4 — Frontend для исследований ⏳

- ⏳ Список экспериментов + форма запуска
- ⏳ Оверлей сделок на график (entry/exit маркеры из ledger)
- ⏳ Equity/drawdown панель

## Этап 5+ ⏳

- ⏳ Matrix runs, walk-forward
- ⏳ Oracle/MFE/MAE режим
- ⏳ Paper trading через T-Invest → live adapter

### Осталось в этапе
- ⏳ Этап 2: signal_decisions (accepted/rejected) + слои графика
- ⏳ Кворум K-of-N поверх сохранённых runs (window_bars=0, этап 3)

## Этап «Фронт панели» дополнено — 2026-08-23

- ✅ Ленивая докачка на прокрутке: выход за покрытый диапазон → ensure только
  видимого окна (POST sync + from_ts/to_ts) → перерисовка без сброса вида.
  Никаких пустых зон: данные сами доезжают и остаются в базе (без артефактов)
- ✅ Нижняя панель: вкладки [Стратегии | Отчёты]
  - карточки каталога (фемили-бейджи, волна, правила long/short, инлайн-параметры
    из params_schema с дефолтами)
  - «▶ Рассчитать выбранные» → POST compute по каждому → маркеры ▲/▽ с тегами
    (RSI/BB/PB/VWAP/SQZ/MACD/DON), тумблер маркеров
  - Отчёты: таблица runs (стратегия, ТФ, баров, параметры, когда рассчитан)
- ✅ tsc clean + vite build ok; фронт на .3 обновится сам (HMR)

## Ночная смена — 2026-08-24

### Починено на графике
- ✅ Ленивая докачка РАБОТАЕТ: причина бага — getVisibleRange() обрезается по данным.
  Теперь использую логические индексы из колбэка → перевод в время по STEP_SEC →
  ensure диапазона → перерисовка с сохранением позиции (addedLeft-коррекция)
- ✅ Выключение MACD/RSI → панель осциллятора исчезает, цена занимает всё окно
  (пересоздание графика при смене структуры панелей, stretch 3:2 когда есть)
- ✅ Меню «ƒ Индикаторы ▾» как в TradingView: SMA20, EMA50, Bollinger(20,2σ),
  RSI14, MACD — состояние в localStorage; бэкенд /api/analysis отдаёт все массивы
- ⏳ Переключатель Свечи/Линия/Область — отложено (украшательство)

### Этап 2 (backend)
- ✅ Таблица signal_decisions + сервис decisions.py: прогон policy ignore_same_side
  (+min_hold_bars) поверх сохранённых signals, state FLAT/LONG/SHORT
- ✅ POST /api/v1/signals/{run_id}/decisions
- ✅ engine/metrics.py: summarize/PF/expectancy/equity/max_drawdown/H1-H2/by_figi/
  full_report + 6 golden-тестов (итого 36 passed)

### Этап 3 (backend)
- ✅ Таблица run_dependencies (воспроизводимость кворума)
- ✅ services/quorum.py: same-bar голосование window_bars=0, features содержат
  votes/members_for/opposition; stale-replacement по combined data_version
- ✅ POST /api/v1/quorum/compute
- ✅ Проверено на .3: rsi+bb+pullback, k=2 → 18 сигналов кворума с составом голосов

### Фронт
- ✅ Кнопка «⚖ Кворум N-из-M»: считает поверх рассчитанных стратегий,
  маркеры Q2/3 фиолетовым; счётчик M активных стратегий
- ✅ tsc clean, vite build ok

### Осталось
- ⏳ Слои accepted/rejected на графике (данные decisions уже в API)
- ⏳ Alembic миграции
- ⏳ Lab API + experiments (после визуального QC пользователем)

## Идеи на радаре 📌

- 📌 **Plugin-архитектура** (docs/plugin_strategy.md): стратегии/policies/exits/risk —
  отдельные модули + registry; движок защищён и версионируется. УЖЕ совпадает с нашим
  путём (registry, каталог, signals-in-db). Прийти к структуре backend/app/research/*
  при старте Lab (этап 5), не раньше. Точки влияния: контракты RawSignal/PolicyDecision/
  OrderIntent уже близки к engine/models.py — при этапе 2 (signal_decisions)
  выровнять именования; POLICY_REGISTRY/EXIT_REGISTRY оформить при Lab.
- 📌 **ML как слой оценки сигналов** (docs/ML_idea.md): logistic regression /
  gradient boosting поверх сохранённых signals → probability_good → policy threshold.
  НЕ прогноз цены, НЕ пересчёт стратегии. Таблицы ml_models/ml_predictions и
  signal_predictions добавляем ПОСЛЕ signal_decisions (этап 2) — predictions
  ссылаются на signal_id. Walk-forward + point-in-time features обязательны.
  Первая гипотеза: rsi_reversal, target +1R/−1R за 20 баров, LR, порог 0.60.
- 📌 **18 исследовательских режимов** (docs/Addiional_idea.md). Приоритет пакета A:
  слои сигналов (raw/quorum/accepted/rejected) — частично готово; explainable
  tooltips (features уже в signals); disagreement map = кворум-слой ✅;
  regime background (TREND_UP/DOWN/RANGE/CHAOS по EMA-наклону+ATR-percentile);
  signal lifetime; counterfactual для rejected; data quality panel.
  Пакет B = Lab (этап 5), пакет C = после Lab (oracle, sensitivity, ML-фильтр,
  portfolio, MTF). Replay-режим назван самой сильной идеей — к нему вернуться
  сразу после Lab.

---

## Этап «Каталог сигналов» 🔄 (текущий, дизайн: docs/SIGNALS_CATALOG.md)

- ✅ Дизайн-документ SIGNALS_CATALOG.md: сигналы=данные, 15 стратегий волнами,
  слои поверх (policies/exits/sizing), кворум как мета-стратегия — 2026-08-22
- ✅ ORM: strategy_runs + signals (кеш сигналов по params_hash) — 2026-08-22
- ✅ engine/catalog.py: карточки 7 стратегий + params_schema + canonical_params_hash
- ✅ GET /api/v1/strategies/catalog — работает на .3

### Осталось в этапе
- ⏳ Фронт: панель каталога с чекбоксами/пресетами, маркеры на графике, отчёты
- ⏳ Кворум K-of-N поверх сохранённых runs (window_bars=0, этап 3)
- ⏳ signal_decisions слой (accepted/rejected, этап 2)

## Этап «Каталог» дополнено — 2026-08-23

- ✅ Контракт v1.1 (SIGNALS_CATALOG.md): кворум same-bar window=0; signals≠decisions;
  cache key + data_version; signal_ts/execution_ts разделение
- ✅ Волна 1 реализована: rsi_reversal, bollinger_reclaim, pullback_ema,
  vwap_reclaim, range_compression_breakout (engine/wave1.py, инкрементальные)
- ✅ validate_params/build_strategy: схема каталога = границы и дефолты параметров
- ✅ services/signals.py: compute_signals с exact-cache (params_hash+data_version),
  замена stale-run; generate_signals общий для всех стратегий реестра
- ✅ API: POST /api/v1/signals/compute, GET /api/v1/signals/{run_id}, GET /runs
- ✅ Проверено на .3 (SBER hour 120d): все 7 стратегий считают и кэшируются;
  повторный вызов = cached:true без расчёта
- ✅ Golden-тесты: 30 passed (11 новых на правила волны 1)


## Большая ночная сборка — 2026-08/09 (Lab + Bot + ML + новый фронт)

### Lab — ГОТОВО ✅
- ✅ experiments/experiment_trades таблицы (immutable, config_hash dedup)
- ✅ ReplayStrategy: Lab играет СОХРАНЁННЫЕ сигналы из базы (signals=данные)
- ✅ atr_trailing exit policy (activation+trail) в canonical engine
- ✅ POST /api/v1/experiments — полный прогон с метриками и вердиктами
  (POSITIVE/WEAK/NEGATIVE/INSUFFICIENT_DATA → DESIGN_CANDIDATE/REJECT/INCONCLUSIVE;
  проверка H1/H2 стабильности; STOP_LOSS_FIRST; next_open)
- ✅ POST /lab/sweep — автоподбор параметров (сетка ≤24 прогона, ранжирование по net,
  детектор плато); SBER hour: 6 комбинаций за 2.7с — все REJECT, лучший sl0.8%/tp3%
- ✅ POST /lab/batch — батч по акциям: 10 ликвидных за 138с, per-stock net/PF/trades.
  Честный результат rsi_reversal: положительных 2/10
- ✅ GET /policies/catalog (exit policies + signal + session)

### Paper-бот — ГОТОВО ✅
- ✅ bot/paper_broker.py: виртуальный портфель в БД (accounts/positions/trades),
  комиссии+slippage те же что в Lab (canonical_v1)
- ✅ bot/universe.py: отбор топ-N волатильных ликвидных акций по ATR%
  (проверено: ALRS 6.2%, PLZL 6.0%, MGNT 5.3%, CHMF 4.8%)
- ✅ bot/feed.py: стрим Т-Банка (waiting_close=True → только закрытые свечи =
  canonical timing!) с автофоллбэком на polling
- ✅ bot/runtime.py: оркестратор — сигнал на закрытии бара t → исполнение на open
  следующего полученного бара; стоп/тейк интрабар; REST start/stop/status/reset
- ⚠ В выходные рынок закрыт: бот корректно ждёт, стрим закрывается сервером → polling

### ML-фильтр сигналов v1 — ГОТОВО ✅
- ✅ services/ml.py на чистом Python (без sklearn — ноль friction деплоя):
  point-in-time фичи (RSI/ATR%/EMA50-slope/vol-ratio/dist-SMA20/час/side),
  label = +1R раньше −1R за H баров (R=ATR14), logistic regression batch-GD,
  chronological holdout 25%
- ✅ Таблицы ml_models (веса JSON — версионируемо) / ml_predictions
- ✅ API: POST /ml/train, GET /ml/models, POST /ml/predict, GET predictions
- ✅ Проверено: SBER hour rsi_reversal 240d → 40 сигналов (24 good/16 bad),
  accuracy_holdout=0.60 при base_rate=0.60 — честно записано: ML пока НЕ даёт
  преимущества. Нужны больше данных/фичей — это следующий этап
- ✅ FK миграция: experiments/ml_models.strategy_run_id ON DELETE SET NULL

### Фронт — НОВАЯ ОБОЛОЧКА ✅
- ✅ Сайдбар: График | Lab | Бот | Портфель; закреплённый header с equity-чипом
  и статусом бота (ON/OFF пульсирующая точка, режим stream/polling);
  кнопка Старт/Стоп бота в сайдбаре; футер engine/costs
- ✅ Lab-страница: конфиг (стратегия+параметры из схемы, ТФ, история, qty,
  exit policy+параметры), кнопки Эксперимент/Автоподбор/Батч, карточка результата
  с вердиктами, таблица истории экспериментов
- ✅ Бот-страница: форма запуска, статус-карточки (режим/свечи/сигналы/equity/pnl),
  чипы вселенной (тикер·ATR%), таблицы позиций и сделок, автопросмотр 8с
- ✅ Портфель: equity/кеш/P&L/winrate + все paper-сделки
- ✅ tsc clean + vite build ok (216KB gzip 69KB)

### Осталось (следующие сессии)
- ⏳ ML: интеграция порога в experiment (ml_filter param), больше фичей,
  walk-forward обучение, portfolio-уровень
- ⏳ Bot: ML-фильтр в live-контуре, trailing позиции, короткие продажи
- ⏳ Alembic; артефакты экспериментов (summary.md/csv); replay-режим из Additional_idea

## Решения и договорённости (append-only)

- 2026-08-22: Parquet отложен; свечи в Postgres до реальных тормозов
- 2026-08-22: Jobs = таблица в PG + asyncio BackgroundTasks (Redis не нужен на 2 ноутбуках)
- 2026-08-22: Движок — чистый Python без БД/API, вход через runner.run(candles)
- 2026-08-22: План main_plan.md живой, изменения фиксируем здесь
- 2026-08-22: ПРАВИЛО ДАННЫХ: БД — единственный источник свечей для всех блоков
  (график, эксперименты, стратегии). Недостающее докачивает candle_cache.ensure_candles,
  потребитель всегда читает из базы. Не качаем >3-4 мес за раз (лимиты в ENSURE_DAYS)
  Лимит 3-4 мес — стартовый, со временем база наполняется сама, лимиты растут по need
- 2026-08-22: СИГНАЛЫ = ДАННЫЕ: расчёт кэшируется по (figi, interval, strategy,
  params_hash); смена SL/TP/трейлинга/кворума НЕ пересчитывает стратегии.
  Кворум K-of-N — слой чтения поверх сохранённых signals, результат тоже в signals
- 2026-08-22: ИИ/MCP-направление принято (docs/MCP_idea.md): движок API-first и
  versioned (ENGINE_ID=trade_engine_v1, в каждом run), params_schema с границами =
  будущая валидация конфигов от ИИ. MCP — тонкий адаптер над FastAPI, этап Lab+.
  Approval gates paper/live — человек, не ИИ

## Финальная сборка «Конфетка» — 2026-08-24

### Warehouse → Lab (по вашему ТЗ из скринов) ✅
- ✅ Конфигурации = первоклассная сущность (configurations, статусы
  DRAFT/PREVIEW_COMPLETED/READY_FOR_LAB/IN_LAB/LAB_COMPLETED)
- ✅ POST /api/v1/warehouse/configurations — собрать пакет: стратегии-голоса +
  кворум K-of-N + выходы + min_hold + short + сессия
- ✅ PREVIEW: быстрый расчёт на текущем графике с ВОРОНКОЙ сигналов
  (raw по каждой стратегии → кворум → сделки) и пометкой «не полноценный backtest».
  Проверено: RSI+BB 2of2 → 179 raw → 12 кворум → 9 сделок
- ✅ Send-to-Lab: полный прогон конфигурации на пуле акций в DUAL режиме
  (long и short раздельно) с разбивкой как на скринах:
  Тикер | Режим | Дней | W/L (L-x / S-y) | Сделки | PnL (+/−)
  Проверено: 8 акций × 2 режима = 49 сделок, REJECT (честно)
- ✅ Все сделки сохраняются в lab_result.trades → кнопка «📈 На график»
  у каждой строки открывает сделку на Графике (маркеры вход/выход + линия цены)
- ✅ Кнопка «В Lab ➜» во вкладке Отчёты на Графике: собирает конфигурацию
  из рассчитанных стратегий одним кликом

### Фронт Lab — трёхколоночный как на скринах ✅
- Слева: список конфигураций (статус-бейджи, чипы членов, K=бейдж,
  preview net) + редактор «＋» (стратегии с параметрами, кворум селект,
  выходы+параметры, min hold, short)
- Центр: очередь/идут (спиннер + прогресс-бар при прогоне)
- Справа: протестированы (totals net, клик → полные результаты:
  totals-line, таблица по акциям/сторонам, все сделки с переходом на график)

### Исправлено при сборке
- FK experiments/ml_models.strategy_run_id → ON DELETE SET NULL
  (замена устаревшего run больше не блокируется историей)
- ReplayStrategy принимает ISO-строки времени
- EngineConfig.mode long/short — dual-режим тестирования

### Итого комплекс готов к работе
Warehouse(конструктор) → Lab(валидатор пула) → Бот(paper) → Портфель.
36 тестов. Всё проверено на живом .3.

### Следующие шаги (после вашего визуального QC)
- ⏳ SL/TP линии на графике у сделки (нужны в trade ledger — добавить поля)
- ⏳ IMOEX overlay; regime gate; session shading на графике
- ⏳ ML: интеграция порога в experiment; walk-forward; больше фичей
- ⏳ Alembic; артефакты summary.md/csv; replay-режим

## Дополнение — 2026-08-24: расчёт сигналов по видимому диапазону ✅
- Кнопка «Рассчитать выбранные» теперь считает ТОЛЬКО видимый участок графика
  (+5% запас), а не весь период. Диапазон входит в кеш-ключ — каждый диапазон = свой run.
- При прокрутке за рассчитанную область свечи докачиваются и сигналы АВТОМАТИЧЕСКИ
  пересчитываются для нового окна (тихо, маркеры обновляются).
- Спецификации Preview_warehouse.md / Preview_bot.md / Prewiev_lab.md изучены,
  зафиксированы в роадмапе ниже.

### Из спецификаций принято в ближайший план
- Warehouse MVP: типизированный pipeline BIAS→SETUP→ENTRY (карточки ролей),
  "Why no entry?" коды причин, funnel-панель, Compare configs — этап 4 фронта
- Bot MVP (Preview_bot.md): idempotency ключи сигналов, pre-trade risk gate,
  order lifecycle, reconciliation после рестарта, circuit breakers, session lifecycle,
  kill-switch кнопки, universe snapshot для воспроизводимости — этап бота 2+
- Prewiev_lab.md пустой — попросить заполнить или уточнить требования

## Спецификации Lab/Bot приняты — 2026-08-24

- ✅ Prewiev_lab.md изучен: equity-кривая (SVG), P&L по дням, серии убытков,
  net-without-top1 / концентрация топ-1 — добавлено в метрики и UI Lab
- ✅ ПРАВИЛО РЕЖИМОВ: до полного завершения бота режим LIVE не делаем вообще.
  Все работы только PAPER. Ступени из Preview_bot.md (QC→DESIGN→VAL→PAPER)
  зафиксированы; BrokerExecutionAdapter/Live — за красной чертой
- ⏳ Из спеки Lab осталось: вкладка Decisions в UI (бэкенд есть), QC-чеклист,
  drawdown episodes, артефакты summary.md/csv, сравнение конфигураций
- ⏳ Из Preview_warehouse.md MVP: pipeline BIAS→SETUP→ENTRY карточками ролей,
  коды причин "Why no entry", Compare configs

## Завершение доделок — 2026-08-24 (вечер) ✅
- ✅ SL/TP в trade ledger: Trade.initial_stop/take_profit, колонки в experiment_trades,
  в lab_result.trades и в ответе API — фронт рисует линии SL/TP у сделки на графике
- ✅ ML-порог в экспериментах: req.ml_filter {model_id, threshold} → слабые сигналы
  отбрасываются до прогона, счётчик в summary.ml_filtered. Проверено: rsi_reversal hour
  240d без ML net −33.6 → с порогом 0.55 net −7.9 (47→18 сигналов). Первое реальное
  улучшение от ML!
- ✅ Расчёт сигналов по видимому диапазону графика (+автопересчёт при прокрутке)
- ⚠ Важно про ML: модели привязаны к run; при обновлении данных run заменяется,
  предсказания нужно пересчитать (POST /ml/predict с новым run_id)

### Roadmap (актуальный порядок)
1. Пользовательский QC всех страниц
2. Decisions-вкладка в UI; QC-чеклист; drawdown episodes
3. Warehouse MVP pipeline BIAS→SETUP→ENTRY; Compare configs
4. Bot paper: ML-фильтр в live-контуре, trailing позиции, universe snapshot
5. Alembic; артефакты summary.md/csv; walk-forward для ML

## Дополнение — 2026-08-24: Lab-выводы по скринам ✅
- Одна строка на акцию (long+short объединены), W/L в формате «6 (L-4 / S-2)»,
  кнопки убраны — клик по строке открывает сводку по акции
- Сводка: сделок всего, long/short отдельно с PnL, таблица сделок
  (время+цена входа одной ячейкой, время+цена выхода, Gross со скобкой комиссии, PnL)
- P&L по дням → квадратики-календарь (зелёный/красный)
- Голоса кворума прикреплены к каждой сделке (votes/members_for/opposition)
- Lab-график: отдельный lightweight-charts на странице Lab — все сделки выбранной
  акции на периоде теста, фоновые зоны вход→выход (зелёные/красные),
  маркеры L/S с числом голосов и причиной выхода, хинт при наведении:
  сторона, голоса кворума, состав «за», вход/выход, PnL
- Исправлен JSON Infinity в PF (PostgreSQL не принимает) + санитайзер _json_safe

## Финал итерации — 2026-08-24 ✅
- Критические баги данных исправлены: режим long/short не перетирался сессией;
  период days включён в кеш-ключ (члены конфигурации считаются на одном периоде);
  0 нарушений режима / 0 дублей — проверено
- Диапазон дат в Lab-тестах работает (проверено: сделки только июль–август)
- Async send-to-lab: конфигурация → IN_LAB → инкрементальный прогресс
  (done/total/current) → LAB_COMPLETED; фронт поллит раз в 3с и показывает live
- Кнопка «Тестировать» вместо «В Lab»; Preview убрана; диалог выбора акций и дат
- Авто-имя конфигурации: RSI+BB 2of2 · hour (редактируемо, сброс при вводе)
- Lab-график: TF-контекст от конфигурации, зоны сделок зелёный/красный,
  маркеры входа с голосами кворума, хинт при наведении; ресайз по вертикали
- Стрелки ▲/▼ вместо long/short в таблицах; одна строка на акцию;
  клик по тикеру → фильтр его сделок; ячейки «время · цена» объединены,
  Gross со скобкой комиссии

## Фиксы UI Lab — 2026-08-24 (вечер 2) ✅
- Ошибка 500 при сохранении: проценты SL/TP (1 = 1%) не проходили валидацию долей.
  Теперь фронт конвертирует %→доли, бэкенд отдаёт понятную ошибку границ
- Редактор → модальное окно по центру с затемнением
- Карточки конфигураций: ✏️ Редактировать · 🗑 Удалить · 📊 Результаты;
  редактирование LAB_COMPLETED возвращает в DRAFT → можно Тестировать заново
- DELETE /api/v1/warehouse/configurations/{id}

## Унификация движка — 2026-08-24 ✅
Проблема: три разных механизма поиска входов (склад/Lab/бот).
Исправлено — единый движок app/engine/:
- Единый кворум app/engine/quorum.py (merge_quorum, same-bar). Используется:
  склад (POST /quorum/compute) и Lab (warehouse.send_to_lab) — локальная копия удалена
- Бот переведён на блоки движка: intrabar_exit (стопы/тейк/гэп), SignalPolicy.decide
  (ignore_same_side/min_hold/accept exit), FixedSlTpPolicy.plan_entry (расчёт SL/TP) —
  самопальные проверки удалены
- Бот греет буфер свечей ТОЛЬКО из БД (ensure_candles + загрузка истории 7 дней),
  стрим лишь докачивает закрытые бары
- Правило зафиксировано: свечи всегда из PostgreSQL, недостающее докачивается,
  сигналы/позиции/стопы считает один и тот же код

## Фикс «тест пролетает за секунду» — 2026-08-24 ✅
Причина: TypeError can't compare offset-naive and offset-aware datetimes —
фронт слал даты input[type=date] как naive, бэкенд сравнивал с tz-aware свечами.
Все 16 прогонов (8 акций × 2 режима) падали мгновенно.
- Нормализация дат в send_to_lab (naive → UTC)
- Воронка сигналов накапливается в lab_result.funnel (BUY/SELL по каждой стратегии
  + число кворум-сигналов) + elapsed_sec — видно ПОЧЕМУ мало сделок
- UI: блок «Воронка сигналов» + время расчёта в результатах Lab

## Lab: конвейер тестов с очередью — 2026-08-24 ✅
- Новая модель TestRun (очередь) + LabSetting (настройки)
- Диспетчер очереди (app/services/test_queue.py): стартует в lifespan,
  атомарный захват FOR UPDATE SKIP LOCKED (без гонок), лимит max_concurrent_tests
  (по умолчанию 1), прогресс синхронизируется config.lab_result → TestRun.progress,
  при рестарте зависшие RUNNING возвращаются в QUEUED
- API: POST/GET /lab/queue, pause/resume/stop, DELETE /lab/runs/{id},
  /lab/runs/{id}/recycle (→ левая колонка как DRAFT), GET/PUT /lab/settings
- send-to-lab-async удалён; send_to_lab больше не трогает статус конфигурации
- Фронт: три колонки из очереди (Ожидают/Очередь идут/Протестированы),
  единая плашка с составом, датами, прогрессом; кнопки Тестировать/⏸/▶/■/
  🗑/⬅️/📊; модалка «⚙️ Настройки Lab» в шапке; колонки равной ширины,
  высота ~5 плашек со скроллом
- Проверено на .3: 3 теста в очередь, лимит 1 → идут последовательно, все DONE
- Баг «Статус: FAILED» исправлен: показывается error теста; если есть результат — рендер

## Исправления визуализации Lab — 2026-08-24 (вечер) ✅
- Левая колонка: только конфигурации без единого теста (дубль «и в левой и в правой» убран)
- Состав конфигурации на всех плашках: стратегии+параметры, K-кворум, exit_policy, таймфрейм
- Выбор таймфрейма в редакторе конфигурации (селект 1min/5min/15min/hour/4h/day)
- Кнопка «Тестировать» без всплывающего окна — сразу ставит в очередь с дефолтами
- Модалка настроек Lab (⚙ рядом с ＋)
- Файл MCP_API.md — справочник для нейросети (MCP-совместимый)

## Восстановление после отвала шары — 2026-08-24 ✅
- Шара отмонтировалась после перезагрузки .3; доступ восстановлен (Finder mount)
- lab.ts переписан целиком (повреждён агрессивной чисткой): 3 колонки очереди,
  плашки без вложенных таблиц, прогресс только у RUNNING/PAUSED, редактор с
  выбором таймфрейма (cfg-interval), прямой enqueue без диалога, сохранение с
  «Тестировать сразу», модалка настроек Lab, createFromActiveRuns возвращён
- api.ts: config_snapshot в тип lab_result
- Бэкенд: config_snapshot (слепок конфигурации) в lab_result
- Проверено: 4 теста в очереди обработаны диспетчером (все DONE)

## Правки модалки и плашек — 2026-08-24 ✅
- Вернул в index.html блок «Стратегии-голоса» (#cfg-strategies) и «Выходы» select —
  модалка редактора снова полноценная (карточки как на складе: чекбокс, семейство,
  волна, правила ▲/▼, параметры с значениями)
- GET /lab/queue отдаёт полный members с params + min_hold_bars + allow_short —
  состав на плашках теперь полный: функции(параметры)|ТФ|К-кворум|выходы|min_hold|short
- Правая колонка больше НЕ показывает идущие тесты (RUNNING/PAUSED убраны) —
  они только в центре, дублирование устранено
- configDump() — единый рендер состава на всех плашках

## Live-прогресс Lab + формат W/У — 2026-08-24 ✅
- Формат W(L/S)/У(L/S): «10 (L-5 / S-5) / 13 (L-8 / S-5)» = прибыльные / убыточные
  по сторонам (бэкенд per_stock + renderLab + live-таблица)
- Починено зависание прогресса: тест из левой колонки получал tickers:[] →
  бэкенд НЕ резолвил их в run (общий бар 0/0). Теперь preresolve при постановке,
  tickers + stock_progress попадают в run/lab_result
- renderLiveRun: пер-акция прогресс из stock_progress (длин/шорт = 50/100),
  live-таблица перерисовывается автоматически при поллинге очереди (liveViewRunId)
- По завершении теста live-панель сменяется финальной таблицей (renderLab)

## Исправление движка Lab — единый поток (как в реальной торговле) ✅
- Убраны «два мира» long-run / short-run. Теперь один проход движка на акцию:
  mode="both", allow_short из конфигурации. Одна позиция в моменте:
  BUY-сигнал → LONG, SELL-сигнал → SHORT, противоположный сигнал закрывает.
- Проверено: MACD с allow_short=True, 180 дней SBER → 154 сделки,
  84 SHORT + 70 LONG вперемешку в одном потоке (SLSSSSSLLLLLLSLLLLLL)
- per_stock: одна запись на акцию (без mode), progress total = len(figis),
  stock_progress 0→100% на акцию (одна сторона прохода)
- F'rontend: live-таблица без колонки «Режим», формат W(L/S)/У(L/S)

## Единая система процентов — 2026-08-24 ✅
- ВЕЗДЕ проценты: fixed_sl_tp stop_pct/target_pct принимаются в % (min 0.1,
  max 50/100), бэкенд делит на 100 ТОЛЬКО при построении политики (build_exit_policy)
- Фронт: убраны все конвертации (PCT_KEYS удалён), поля подписаны (%),
  ошибка «stop_pct=0.0001 out of bounds» устранена навсегда
- Функции в модалке: параметры раскрываются ТОЛЬКО по клику на галочку
  (при загрузке на редактирование — свёрнуты)

## ▼ 2026-08-24, финальная отладка очереди ✅
- Корень зависаний НАЙДЕН и исправлен:
  1) O(n²) в generate_signals: стратегии вызывались на срезе candles[:i+1] —
     для 1min/40д = 130к баров → вечные копии. Теперь окно 400 баров
     (инкрементальные стратегии видят каждый бар по разу, оконные — хвост)
  2) compute_signals не ограничивал расчёт по days без явных дат —
     загружал ВСЮ историю (130к свечей 1min). Добавлен cutoff = now-days
  3) send_to_lab: члены считались с max(period, 60) дней → теперь period+10
- Ведение логов: диспетчер (TICK/RUN), send_to_lab (S2L-этапы) — live-диагностика
- Удалён мусорный корневой .venv (0 байт) на .3 — остался backend/.venv
- Контроль: часовая конфигурация прошла START→DONE (e4aa68a9)
- Ремонт сервера: kill → nohup uvicorn → /tmp/deeptrading.log (без reload)

## Финальные фиксы очереди (движок считается корректно) ✅
- БАГ: member_runs.append был ВНЕ цикла for m (отступы сломаны патчами) →
  кворум считался по пустому списку → тест проходил «впустую».
  Восстановлен отступ append в цикл
- БАГ: timezone.now() не существует → каждая акция падала, ошибки прятались
  в lab_result.errors. Исправлено: datetime.now(timezone.utc)
- БАГ: синхронизация прогресса в TestRun пропускалась при лимите 1
  (_sync_progress вызывался после раннего return) → перенесён в начало _tick
- send_to_lab: загрузка свечей обрезана периодом теста (не вся история 130к)
- ПОДТВЕРЖДЕНО: тест 1min SBER → per_stock=1, сделок=266 — данные реальные

## Фаза 1-2+4 (Order_state.md): контракты + WebSocket ✅
- Роадмэп: docs/ROADMAP_order_state.md (6 фаз)
- app/engine/orderflow.py: OrderIntent, Order, Fill, Decision, PositionEvent,
  RiskCheckResult, enums (OrderStatus/Action/RiskDecision/ExitReasonCode),
  idempotency/client_order_id генераторы
- app/services/eventbus.py: outbox-паттерн (EventLog в БД, sequence) +
  in-process fanout; события сначала в БД, потом подписчикам
- app/api/routes/ws.py: /ws — SUBSCRIBE/SUBSCRIBE → SUBSCRIBED/SNAPSHOT/RESUME,
  каналы job:{run_id}, experiment:{id}, bot:{id}, figi:{figi}
- Диспетчер шлёт JOB_STARTED/JOB_COMPLETED в job:{run_id}
- Фронт: src/ws.ts (connect/subscribe/unsubscribe, reconnect 3с),
  lab.ts подписан на активные job-каналы → UI обновляется по событиям,
  поллинг остался fallback-ом
- Проверено live: outbox запись, WS SUBSCRIBE→SNAPSHOT с событием
- Далее по роадмэпу: Фаза 3 (FIGI_STARTED/BAR_PROGRESS/FIGI_COMPLETED события
  из send_to_lab, троттлинг 500мс), Фаза 5 (paper поверх orderflow)

## Фаза 3 (Order_state.md): полный поток событий прогресса ✅
- send_to_lab публикует в job:{run_id}: FIGI_STARTED, FIGI_COMPLETED
  (trades+pnl), TRADE_CREATED, JOB_PROGRESS (done/total/current), JOB_COMPLETED
- Диспетчер передаёт channel в send_to_lab
- Проверено live (3 акции): 16 событий в outbox ровно по цепочке спеки
- Фронт lab.ts обрабатывает события → статус-бар и таблицы обновляются
  по WebSocket, поллинг остался fallback-ом
- Осталось по роадмэпу: Фаза 5 (paper поверх orderflow),
  Фаза 6 (live) — не сейчас

## Фикс «фронт молчит» — WebSocket прокси ✅
- ПРИЧИНА: vite.config.ts проксировал только /api — WebSocket на /ws не проходил
  (браузер получал 404). Добавлен прокси /ws с ws:true (target ws://backend)
- Vite на .3 перезапущен с новым конфигом (node через /usr/local/opt/node/bin)
- Обработчик событий в lab.ts пишет в центральную колонку (спиннер акции,
  прогресс-бар done/total, очистка при завершении), а не в невидимый status-text
- Проверено: ws://localhost:5173/ws → SUBSCRIBED + SNAPSHOT (67 событий)

## Прогресс внутри акции + оптимизация — 2026-08-25 ✅
- EngineRunner.run(candles, progress_cb) — callback каждые 500 баров
- send_to_lab: _on_bars пишет bar_info/bar_pct в lab_result
  («VWAP: бар 1200/43000 (25.05 14:00)») — live-виден на плашке центра
- compute_signals: опциональные готовые candles — грузятся один раз на акцию,
  не дублируются для каждого члена (экономия ~50% на I/O)
- Проверено: VWAP 2 акции → DONE, per_stock заполнен корректно

## Фикс зависания в потоке — 2026-08-25 ✅
- asyncio.get_event_loop().create_task() из to_thread потока вызывал
  RuntimeError (event loop недоступен в не-main потоке) → тест падал
  с 0 сделок, ошибка пряталась в lab_result.errors
- Убран create_task из _on_bars — bar_info обновляется в памяти,
  коммит после завершения каждой акции (уже было)
- Проверено на .3: MACD hour 180d SBER → 113 сделок, net -38.75

## Фикс дёрганья плашек — 2026-08-25 ✅
- renderMiddle переписан: карточки НЕ пересоздаются при каждом поллинге.
  Новые создаются, существующие обновляются точечно (только ширина прогресс-бара
  и текст статуса) — нет мигания и дёрганья
- renderLeft/renderRight: аналогично — обновление только изменённых полей

## Плавный прогресс + заливки таблиц (по мотивам trading-bot-t) — 2026-08-25 ✅
- Бэкенд: _progress_committer коммитит сессию каждые 2с во время прогона →
  bar_pct/bars_done/stock_progress доходят до TestRun.progress через _sync_progress
  → бар движется плавно внутри акции, а не прыжками по 10%
- progress: {done,total,current,bars_done,bars_total,bar_pct,stock_progress{ticker:{done,total}}}
- Фронт: ВСЕ колонки теперь рендерятся по сигнатурам (cardSigs) — DOM не
  пересоздаётся без изменений, дёрганья нет; WS-события только троттлят refresh
  (600мс), polling адаптивный 1с актив / 15с простой
- Сегментный мидбар в карточке очереди: сегмент на акцию, текущий заполняется
  по stock_progress (как LabMidbar в trading-bot-t)
- Таблица live-результатов: ячейки W/L и PnL с пропорциональными зелёно-красными
  градиентными заливками (splitFillStyle); сигнатура liveSig не даёт панели
  перестраиваться без изменений

## Фиксы по шагам (плашки/кнопки/таблица) — 2026-08-25 ✅
- ■ СТОП: backend lab_queue_stop теперь УДАЛЯЕТ TestRun + cfg→DRAFT →
  плашка уходит из очереди в ЛЕВУЮ колонку (не в «Протестированы»)
- Пауза⏸/пуск▶: при смене статуса сигнатура меняется → карточка полностью
  пересобирается → кнопка честно меняется ⏸↔▶
- Таблица результатов постоянна: клик по любой плашке (идёт/готова) открывает
  одну и ту же панель; для остановленных без totals, но с per_stock —
  рендер частичных данных через renderLiveRun (fallback-ветка)
- Дубли убраны: left=конфиги без раннов, middle=активные, right=терминальные;
  одна конфигурация физически не может висеть в двух колонках одновременно
- renderRight/renderMiddle: полная пересборка только при изменении сигнатуры,
  биндинг кнопок вынесен в bindRunCardButtons/bindTestedCard
- set_status уже guard'ит удалённые run_id — гонка стоп vs завершение безопасна

## oracle_coverage: починка + тесты — 2026-08-25 ✅
- ФИКС КРАША: _oracle_coverage ссылалась на несуществующий trades_out (NameError
  на каждом static-прогоне → /api/v1/test/ensemble падал 500)
- executed теперь считается честно: в _run_pipeline собирается
  executed_signal_keys {(signal_ts_iso, side)} по факту сделки движка и
  передаётся в _oracle_coverage(executed_signals=...); раньше сравнивали
  entry_ts сделки с ts сигнала — всегда мимо (исполнение на бар позже)
- Убран мёртвый код (acc_by/rej_by), добавлен блок confirmation_lag_bars
  {mean, median, max} = conf_idx - point_idx по всем свингам оракула
- Новые тесты backend/tests/test_oracle_coverage.py (5 шт): разделение
  geometric/causal воронок, подсчёт accepted/executed, маппинг гейтов
  quorum/bias, lag-статистика, smoke compute_ensemble на наличие контракта.
  Итог: 51 passed (было 46)
- Внимание: файл ensemble.py правился параллельно (call site с trades_out
  позиционно) — сведено к keyword executed_signals=executed_signal_keys

## Warehouse-конструктор на Складе (по Preview_warehouse.md) — 2026-08-26 ✅
- 3 колонки: Каталог | Конструктор | Параметры блока
- Пайплайн BIAS→SETUP→ENTRY→POSITION + EXIT/RISK сбоку + FILTERS, цвета типов блоков
- Слоты наполняются по роли из #role-select карточек; renderConstructor() на расчёт/чекбокс
- EXIT из реестра EXIT_POLICY_SPECS (каталог API отдаёт exit_policies); выбор идёт в конфиг
- Плашки «?» на нереализованном: RISK, FILTERS-гейты, мультипозиции (POSITION)

## Единая вкладка «Бот» (черновик по Preview_bot.md §19) — 2026-08-25 ✅
- Вкладки «Бот» + «Портфель» слиты в одну «Бот» (page-portfolio удалена, nav очищен)
- Новый layout дашборда: status-strip (СТАТУС/РЕЖИМ/ДАННЫЕ/СИГНАЛЫ/ОШИБКА,
  цвета green/yellow/red/gray по §19) → конфиг бота + стат-карточки →
  метрики портфеля (equity/кеш/PnL/сделки/win-rate) → universe-чипы →
  открытые позиции → последние сделки → вся история сделок paper-портфеля
- bot.ts: pollOnce рендерит всё в одном цикле (renderStatusStrip/renderTrades/
  renderPortfolioSummary), мёртвый renderPortfolio + tradesCache удалены
- main.ts: PAGE_TITLES без portfolio; style.css: .status-strip/.st-chip
- Проверка: npx tsc -b ✅, npm run build ✅
- Далее по Preview_bot.md: kill-switch кнопки (pause/cancel/close-all),
  orders lifecycle, signal stream, risk panel — нужны эндпоинты /api/v1/bot/*

## Бот: статус-строка, kill-switch, заявки, события (Preview_bot.md шаги 1-3) — 2026-08-25 ✅
### Бэкенд
- /api/v1/bot/status дополнен: session (PRE_MARKET/OPENING/TRADING/CLEARING/
  EVENING/POST_MARKET/WEEKEND, app/bot/session.py), data {health, source,
  last_candle_ts} (HEALTHY/STALE/NO_DATA), risk {state, daily_pnl,
  daily_loss_limit, entries_paused} (app/bot/risk.py: NORMAL/PAUSED/LOSS_LIMIT)
- НОВЫЕ ЭНДПОИНТЫ: POST /pause {paused}, POST /orders/cancel-pending,
  GET /orders, GET /events, POST /positions/close-all; start принимает
  daily_loss_limit
- Kill-switch: пауза входов (выходы работают), отмена pending-заявок, закрыть
  все позиции; авто-circuit-breaker: дневной убыток ≥ лимита → CIRCUIT_BREAKER_
  TRIGGERED + пауза входов (§16)
- Orders lifecycle: BotOrder c client_order_id paper-xxxx, статусы
  SUBMITTED→FILLED/CANCELLED, журнал в runtime.orders + события
- EventLog (§20): кольцевой буфер 500, SIGNAL_CREATED/SIGNAL_REJECTED(reason)/
  SIGNAL_IGNORED/ORDER_SUBMITTED/ORDER_FILLED/ORDER_CANCELLED/POSITION_OPENED/
  POSITION_CLOSED/CIRCUIT_BREAKER_TRIGGERED/BOT_STARTED/BOT_STOPPED/ERROR
- /start теперь асинхронный: тяжёлая подготовка в фоне (starting=true в
  статусе); раньше обрыв клиента отменял старт на полпути
### ФИКСЫ (бот был нежизнеспособен)
1. paper_broker: NameError datetime (краш на первом закрытии позиции!)
2. paper_broker: CostModel.fill_price(side_buy=) — неверная сигнатура (краш на
   первом открытии!); приведено к канону движка fill_price(price, Side.X)
3. grpc.aio market-data СТРИМ мгновенно CancelledError под uvicorn (унитарные
   вызовы работают) → CandleFeed: cancel подписки ДО первой свечи = fallback на
   polling; request_stop() отличает реальный стоп
4. /start блокирующий → фоновый _startup + starting-флаг
### Фронтенд (вкладка «Бот»)
- Чипы СЕССИЯ и РИСК в status-strip; kill-row: ⏸ пауза входов /
  ✖ отменить заявки (счётчик) / ▼ закрыть всё (confirm)
- Таблица «Заявки (lifecycle)» + «Поток событий» (poll 3с, цветные типы)
- api.ts: BotOrderRow/BotEventRow, botPause/botCancelPending/botCloseAll/
  botOrders/botEvents
### Проверки
- pytest 55 passed (+4 test_bot_session_risk.py); tsc ✅; build ✅;
  живой смок: старт→RUN, pause/cancel/close-all/stop, events пишутся
### Внимание для .3
- uvicorn запускать с --loop asyncio (уже в dev.sh); stream-фид может не
  работать под uvicorn — бот честно падает в polling (data.source=polling)

## Вход с подтверждением свечами — 2026-09-08 ✅
- Новая фича движка: `entry_confirm_bars` в SignalPolicyConfig — вход ждёт N
  подряд ПОДТВЕРЖДАЮЩИХ свечей в сторону входа (close>open для LONG, close<open
  для SHORT), не противоположных; при свече не в сторону — сброс счётчика.
  Исполнение по open бара после последней подтверждающей свечи. N=0 (дефолт) =
  прежнее поведение (вход на open следующего бара).
- Механика «двойной semi-flip»: на выходе бота было confirm_flip=2 = ДВА
  ПРОТИВОПОЛОЖНЫХ сигнала кворума подряд (не «две свечи», как предполагалось);
  на входе подтверждения не было вовсе — теперь есть, на 1m.
- Реализация: policies.py (поле), runner.py (состояние entry_confirm + накопление
  счётчика + сброс на свече не в сторону + guard position is None + reset при flip),
  ensemble.py (проброс из req), бот ensemble_strategy.py (поле params → req),
  bot runtime.py (entry_confirm_bars в optuna-параметры бота),
  scripts/all20_compare_today.py (entry_confirm_bars из optuna_params файла)
- Проверено на SBER 1m (2026-08-01..08-11, все 7 стратегий, кворум 2, semi_flip):
  без подтверждения 363 сделки / net −445; с подтверждением 2 свечей — 349 сделок /
  net −2564 (входы сместились позже, сменились стороны)
- 3 новых golden-теста (задержка входа / сброс на невосходящей / noop при N=0):
  итого **209 passed**
- Следующий шаг: optuna на машине .5 на всё лето (semi-flip + вход подтверждением
  2 свечей, все 7 голосов + SL/TP + volume, сессии утро/день/вечер, режимы отдельно,
  20 акций × 10k с накоплением); июньский прогон эталона — повторить с большим
  таймаутом.


## Вкладка Analytics — прогоны из reports/ — 2026-09-30 ✅

- ✅ Таблицы `report_runs/report_rows/report_trades/report_slices` (`app/models/reports.py`),
  создаются `Base.metadata.create_all` в lifespan; `experiments`/`experiment_trades`/
  `sandbox_trades` не тронуты (это отдельный контур).
- ✅ `app/services/report_slices.py` — ядро срезов (сессия МСК, режим ADX, ER, час, weekday,
  тикер). Переиспользуется `scripts/trades_split.py`, импортёром и API — один источник правды.
  `ordered_buckets` для фиксированных измерений отдаёт ВЕСЬ канонический набор бакетов,
  чтобы у разных роботов колонки не разъезжались («пусто» ≠ «нет в выдаче»).
- ✅ `scripts/import_reports.py` — kinds real/wf/matrix/exp, идемпотентность по sha256
  (`content_hash`), `--force/--dry-run/--no-enrich`; обогащение сделок: сессия, ADX-режим,
  ER входа, SL/TP из `EXITS` политик прогона (`ose_exit_matrix.py`), MAE/MFE в ATR по свечам.
  `--force` снимает детей вручную: у SQLite `PRAGMA foreign_keys=OFF` → каскад не работает.
  Импорт: **70 файлов → 70 прогонов, 2369 строк, 39770 сделок, 3554 среза** (20.7с),
  повторный запуск — «новых 0, пропущено 70».
- ✅ API `app/api/routes/analysis_reports.py` → `/api/v1/analysis/…`:
  `reports`, `reports/{id}`, `reports/{id}/slices?dim=`, `reports/{id}/trades`,
  `market/leaders?window=`. Зарегистрирован в `main.py`; конфликта путей с
  старым `/api/analysis/{figi}` нет. В ответах на первый план вынесены количество сделок
  и gross W/L **в штуках и рублях** (методология владельца), не net.
- ✅ UI: в «Анализе» две подвкладки — «Живая» (прежнее поведение) и «Прогоны (reports/)»:
  список прогонов слева (вид + поиск), заголовок прогона со сводкой, вкладки
  «Роботы» (сортировка по сделкам, НЕ по net по умолчанию), «Срезы» (6 измерений × робот,
  подсветка лучшего по net бакета при ≥3 сделках), «Сделки» (фильтры, карточки
  min/max SL/TP, MAE/MFE,WR/PF/Exp, пагинация по 200), «Рынок» (лидеры/аутсайдеры
  день/неделя/месяц). Изменены `frontend/index.html`, `frontend/src/style.css`,
  `frontend/src/main.ts`.
  ⚠️ **править надо `frontend/index.html`** — `frontend/src/index.html` это несервируемая
  копия (vite отдаёт корневой `index.html`), правки в неё в UI не попадают.
- ✅ Проверено: 25 новых pytest (`backend/tests/test_report_analytics.py`, sqlite+aiosqlite);
  весь suite **886 passed / 5 failed** — эти 5 падали и до правок (пустая локальная БД .7
  для `test_research_pack`, «10min» не входит в список ТФ каталога OSE для
  `test_engine_units`); ruff по новым файлам чисто (остались только штатные B008);
  `tsc -b` и `vite build` зелёные; ручной прогон в headless Chrome (CDP): 70 прогонов,
  приёмочный `bt_ose_real_20260930_0145` (run #15) открывается, роботы/срезы/сделки
  считаются, «Живая» панель не сломана, возврат между подвкладками работает.
- ⚠️ SL/TP, ADX/ER и панель «Рынок» на машине .7 пустые: локальная БД без свечей
  (`instruments`/`candles` = 0 строк), `192.168.1.2:5432` сейчас недоступен.
  На живой БД обогащение заполняется — логика подтверждена тестами на синтетических свечах.
  Запуск для просмотра: `uvicorn app.main:app :8000` (backend) + `npx vite --port 5174` (фронт).

## Вкладка Analytics — Этап 2: реплеи + теги пресета + порядок — 2026-10-01 ✅

Заказ владельца (`docs/ANALYTICS_TAB.md` §Этап 2): реплейные тесты видны в Analytics
живым чтением из БД, карточка — теги конфига из сайдкара, порядок во фронте
(клик → сделки, перезапуск = новый тест, удаление по одному/массово), выбор пресета
в модалке запуска.

- ✅ **`app/services/preset_tags.py`** — единый источник: `SIDECAR_DIR`,
  `sanitize` (NAME_LIMIT=48, тот же `\/?%*:|"<>'`), `sidecar_path/load_sidecar/
  save_sidecar/remove_sidecar/load_all_sidecars`, `tags_for_names` — 10 групп тегов
  (Пресет/Роботы/Период/Сессии/Выходы/Деньги/Лимиты/Вход/Bias/Гейты/Издержки).
  `scripts/preset.py` и `bot.py` переведены на сервис (один источник имени).
- ✅ **`app/api/routes/analysis_replays.py`** → `/api/v1/analysis/replays`
  (+зарегистрирован в `main.py`): `GET replays` (в т.ч. `losses`), `GET replays/{name}`
  (summary + config_tags + groups), `slices?dim=` (8 измерений), `trades` (фильтры
  outcome/ticker/side/exit/reason + карточки SL/TP/MAE/MFE/bars). Окна — из
  `bot_test_runs` в ISO (фолбэк — первая сделка), источник правды по датам.
- ✅ **Админка `bot.py`**: `GET /presets` (`PRESETS_DIR`, полный JSON пресета),
  `GET /tests` — SQL-агрегат (без N+1): count/closed/wins/gw/gl/open + окно,
  `GET/DELETE /tests/{name}` (убирает сделки+окно+сайдкар, возвращает details),
  `POST /tests/delete` (массово), `POST /tests/{name}/restart` — новое имя
  `<preset.id> <YYYYMMDD-HHMM>`, тот же payload/окно, старый тест не трогается;
  `_save_test_sidecar()` вынесен из `/mode`, санитизация имени — через сервис.
- ✅ **Фронт «Бот»** (`frontend/index.html`, `src/main.ts`, `src/style.css`):
  таблица тестов (поиск, счётчики, checkall, сортировка по 10 колонкам с
  `th[data-k]`), строка → дроуэр (карточки Net/WR/PF/Exp + все сделки 13 колонок,
  «📊 В аналитику» уводит в карточку реплея), кнопки ↻/🗑 (перезапуск/удаление),
  массовое удаление с confirm, обновление каждые 5с (`pollTests`, чистит сироты
  выбора). Модалка теста: `#ts-preset` (пресеты подгружаются при показе вкладки),
  `#ts-logdb`, автo-имя от пресета.
- ✅ **Анализ**: источник `#rep-source=replay` → список/карточка/действия
  (`renderRepRunHead` + `renderRepTags`, `renderRepActions` — ↻/🗑), панели
  «Срезы»/«Сделки» работают и для реплеев; вид прогона скрыт.
- ✅ **Тесты**: `backend/tests/test_replay_analytics.py` — **17 passed** (sqlite+
  aiosqlite, monkeypatch `SIDECAR_DIR`/`PRESETS_DIR`/`SessionLocal`/`bot_set_mode`):
  список/карточка/теги/деградация без сайдкара/404/8 срезов/фильтры сделок/
  `GET /presets`/SQL-агрегат/админ-перезапуск/400 без окна/`_save_test_sidecar`.
  Всего по прогону `test_replay+test_report+test_test_preset` — 45 passed;
  весь suite (без `test_reference_run`/`test_research_pack`) — **956 passed, 2 skipped**.
- ✅ **Приёмка живая**: curl всех новых эндпоинтов 200; синтетический тест
  (4 сделки + сайдкар) → список (агрегаты/окно), карточка (теги/группы), все 8 срезов
  с корректными session/hour/weekday, фильтры сделок, restart → новое имя в БД
  (старый цел), delete → trades/windows/sidecar убраны, `.env` восстановлен,
  мусорных строк в БД/пресетах не осталось. CDP-смоук UI (headless Chrome :9222 +
  vite :5174): список/сортировка/чекбоксы/дроуэр/модалка/переход в Аналитику с
  тегами и действиями — всё отрисовывается, JS-ошибок в новом коде нет.
  `tsc -b` и `vite build` зелёные; ruff по новым файлам чисто.
- ⚠️ Локальные ограничения: запуск реплея на .7 невозможен (нет `instrument_info`,
  `instruments`=0); `192.168.1.2`/`.3` недоступны (обогащение ADX/ER/MAE/MFE,
  «Рынок» — на живой БД); `psycopg2`/`psql` к локальной PG висят → только `asyncpg`.
- ⚠️ Методология: в списке/карточке на первом плане сделки и gross W/L в штуках и ₽,
  не net; PnL — не показатель.

## Heatmap «Анализ»: торговые часы + плотная сетка — 2026-10-02 ✅

- **Диагноз**: колонки 00:00–06:00 МСК (будни) и выходные в heatmap — ночные бары в БД,
  которых T-Invest API сейчас не отдаёт (проверено: 21:00–24:00 UTC → 0 баров для
  29.09/01.10), выходные бары API отдаёт легитимно; час 6 МСК чист (0 баров до 06:50).
  Пропуски в ячейках — дыры данных (бот стоп/неполные часы); «не все тикеры» — кэш
  `_HM_CACHE` без подписи universe (флуктуация 15↔30) + `if bars:` отбрасывал пустых.
- ✅ **`app/api/routes/bot.py`**: `_HM_WH` — общий предикат (пн–пт, часы 6–23 МСК) в
  `bot_heatmap` и `_hm_compute_meta`; каноническая часовая сетка окна + forward-fill
  close (нет пропусков; цена «не двигалась» = нейтральный цвет); pre-window close
  (последний бар до окна) — сетка полная с первого часа; возвращаются ВСЕ тикеры
  universe (пустые строки тоже); в ключ `_HM_CACHE` добавлена подпись состава universe.
- **Проверка**: офлайн-вызов `bot_heatmap` против живой БД .7 (read-only) —
  30/30, единая плотная сетка 55 часов у всех, неторговых ячеек нет, meta 1463/1650;
  `compileall` ок; hermetic suite **1024 passed / 0 failed**.
- ⚠️ **Не применено на живом `:8000`** (решение владельца 02.10: не перезапускать) —
  uvicorn на .7 без `--reload`, фикс подхватится при следующем рестарте сервера.
  Рестарт поднимет replay-тест «final 0901-05» из `.env` (автостарт в `main.py`).


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
