# executor_task_imoex_stop_concept.md — B2b: IMOEX-INFORMED STOP (SHADOW counterfactual)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (продолжение B2; НЕ experiment, НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению (follow-up — в B2 этот под-блок не реализован).

**Цель:** реализовать то, что в B2 осталось — контрфактуальный «IMOEX-informed stop»: проверить гипотезу
владельца, что стопы можно ставить/улучшать по IMOEX, игнорируя внутренний шум акции. ТОЛЬКО диагностика.

**Данные:** IMOEX 1m уже выровнены по MSK (12581 баров, 06:50..16:00 UTC) — взять из B2. Торговые сделки —
baseline Июль 5b44f3b383df (trades.csv: entry_ts, side, entry_price, baseline exit price/time, MAE, net).

**Метод (counterfactual, no look-ahead):**
1. Для каждой сделки построить IMOEX-path с decision_ts до baseline exit.
2. Смоделировать альтернативный выход: выход, когда IMOEX разворачивается >= threshold от локального
   пика (для LONG) / впадины (для SHORT). Параметризовать threshold ∈ {10, 20, 30} bps.
3. Сравнить с baseline exit (target/stop/signal) по: net, MAE (улучшение?), win, hold, частота преждевременных выходов.
4. НЕ использовать будущий IMOEX в реальном сигнале; только для оценки stop-логики на истории.

**Деливераблы:**
- `backend/reports/imoex_stop_concept.json` + `imoex_stop_concept.md` — таблица по thresholds, сравнение с baseline.
- Явный вывод: уменьшает ли IMOEX-stop MAE / улучшает ли net, или только ruindrej тайминг.

**Тесты:**
- no look-ahead (только IMOEX <= trade_time + hold_duration);
- реконсиляция n сделок = baseline;
- EngineRunner не меняется; IMOEX НЕ в execution path.

**Жёсткие запреты:** shadow_only; НЕ предлагать как policy без отдельного pre-reg эксперимента на disjoint 2025.
