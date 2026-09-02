# executor_task_imoex_context.md — B2: IMOEX_CONTEXT_ATTRIBUTION (SHADOW / READ-ONLY)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (НЕ experiment, НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению ПРИ наличии корректно выровненных point-in-time IMOEX-свечей.

**Цель:** понять, несёт ли контекст рынка (IMOEX INDEXCF) доп. информацию поверх ATR-волатильности:
согласованность/расхождение акции с индексом, и — отдельная метрика по интуиции владельца — является ли
IMOEX **опережающим** индикатором (на 2/5/10 мин) для российских акций, и можно ли по IMOEX ставить/улучшать
**стопы**, игнорируя внутренний шум акции. Всё SHADOW — IMOEX НЕ добавляется в execution policy.

**ШАГ 0 — проверка данных (критично):** найти IMOEX (INDEXCF) 1m/5m/30m свечи для периодов canonical runs.
Проверить point-in-time выравнивание (timestamp alignment, tz MSK). Если данных нет/не выровнены — остановить
и вернуть DATA GAP (без фабрикации). Не запускать hard gate «LONG only if IMOEX above MA».

**Метод (contrastemporaneous context):**
1. Для каждой сделки взять IMOEX-признаки, доступные СТРОГО на `decision_ts` (без look-ahead):
   - imoex_return_5m, imoex_return_30m, imoex_return_1h
   - imoex price vs rolling EMA (период зафиксировать), imoex EMA slope
   - imoex realised volatility (rolling)
   - stock residual return = stock_return − beta × imoex_return (beta оценить на окне, не на том же decision)
   - alignment = (signal side акции) согласован с направлением IMOEX (true/false)
2. Сгруппировать сделки по: aligned vs counter-market; IMOEX trend vs range; high/low index-vol.
3. Сравнить economics по группам: net/trade, PF, win, MFE/MAE, hold.

**Метод (lead-lag / predictive + STOP concept — SHADOW ONLY, по интуиции владельца):**
4. Опережение: для каждой сделки проверить, предсказывал ли IMOEX направление акции на 2/5/10 мин вперёд
   (directional accuracy IMOEX-lead → stock-next-move). Посчитать метрику предсказательной силы (без использования в execution).
5. Прототип-концепт «IMOEX-informed stop» (контрфактуально, на исторических сделках): смоделировать выход,
   когда IMOEX разворачивается X bps (игнорируя внутренний шум акции), и измерить, сократило ли бы это MAE/
   улучшило net. ТОЛЬКО диагностика; не policy. Вернуть как гипотезу, не как рекомендацию.

**Деливераблы:**
- `backend/reports/imoex_context_attribution.json` + `.csv`
- `backend/reports/imoex_context_attribution.md` — группы, economics, lead-lag таблица, STOP-concept результат,
  DATA GAP и timestamp-alignment отчёт.

**Тесты:**
- IMOEX-признаки используют только данные ≤ decision_ts (для contemporaneous) и строго будущие (для lead) — помечено;
- нет look-ahead; EngineRunner не меняется; IMOEX НЕ в execution path;
- реконсиляция сделок по группам = total.

**Жёсткие запреты:** shadow_only; не добавлять IMOEX в policy; не делать hard gate; не использовать будущий IMOEX в features.
