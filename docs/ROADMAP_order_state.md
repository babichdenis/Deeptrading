# ROADMAP — Order/State механизм (docs/Order_state.md)

> Целевая архитектура: единое торговое ядро для Lab/Paper/Live, WebSocket для событий,
> workers для тяжёлых расчётов. Реализуем строго по фазам.

## Фаза 1. Domain contracts (движок) ✅ начато
- `app/engine/orderflow.py`: OrderIntent, Order, Fill, Decision, PositionEvent, ExitDecision
  (у нас уже есть Signal/Position/Trade в engine/models.py — дополняем недостающими)
- Reason codes, event types, order statuses — единые enum
- golden tests на переходы

## Фаза 2. Event bus + WebSocket (причина «фронт зависает»)
- `app/services/eventbus.py`: in-memory pub/sub + outbox в PostgreSQL
  (event_log таблица: id, sequence, channel, event_type, payload, delivered)
- `app/api/routes/ws.py`: /ws — SUBSCRIBE/SNAPSHOT/RESUME, каналы
  job:{id}, experiment:{id}, bot:{id}, figi:{figi}
- fastapi WebSocket endpoint + auth token (позже)

## Фаза 3. Job worker для Lab
- test_runs уже есть; диспетчер отправляет события: JOB_STARTED, FIGI_STARTED,
  BAR_PROGRESS (не чаще 500мс), FIGI_COMPLETED, TRADE_CREATED, JOB_COMPLETED
- HTTP: POST /lab/queue → 202 {run_id, channel}; GET для снапшотов

## Фаза 4. Frontend: WebSocket client + reducer
- `frontend/src/ws.ts`: connect /ws, subscribe job:{run_id}, reducer по событиям
- Заменить поллинг очереди (2.5с) на WS-события; падение на HTTP-снапшот

## Фаза 5. Paper адаптер поверх единого ядра
- runtime.py переключить на orderflow: intent → paper fill → position engine → ledger
- PaperExecutionAdapter (уже частично в bot/)

## Фаза 6. Live позже
- BrokerExecutionAdapter, reconciliation, risk gates, kill switch — НЕ сейчас (paper-only)

## Ключевые границы (из Order_state.md)
- HTTP = команды/снапшоты; WebSocket = поток изменений
- Worker = тяжёлый расчёт; Engine = единая логика; Adapter = среда исполнения
- Signal ≠ Intent ≠ Order ≠ Fill ≠ Trade
- Frontend не считает торговую математику
