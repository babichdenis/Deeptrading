# SCRIPTS_INDEX.md — Реестр скриптов бэкенда (сентябрь 2026)

> **Назначение:** не изобретать заново. ~180 скриптов в `backend/scripts/`.
> Категории по назначению. Машины: .3 = основной (Mac, база тут), .5 = Windows (та же база).

---

## 1. БЭКТЕСТ-ДВИЖКИ (как тестировать)

| Скрипт | Что делает | Когда использовать |
|--------|-----------|-------------------|
| `compare_engines.py` | OLD (compute_ensemble) vs NEW (runtime._process_candle) | Сравнить пути исполнения |
| `compare_v4.py` | То же, session=main vs all | Паритет движков |
| `backtest_v2.py` | **РЕАЛЬНЫЙ runtime._process_candle + FakeDatetime** (no code dup) | Self-test движка на истории |
| `backtest_full_bot.py` | Копия логики бота (свой BacktestBroker, merged timeline) | Портфельный merged-прогон (медленный) |
| `portfolio_optuna_backtest.py` | То же + per-ticker optuna_params из БД, budget=20%, CLI (--neutral/--period/--pos-pct) | Портфель с оптимизированными параметрами |
| `run_bot_engine_backtest.py` | Canonical V4 через движок (compute_ensemble → trades) | ⚠️ **FIGIs НЕПРАВИЛЬНЫЕ** (см. AGENTS) |
| `engine_baseline_oos.py` | EngineRunner entry-only replay на OOS | Baseline проверка |

## 2. ПОРТФЕЛЬНЫЕ СИМУЛЯЦИИ (общий пул)

| Скрипт | Модель |
|--------|--------|
| `run_pool_backtest.py` | Общий пул 10K на 5 акций, сделки по времени входа |
| `run_compound_pool.py` | **Компаундинг: 20% от текущего equity на позицию** |
| `run_pool_risk_limited.py` | Риск-лимит: доля капитала на позицию |
| `bt_portfolio_margin.py` | Реальная маржа (per-side leverage), merged timeline |
| `compare_50k.py` | 5×10k independent vs 50k pool (20% each) |
| `compare_capital.py` / `compare_v2.py` | 10000 vs 2000 на акцию |
| `portfolio_merge.py` | **Сборка per-ticker сделок (jsonl) на общий пул, 20%, LONG+SHORT** (новый) |
| `per_ticker_dump.py` | Дамп сделок compute_ensemble в jsonl для portfolio_merge (новый) |
| `volume_exhaustion_stats.py` | Шаг 1 Volume Exhaustion: «сигнал на входе → side/exit_reason», delta WR/Net по V1–V5. Прогон Jul/Aug на 10 тикерах → §11 роадмапа |
| `volume_gate_ab.py` | Шаг 2 Volume Exhaustion: A/B gate (require/block) vs baseline → §12 (gate не даёт edge) |
| `volume_vote_ab.py` | Шаг 3: volume-стратегии как голоса кворума (volume_drop/climax/divergence) |
| `score_gate_ab.py` | Series 3: A/B score_gate по порогам (gate режет Net) → §23.1 |
| `score_binning.py` | Series 3: бининг сделок по score/компонентам (calibration check, нет монотонности) → §23.2-23.3 |

## 3. OPTUNA

| Скрипт | Что делает |
|--------|-----------|
| `optuna_sweep_week.py` | Пилот v1: глобальные параметры (SL/TP/quorum/vol/inc) на w1, валид w2 |
| `optuna_sweep_params.py` | v2: + параметры стратегий (MACD/RSI/Boll и т.д.), валид w2+w3. **60 trials → 20 тикеров OOS +13.8%** |
| `optuna_matrix.py` | Матрица с параметрами из БД (5 конфигов × тикер) |
| `save_optuna_to_db.py` | Результаты → `instruments.optuna_params` (JSONB) |
| `test_params.py` | ATR 2.0 vs 4.0 при comm=0.05% |

## 4. СЕРИЯ 5 (s5_*)

| Скрипт | Что делает |
|--------|-----------|
| `s5_diag.py` | 5.0: диагностика flip по режимам |
| `s5_neutral_gate.py` | 5.1: NEUTRAL-gate + semi-flip (тесты A,B,C) |
| `s5_diagnostics.py` | Полная диагностика: regime/votes/indicators per trade |
| `s5_matrix.py` | Матрица 6 тестов (volume + semi/full flip + quorum) |
| `s5_matrix_full.py` | То же + полные таблицы (per-ticker, per-regime, votes) |

## 5. ИССЛЕДОВАНИЯ (SHADOW, read-only) — H-номера

