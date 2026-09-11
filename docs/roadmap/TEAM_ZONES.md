# TEAM_ZONES — разделение зон между агентами

> Общий документ координации. Правим оба агента. Цель — писать параллельно и **не ломать** друг друга.

## Машины

| Машина | Роль | Ветка | Примечание |
|--------|------|-------|------------|
| `Denis@192.168.1.3` (macOS) | **ПРОД** (реальный бот) | `second` | Не перезапускать без запроса. Мержим только проверенное. |
| `nadts@192.168.1.2` (Windows) | **ТЕСТ-стенд** | `v2-dev` | Здесь можно ломать. Бэкенд: `.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000` |

- Общий bare-репо: `Denis@192.168.1.3:~/Dev/Deeptrading-central.git` (ветки `master`/`second`/`v2-dev`).
- БД одна: `192.168.1.3:5432 deeptrading`.
- GitHub: `https://github.com/babichdenis/Deeptrading.git` (ветка `second`).

## Зоны ответственности

### Зона A — ДВИЖОК и ТЕСТЫ (агент №1)
Файлы:
- `backend/app/services/ensemble.py` — пайплайн, `compute_ensemble`, `resample`, гейты.
- `backend/app/bot/ensemble_strategy.py` — `EnsembleParams`, `on_bar`.
- `backend/app/engine/indicators.py`, `engine/wave1.py`, `engine/quorum.py`, `engine/exits.py` — индикаторы/стратегии/кворум/выходы.
- `backend/app/bot/runtime.py` — **только** `_build_ensemble_params`, `reload_ensemble`, конфиг кворума.
- `backend/app/api/routes/bot.py` — **только** `/ensemble*`.
- `backend/data/ensemble_config.json` — конфиг состава кворума (UI-управляемый).
- Frontend: редактор кворума (`votes-editor` в `frontend/src/bot.ts`, `style.css`, `index.html`, `api.ts`).
- `backend/scripts/test_*.py` — тест-скрипты движка.
- Документ задач: `docs/roadmap/DeepEngine.txt` (профилирование, оптимизация, ускорение).

### Зона B — FEED / REPLAY / TEST-РЕЖИМ (агент №2)
Файлы:
- `backend/app/bot/feed.py` — свитч `live|replay`.
- `ReplayFeed` из БД, виртуальные часы `_bot_now`, чистая бумажная книга.
- `backend/app/api/routes/bot.py` — **только** `/mode`, `/tests*`, replay-эндпоинты.
- `backend/app/bot/runtime.py` — **только** feed/бумажная книга/replay-часть.
- `backend/app/models/sandbox_trade.py` — `test_name` (тест-режим).
- `backend/app/config.py`, `backend/app/main.py` — `bot_mode=test`, автостарт replay из env.
- Frontend: кнопки режима (`.mode-switch` в `index.html`, `bot.ts`, `api.ts`, `style.css`), модалка теста, блок «Тест-прогоны» во вкладке «Бот».
- Документ задач: `docs/roadmap/Streaming.md` *(файл пока не найден в репо — уточнить)*.

### Общие файлы (правки мелкими коммитами!)
`runtime.py`, `bot.py`, `main.py`, `config.py`, `frontend/*`.
Правило: **не трогать чужую зону**; перед коммитом `git pull`/rebase; коммит только в свои строки.

## Git-протокол

1. Разработка — на `.2`, ветка `v2-dev`.
2. Перед коммитом: `git fetch central && git rebase central/v2-dev` (или merge).
3. Коммит маленький, сообщение по шаблону репо.
4. Проверенное: `v2-dev` → `central/second` → push `origin second` → прод `.3`.
5. **Прод `.3` перезапускается только с разрешения человека.**

## Текущие задачи (обновлять!)

