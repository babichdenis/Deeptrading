# executor_task_vote_attribution_recompute.md — VOTE_ATTRIBUTION completion / recompute

**Выдал:** My3 (research lead), 2026-08-27 — на основе явной спецификации владельца.
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** AUDIT_ONLY / DATA_RECOMPUTE (НЕ experiment, НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению. Исходный VOTE_ATTRIBUTION run дал PARTIAL (per_function/by_*/exact_combos/
totals + shadow k=20 готовы; НЕТ pair_present, dataset dir, report md; shadow на k=20 вместо k=200).
Эта задача ДОПОЛНЯЕТ и ИСПРАВЛЯЕТ, не перезапуская EngineRunner.

**Источник:** существующий canonical July 2026 baseline `5b44f3b383df`
(`config_hash=1c7f75dc44c2aa67`, A–F telemetry). НЕ перезапускать EngineRunner, НЕ менять trades,
НЕ менять baseline run result. Только пересчёт shadow-весов + добавление недостающих артефактов.

Полная схема/спек: `docs/research/VOTE_ATTRIBUTION.md`; оригинальная задача: `executor_task_vote_attribution.md`.

---

## Жёсткие запреты
- Не менять EngineRunner, SignalPolicy, CostModel, session policy, quorum, cooldown, exit policy, sizing, ML, торговые решения.
- Не перезапускать EngineRunner; не менять existing trades; не менять baseline run result.
- `shadow_score` остаётся shadow_only (causal_use=false) — не влияет на execution.

## Что сделать

### 1. Исправить shadow-weight shrinkage
- Canonical `shrinkage_k = 200`. Формула: `weight = (n / (n + 200)) * median_net_bps`.
- Убрать hardcoded `k=20`. `k` меняется только через explicit versioned config; текущий canonical report
  обязан содержать: `shrinkage_k=200`, `formula_version`, `training_period`, `weights_as_of` timestamp.
- Сохранить walk-forward семантику: вес для сделки T считается только по данным ДО T
  (`training_period_end` < decision_time T; `weights_as_of` = decision_time T).

### 2. Пересчитать только shadow-артефакты
- `vote_shadow_scores.csv` (перезаписать; колонки: intent_id, candidate_id, trade_id, figi, side,
  decision_time, functions, quorum_count, shadow_score, weights_snapshot, training_period_end, weight_timestamp)
- поля weight/score в `vote_attribution.json` (только связанные с весом/скором).
- НЕ трогать остальные таблицы (per_function/by_*/exact_combos/totals — они валидны и k-независимы).

### 3. Добавить `pair_present`
- Для каждой из 21 неупорядоченных пар из 7 функций:
  `pair_present` = оба сигнала присутствуют (допускаются доп. сигналы); отдельно `pair_exact` = ровно эта пара и никаких других.
- Для каждой пары: count, executed count, net, net_bps, PF, win rate, median net, CI95,
  при достаточном n — split by side/session/volatility. Малые n НЕ интерпретировать как edge.

### 4. Создать dataset-директорию
`backend/reports/{run_id}/vote_attribution_dataset_v1/`:
- `function_attribution.csv`
- `pair_present_attribution.csv`
- `pair_exact_attribution.csv`
- `intent_vote_features.csv`
- `shadow_weights.json`
- `vote_shadow_scores.csv`
- `manifest.json` (config_hash, run_id, training cutoff, shrinkage_k, formula_version, generated_at)

### 5. Создать report
`backend/reports/{run_id}/vote_attribution_report.md` со строго:
- точный scope данных + canonical config hash;
- определения величин;
- таблица function;
- таблица pair_present;
- таблица pair_exact;
- n и CI;
- shrinkage-формула и k=200;
- warning: shadow-only, не меняет execution;
- правила исключений;
- ограничения и data gaps.

### 6. Добавить тесты (`tests/test_vote_attribution.py`, дополнить)
- default/canonical `shrinkage_k == 200`;
- ни одни future-данные не влияют на месячный score;
- редкое n даёт СИЛЬНЕЕ shrinkage при k=200 чем при k=20;
- `pair_present` включает `pair_exact`;
- существуют все 21 неупорядоченных пары функций;
- shadow-скоры не могут менять решения EngineRunner;
- артефакты содержат config_hash, run_id и training cutoff.

## Вернуть
- список изменённых файлов;
- вывод тестов;
- before/after checksum shadow-артефакта;
- подтверждение, что trade count / P&L / вывод EngineRunner НЕ изменились;
- пути к артефактам.
