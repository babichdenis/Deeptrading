# executor_task_vol_sizing.md — C1: HIGH_VOL_CONVICTION_SIZING (PRE-REGISTERED, ПОДГОТОВИТЬ, НЕ ЗАПУСКАТЬ)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** PRE-REGISTERED EXPERIMENT — ПОДГОТОВИТЬ конфиг + выбор окна; **НЕ ВЫПОЛНЯТЬ прогон**.
**Статус:** prepare-only. Запуск — позже, после готовой attribution/MTM telemetry и подтверждения окна My3.

**Идея:** вместо отсекания low-vol входов (EXP-002b) — масштабировать РАЗМЕР позиции по волатильности
(high_vol_conviction_sizing). Это regime-based conviction sizing, НЕ классический volatility targeting
(не путать: классический VT уменьшает notional в high-vol; здесь гипотеза «high-vol сделки лучше» →
в high-vol размер больше). Никакого увеличения размера выше текущих 10 000 ₽.

**Единственная переменная:** position sizing по волатильностному режиму. Entry/exit/quorum/cooldown не трогаем.

**Классификация режима (point-in-time, без look-ahead):**
- `hi_vol` = ATR_5m(decision_ts) > rolling median ATR_5m за пред. 20 завершённых торг. дней per-FIGI
  (аналогично EXP-002b gate, НО применяется к размеру, не к входу).
- `low_vol` = иначе. Warm-up первые 20 дней окна → все сделки базовый размер.

**Конфигурация (первый тест, простая):**
- Baseline: 10 000 ₽ на каждую сделку (как сейчас).
- Experiment: hi_vol → 10 000 ₽; low_vol → 5 000 ₽. Без размера > 10 000 ₽.
- НЕ использовать настоящий Kelly (для 517 сделок и regime-dependent edge он нестабилен/агрессивен).

**Окно валидации (выбрать и зафиксировать, НЕ запуская):**
- ОДИН ранее не использованный 2025-месяц (напр. 2025-11), **disjoint от окна EXP-002b**.
- Подтвердить с My3 перед любым будущим запуском.

**Критерии успеха (для будущего прогона):**
- net >= baseline; MTM max DD < baseline; net PF >= baseline; trade coverage = 100% (все входы сохранены);
  no increase in maximum per-FIGI exposure.

**Деливераблы СЕЙЧАС (prepare-only):**
- `docs/research/C1_HIGH_VOL_CONVICTION_SIZING.md` — pre-reg документ (идея, переменная, классификация,
  конфиг, окно, критерии, guardrails).
- Конфиг/скрипт готовности (без запуска): пометить `status: PREPARED, NOT RUN`.
- Явно: НЕ выполнять backtest.

**Guardrails:** single variable; всё остальное зафиксировано; costs 5+2 bps/side; session/main; capital truth 10k;
нет pyramiding (заблокирован); не путать с volatility targeting.
