# Architecture Standardization — аудит 30.09 и план сборочного этапа

> Источник: внешний аудит `master` после 30.09 (Universe 2.0, Analytics, presets, regime-контуры).
> Главный вывод: **не нужен новый функциональный слой — нужен слой стандартизации**:
> `Experiment → Reference Run → Result → Analytics`. Новые роботы/режимы/сигналы — на паузу,
> пока контуры не выровнены вокруг одного эталона.

## Ключевые решения аудита

1. **Два детектора режима — это архитектурная проблема, но третий создавать НЕЛЬЗЯ.**
   - `app/services/regime.py` — **канон** (causal, batch+incremental, `regime_at`, timeline,
     parity-тест `RegimeState == RegimeDetector.compute()`). Его пороги — исследовательские,
     но реализация каноническая.
   - `app/engine/regime_strategies.py` — **LEGACY / стратегийный адаптер** (`RegimeResult` для
     TrendFollowing/MeanReversion/Breakout). Не удалять; пометить и уводить потребителей на канон.
2. **Три разных понятия не смешивать**: `ExecutionMode` (sandbox/test/live) ≠
   `MarketRegime` (RANGE/TREND_UP/TREND_DOWN/HIGH_VOLATILITY/NEUTRAL) ≠ `StrategyFamily`.
3. **Иерархия**: Indicator → Feature → Signal Detector → Signal → Strategy → Portfolio/Execution.
   OSE-роботы = external/reference implementations (parity/discovery), не центр архитектуры.
4. **Эталон — это не «лучший робот»**, а зафиксированный прогон: dataset/universe/TF/индикаторы/
   режим/стратегия/выходы/исполнение/издержки/период + **expected fingerprint & ledger**,
   воспроизводимый в EngineRunner / Replay / Runtime.
5. **Analytics = Experiment Analytics** («что произошло в конкретном эксперименте»), не «кто лучший».
   Идея «подсветить лучшего робота в режиме» заменяется сравнением роботов внутри одного слайса.
6. Preset/`TEST_PRESET` — правильное направление; сверху нужен объект **Experiment**
   (dataset/universe/strategy/signals/regime/execution/costs/period/validation/expected_reference).

## Реестр компонентов (Шаг 1)

| Компонент | Роль | Статус |
|---|---|---|
| `app/engine/indicatorhub.py` | канонические формулы индикаторов (+ER) | **CANONICAL** |
| `app/services/regime.py` (RegimeDetector/RegimeState) | канонический детектор режима | **CANONICAL** |
| `app/engine/runner.py` (EngineRunner) + `models.Trade`/ledger + `fingerprint()` | канонический бэктест-движок | **CANONICAL** |
| `tests/test_engine_golden.py` (+ audit/canon тесты) | reference-тесты движка | **REFERENCE TESTS** |
| `app/bot/universe/*` (Universe 2.0) | измерения/скринеры/селекция/аллокация | **CANONICAL** |
| `bench scripts/preset.py`, `configs/presets/*` | конфигурация эксперимента (ветки) | **CANONICAL** |
| Analytics (`report_*`, report_slices, UI) | отчётность эксперимента | **CANONICAL (reporting)** |
| `bt_ose_sweep.py` + `ose_exit_matrix.py` + OSE robots | OSE compatibility / discovery | **ADAPTER / RESEARCH** |
| `app/bot/replay_feed.py`, runtime `mode=test` | replay adapter / project replay | **ADAPTER** |
| `research_pack`, Exit Lab, Stop Oracle, QuantStats | research analytics | **RESEARCH** |
| Optuna-скрипты | optimization adapter | **ADAPTER (пауза до Reference Run)** |
| `app/engine/regime_strategies.py` | legacy regime-адаптер для старых стратегий | **LEGACY (не трогать, пометить)** |
| старые `backtest_*.py`, `bt_debug*.py`, `vol_carousel`, `marketdata/indicators.py` | прочее | **LEGACY / DEV-ONLY** |

## Пирамида тестирования (L0–L4)

- **L0 Mathematical** — формулы индикаторов на фикстурах (без денег/БД).
- **L1 Measurements** — Volatility/Trend/Sector/Regime: causal, deterministic, warmup,
  incremental==batch, no-lookahead.
- **L2 Signals** — candles+features+regime → Signal: side/time/reason/features, без PnL.
- **L3 Strategy/Engine reference** — signal stream → EngineRunner → TradeLedger: вход/выход/SL/TP/
  комиссии/next_open/конфликты/шорт/жизненный цикл. Здесь `test_engine_golden.py`.
- **L4 Experiment/Research** — dataset+strategy+WF/OOS/robustness → Analytics (PnL/PF/DD/срезы).

## ДОКАЗАНО эмпирически (30.09): T-Invest метит свечи по НАЧАЛУ (START)

Проба на живой API (SBER, 24.09.2026): нативные 10m/1h свечи T-Invest сверены с агрегацией 1m
START (эпоха/floor) vs END (ceil):
- **10min: START 102/102 · END 0/102**
- **hour: START 18/18 · END 0/18**

Следствия:
1. **Канон конвенции = START** (совпадает с T-Invest, SQL `date_bin`, `Resampler`).
2. Старое допущение `candle_cache` «метка = закрытие, как у T-Invest» — **ошибка** (уже исправлено на START).
3. **Корпус ТФ-таблиц в БД построен старой END-сеткой** (10m: 4.36M строк с 2024 по 43 фигам; 5m: 3.86M; hour: 1.6M; 2h/4h/week/month…) → **требует пересбора** (`scripts/rebuild_tf_tables.py`).
4. `candlehub::CandleSeries/bucket_close` и `services.ensemble.resample/cached_resample`, `ensemble_ctx` (END) — к конвергенции на START (по одному потребителю, с тестами).
5. Инвариант-тест: `tests/test_db_tf_parity.py` — ТФ-строки БД == канонический Resampler.

