# executor_task_exit_oracle.md — EXIT ORACLE AUDIT (DIAGNOSTIC_ONLY)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** DATA_PREP / DIAGNOSTIC_ONLY (НЕ experiment, НЕ меняет EngineRunner/стратегию)
**Статус:** готово к исполнению после EXP-002 (текущий EXP-002 перезапустить строго на 2025-08-01..2025-12-31).
**Источник:** immutable canonical July 2026 baseline `5b44f3b383df` (не трогать, не перезаписывать).

---

## Цель
Не улучшать и не изменять стратегию. Только измерить, как фактический exit соотносится с лучшим
будущим выходом (внутри ЗАФИКСИРОВАННОГО horizon) ПОСЛЕ уже реального entry. Роль — «рентген»
exit policy. Полная спецификация: `docs/research/EXIT_ORACLE_AUDIT.md`.

## Ограничения (жёсткие)
- Не менять EngineRunner, SignalPolicy, CostModel, ML, thresholds, session policy, stops/targets, trading decisions.
- Oracle data — diagnostic_only. Поля oracle НЕ включать в runtime features / entry intents / ML training / execution path.
- Не перезаписывать legacy/canonical results.
- Горизонты зафиксированы ДО прогона: `20m`, `60m`, `to_main_session_close` (или vertical barrier = max_hold_bars / session close). НЕ подбирать horizon под результат.
- Не давать policy-рекомендаций, только диагностику.

## Что сделать (для каждой closed trade из 5b44f3b383df)
1. Взять actual `entry_time` и actual fill price.
2. Рассчитать oracle exits на фиксированных horizons (20m, 60m, to_session_close):
   - LONG: best exit = `max high` после entry до horizon.
   - SHORT: best exit = `min low` после entry до horizon.
3. Вычислить per-trade метрики (см. схему): oracle_best_exit_time/price, oracle_favorable_bps,
   realized_gross_bps, realized_net_bps, capture_ratio (=realized_gross/oracle_favorable, raw, БЕЗ обрезки),
   post_exit_favorable_bps, post_exit_adverse_bps, exit_lag_minutes, future_bars_available, exclusion_reason.
4. Агрегации: overall; by exit_reason; by FIGI; by side; by session bucket; by volatility_regime
   (hi_vol = ATR_5m(entry) > rolling median ATR_5m за пред. 20 зав. торг. дней, point-in-time).
5. Exclusions: `end_of_data` trades и trades без достаточного числа future bars для выбранного horizon.
   Отчитать exclusions явно (counts).
6. Oracle P&L: использовать raw OHLC только для диагностической oracle-цены; НЕ включать гипотетические
   fees/slippage в oracle exit P&L (опционально — отдельная консервативная net-after-costs оценка).
7. Добавить флаги: `oracle_diagnostic_only=true`, `causal_use=false`.

## Деливераблы
- `backend/reports/5b44f3b383df/exit_oracle_audit.json`
- `backend/reports/5b44f3b383df/exit_oracle_trades.csv`
- `docs/EXIT_ORACLE_AUDIT.md` (сам отчёт с таблицами; НЕ путать с `docs/research/EXIT_ORACLE_AUDIT.md` — спецификацией).

## Обязательные тесты
1. long/short best exit correctness (max high / min low).
2. horizon truncation (нет выхода за horizon).
3. no future bars beyond horizon.
4. end-of-data exclusion.
5. oracle fields НЕ присутствуют в runtime ML/decision объектах (causal_use=false соблюдён).
6. EngineRunner outputs не меняются при включении audit.

## Что искать в результатах (паттерны, не «oracle заработал больше»)
- Signal exits: после выхода рынок в ср. шёл против старой позиции? → signal exit полезен.
- Target: capture низкий + продолжение после TP? → гипотеза partial exit / runner (будущий эксперимент).
- Stop: разворот после стопа? → entry timing или stop geometry (отдельный эксперимент).
- High/low-vol: в каком режиме отдаём больше MFE? (волатильность и для hold policy).
