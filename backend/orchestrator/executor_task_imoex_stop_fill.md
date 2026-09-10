# executor_task_imoex_stop_fill.md — B2b-II: IMOEX-INFORMED STOP (fill simulation)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.3`)
**Тип задачи:** SHADOW_RESEARCH / READ-ONLY (продолжение B2b; НЕ меняет EngineRunner/стратегию)
**Статус:** к исполнению (B2b дал частоты, но НЕ экономический эффект).

**Цель:** ответить на вопрос владельца — можно ли стопы ставить по IMOEX, срезая внутренний шум акции, и улучшает ли это исход.
B2b посчитал только ЧАСТОТУ ранних IMOEX-разворотов (threshold_10: 52.9%, _20: 30.7%, _30: 16.0% сделок). Здесь —
полная fill-симуляция экономического эффекта.

**Метод (counterfactual, no look-ahead):**
1. Для каждой из 505 июльских сделок (baseline: net 10446, net/trade 20.69, win 71.1%, median MAE 0.27R, hold 7):
   смоделировать альтернативный выход, когда IMOEX разворачивается >= threshold (10/20/30 bps) от локального пика
   (LONG) / впадины (SHORT). Использовать IMOEX 1m (уже выровнены).
2. Fill на СЛЕДУЮЩЕМ 1m баре после сигнала IMOEX-разворота (реалистично). Посчитать net, MAE, win, hold vs baseline.
3. Доп. метрика: для сделок, где IMOEX-stop сработал бы, каким был фактический stock MAE ПОСЛЕ IMOEX-разворота?
   (Если IMOEX-разворот ОПЕРЕЖАЕТ неблагоприятное движение акции → IMOEX-stop режет шум.)

**Деливераблы:** `backend/reports/imoex_stop_fill.json` + `imoex_stop_fill.md`
(таблица net/MAE/win/hold по thresholds vs baseline; lead-lag IMOEX-reversal → stock adverse move).

**Тесты:** no look-ahead (IMOEX-path только до trade_time + hold); реалистичный fill (next bar); реконсиляция n=505;
EngineRunner не меняется; IMOEX НЕ в execution path.

**Жёсткие запреты:** shadow_only; НЕ предлагать как policy без отдельного pre-reg эксперимента на disjoint 2025.
