# executor_task_time_of_day.md — B1: TIME_OF_DAY_ATTRIBUTION (SHADOW / READ-ONLY)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (НЕ experiment, НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению.

**Цель:** узнать, есть ли внутри `main session` (MOEX 10:00–18:45 MSK) часы, где стратегия
зарабатывает после costs, и часы, где только платит комиссии. Дёшево, параллельно основной ветке.
НЕ предлагать time-filter без повторяемости рисунка на разных периодах.

**Источники (canonical runs, «на небольшом периоде» = месячные packs):**
- Июль 2026: `backend/reports/5b44f3b383df/` (research_pack.json, trades.csv, entry_intents.csv, intent_lifecycle.csv, mtm_equity_1m.csv)
- Март–Апр 2026: `backend/reports/57b6244ee3eb/`
- Май–Июнь 2026: использовать полный pack, если есть; иначе агрегированные DRAFT-данные
  (e2_cooldown_202605_202606_DRAFT.json / e5_trailing_202605_202606_DRAFT.json / regime_diagnostic_e5_MayJune.md)
  и явно отметить, что trade-level bucket-анализ ограничен доступными packs.

**Метод:**
1. Для каждого run взять фактические сделки (trades.csv) и связать с `decision_time` через entry_intents/intent_lifecycle.
2. Разбить по `decision_time` в MSK на **30-минутные** корзины внутри 10:00–18:45 (дополнительно — 60-мин для проверки).
3. НЕ фиксировать заранее «11:00–16:30» — пусть данные покажут.
4. По каждой корзине посчитать:
   - trades, gross, commission, slippage, net, net/trade, PF (агрегат и по FIGI),
   - mix exit_reason (target / stop_loss / signal_exit),
   - MFE, MAE (медиана/среднее), hold time (bars/min),
   - MTM drawdown contribution (приближённо по mtm_equity_1m.csv: вклад позиций, открытых в корзине, в просадку).
5. Сравнить СТАБИЛЬНОСТЬ рисунка корзин между периодами (Июль vs Март–Апр vs Май–Июнь):
   одинаковые ли корзины прибыльны/убыточны на ≥2 периодах?

**Деливераблы:**
- `backend/reports/time_of_day_attribution.json` (по периодам + combined)
- `backend/reports/time_of_day_attribution.csv`
- `backend/reports/time_of_day_attribution.md` — отчёт: таблицы по корзинам, кросс-периодная стабильность,
  вывод (есть ли повторяемый pattern), явное «НЕ рекомендуется filter без повторяемости».

**Тесты:**
- сумма по корзинам = total trades/net (реконсиляция);
- decision_time корректно в MSK (проверка tz/сдвига);
- нет look-ahead; EngineRunner не меняется;
- все сделки покрыты ровно одной корзиной.

**Жёсткие запреты:** не менять стратегию; не предлагать entry_time_filter как вывод (только как
возможную future policy при повторяемости); shadow_only.