| Группа | Скрипты | Что изучают |
|--------|---------|-------------|
| Атрибуция сделок | `vote_attribution.py`, `entry_confluence.py`, `entry_exit_vote_behavior.py`, `entry_quality.py`, `time_of_day_attribution.py` | Голоса/качество/время входов canonical July |
| IMOEX-контекст | `imoex_context_attribution.py`, `imoex_stop_concept.py`, `imoex_stop_fill.py`, `imoex_trend_attribution.py` | Режим/стоп по IMOEX |
| Макро-режим | `macro_regime_attribution.py`, `_v2.py`, `_v3.py` | Brent/Gold/USD-RUB факторы |
| Индикаторы | `batch_block_A..E.py`, `p2_5_indicator_attribution.py`, `run_ensemble_vortex_adx*.py`, `diag_vortex.py` | Vortex/ADX/VFI и др. |
| ML | `h057_extract.py`, `h057_train.py`, `h058_h059_significance.py`, `h_label_dataset.py`, `h_train_ml.py`, `ml_train_csv.py`, `run_v4_mltest.py` | Meta-model / triple-barrier |
| Аллокация | `batch_block_E.py`, `_E3.py`, `h081_leverage.py`, `h082_pyramiding.py`, `h083_hrp.py` | Capital allocation |
| Эксперименты (P0/P1) | `run_exp002.py`, `run_exp002b.py`, `run_exp003a.py`, `run_exp_e1.py`, `run_exp_e2.py`, `run_exp_e5_trailing.py`, `run_exp_regime_gated.py`, `run_b4run*.py`, `run_session_expand.py` | Vol-gate/RSI-remove/signal_exit/cooldown/trailing/regime-gated |
| Режимы | `run_vol_regime_h1.py`, `regime_diagnostic.py`, `diag_regime.py`, `sector_correlation_shadow.py`, `regime_calibration.py`, `regime_calibration_dataset.py`, `regime_v2_axis_validation.py` | Vol-regime анализ; `regime_calibration*` — read-only диагностика/forward-датасет canonical RegimeDetector; `regime_v2_axis_validation.py` — OOS-валидация осей Regime v2 (train/OOS, сетка W×h) на боевой БД |

## 6. SANDBOX / LIVE (sb_*, sandbox_*)

- `sandbox_v4_bot.py` — Sandbox V4 Bot v7 (SDK)
- `sb_instruments_info.py`, `sb_save_instruments.py`, `sb_all_instruments.py` — инфо инструментов
- `sb_maxlots.py`, `sb_qty_test.py`, `sb_order_methods.py`, `sb_order_price.py`, `sb_v7_test.py` — ордера/qty
- `sb_portfolio*.py`, `sb_check*.py`, `sb_clean*.py`, `sb_test_*.py` — портфель/проверки
- `clean_sandbox*.py` — очистка sandbox

## 7. СТРИМЫ (тесты T-Invest gRPC)

- `cycle_test_streams.py` — полный цикл (positions+trades snapshot)
- `test_candle_stream*.py`, `test_order_stream.py`, `test_positions_stream.py`, `test_portfolio_stream.py`, `test_stream_timeout.py` — по стримам

## 8. ДАННЫЕ (загрузка/подготовка)

- `backfill_data.py` — восполнение 1m свечей (CLI)
- `load_history_csv.py` — CSV T-Invest в БД
- `build_multi_tf.py` — 10m/30m/1h/4h из 1m
- `download_futures_5m.py`, `download_imoex.py`, `download_iss_macro.py`, `download_macro_5m_2025.py`, `download_macro_futures.py` — фьючерсы/индексы/макро
- `build_imoex_5m.py`, `build_g2_daily.py` — подготовка
- `last_closed_bar.py` — хелпер point-in-time (5m бар закрыт в ts+5m)
- `imoex_sensitivity.py` — IMOEX-чувствительность: порог следования акций за индексом, beta/corr/R², lead-lag, fade после экстремума (отчёт `docs/results/IMOEX_SENSITIVITY_2026-09-15.md`); `--save-db` пишет beta/corr/R² в `instruments.imoex_beta/corr/r2`
- `pair_leadlag.py` — парный лид-лаг 26 акций + IMOEX (k=0..3), топ направленных пар и догон (отчёт `docs/results/PAIR_LEADLAG_2026-09-15.md`)
- `imoex_fade_backtest.py` — fade-бэктест: откат акций после |move20| IMOEX ≥ T (immediate/retrace25/50), издержки бота и тариф

## 9. ДИАГНОСТИКА / ПРОФИЛИРОВАНИЕ

- `profile_ensemble.py` — cProfile compute_ensemble (месяц) → где время
- `bench3.py`, `bench_onbar.py` — замеры скорости on_bar
- `compare_detail.py`, `analyze_trades.py`, `exit_reason.py` — анализ сделок
- `db_*.py`, `check_*.py`, `diag_*.py` — проверки БД/структуры
- `audit_replay.py`, `audit_gold_regime.py` — frozen audit
- `test_adaptive.py` — adaptive per-regime с RegimeDetector

## 10. ПРОЧЕЕ (одноразовые/debug)

- `bt_debug*.py`, `bt_trace.py`, `debug_smlt*.py`, `debug_optuna_exact.py` — точечная отладка
- `fix_bot_ts.py`, `fix_html*.py`, `sync_gap.py` — фиксы
- `sb_*` не перечисленные — одноразовые проверки sandbox

---

## Ключевые правила

1. **Реальный движок = backtest_v2.py** (runtime._process_candle + FakeDatetime). Остальные merged-скрипты — копии логики бота, рискуют расхождением.
2. **Быстрый research = compute_ensemble** (per-ticker, изолированный). Портфель из него собирает `portfolio_merge.py`.
3. **Per-ticker оптимизированные параметры** живут в `instruments.optuna_params` (JSONB), заливает `save_optuna_to_db.py`.
4. ⚠️ `run_bot_engine_backtest.py` — FIGIs неправильные, не использовать.
5. Оценка скорости: compute_ensemble месяц/1 тикер = ~71с (SMLT). merged real-bot = ~0.3с на 5m бар → часы на месяц 20 тикеров.
