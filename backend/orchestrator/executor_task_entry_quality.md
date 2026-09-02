# executor_task_entry_quality.md — B4: ENTRY-QUALITY MICRO-ANALYSIS (SHADOW/READ-ONLY)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.54`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (заменяет B1; НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению.

**Цель:** найти пре-входные условия, предсказывающие качество входа (будущий фильтр/тайминг входа).
НЕ меняет стратегию — только диагностика.

**Для каждой исполненной сделки вычислить на decision_ts (research_pack candles, point-in-time, без look-ahead):**
- (a) цена акции vs собственная rolling EMA (напр. EMA_20 на 5m) → акция в uptrend на входе (для LONG) /
  downtrend (для SHORT)? alignment с стороной сделки;
- (b) pullback depth перед breakout: макс. adverse retrace от недавнего swing high/low до decision (в R или %);
- (c) немедленная adverse экскурсия в первые 1–3 бара после входа (ранний шум / micro-whipsaw) — MAE_early.

**Группировки и сравнение:** net/trade, PF, win, MFE/MAE, hold по группам (trend-aligned vs not;
  shallow/medium/deep pullback; low/high early-noise).

**Деливераблы:** `backend/reports/entry_quality.json` + `.csv` + `.md` (таблицы по группам).

**Тесты:** все пре-входные признаки используют данные ≤ decision_ts (без look-ahead); реконсиляция n;
EngineRunner не меняется.

**Жёсткие запреты:** shadow_only; НЕ предлагать entry filter без повторяемости на ≥2 периодах.