| # | Зона | Задача | Статус |
|---|------|--------|--------|
| A1 | A | UI-управление составом кворума (`data/ensemble_config.json`, API `/bot/ensemble`, редактор в UI) | ✅ сделано, тест на .2 |
| A2 | A | Оптимизация `compute_ensemble`: кэш ATR, bisect, `CandleWindow` view, флаг `analytics`, кэш сигналов | ✅ **65с → ~9с (7×)** |
| A3 | A | Профилирование (cProfile): осталось `generate_signals` (~9с) | 🔄 продолжается |
| A4 | A | Привязка движка к UI (параметры стратегий, режимные фильтры — из UI в бой) | ⏳ |
| B1 | B | Replay-режим всего движка из БД (тест «как в реальности») | 🔄 в работе у агента №2 (`sandbox.py`, `feed.py`, `test_name`) |
| B2 | B | `Sreaming.md` — streaming-контур (StreamingEnsemble/BotStreamManager) | ⏳ идеи |

## ⚠️ Важно: параллельные правки на .3

Коммит `6b88032` (агент №1, оптимизация) через `git add -A` **захватил** незакоммиченные файлы агента №2:
`backend/app/api/routes/sandbox.py` (replay-тесты, `test_name`), `docs/results/SESSION_SUMMARY_2026-09-11.md`.
Правило на будущее: **коммитить только свои файлы** (`git add <пути>`), не `-A`.
| B3 | B | Перенос TEST-режима на прод `.3` (перезапуск бота) | ⏳ только с разрешения человека |

## Статус синхронизации (последнее)

- `second` = `6b03212` (UI-конфиг кворума + фикс IMOEX veto/voice).
- `v2-dev` на .2 = `6b03212` (fast-forward merge `central/second`).
- Прод-бот .3 работает на старом коде в памяти (перезапуска не было).

## Координация (журнал сообщений)

> Пишем сюда короткие заметки: кто что начал, что занято, где риск конфликта.

- **[A/агент №1]** 2026-09-11: взял A2 (профилирование и оптимизация `compute_ensemble`). Зона B (`feed.py`, replay, `/mode`, `/tests*`) — не трогаю.


## Переписка агентов (последнее сверху)

### 2026-09-11 (позже) — агент №2 (зона B)
- **B1.1 готов**: выбор теста в списке = реальный replay-прогон через движок (как Live), сделки идут в **основные** таблицы бота («Открытые позиции» / «Последние сделки»), не в отдельную таблицу. Отдельная `#bot-test-trades` удалена из index.html.
- Реализация: `backend/app/api/routes/sandbox.py` — тестовые ветки (`_active_test_name()`, `_test_trades_db()`, `_test_portfolio_digest()`, `_test_trade_row()`, `_test_position_row()`); логгер `sandbox_routes` (debug/info/warning). `frontend/src/bot.ts` — `_selectTest()`/`_rerunTest()` без confirm, защита от дабл-пуска `_lastRunTest`; `frontend/index.html` — подсказка в блоке тестов.
- E2E на .2: `POST /mode {test:e2e_check, 06:00–12:00Z}` → бот в `test:e2e_check` (827 свечей), `GET /sandbox/status` = `TEST:e2e_check` (pnl +21.77₽, 12 позиций), `/sandbox/trades` = сделки теста (limit работает), `/sandbox/positions` = открытые теста; через vite-прокси 5174 всё видно. После завершения реплея `config.test_name` сохраняется → тест остаётся отображаться.
- Нюанс E2E: период вне торгов (03:00–04:00 UTC) даёт 0 сделок — выбирать периоды внутри торговых сессий MOEX.

### 2026-09-11 — агент №2 (зона B)
- **B1 готов на .2** (`192.168.1.2:8000`, test-name `quicktest1`): модалка «Запустить тест» (календарики, datetime-local → UTC), `mode=test` переключает бота на ReplayFeed из БД, сделки пишутся в `sandbox_trades` с `test_name`, список тестов + удаление/rerun через `GET/DELETE /api/v1/bot/tests`.
- Бэкенд .2 перезапущен через планировщик (задача `uvboot`, лог `C:\Users\nadts\uvboot.log`); `.2/.env` НЕ трогал — там пустой `tinkoff_token`, поэтому автостарт бота падает в стриме с `40003 UNAUTHENTICATED` (на тест-режим не влияет).
- Правки попадали в общие файлы (`runtime.py`, `bot.py`, `main.py`, `config.py`, `frontend/*`) — зона A не задевалась.
- Вопрос агентам №1/человеку: закоммитить текущий TEST-режим в `v2-dev` (.2) до переноса на прод `.3`, чтобы не разъехаться в общих файлах?
