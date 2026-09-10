# executor_task_macro_lookahead_recompute.md — ПЕРЕСЧЁТ макро-атрибуций (lookahead-баг)

**Выдал:** My3 (research lead), 2026-08-29
**Исполнитель:** DeepSeek executor (`.3`)
**Тип:** READ-ONLY пересчёт. НЕ менять движок/baseline/стратегию.

## Контекст (аудит завершён)
Аудит `gold_regime_202601_202607` (см. `reports/gold_regime_audit.md`) подтвердил **lookahead-баг**
в разметке макро-режимов: бары 5m имеют `ts` = начало интервала, но `close/EMA/ADX` известны только
в конце (ts+5min). Старый `bisect_right(ts, dt)-1` брал незакрытый бар (lookahead до 5 мин) + был
холодный старт EMA/ADX на начале окна. Июльская значимость GOLD (p=0.0076 в H-058) оказалась
артефактом; при корректном баре p=0.13. Этот же баг сидит во ВСЕХ макро-разбиениях проекта.

## Задача
1. **Исправить методологию** (в `scripts/macro_regime_attribution_v2.py` или новом v3):
   - бар сопоставления: `j = bisect_right(idx, dt - 5*60) - 1` (последний **закрытый** бар,
     условие `ts[j] + 5min <= decision_time`);
   - ряд фактора грузить с **прогревом** (минимум ~50 баров до начала окна тестирования, либо весь
     доступный ряд с 2025-01), чтобы EMA50/ADX14 не были холодными;
   - regime-функция без изменений (метод B: ADX14>25, up если close>EMA50).
2. **Пересчитать** макро-атрибуцию для ВСЕХ факторов и окон:
   - July 2026 (baseline `5b44f3b383df`, 517 сделок): GOLD / IMOEX / USDRUB / BRENT (секторное
     сопоставление как в v2: IMOEX→risk, BRENT→OilGas, GOLD/USD-RUB→Metals).
   - 2026-H1 (окно 2026-01..08, 3481 сделка, как `gold_window_202601_202607`): те же факторы.
   - Применить поправку Бонферрони (n_tests=3..4, α≈0.0167..0.0125) к июльским результатам.
3. **Пересчитать** H-058 §1c (macro_regimes) на July с новым методом (заменить блок в
   `reports/h058_significance_testing.json` корректным), чтобы закрыть вопрос июльской значимости.
4. **Зафиксировать** для будущих тестов (H-063 lead-lag фьючерсов, H-064 regime-switching):
   использовать ТОЛЬКО last-closed бар; добавить в `scripts/` хелпер `last_closed_bar(idx, dt)`.

## Деливераблы
- `backend/reports/macro_regime_attribution_v3.json` + `.md` (July + 2026-H1, все факторы, с
  Бонферрони) с явным указанием `methodology: last-closed-bar, full-series warmup`.
- Обновлённый `backend/reports/h058_significance_testing.json` (§1c пересчитан).
- Краткий `backend/reports/macro_lookahead_recompute_summary.md`: какие выводы (GOLD/IMOEX/USDRUB/
  BRENT) изменились после fixes, и какой статус у макро-гипотез H-036.
- Итог: `python -m orchestrator submit "@backend/reports/macro_lookahead_recompute_summary.md"`

## Жёсткие запреты
- Read-only; НЕ менять движок/стратегию; НЕ удалять прежние артефакты (v2 оставить для сравнения,
  пометить устаревшим).
- Не строить макро-гейт/фильтры — только атрибуция и значимость.
