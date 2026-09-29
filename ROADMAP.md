# ROADMAP — Deeptrading: стабилизация + порт идей OsEngine

Живой документ: обновляется в каждой рабочей сессии, чтобы всегда было видно,
на каком этапе проект и что дальше. Последнее обновление: 2026-09-28.

## Контур

- Разработка: MacBook Pro (Denis); код — `/Volumes/Dev/Deeptrading` (git-репозиторий)
- GitHub — канонический удалённый репозиторий (адрес см. `git remote -v`)
- **Рабочая машина — 192.168.1.8: ВСЕ РАБОТЫ ЗДЕСЬ** — `ssh nadts@192.168.1.8` БЕЗ пароля (ключ настроен); репо `C:\Users\nadts\Dev\Deeptrading`, Python 3.12, git
- 192.168.1.2 — сейчас там живёт рабочий прототип (изменения не откатываем, если движок не задет); выводится из контура после переезда на .8
- OsEngine: клон исходников `~/OsEngine` (C#, .NET 10, WinForms) — только чтение и изучение, НЕ запуск

## Ключевые решения (2026-09-25)

1. **OsEngine под Wine не запускаем** — извращение и непрозрачный результат.
   Только чтение исходников и порт идей в наш стек (Python/FastAPI + TS/Canvas).
2. **Лицензия OsEngine — EULA, не open-source**: код не копируем; концепты реализуем с нуля (clean-room port).
3. **CandleManager/CandleSeries не отбрасываем — наоборот, изучаем**: наш свечной контур
   (bot/feed.py, replay_feed.py) регулярно сбоит; их архитектура (пул серий, подписки, сборка свечей,
   события готовности, хранение) — кандидат на замену.
4. **Chart**: разбираем, почему их графики двигаются так быстро (завидно), и переносим приёмы
   в наш фронтенд (labchart.ts и др.).
5. **Пилот — 1-2 робота**: сначала понимаем механику импорта (параметры -> конфиг -> UI -> исполнение),
   потом масштабируем.

## Фазы

### Фаза 0 — инфраструктура тест-режима [готово, 2026-09-24]

- PaperBroker: `equity / margin_attributes / last_prices / market_value / free_funds`
- `_finalize_replay`: закрытие позиций в конце реплея + upsert `bot_test_runs` (сделки помечены `test_name`)
- CostModel: комиссия и проскальзывание в тест-режиме
- `replay_log_persist` (запись логов реплея в БД по флагу) + `replay_pace` (fast | wall)
- Фиксы: NameError в runtime, экспозиция-гейт, сильные ссылки на задачи (утечка задач)
- Фронт: модалка тест-режима (имя, даты, темп, чекбокс «логи в БД»), тест-поля в `/bot/status`

### Фаза A — фиксация + переезд на .8 [текущая, параллельно с C]

- [x] ROADMAP.md создан (этот файл)
- [ ] Commit всех изменений бота + push в GitHub
- [ ] Разведка .8: доступ, ОС, python, postgres, node, свободные порты
- [ ] Синхронизация кода и окружения на .8 (репо, venv, зависимости)
- [ ] БД на .8: Postgres, схема/данные ⚠️ **АКТУАЛЬНО (2026-09-28): БД УЖЕ ЖИВЁТ В DOCKER НА .2 — `192.168.1.2:5432/deeptrading`, ssh `Denis@192.168.1.2`, пароль `0987`, доступна СО ВСЕХ машин. Отдельный Postgres на .8 НЕ нужен — просто `postgres_host=192.168.1.2`**
- [ ] Запуск uvicorn + фронт на .8; верификация `/api/v1/bot/status` и короткого реплея
- Критерий: .8 полностью обслуживает API и UI; тест-режим проходит; .2 больше не нужен

### Фаза B — изучение OsEngine (без запуска) [готово, 2026-09-26]

Разобраны слои: Candles/ (CandleManager, CandleSeries, TimeFrameBuilder, Factory),
Charts/CandleChart/ (ChartCandleMaster, WinFormsChartPainter), OsTrader/Panels/
(BotPanel, BotTabSimple — плагин-архитектура роботов), RiskManager, Journal,
Market/ (IServer, ServerMaster, TesterServer), OsOptimizer/ (OptimizerExecutor,
AsyncBotFactory, walk-forward), Robots/BotFactory.

Выход (в репо):
- `docs/osengine/PORT_NOTES_CANDLEHUB.md` — свечной контур → вход в Фазу C
- `docs/osengine/PORT_NOTES_ENGINE.md` — движок/плагины/режимы → вход в Фазы C/D

Главные выводы (сравнение с нашим движком):
1. Режим-агностичность: у них live/тестер/оптимизатор = подмена IServer при неизменном
   коде робота; у нас то же через «тест = бот» (EngineRunner + писатели feed/replay).
   CandleHub формализует границу: писатели → hub → подписчики, без веток в runtime.
2. Плагин-параметры: их робот = поля класса → автогенерация UI. У нас аналог УЖЕ ЕСТЬ —
   `STRATEGY_CATALOG.params_schema` (app/engine/catalog.py: type/min/max/default
   на каждую стратегию, 21 стратегия в STRATEGY_REGISTRY) → `/strategies/{id}/schema`
   почти бесплатен (Фаза D).
3. Walk-forward-фолды (их OsOptimizer) → `scripts/walk_forward.py` поверх Optuna per-ticker.
4. Глобальный RiskManager (дневные лимиты на всё приложение) → `app/bot/risk_limits.py` (Фаза E).
5. Наше преимущество сохраняем: стратегии stateless + golden-тесты + fingerprint —
   их роботы с mutable-состоянием так не тестируются.

### Фаза C — CandleHub: новый свечной контур [текущая, старт 2026-09-26]

Референс и маппинг кода: `docs/osengine/PORT_NOTES_CANDLEHUB.md`.

- `app/engine/candlehub.py`: CandleHub (единственный владелец серий) + CandleSeries
  (deque maxlen, события on_closed/on_updated, snapshot/range) + build_tf (любой ТФ
  из 1m на лету + кэш; в БД — только 1m, без дублирования под каждый ТФ)
- Писатели: feed.py (live), replay_feed.py (реплей), moex.py (бэкфилл) — кормят hub
- Потребители: runtime (on_closed), тест-режим, API для фронта
- WS-события готовых свечей на фронт; `GET /api/v1/candles?figi=&tf=&limit=`
- Приёмка: 7 дней live без пропусков; реплей 1:1 с текущими сделками;
  фронт тянет 1m/5m/1h без отдельного кода на каждый ТФ

### Фаза D — пилот: 1-2 робота + плагин-контур [далее]

- `GET /api/v1/strategies/{id}/schema` — схема параметров из STRATEGY_CATALOG.params_schema
  → автогенерация формы на фронте (их подход: контролы из типов полей робота)
- Адаптер робота: конфиг-схема → UI; исполнение через существующие брокеры
- `scripts/walk_forward.py`: N×(train/forward) фолдов поверх Optuna per-ticker,
  метрики на forward, медиана по фолдам — честная OOS-оценка
- Пилот: 1-2 робота из каталога OsEngine (концепт, clean-room):
  кандидаты — BollingerTrendVolatilityStagesFilter, PinBarScreener, RsiContrarian
- Прогон тест-режимом (реплей) + метрики (PnL, winrate, drawdown)
- Приёмка: робот настраивается из UI без ручной вёрстки; сделки воспроизводимы в реплее

### Фаза E — production на .8 [далее]

- Деплой из git на .8: watchdog, ротация логов, мониторинг
- `app/bot/risk_limits.py` — сводные дневные лимиты (max DD%, max trades/day, max volume)
- Страховочный server-side SL для live-позиций (из OsEngine: стопы — ордера, живут
  у брокера; у нас выходы считает процесс бота — падение = незащищённая позиция)
- 192.168.1.2 — вывод из контура (или холодный резерв); сейчас на нём живой рабочий прототип — не трогать без нужды

## Бэклог проблем

- Утечка сессий SQLAlchemy: доказана (GC-варнинги в uvicorn-логах), частично закрыта сильными
  ссылками на задачи; нужен контрольный замер pg_stat_activity уже на .8
- Live-свечи: пропуски и рассинхрон — закрывается Фазой C
- git поверх SMB-маунта медленный: git-операции выполнять с запасом по таймауту

## Журнал

- 2026-09-25 — создан роадмап. Решения: без Wine; CandleManager/Chart изучаем;
  пилот 1-2 робота; деплой-машина — 192.168.1.8.
- 2026-09-26 — Фаза B ЗАКРЫТА (свечи+чарты+движок+плагины+оптимизатор). Выход —
  два PORT_NOTES (CANDLEHUB + ENGINE) + секция OSENGINE в MEMORY.md. Сверка с нашим
  кодом: STRATEGY_CATALOG.params_schema (catalog.py) уже даёт всё для автогенерации
  UI-форм — /schema-эндпойнт почти бесплатен. План на сегодня: старт Фазы C —
  candlehub.py (CandleHub + CandleSeries + build_tf) + тесты склейки ТФ (стык сессий,
  гэпы, дубли, неполный фрейм); при остатке времени — read-only GET /api/v1/candles
  из БД через build_tf. План на завтра (2026-09-27): писатели за флагом (feed.py →
  hub, реплей → hub), сверка реплея 1:1 с текущими сделками, переключение
  ensemble_strategy на серии hub. Затем Фаза D: /strategies/{id}/schema + walk_forward.py.
- 2026-09-26 (вечер) — CandleHub v2 ГОТОВ и доставлен на .8: 64/64 тестов
  (локальный staging + venv на .8). Вошло: validate_candle (битые свечи →
  on_rejected), приоритет источников live > rest > db (replace/ignore), вставка
  опоздавших минут + rebuild производных ТФ (on_rebuilt), gap_report() для
  докачки, build_tf как единственная точка склейки ТФ. Тесты поймали реальный
  баг off-by-one в _register (replace переписывал соседний бар) — исправлен.
  ГРАНИЦА РОЛЕЙ закреплена: CandleHub — чистый движок свечей (без сети/БД/
  подписок); оркестратор (докачка по gap_report, live-подписки, сигнальные
  фиды IMOEX) — отдельный модуль поверх, следующий шаг. Примечание: на
  SMB-шаре v2-файлы переименованы вручную в *.removed_20260926 — на шару не
  возвращаем без решения пользователя; канон кода — .8.
- 2026-09-27 — Сессия порта роботов потеряна без кода (12 ч, 376 сообщений,
  1/6 todo: только сбор материала; tmp-черновики удалены). Восстановление —
  не из кэша, а напрямую из первоисточника ~/OsEngine. Прочитано в этой
  сессии: 5 роботов (Trend: PriceChannelTrade, EnvelopTrend, SmaStochastic;
  CounterTrend: RsiContrtrend, StrategyBollinger) + 6 индикаторов (Scripts/:
  Sma, Rsi, Stochastic, Bollinger, Envelops, PriceChannel) + PORT_NOTES_ENGINE
  (TesterServer-исполнение). Правило сессии: каждый шаг порта фиксировать в
  этом журнале; код писать сразу в репо (app/engine/ose/), не в tmp. Далее:
  ose/indicators.py → ose/robots.py (5 роботов + BotTradeRegime) → тесты → .8.
- 2026-09-27 (шаг 2) — app/engine/ose/indicators.py НАПИСАН (clean-room, EULA:
  поведение, не код) + tests/test_ose_indicators.py: sma, rsi, stochastic,
  bollinger, envelops, price_channel. Портированные quirks (докстринги+тесты):
  Sma-окно [i-len+1, i], первый валид i=len (свеча 0 не входит); Rsi через
  MovingAverageHard — первый валид i=len+21, флэт/чистый тренд → 100;
  Stochastic k=0 в под-прогреве, деление всегда на length; Bollinger len>30 →
  /(len-1), round 6; Envelops ±% от SMA; PriceChannel окна [i-len+1, i].
   Отличия от наших engine/indicators.py — сознательные (совместимость с
   тестером OsEngine). Далее: robots.py (5 роботов + BotTradeRegime +
   TesterServer-подобный симулятор исполнения), тесты роботов.
- 2026-09-28 — РЕШЕНИЕ: полный порт OsEngine «по кусочкам» (clean-room);
  тесты всегда гоняем на .8, после зелёной приёмки — поэтапная установка
  на .2 (там живой прототип, не ломать). Сделано: ose/robots.py (5 роботов +
  собственный TesterTab-реплей на барах), ose/strategy.py (адаптеры под
  контракт Strategy: голос = смена позиции робота на закрытии бара;
  ose_all = 5 голосов + merge_quorum), 6 стратегий в STRATEGY_REGISTRY и
  STRATEGY_CATALOG (wave 5): ose_all, ose_price_channel, ose_sma_stoch,
  ose_envelop_trend, ose_rsi_contrtrend, ose_bollinger. 44 теста зелёные
  локально и на .8 (test_ose_indicators 14 + test_ose_robots 18 +
  test_ose_strategy 12); test_engine_units wave-фильтр расширен (1,3,4,5).
  Портинг-мап на остальные роботы: entry-логика → Strategy.on_bar → Signal;
  выходы (SL/TP/трейлинг) → ExitPolicy, НЕ внутрь робота; комбинированные
  роботы (несколько условий) → голоса членов + merge_quorum; параметр →
  Params dataclass + params_schema в каталоге. Специальные семейства
   (грид/арбитраж/мм) в directional-движок НЕ портируем.
- 2026-09-28 (продолжение) — Wave A (выходы) готова: слоты стоп/тейк на
   позиции (OCO, перезарядка TryReloadStop/Profit), трейлинг на стоп-слоте,
   срабатывание между барами по касанию диапазоном — docs/osengine/PORT_NOTES_EXITS.md,
   реализация в TesterTab (app/engine/ose/robots.py); ose-тесты зелёные на Mac и .8.
   Полный скан ~/OsEngine Robots/: 201 файл → direct 58, SPECIAL 40,
   no-entry 57, учебные 29, UI 12, портировано 5 — docs/osengine/PORTING_MAP.md
   (+ robots_registry.tsv, scan_robots.py). Волны порта: B OnScriptIndicators
   (13), C Trend+CounterTrend (10), D Patterns+Monitors (11),
   E PositionsMicromanagement (8), F айсберг+фьючерсы (10), G остатки (6),
   H SPECIAL (40). Гриды — вне плана. Далее: волна B.
