# PROGRESS — ход работ

> Обновлять при завершении каждого блока. Формат: [статус] блок — дата — заметки.
> Статусы: ✅ готово | 🔄 в работе | ⏳ план | ❄️ заморожено

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
