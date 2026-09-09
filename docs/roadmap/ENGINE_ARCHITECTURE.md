# ENGINE_ARCHITECTURE.md — Полная архитектура движка и тестов

> **Дата аудита:** 2026-09-08
> **Версия движка:** trade_engine_v1
> **Фреймворк тестов:** pytest + pytest-asyncio (asyncio_mode = auto)
> **Всего строк:** ~16,800 (tests ~8,728 + engine ~4,500 + bot ~2,300 + ensemble ~1,275)

---

## СОДЕРЖАНИЕ

1. [Архитектура модулей](#1-архитектура-модулей)
2. [Engine — ядро](#2-engine--ядро)
3. [Services/Ensemble — ансамбль V4](#3-servicesensemble--ансамбль-v4)
4. [Bot — runtime и брокеры](#4-bot--runtime-и-брокеры)
5. [Тесты — что покрыто](#5-тесты--что-покрыто)
6. [Известные баги и статус исправлений](#6-известные-баги-и-статус-исправлений)
7. [Как писать новые тесты](#7-как-писать-новые-тесты)
8. [Валидация и оптимизация движка](#8-валидация-и-оптимизация-движка)
9. [Quick Reference](#9-quick-reference)
10. [SL / TP / Trailing Stop — единый механизм](#10-sl--tp--trailing-stop--единый-механизм)
11. [Журнал багов и находок](#11-журнал-багов-и-находок)

---

## 1. Архитектура модулей

```
┌─────────────────────────────────────────────────────────┐
│                    BOTS LAYER                           │
│  runtime.py → live_broker / paper_broker                │
│  ensemble_strategy.py → compute_ensemble()              │
│  stream_manager.py → gRPC positions/trades/orders       │
│  feed.py → CandleFeed (gRPC or REST polling)            │
└──────────┬──────────────────────┬───────────────────────┘
           │                      │
           ▼                      ▼
┌──────────────────────┐  ┌──────────────────────────────┐
│   ENGINE (core)      │  │   SERVICES (analytics)       │
│                      │  │                              │
│  runner.py           │  │  ensemble.py                 │
│  ├── policies.py     │  │  ├── resample()             │
│  ├── exits.py        │  │  ├── generate_signals()     │
│  ├── costs.py        │  │  ├── merge_quorum()         │
│  ├── ledger.py       │  │  ├── compute_bias()         │
│  ├── models.py       │  │  ├── micro_breakout()       │
│  ├── strategies.py   │  │  ├── _validate_candles()    │
│  ├── wave1.py (×5)   │  │  ├── _run_pipeline()       │
│  ├── indicators.py   │  │  └── oracle/analytics       │
│  ├── sessions.py     │  │                              │
│  ├── catalog.py      │  │  signals.py                  │
│  ├── orderflow.py    │  │  regime.py                   │
│  └── quorum.py       │  │  ml_ensemble_filter.py      │
└──────────────────────┘  └──────────────────────────────┘
```

### Зависимости (кто что импортирует)

```
runtime.py
  ├── engine.runner (EngineRunner)
  ├── engine.strategies (build_strategy)
  ├── engine.exits (ATR/Fixed policy)
  ├── engine.sessions (SessionPolicy)
  ├── engine.models (Candle, Position, etc)
  ├── services.ensemble (compute_ensemble)
  ├── bot.feed (CandleFeed)
  ├── bot.stream_manager (StreamManager)
  ├── bot.paper_broker / bot.live_broker
  └── bot.risk / bot.events / bot.session

ensemble_strategy.py
  ├── engine.models (Candle, Signal)
  └── services.ensemble (compute_ensemble) ← MAIN CALL

ensemble.py
  ├── engine.runner (EngineRunner)
  ├── engine.strategies (build_strategy, STRATEGY_REGISTRY)
  ├── engine.quorum (merge_quorum)
  ├── engine.indicators (atr)
  ├── engine.costs (CostModel)
  ├── engine.models (Candle, ExitReason)
  └── services.signals (generate_signals)

runner.py
  ├── engine.policies (SignalPolicy)
  ├── engine.exits (intrabar_exit, AtrTrailingPolicy, etc)
  ├── engine.costs (CostModel)
  ├── engine.ledger (TradeLedger)
  └── engine.models (все модели)
```

---

## 2. Engine — ядро

### 2.1 models.py (105 строк) — типы данных

| Класс | Назначение |
|-------|-----------|
| `Side(BUY, SELL)` | Направление сделки |
| `PositionState(FLAT, LONG, SHORT)` | Состояние позиции |
| `DecisionAction` | 10 вариантов решений policy |
| `ExitReason` | stop_loss, target, signal_exit, session_close, end_of_data |
| `Candle(ts, open, high, low, close, volume)` | frozen dataclass |
| `Signal(strategy_id, side, time, reason, features, kind)` | kind="entry"\|"exit" |
| `ExitPlan(stop_loss, take_profit)` | План выхода |
| `Position(figi, state, qty, entry_time, ...)` | Открытая позиция |
| `Trade(trade_id, ...)` | Завершённая сделка |
| `AuditEntry(index, time, kind, detail)` | Аудит-журнал |

**Зависимости:** только stdlib. Можно тестировать полностью.

### 2.2 strategies.py (199 строк) — стратегии + реестр

| Стратегия | ID | Features |
|-----------|-----|----------|
| `MacdCrossStrategy` | macd_cross | `{"macd", "signal", "hist"}` |
| `DonchianBreakoutStrategy` | donchian_breakout | `{"donchian_high", "donchian_low"}` |

**`STRATEGY_REGISTRY`** — dict[strategy_id → class] (7 стратегий)
**`build_strategy(strategy_id, params)`** — фабрика
**`validate_params(strategy_id, params)`** — валидация по catalog schema

### 2.3 wave1.py (464 строки) — 5 стратегий

| Стратегия | ID | Features | Entry Reasons |
|-----------|-----|----------|---------------|
| RsiReversal | rsi_reversal | `{"rsi", "prev_rsi"}` | rsi_turn_up_oversold, rsi_turn_down_overbought |
| BollingerReclaim | bollinger_reclaim | `{"band_lower", "band_upper"}` | reclaim_lower_band, reclaim_upper_band |
| PullbackEma | pullback_ema | `{"ema_trend", "ema_pull"}` | pullback_resume_up, pullback_resume_down |
| VwapReclaim | vwap_reclaim | `{"vwap", "dev", "sigma"}` | vwap_reclaim_up, vwap_reclaim_down |
| SqueezeBreakout | range_compression_breakout | `{"atr", "atr_rank_pct", "range_high", "range_low"}` | squeeze_breakout_up, squeeze_breakdown |

**Особенность VWAP:** state сбрасывается по сессии (Europe/Moscow).

### 2.4 indicators.py (30 строк)

```python
atr(bars: list[Candle], period: int = 14) -> list[float | None]
```
Wilder ATR. Зависимости: только models.Candle.

### 2.5 exits.py (219 строк) — политики выхода

| Policy | ID | plan_entry | update_stop |
|--------|-----|-----------|-------------|
| `AtrTrailingPolicy` | atr_trailing | ATR-based SL, activation + trail | trailing по ATR |
| `FixedSlTpPolicy` | fixed_sl_tp | SL/TP % от entry | — |
| `AtrStopPolicy` | atr_stop | ATR SL + optional TP + trail | trailActivation + trailDistance |

`AtrStopPolicy` v1.2.0 — **два режима трейлинга**:
- **Комиссионный** (`trail_activation_comm_mult`): активация при `pnl >= comm × mult`; дистанция трейла считается от **чистого ATR** (`trail_distance_atr`), независимо от ширины SL (`multiplier = sl_mult`). Используется ботом (`trail_activation_comm_mult=4.0`, `trail_distance_atr=2.5`).
- **ATR-режим** (`trail_activation_r` / `trail_distance_r`): активация по ATR-порогу move; дистанция `trail_distance_r × risk`. Используется backtest (E5).

```python
intrabar_exit(bar, state, stop_loss, take_profit) -> (price | None, reason | None)
```

### 2.6 policies.py (72 строки) — decision logic

```python
SignalPolicy.decide(signal, state, bars_held) -> (DecisionAction, note)
```

| State | Condition | Result |
|-------|-----------|--------|
| FLAT | signal exists | ACCEPT_ENTRY |
| LONG/SHORT | same side | IGNORE_SAME_SIDE |
| LONG/SHORT | opposite + min_hold not met | REJECT_MIN_HOLD |
| LONG/SHORT | opposite + hold met | ACCEPT_EXIT |

`SignalPolicyConfig`: min_hold_bars, allow_flip, same_side_reentry_cooldown_bars, exit_confirm_window_bars, opposite_hold, confirm_flip

### 2.7 quorum.py (58 строк) — голосование

```python
merge_quorum(member_runs: list[dict], quorum: int) -> (list[Signal], dict)
```

**Алгоритм:**
1. Группировка по `ts` + `side`
2. Если `buy_count >= quorum AND buy_count > sell_count` → BUY signal
3. Аналогично для SELL
4. При равенстве — нет сигнала

**Features в merged signal:**
```python
{
    "votes": n_votes,
    "buy_votes": buy_n,
    "sell_votes": sell_n,
    "members_for": [...strategy_ids],   # Кто голосовал за
    "opposition": [...strategy_ids],    # Кто голосовал против
    "window_bars": 0,                   # Захардкожен!
}
```

**Limitation:** `window_bars=0` — нет multi-bar window quorum.

### 2.8 runner.py (359 строк) — EngineRunner.run()

```
┌─ run(candles) ─────────────────────────────────────┐
│ for each candle:                                    │
│  1. Session management (overnight close, force_flat)│
│  2. Pending signal execution (entry/flip/exit)      │
│  3. Position state update (trailing, intrabar exit)  │
│  4. Signal generation (strategy.on_bar)              │
│  5. Policy.decide(signal, state, bars_held)          │
│  6. Entry gates: cooldown, session, mode, short      │
│  7. Exit flow: exit signals, confirm window          │
│  8. End-of-data: force close                         │
└─────────────────────────────────────────────────────┘
```

**Трейлинг в runner (`EngineRunner.run`):**
- `self._trailing_active` сбрасывается в `False` **в `_open()` на каждый вход** (per-trade, не глобально).
- Активация: `exit_policy.trailing_activated(entry_px, qty, comm, bars)` → `pnl >= comm × 4` → `_trailing_active=True`, `position.target=None` (TP выключается).
- При `_trailing_active` **любой** противоположный сигнал (и `kind=="exit"`, и `kind=="entry"`-flip) игнорируется через `HOLD_TRAILING` — позиция живёт до подтянутого стопа.
- Стоп обновляется каждый бар: `update_stop(...)` ratchet-логика (только в сторону прибыли).

**Важно:** `update_stop` и `trailing_activated` вызываются с `qty=position.qty` и `commission=position.entry_commission` — это сумма, которую в движение считает и бот (`comm = entry_px × qty_sh × rate`). test=bot.

### 2.9 costs.py (27 строк)

```python
CostModel(round_price, fill_price, commission)
```
Defaults: commission_rate=0.0005, slippage_bps=2.0, tick_size=0.01

### 2.10 sessions.py (70 строк)

```python
SessionPolicy.can_enter(ts, tf_minutes) -> (bool, reason)
```
Timezone: Europe/Moscow. Config: open_time, close_time, entry_cutoff_bars, overnight, force_flat_at_session_end.

### 2.11 metrics.py (177 строк)

| Функция | Назначение |
|---------|-----------|
| `summarize(trades)` | trades, wins, losses, WR, PF, expectancy, net |
| `equity_curve(trades, capital)` | Cumulative equity |
| `max_drawdown_pct(curve)` | Max DD % |
| `halves(trades)` | Split by midpoint |
| `by_figi(trades)` | Group by instrument |
| `max_consecutive_losses(trades)` | Worst streak |
| `top1_analysis(by_figi_map)` | Concentration |
| `full_report(trades, capital)` | Everything combined |
| `per_day(trades)` | Daily breakdown |

### 2.12 ledger.py (25 строк)

```python
TradeLedger.log() / add_trade() / fingerprint()
```
Auto-ID: T0001, T0002... SHA256 fingerprint для детерминизма.

### 2.13 orderflow.py (165 строк) — data models

`OrderAction`, `OrderStatus`, `RiskDecision`, `PositionEventType`, `ExitReasonCode`, `OrderIntent`, `Order`, `Fill`, `Decision`, `PositionEvent`

### 2.14 catalog.py (139 строк)

`StrategyCard` metadata + `STRATEGY_CATALOG` (params_schema: type, default, min, max)
`canonical_params_hash(params)` → SHA256 hex.

---

## 3. Services/Ensemble — ансамбль V4

### 3.1 compute_ensemble(candles_1m, req) → dict

```
Полный pipeline:
│
├─ 1. _validate_candles()          ← фильтрация битых свечей
├─ 2. Time filter (from_ts/to_ts)  ← временной фильтр
├─ 3. compute_bias()               ← EMA на hourly, bias dict[bucket → ±1]
├─ 4. generate_signals()           ← для каждой стратегии в setups
├─ 5. merge_quorum()               ← голосование
├─ 6. micro_breakout()             ← breakout на entry TF
├─ 7. Gates:                       ← фильтрация кандидатов
│   ├─ bias gate
│   ├─ vol_gate (rolling_atr_high_only)
│   ├─ pullback_depth
│   ├─ adaptive regime
│   └─ ML filter (MlEnsembleFilter)
├─ 8. ReplayStrategy               ← accepted entries → EngineRunner
├─ 9. EngineRunner.run()           ← backtest
├─ 10. Oracle                      ← zigzag swings
└─ 11. Analytics                   ← MFE/MAE, counterfactual, episodes, quality
```

### 3.2 generate_signals(candles, req) → dict[strategy_id → list[Signal]]

Для каждой стратегии в `req.setups`:
- Создаёт strategy через `build_strategy(id, params)`
- Прогоняет `strategy.on_bar(candles)` для каждого бара
- Возвращает dict[strategy_id → list[Signal]]

### 3.3 resample(candles, tf_seconds) → list[Candle]

OHLCV resampling. Поддерживает: 5m (300s), 15m (900s), 30m (1800s), 1h (3600s).

### 3.4 _validate_candles(candles) → list[Candle]

Фильтрует:
- `open <= 0 or close <= 0`
- `high < low`
- `high < open or high < close`
- `low > open or low > close`
- `volume < 0`

### 3.5 _run_pipeline(candles, req) → dict

Основной pipeline для single-ticker backtest:
1. Generate signals → quorum → micro_breakout → gates
2. Build ReplayStrategy с accepted entries
3. Build EngineRunner (figi, qty, exits, sessions, etc)
4. Run backtest
5. Collect analytics (per-regime, episodes, quality, counterfactual)

---

## 4. Bot — runtime и брокеры

### 4.1 runtime.py (~1,697 строк) — PaperBotRuntime

**Основной цикл:**
```
start() → _run() → CandleFeed stream → _process_candle(c)
```

**_process_candle(c) — порядок действий:**
1. Buffer candle (figi_buffers) — 1m
2. Check circuit breaker (daily loss limit)
3. Выход: `_step_exit(c)` (SL/TP/trailing по 1m, собственный учёт — см. §10)
4. Trailing stop update (комиссионная активация, ratchet)
5. Overnight close check
6. Strategy signal (ensemble or single)
7. Policy decide
8. Entry gates (cooldown, session, margin)
9. Execute order (submit_order → _execute_pending → fill)

**Ключевые методы:**
- `_submit_order()` — sizing: `qty = max(1, int(budget / (lot_cost / lev)))` лотов; budget = min(equity×20%, free_cash); при марже `own_per_lot = lot_cost / lev`
- `_execute_pending()` — fill с exit plan (ATR or Fixed), `sl_mult` из optuna `strat.p.sl_mult`
- `_step_exit()` — выход: активация трейлинга `pnl >= comm×4`, update_stop, intrabar_exit (см. §10)
- `_ensure_exit_state()` — lazy-инициализация exit state (позиция без плана после рестарта)
- `_clear_exit_state()` — сброс state при закрытии
- `close_all()` — kill switch
- `set_entries_paused()` — manual pause
- `_sync_held()` — reconciliation (every 30s)
- `_hot_add_universe()` — hot-add new eligible tickers (every 60s)
- `_reconcile_loop()` — StreamManager vs broker
- `_flush_persist()` — batch candle persistence to DB
- `_st_update_sl()` — персистенс стопа/TP/трейлинга в `sandbox_trades`

### 4.2 ensemble_strategy.py (155 строк) — EnsembleV4Strategy

**V2_SETUPS** — 7 стратегий с параметрами:
```python
{
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}
```

**on_bar(candles) → Signal | None:**
1. `compute_ensemble(candles_1m, req)`
2. Take last fresh entry (FRESH_MIN=30 min)
3. Return `Signal(kind="entry")` with meta (quorum_event, setups)

**SESSION_WINDOWS:** morning=06:50-09:50, day=09:50-18:45, evening=19:05-23:50

### 4.3 live_broker.py (343 строки) — T-Investments

Methods: ensure_account, cash, positions, get_position, open_position, close_position, get_max_lots, update_protective_levels (no-op)

### 4.4 paper_broker.py (149 строк) — DB-backed

Methods: ensure_account, reset, positions, get_position, open_position, close_position, update_protective_levels, trades_history

### 4.5 feed.py (134 строки) — CandleFeed

Modes: `_stream_grpc()` (primary), `_polling()` (REST fallback)

### 4.6 stream_manager.py (417 строк) — gRPC streams

Positions, trades, orders streams. Reconnect with exponential backoff.

---

## 5. Тесты — что покрыто

### 5.1 Тесты движка (отличное покрытие)

| Файл | Строк | Тестирует | Тип |
|------|-------|----------|-----|
| `test_engine_golden.py` | 185 | Golden path: LONG target, SHORT stop, gap-through, same-bar, same-side, opposite-signal, min_hold, costs, deterministic replay | Unit, synthetic |
| `test_engine_reentry.py` | 190 | Re-entry cooldown, exit confirm, opposite_hold, confirm_flip, trailing stop | Unit, synthetic |
| `test_sessions_donchian.py` | 169 | Session policy: cutoff, weekend, force_close, daily TF, Donchian breakout | Unit, synthetic |
| `test_wave1_signals.py` | 216 | Все 5 wave-1 стратегий + validate_params + build_strategy | Unit, synthetic |
| `test_metrics.py` | 80 | summarize, equity_curve, max_drawdown, halves, by_figi, full_report | Unit |
| `test_bot_session_risk.py` | 58 | Session state, RiskSnapshot, EventLog, BotOrder | Unit |
| `test_stream_manager.py` | 239 | StreamManager: dataclasses, lifecycle, status mapping, mock gRPC | Unit, mock |
| `test_audit_engine.py` | 261 | Audit: slippage, reprice, session_at, funnel, intrabar, qty sizing, contention, lifecycle replay | Unit |

### 5.2 Тесты (хорошее покрытие)

| Файл | Строк | Тестирует |
|------|-------|----------|
| `test_b4run.py` | 115 | entry_pullback_depth gate |
| `test_oracle_coverage.py` | 111 | Oracle: geometric vs causal, accepted/executed, funnel |

### 5.3 Shadow тесты (read-only, требуют reports/*.json)

| Файл | Строк | Тестирует |
|------|-------|----------|
| `test_research_pack.py` | 311 | 10+ требований: read-only, oracle fields, funnel, costs, qty, MSK timezone, quorum |
| `test_entry_confluence.py` | 63 | B3 entry confluence: quorum reconciliation |
| `test_entry_exit_vote_behavior.py` | 72 | B5b vote behavior: direction, strength, dynamics |
| `test_entry_quality.py` | 48 | B4 entry quality: features point-in-time |
| `test_exp002b.py` | 45 | EXP-002b: config immutable, criteria, review MD |
| `test_imoex_context_attribution.py` | 64 | B2 IMOEX: group counts, contemporaneous |
| `test_imoex_stop_concept.py` | 51 | B2b IMOEX stop concept |
| `test_imoex_stop_fill.py` | 51 | B2b-II IMOEX stop fill |
| `test_imoex_trend_attribution.py` | 53 | B5 IMOEX trend attribution |
| `test_macro_regime_attribution.py` | 64 | H-036 macro regime |
| `test_vote_attribution.py` | 206 | Vote attribution: no future trades, shrinkage, walk-forward |
| `test_time_of_day_attribution.py` | 79 | B1 time-of-day buckets |

### 5.4 Сводка покрытия

| Область | Тесты | Покрытие |
|---------|-------|----------|
| Engine core (models/strategies/indicators) | 4 файла | **Отличное** |
| Engine exits/policies/quorum | 3 файла | **Хорошее** |
| Engine runner/sessions/costs | 3 файла | **Хорошее** |
| Engine metrics/ledger | 1 файл | **Среднее** |
| Services/ensemble | 2 файла (smoke only) | **Слабое** |
| Bot runtime | 1 файл (session/risk) | **Слабое** |
| Bot brokers/feed/stream | 1 файл (stream) | **Среднее** |
| Scripts (backtests) | 0 | **Нет** |

---

## 6. Известные баги и статус исправлений

> **Обновлено:** 2026-09-05 (после сессии исправлений)

| # | Файл | Строка | Описание | Статус |
|---|------|--------|----------|--------|
| 1 | `scripts/run_bot_engine_backtest.py` | — | Неправильные FIGIs — загружает свечи других акций | **АКТИВЕН** (низкий приоритет, скрипт не используется в live) |
| 2 | `bot/live_broker.py` | 103, 107, 111 | NameError: `code`, `metadata`, `RESOURCE_EXHAUSTED` не определены | **FIXED** (строки-литералы `"code"`, `"metadata"`, `"RESOURCE_EXHAUSTED"`) |
| 3 | `ensemble.py` | 961 | `len(member_runs)` → `len(setup_runs)` | **FIXED** |
| 4 | `quorum.py` | 48 | `window_bars=0` захардкожен — нет multi-bar window quorum | **BY DESIGN** (расширять при необходимости) |
| 5 | `ensemble.py _run_pipeline` | — | Нет unit-тестов на основной pipeline | **АКТИВЕН** (нужны тесты) |
| 6 | `runtime.py` | 807-808 | `resample5u` вызывается на каждом баре для trailing | **FIXED** (кэширование: обновление раз в 5 баров через `_get_5m_bars()`) — **заменено** на 1m stepping в сессии 2026-09-08 (§10) |
| 7 | `exits.py` AtrTrailingPolicy | `update_stop` | `len(bars) < 3` возвращает current_stop без обновления | **FIXED** (acceptable design — ATR не определён на <3 барах) |
| 8 | `ensemble.py` | 1085 | Логирование через `print()` вместо logging | **FIXED** (`logger.warning()`) |
| 9 | Shadow тесты | — | Зависят от `reports/*.json` — если удалены, падают | **АКТИВЕН** (нужны фикстуры) |
| 10 | regime/ml | — | Нет unit-тестов для `RegimeDetector`, ML filter | **АКТИВЕН** (нужны тесты) |
| 11 | `compute_ensemble` | — | Нет интеграционных тестов для полного pipeline | **АКТИВЕН** (нужны тесты) |
| 12 | brokers | — | PaperBroker/LiveBroker — нет unit-тестов | **АКТИВЕН** (нужны mock тесты) |
| 13 | session logic | — | Дублирование в `runtime.py` и `ensemble_strategy.py` | **FIXED** (общий `is_session_active()` в `engine/sessions.py`) |
| 14 | `V2_SETUPS` | — | Захардкожены, дублируют параметры из AGENTS.md | **АКТИВЕН** (low priority) |
| 15 | `runtime.py` | SL/TP | Бот читал `stop_loss/take_profit` из пустых полей T-Invest (`ServerPosition.entry_price=0`, `LiveBroker positions stop_loss=None`) → стопы/трейлинг считались на нулевых данных | **FIXED** (собственный учёт выходов: `_exit_plans/_trail_stop/_exit_target`, персистенс в `sandbox_trades`, restore при старте) — см. §10 |
| 16 | `runtime.py` | 5m→1m | Trailing/SL ATR считался на 5m ресемпле, а бэктест (`EngineRunner.run`) — на 1m → несовпадение дистанций | **FIXED** (все ATR для выходов на 1m `buffers`, ресемпл убран) — см. §10.6 |
| 17 | `runner.py` | `_trailing_active` | Флаг был глобальный (не сбрасывался между сделками) → активация трейлинга в одной сделке «протекала» в следующую | **FIXED** (сброс в `_open()`, per-trade) — см. §10.3 |
| 18 | `exits.py` | v1.2.0 | Дистанция трейла считалась `trail × risk = trail × ATR × sl_mult` → при sl_mult=4 стоп почти не двигался | **FIXED** (в comm-режиме дистанция от чистого ATR) — см. §10.4 |

---

## 7. Как писать новые тесты

### 7.1 Конфигурация

```ini
# pyproject.toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
```

Тесты запускаются: `pytest tests/ -v`
Один тест: `pytest tests/test_engine_golden.py::test_long_target -v`

### 7.2 Структура теста движка

```python
"""Шаблон теста EngineRunner"""

from app.engine.models import Candle, Side
from app.engine.runner import EngineRunner, EngineConfig
from app.engine.strategies import build_strategy
from app.engine.exits import AtrStopPolicy
from app.engine.policies import SignalPolicy
from app.engine.sessions import SessionPolicy
from app.engine.costs import CostModel

def make_candles(prices: list[float]) -> list[Candle]:
    """Хелпер для генерации synthetic candles."""
    candles = []
    for i, p in enumerate(prices):
        candles.append(Candle(
            ts=1_700_000_000 + i * 60,
            open=p * 0.999, high=p * 1.005,
            low=p * 0.995, close=p, volume=1000
        ))
    return candles

def test_basic_long_entry():
    candles = make_candles([100]*20 + [101, 102, 103])
    strategy = build_strategy("rsi_reversal", {"period": 14, "oversold": 30, "overbought": 80})
    cfg = EngineConfig(
        figi="TEST", qty=1, mode="long",
        strategy=strategy,
        exit_policy=AtrStopPolicy(atr_mult=2.0),
        signal_policy=SignalPolicy(),
        session_policy=SessionPolicy(),
        cost_model=CostModel(commission_rate=0.0005, slippage_bps=0),
    )
    runner = EngineRunner(cfg)
    result = runner.run(candles)
    assert result.trades  # there should be trades
```

### 7.3 Структура теста ensemble (smoke)

```python
"""Шаблон теста compute_ensemble"""

from app.services.ensemble import compute_ensemble

def test_ensemble_smoke():
    candles = [Candle(ts=..., open=100, high=101, low=99, close=100.5, volume=1000)] * 100
    req = {
        "setups": ["rsi_reversal", "macd_cross"],
        "params": {"rsi_reversal": {"period": 14, ...}, ...},
        "quorum": 2,
        "interval": "1m",
        "figi": "TEST",
        "qty": 1,
    }
    result = compute_ensemble(candles, req)
    assert "trades" in result
    assert "quorum_funnel" in result
```

### 7.4 Паттерны для тестирования

| Что | Как | Пример |
|-----|-----|--------|
| Strategy signal | Synthetic candles с трендом | `make_candles([100]*20 + [101, 102])` |
| Policy decision | Mock Signal + Position state | `SignalPolicy().decide(sig, pos, 0)` |
| Exit policy | Single position + bars | `policy.plan_entry(Side.BUY, 100, atr)` |
| Quorum | Synthetic member_runs | `merge_quorum([{...}, {...}], quorum=2)` |
| Session | Specific datetime | `SessionPolicy.can_enter(ts, 1)` |
| Metrics | Synthetic trades | `summarize([trade1, trade2])` |
| Full pipeline | compute_ensemble с synthetic data | `compute_ensemble(candles, req)` |

### 7.5 Важные правила

1. **Все тесты — synthetic data.** Не取决于 реальных данных из БД или API.
2. **pytest fixtures** для повторяющихся объектов (candles, strategies).
3. **Parametrize** для тестирования разных parameter sets.
4. **Deterministic** — одинаковый input → одинаковый output (seed, fixed timestamps).
5. **Изоляция** — каждый тест независим, не зависит от порядка запуска.

---

## 8. Валидация и оптимизация движка

> **Обновлено:** 2026-09-05

### 8.1 Валидация — как проверять движок

#### Детерминизм (replay-тест)

Цель: при одинаковых входных данных → одинаковые сделки.

```python
def test_deterministic_replay():
    candles = load_synthetic_candles()
    result1 = compute_ensemble(candles, req)
    result2 = compute_ensemble(candles, req)
    assert result1["trades"] == result2["trades"]
    assert result1["analytics"]["equity_curve"] == result2["analytics"]["equity_curve"]
```

Если есть расхождения → недетерминизм (рандом, порядок словарей, timezone).

#### Golden-тесты (известные сценарии)

Уже покрыты в `test_engine_golden.py`:
1. LONG target hit ✅
2. SHORT stop loss ✅
3. Gap-through-stop ✅
4. Same-bar entry/exit ✅
5. Same-side re-entry (cooldown) ✅
6. Opposite signal (flip) ✅
7. Min hold ✅
8. Costs ✅

#### Edge cases (нужны тесты)

| Сценарий | Ожидаемое поведение |
|----------|-------------------|
| 0 свечей | `compute_ensemble` не падает, пустой результат |
| 1 свеча | 0 сделок (не хватает баров для индикаторов) |
| Битые свечи (high<low, open<=0) | `_validate_candles` фильтрует, нет крэша |
| 10 убыточных подряд | Нет крэша, DD считается |
| 20 позиций одновременно | Лимиты маржи соблюдаются |
| Переход через сессию | `overnight_close`, `force_flat` работают |

#### Интеграционные тесты (нужны)

```python
def test_full_pipeline():
    candles = load_real_candles(figi="BBG004730N88", days=1)  # SBER
    result = compute_ensemble(candles, req)
    assert "trades" in result
    assert "analytics" in result
    assert isinstance(result["trades"], list)
```

### 8.2 Оптимизация — скорость

#### Текущие узкие места

| Место | Проблема | Решение | Статус |
|-------|----------|---------|--------|
| `runtime.py` trailing | `resample()` на каждом 1m баре — O(n) | Кэширование 5m (`_get_5m_bars()`) | **FIXED** |
| `compute_ensemble()` | Пересчёт всего с нуля на каждом баре | Streaming-версия с состоянием | **Нужно** |
| 20 акций последовательно | Нет параллелизма | `multiprocessing.Pool` по FIGI | **Нужно** |
| Индикаторы (RSI, MACD) | Пересчёт O(N) на каждом баре | Инкрементальное кэширование | **Нужно** |

#### Профилирование

```bash
# cProfile
python -m cProfile -o profile.stats scripts/backtest_v2.py
python -c "import pstats; pstats.Stats('profile.stats').sort_stats('cumtime').print_stats(20)"

# py-spy (на running процессе)
py-spy top --pid <PID>
```

Что искать: функции с最大的 `cumtime`, вызовы上千 раз.

#### Кэширование индикаторов (план)

```python
class RsiCache:
    def __init__(self, period=14):
        self.gains = []
        self.losses = []

    def update(self, close: float) -> float | None:
        # Инкрементальный RSI — O(1) вместо O(N)
        ...
```

Эффект: RSI/MACD/ATR вместо O(N) → O(1) на бар.

#### Параллелизм по FIGI (план)

```python
from multiprocessing import Pool

def backtest_figi(args):
    figi, candles, req = args
    return compute_ensemble(candles, req)

with Pool(processes=4) as pool:
    results = pool.map(backtest_figi, [(f, candles[f], req) for f in figis])
```

Эффект: ~3-4x на 4 ядрах.

#### Streaming-версия compute_ensemble (план)

```python
class EnsembleState:
    def __init__(self, req):
        self.indicator_cache = {}
        self.signal_cache = {}

    def update(self, candle: Candle) -> Signal | None:
        # Обновляем индикаторы инкрементально
        # Обновляем сигналы
        # Возвращаем Signal (если есть)
        ...
```

Эффект: O(1) вместо O(N) на бар.

### 8.3 Оптимизация — качество

#### Анализ «почему нет сделок»

Добавить в `runtime.py` логирование `NO_TRADE`:

| Причина | Действие |
|---------|----------|
| `quorum_not_met` доминирует | Снизить кворум или добавить стратегии |
| `session_filter` доминирует | Расширить сессии |
| `cooldown` доминирует | Снизить cooldown |
| `margin_exceeded` | Увеличить capital или снизить qty |

#### Анализ «почему выход»

Агрегировать `exit_meta.reason`:

| Причина | Действие |
|---------|----------|
| 90% `stop_loss` | SL слишком узкий — увеличить ATR multiplier |
| 90% `target` | TP слишком близкий — увеличить risk/reward |
| Много `signal_exit` | Стратегии часто меняют мнение — стабилизировать |

#### Per-dimension анализ

| Dimension | Что смотреть |
|-----------|-------------|
| Per-strategy | Какая стратегия даёт больше прибыльных сделок |
| Per-ticker | Какие акции лучше/хуже для ансамбля |
| Per-session | Morning vs day vs evening — когда больше прибыли |
| Per-time-of-day | Утро, день, вечер — распределение сделок |
| Per-MFE/MAE | Max favorable/adverse excursion — оптимизация SL/TP |

---

## 9. Quick Reference — запуск тестов и профилирование

```bash
# Все тесты
pytest tests/ -v

# Один файл
pytest tests/test_engine_golden.py -v

# Один тест
pytest tests/test_engine_golden.py::test_long_target -v

# С coverage
pytest tests/ --cov=app.engine --cov-report=term-missing

# Только shadow тесты
pytest tests/ -k "imoex or vote_attribution or time_of_day or research_pack" -v

# Пропустить shadow (требуют reports/*.json)
pytest tests/ --ignore=tests/test_research_pack.py --ignore=tests/test_entry_confluence.py -v

# Профилирование (cProfile)
python -m cProfile -o profile.stats scripts/backtest_v2.py
python -c "import pstats; pstats.Stats('profile.stats').sort_stats('cumtime').print_stats(20)"

# Профилирование (py-spy, на running процессе)
py-spy top --pid <PID>
```
---

## 10. SL / TP / Trailing Stop — единый механизм (сессия 2026-09-08)

> **Цель:** test=bot — бот вживую использует ту же логику стопов/трейлинга, что и движок в бэктесте.

### 10.1 Проблема (до фикса)

Бот (`runtime.py`) вычитывал `stop_loss`, `take_profit`, `entry_price` из объектов позиций
T-Invest (`StreamManager.ServerPosition` / `LiveBroker.positions()`):
- `ServerPosition` — `entry_price` всегда `0.0`, нет полей SL/TP
- `LiveBroker.positions()` — `stop_loss=None, take_profit=None`

**Итог:** стопы/трейлинг в боте считались на пустых данных. Вход «с нуля» давал ATR от
текущего бара (не от реального входа), SL не защищал от разворота, трейлинг не активировался
или активировался на неправильном расстоянии.

### 10.2 Архитектура решения

Вместо чтения из позиции брокера — **собственный учёт** в `runtime.py`:

```
┌─ Execute entry ─────────────────────────────────────────┐
│ _execute_pending → order на биржу                       │
│ _exit_plans[figi] = AtrStopPolicy(sl_mult, rr, ...)    │
│ _exit_side[figi]   = "BUY" | "SELL"                     │
│ _exit_entry_px[figi] = fill_price                       │
│ _exit_qty[figi]    = order.qty × lot (штуки)            │
│ _trail_active[figi] = False                             │
│ _trail_stop[figi]  = plan.stop_loss                     │
│ _exit_target[figi] = plan.take_profit                   │
└─────────────────────────────────────────────────────────┘

┌─ Every candle ──────────────────────────────────────────┐
│ _step_exit(figi, candle, pos):                          │
│  1. comm = entry_px × qty_sh × commission_rate          │
│  2. if trail_active:                                    │
│       → update_stop (ratchet only)                      │
│  3. elif trailing_activated(pnl >= comm×4):             │
│       → activate trail, disable signal exits & TP       │
│  4. intrabar_exit(bar, stop_loss, take_profit)          │
│  5. if triggered: broker.close → _clear_exit_state()    │
└─────────────────────────────────────────────────────────┘

┌─ Restart / lazy init ───────────────────────────────────┐
│ _startup: restore from sandbox_trades (DB):             │
│   entry_price, stop_loss, take_profit, trailing_active  │
│ _ensure_exit_state: fallback plan_entry from 1m buffer  │
└─────────────────────────────────────────────────────────┘
```

### 10.3 Ключевые файлы

| Файл | Изменения |
|------|-----------|
| `engine/exits.py` | `AtrStopPolicy.update_stop()`: commission-режим дистанции считает от **чистого ATR** (не `ATR×sl_mult`). Комментарий v1.2.0 |
| `engine/runner.py` | `_trailing_active = False` сбрасывается **per-trade** в `_open()`. `HOLD_TRAILING` блокирует **любой** противоположный сигнал (и exit, и entry), не только exit-kind |
| `bot/runtime.py` | `_step_exit()`, `_clear_exit_state()`, `_ensure_exit_state()`, exit state dicts, DB persistence |
| `bot/runtime.py` | `_startup`: restore из `sandbox_trades` (entry, sl, tp, trailing_active) вместо рекомпьюта плана |
| `bot/runtime.py` | `_execute_pending`: `sl_mult` из optuna `strat.p.sl_mult`, не из `cfg.initial_sl_atr` |
| `bot/runtime.py` | ATR для стопа/трейлинга считается на **1m** барах (buffers), не 5m |
| `bot/runtime.py` | `_process_candle`: stepping по `self.buffers` (1m) вместо `self.candle_cache_5m` |
| `bot/runtime.py` | `_open_position_from_order`: тестовая запись с `trail_active=False` |
| `models/sandbox_trade.py` | Добавлено поле `trailing_active: bool` (DB column) |
| `api/routes/sandbox.py` | `/positions` читает `stop_loss`, `trail_active`, `take_profit` из runtime state dicts |

### 10.4 Механика трейлинга

**Активация:** `pnl >= comm × trail_activation_comm_mult`
```
comm    = entry_price × qty_sh × commission_rate  (0.05% × notional)
pnl     = (close − entry) × qty_sh                (для BUY)
порог   = 4 × comm = 0.2% от цены (независимо от qty/плеча)
```
При марже: `qty` больше за счёт leverage → `comm` и `pnl` пропорциональны `qty` → `qty` сокращается → порог = **фиксированный % от цены**.

**Дистанция трейла:** `trail_distance_atr × ATR(1m)` (по умолчанию 2.5×ATR, настраивается через `trail_distance_atr`).
- В **comm-режиме** (бот): дистанция от **чистого ATR** (не `risk = ATR × sl_mult`)
- В **ATR-режиме** (backtest E5): дистанция от `risk` (ATR × multiplier)

**Поведение после активации:**
1. Стоп следет за ценой (ratchet: только в сторону прибыли)
2. Сигнальные выходы (opposite signal) **блокируются** — позиция живёт до стопа
3. Take-profit **отключается**
4. Нет SL на графике при неактивированном трейлинге (комментарий: "SL deprecated, trail_stopped")

### 10.5 Тесты

Все golden-тесты проходят (30 passed):

| Тест | Сценарий |
|------|----------|
| `test_trailing_block_exit_after_activation` | После активации трейлинга exit-сигналы игнорируются, позиция закрывается только по стопу |
| `test_trailing_blocks_opposite_signal` | Противоположный entry-сигнал блокируется, пока трейлинг активен |

### 10.6 ATR timeframe: 1m vs 5m

| Параметр | Было (бот) | Стало (бот) | Бэктест (runner) |
|----------|-----------|-------------|-------------------|
| ATR для SL | 5m resample | **1m** (buffers) | 1m (candles) |
| ATR для trailing | 5m resample | **1m** (buffers) | 1m (candles) |
| Порог активации | 5m close | **1m** close | 1m close |
| Ресемпл | `resample(candles, 300)` | **не используется** | — |

**Причина:** в бэктесте `EngineRunner.run(candles)` получает **1m свечи**, ATR считается по ним.
До фикса бот использовал 5m → ATR был в ~√5× больше → SL шире, трейлинг активировался
раньше, дистанция трейла была другой. После фикса test=bot.

### 10.7 Персистентность (per-trade)

| Ключ | Тип | Описание |
|------|-----|----------|
| `_exit_plans[figi]` | ExitPolicy | Политика (ATR/Fixed) |
| `_exit_side[figi]` | str | "BUY" / "SELL" |
| `_exit_entry_px[figi]` | float | Цена входа |
| `_exit_qty[figi]` | int | Штуки |
| `_trail_active[figi]` | bool | Трейлинг активирован |
| `_trail_stop[figi]` | float | Текущий стоп |
| `_exit_target[figi]` | float | TP (None после активации) |

При рестарте читает из `sandbox_trades` (entry_price, stop_loss, take_profit, trailing_active).
Сохранение при каждом `_step_exit` через `_st_update_sl()`.

---

## 11. Журнал багов и находок (2026-09-06)

### 11.1 БАГ: is_session_active вызывался через self (исправлен)

**Файл:** `backend/app/bot/ensemble_strategy.py`

**Симптом:** EnsembleV4Strategy.on_bar() молча не генерировал сигналы → бот/бэктесты давали 0 сделок. В runtime исключение ловилось `except Exception: sig = None`, поэтому ошибка была невидима.

**Причина:** импорт `from app.engine.sessions import is_session_active as _is_session_active` стоял ВНУТРИ тела класса (строка ~52), а вызов шёл как `self._is_session_active(last.ts, sessions)`. При вызове через `self.` Python подставляет `self` первым аргументом → `TypeError: takes from 1 to 2 positional arguments but 3 were given`.

**Фикс:** импорт перенесён внутрь `on_bar()`, вызов без `self`:
```python
def on_bar(self, candles):
    from app.engine.sessions import is_session_active
    ...
    if not is_session_active(last.ts, self.p.sessions):
        return None
```

**Затронуто:** live/песочница бот, backtest_full_bot.py, backtest_v2.py, bt_portfolio_margin.py. После фикса бэктест с маржой начал торговать (227 сделок).

**Урок:** импорты внутри тела класса + вызов через `self.` = TypeError. Не ловить такие исключения молча (`except Exception: sig=None` маскирует баги). Добавлять логирование.

### 11.2 НАБЛЮДЕНИЕ: confirm_flip в compute_ensemble != confirm_flip в боте

- В `compute_ensemble` (ensemble.py:742) `confirm_flip` приводится к **bool** (`bool(req.get("confirm_flip", False))`), а `exit_confirm_window_bars=0`. Итог: `confirm_flip=True` при нулевом окне = **мгновенный переворот** на каждый встречный сигнал (runner.py:180) — агрессивный скальпер, 93.8% сделок = signal_exit.
- В runtime бота `confirm_flip: int` = **счётчик N встречных сигналов перед закрытием** (runtime.py:939 `_opposite_count`). Это РАЗНЫЕ механики.
- Тесты с `confirm_flip=2` через compute_ensemble проверяли НЕ то, что делает бот. Учтено в Серии 5 roadmap.

### 11.3 НАБЛЮДЕНИЕ: RegimeDetector не влияет на решения без adaptive

- RegimeDetector считается на каждом 5m баре (ensemble.py:1135) но используется только в `adaptive` (выключен по умолчанию) и в RegimeExitPolicy (только если adaptive). Без adaptive = чистый расход CPU. Учтено в Серии 5 (этап 5.0-5.4).

### 11.4 РЕЗУЛЬТАТ: портфельный бэктест с маржой (real leverage, 0.05%)

**Скрипт:** `backend/scripts/bt_portfolio_margin.py` (копия backtest_full_bot.py + per-side leverage)
**Конфиг:** top-10 (SMLT, NLMK, ASTR, MAGN, GMKN, NVTK, SNGSP, GAZP, LENT, CHMF), 10К общий пул, 10 акций одновременно (merged timeline), comm 0.05%, ATR 4/4, flip=2, leverage per-side из instrument_margin (long_lev/short_lev), август 2026.

| Метрика | Значение |
|---------|----------|
| Trades | 227 |
| Net PnL | -3,634₽ |
| Final cash | 6,366₽ (из 10 000) |
| Return | -35.6% |
| Win rate | 20.7% |
| PF | 0.26 |
| Gross win | +1,259₽ |
| Gross loss | -4,818₽ |
| Total comm | 148₽ |
| Skipped held | 15,387 |

**Per-ticker net:**
| Ticker | Trades | WR% | Net |
|--------|-------:|----:|----:|
| CHMF | 21 | 33% | +98.87 |
| NVTK | 21 | 38% | +89.55 |
| MAGN | 29 | 17% | +0.75 |
| NLMK | 20 | 25% | -2.39 |
| GAZP | 18 | 17% | -9.33 |
| ASTR | 18 | 22% | -21.68 |
| SMLT | 32 | 16% | -199.83 |
| SNGSP | 27 | 11% | -243.01 |
| LENT | 26 | 15% | -432.92 |
| GMKN | 15 | 20% | -2,839.29 |

**Выводы:**
1. Портфельный одновременный прогон РАДИКАЛЬНО отличается от изолированного per-ticker (compute_ensemble): общий пул 10К/10 акций → бюджет ~1000₽/акцию + одновременность меняет картину.
2. Убыток почти целиком от GMKN (-2,839₽ из -3,634₽ = 78%). GMKN avg -189₽/сделку — аномалия (нужна проверка: крупные лоты × проскальзывание, или плечо 6.2x).
3. WR 11-38% — низкий. В изолированном тесте WR был 59-66%. Причина: в портфельном бэктесте используется другой конвейер (EnsembleV4Strategy → compute_ensemble на лету + broker), другие сигналы/тайминги.
4. Сравнение «10К на акцию изолированно» vs «10К пул на 10 акций» — НЕ эквивалентны. Это разные режимы (см. 5.7 roadmap).

**Нужна проверка GMKN-аномалии:** 15 сделок, средний убыток -189₽ при цене ~2900₽, lot=10. Возможно: qty расчёт с плечом дал крупную позу, а SL по ATR×4 на волатильном GMKN пробивал глубоко. ИЛИ проскальзывание на открытии. Смотреть сделки GMKN детально.
