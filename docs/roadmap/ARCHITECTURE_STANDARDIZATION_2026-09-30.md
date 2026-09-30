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

## План (приоритет — сверху)

1. [x] Реестр компонентов (таблица выше).
2. [ ] Зафиксировать канон явно (доки + пометки LEGACY/ADAPTER в шапках модулей).
3. [x] **REF-001 — Reference Run**: `configs/reference/REF-001.json` +
   `scripts/reference_run.py --write/--check` + `tests/test_reference_run.py`.
   Дальше: прогнать тот же сценарий через Replay/Runtime и сверить ledger.
4. [ ] Regime convergence: потребители `regime_strategies` → канонический RegimeDetector
   (адаптер на переходный период).
5. [ ] StrategyCatalog: единый каталог (id/family/version/warmup/params/signal semantics/
   supported regimes/sides) поверх существующих карточек.
6. [ ] Harness consolidation: один Experiment Runner (single/matrix/WF/OOS/robustness),
   bt_ose_sweep/optuna/replay/research — плагины/адаптеры.
7. [ ] Analytics читает `ExperimentRun` (единый формат), а не 5 разных.

**Пауза до шагов 2–3:** новые роботы, новые режимы, ML, live-оптимизация.
