# executor_task_entry_confluence.md — B3: ENTRY CONFLUENCE / QUORUM OPPORTUNITY-COST (SHADOW/READ-ONLY)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (заменяет B1; НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению.

**Цель:** проанализировать входы со стороны согласованности сигналов — растёт ли качество входа с числом
согласных setup-функций, и сколько edge теряется из-за кворум-фильтра. Кормит будущую теорию
**расширения числа входов** (безопасно увеличить entries).

**Часть A — entry confluence (исполненные сделки):**
- Сгруппировать сделки по `quorum_count` = число согласных setup-функций на decision (ровно 2 / 3 / 4+).
- По каждой группе: trades, net, net/trade, PF, win, MFE/MAE, hold.
- Вопрос: растёт ли качество входа (net/trade, PF, win) с числом согласных сигналов?
  (Информирует будущий policy-тест «require ≥3 signals» — мягкая альтернатива weighted voting.)

**Часть B — quorum opportunity-cost (НЕ исполненные intents):**
- Среди отвергнутых intents (rejections.csv / SETUP_MISSING — не достигли кворума) оценить standalone EV
  присутствующих 1–2 setup-сигналов (через per-function net из vote attribution, k=200).
- Оценить opportunity cost кворум-фильтра: сколько edge отбрасывается требованием quorum=2?
  Это основа будущей теории расширения entries (напр., разрешить high-conviction single-signal входы в hi_vol).

**Источники:** `5b44f3b383df/` (trades.csv, intent_vote_features.csv из dataset_v1, rejections.csv);
опционально перекрёстно Март–Апр `57b6244ee3eb/`.

**Деливераблы:** `backend/reports/entry_confluence.json` + `.csv` + `.md`
(таблица Part A по quorum_count; Part B — оценка opportunity-cost).

**Тесты:** quorum_count пересчитывается независимо из functions_mask; Part B использует ТОЛЬКО не торгованные
intents; нет look-ahead; EngineRunner не меняется.

**Жёсткие запреты:** shadow_only; НЕ предлагать изменение quorum/weighted-vote как вывод (только как будущий
policy-тест при наличии evidence).
