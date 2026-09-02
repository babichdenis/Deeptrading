# executor_task_vote_attribution.md — VOTE ATTRIBUTION & SHADOW SCORING

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** DATA_PREP + TELEMETRY (НЕ experiment, НЕ меняет EngineRunner/стратегию)
**Статус:** готово к исполнению после завершения EXP-002 (текущий EXP-002 перезапустить строго на 2025-08-01..2025-12-31).

---

## Контекст
Нужно копить «репутацию» каждого голоса (функции) и каждой комбинации голосов из canonical
baseline runs, чтобы строить **shadow weighted score**. Это ТОЛЬКО телеметрия/исследование; score
`shadow_only=true`, не влияет на решения. Полная спецификация: `docs/research/VOTE_ATTRIBUTION.md`.

## Что сделать (строго read-only)
1. **Отобрать canonical baseline runs** по фильтру (все должны совпадать):
   strategy_id=`ensemble_main_v1`, config_hash=`1c7f75dc44c2aa67`,
   exit_policy=`atr_stop(14,2.0,2.0)` + `signal_exit:on`, quorum=2, cooldown=15,
   capital=10000/pos, commission 5bps, slippage 2bps, session engine корректный (A–F telemetry
   есть), ML off, universe=5 FIGI, intrabar=conservative_stop_first.
   **ИСКЛЮЧИТЬ:** legacy/session-bug (ada34053fefd, b1e2da3e1a0a, test_research_pack), trailing
   runs (E5 S1, regime_gated), signal_exit-off (E1 S1), любые с другим capital/exit policy.
   (Ожидаемо подходят: 5b44f3b383df — Июль; 57b6244ee3eb — Март–Апр; + любой иной, прошедший фильтр.
   Зафиксировать список включённых run_id.)
2. Для каждого entry intent (исполненного И отклонённого) извлечь поля схемы (§1 спецификации):
   run_id, candidate_id, episode_id, FIGI, side, decision_time, session_bucket, volatility_regime
   (hi_vol = ATR_5m(decision) > rolling median ATR_5m за пред. 20 зав. торг. дней, point-in-time),
   signal_tf, quorum_count, functions_mask, active_functions, function_score_sum,
   engine_terminal_reason, linked_trade_id. Для исполненных сделок добавить gross/commission/
   slippage/net/net_bps/exit_reason/MFE/MAE/hold/target_first/stop_first/signal_exit.
3. Построить агрегации (§2 спецификации):
   - Одиночные голоса: per-function (Intents, Executed, Net, Net/trade, PF, Target rate, Stop rate).
   - Пары: `pair_present` (оба присутствуют) и `pair_exact` (ровно эти двое) — ≤21 пара; + High-vol net/trade.
   - Полные комбинации (≤127): только с достаточным n, остальные → `other`; веса не назначать.
   - Разбиения per-FIGI / side / session_bucket / volatility_regime.
4. **Research score (shadow_only):** per function/direction/regime по ПРОШЛОЙ истории хранить
   win_rate, median_net_bps, expectancy, sample_size, CI. Сглаженный вес
   `w_f = (n_f/(n_f+k)) * median_net_bps_f`, **k=200 зафиксировано ДО прогона**. Не влияет на сделки.
5. **Walk-forward (анти-утечка):** веса для периода T считать только по данным ДО начала T.
   Построить walk-forward таблицу (Trade month | weight history | score applied). В каждой строке
   сохранить training-period и weight-timestamp.

## Деливераблы
- `backend/reports/{run_id}/vote_attribution.json` (per canonical run)
- `backend/reports/{run_id}/vote_shadow_scores.csv`
- `backend/reports/vote_attribution_dataset_v1/` — объединённый датасет (attribution + shadow + walk-forward)
- `backend/reports/vote_attribution_report.md` — одиночные/парные таблицы + разбиения

## Обязательные тесты
1. no future trades used in score (max(data ts) < trade decision_time).
2. rare functions shrink toward zero (n_f→0 ⇒ w_f→0).
3. functions_mask reconciles with active function list (сумма mask = active count = quorum/active_functions).
4. all score rows link to a candidate/intent (нет осиротевших).
5. shadow score cannot affect EngineRunner output (EngineRunner не трогать; score-колонка есть, но вне пути решения).

## Жёсткие запреты
- Не менять EngineRunner, strategy rules, quorum, cooldown, session policy, exit policy, CostModel, ML.
- Не использовать oracle / будущий P&L для текущего score.
- Не смешивать несовместимые runs (§0 спецификации).
- Не назначать решения на основе score (shadow_only=true).