## Инвентарь агрегаторов старших ТФ (находка REF-001b, 30.09)

Де-факто **6 реализаций** агрегации 1m → старший ТФ в **двух конвенциях метки бара**:

| Реализация | Конвенция | Потребители |
|---|---|---|
| `marketdata/resampler.py::Resampler` | **START** (эпоха, floor) — КАНОН | ReplayFeed (runtime replay), `universe/bars`, харнесс (после фикса REF-001b) |
| `ml_ensemble_filter.resample_to_5m` | START (floor) | ML-фильтр |
| `candle_cache.resample_from_1m` (SQL→БД) | **был END** (ceil, «как T-Invest») → **ИСПРАВЛЕН на START** (эпоха) | запасённые ТФ-свечи; прогрев бота (preload) |
| `engine/candlehub.py::CandleSeries/bucket_close` (+`build_tf`) | **END** (ceil) | CandleHub (markethub, marketdata hub/adapter/orchestrator, ensemble_v2 M5); ранее — харнесс |
| `services/ensemble.py::resample/cached_resample` | **END** (ceil; докстрока: «совпадает с bucket_close») | bias/regime, routes, research_pack |
| `ensemble_ctx.py::DataContext.resample` (`_bucket_of`) | **END** (ceil) | ctx-контур ансамбля |

**Почему так:** два контура — порт OsEngine (CandleManager → `candlehub`, метка по закрытию,
как в OsEngine) и проектный marketdata (`Resampler`, метка по началу, совпадает с БД `date_bin`).
Соглашение «CandleHub — источник правды» относилось к **владению 1m-рядами и событиями**,
но агрегация ТФ размножилась по контурам и разошлась по конвенции.

**План конвергенции (по одному, с тестами):**
1. Выбрать единую конвенцию — предложение: **START/эпоха** (совпадает с БД/date_bin, UI, replay).
2. `candlehub.build_tf`/`CandleSeries` → делегирование каноническому агрегатору (или явный
   legacy-статус для OsEngine-parity потребителей).
3. `services.ensemble.resample`/`ensemble_ctx` → тонкие обёртки над каноном (поведенческие
   изменения bias/regime — проверять тестами по одному потребителю).
4. `ml_ensemble_filter.resample_to_5m` → обёртка; `candle_cache` SQL — сверить и выровнять.
5. Parity-тест «все агрегаторы дают идентичный ряд» + заморозка инвентаря (новый агрегатор — запрещён).

## План (приоритет — сверху)

1. [x] Реестр компонентов (таблица выше).
2. [ ] Зафиксировать канон явно (доки + пометки LEGACY/ADAPTER в шапках модулей).
3. [x] **REF-001 — Reference Run**: `configs/reference/REF-001.json` +
   `scripts/reference_run.py --write/--check` + `tests/test_reference_run.py`.
   **REF-001b (сверка EngineRunner ↔ Runtime replay) — вскрыто и зафиксировано:**
   - **НАЙДЕНО (крупное):** харнесс строил ТФ через `candlehub.build_tf` (метка бара по
     ЗАКРЫТИЮ бакета, `bucket_close`), а replay/live — через `marketdata.Resampler` (метка по
     НАЧАЛУ бакета): у границ сессий ряды расходились **203/203**. ФИКС:
     `ose_exit_matrix._bars_tf_canonical` — харнесс переведён на канонический Resampler;
     REF-001 перебазирован (fingerprint `8a703fe9…`, 4 сделки). Внимание: все харнесс-цифры
     до 30.09 считались на старой сетке (кэш переразметится по data_hash сам).
   - **ОСТАЛОСЬ (следующие шаги REF-001b):** (а) **шов окна/прогрева** — движок грузит с 00:00 и включает
     одно-минутную корзину открытия сессии 03:50, окно replay стартует 04:00, preload-граница бота
     другая → одно-баровое расхождение серии и разное время RSI-кроссов (runtime SELL 04:30 vs
     движок 08:30); нужна единая семантика шва (preload без будущих ts + общий старт серии);
     (б) семантика после выхода — бот после signal_exit на противоположном сигнале не открыл
     обратную позицию (сверить flip/entry-after-exit с EngineRunner); (в) прочие research-скрипты
     (labeling/calibrate/trades_split) всё ещё на `build_tf` — выровнять на канон;
     (г) parity-тест «ТФ-таблицы БД == Resampler» (после фикса candle_cache) + пересбор ТФ-таблиц
     всех фиг (SBER пересобран вручную).
4. [ ] Regime convergence: потребители `regime_strategies` → канонический RegimeDetector
   (адаптер на переходный период).
5. [ ] StrategyCatalog: единый каталог (id/family/version/warmup/params/signal semantics/
   supported regimes/sides) поверх существующих карточек.
6. [ ] Harness consolidation: один Experiment Runner (single/matrix/WF/OOS/robustness),
   bt_ose_sweep/optuna/replay/research — плагины/адаптеры.
7. [ ] Analytics читает `ExperimentRun` (единый формат), а не 5 разных.

**Пауза до шагов 2–3:** новые роботы, новые режимы, ML, live-оптимизация.
