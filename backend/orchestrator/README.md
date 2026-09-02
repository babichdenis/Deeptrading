# Orchestrator

Модуль диспетчеризации агентов для проекта (находится в `backend/orchestrator/`).
Следит за состоянием, будит executor (по умолчанию — relay: вторая бесплатная сессия Hy3
через файл; опционально — платный DeepSeek API), пишет общий статус и уведомляет владельца
через Telegram (или консоль в dry-run).

## Принцип
- `state.json` — машиночитаемое состояние (кто сейчас ходит, какая задача).
- `STATUS.md` (корень, рядом с chat.md) — человекочитаемое зеркало (его читают агенты/люди).
- `notifier.py` — уведомления; с токеном — Telegram, иначе консоль.
- `agents.py` — единственный платный вызов LLM: DeepSeek-executor. Роль My3 (review)
  выполняет владелец бесплатно.
- `core.py` — цикл: читает state -> будит агента ИЛИ ждёт решения владельца.

Пути и `.env` резолвятся от расположения пакета: `backend/.env` и корень репозитория
подгружаются автоматически.

## Запуск
```
cd /Volumes/Dev/Deeptrading/backend
python -m orchestrator            # диспетчер + Telegram-бот в одном процессе
```
В режиме owner-решения оркестратор ждёт решения в Telegram (кнопка/команда) либо файл
`backend/orchestrator/decisions.json`. Из другого терминала:
```
python -m orchestrator respond approve
```

## Подключение DeepSeek
Вписать `DEEPSEEK_API_KEY` в `backend/.env` (там же лежат остальные секреты проекта).
Агент-исполнитель станет реальной сессией DeepSeek (единственная платная LLM-сессия).
Роль My3 (review) выполняет владелец бесплатно — оркестратор НЕ делает второй платный вызов.

## Подключение Telegram
1. Создать бота у @BotFather, получить `TELEGRAM_BOT_TOKEN`.
2. `TELEGRAM_CHAT_ID` — id чата, куда бот шлёт сообщения (НЕ bot id из токена!).
   Проще всего: написать боту `/chatid` — он вернёт число, вписать его в `.env`.
   Альтернатива: написать боту /start, открыть `https://api.telegram.org/bot<TOKEN>/getUpdates`
   и взять `chat.id` из JSON.
3. Вписать оба значения в `backend/.env`.
4. Запуск из `backend/`: `python -m orchestrator` — диспетчер + Telegram-бот в одном
   процессе (бот стартует в фоне внутри того же процесса).
5. Команды бота:
   `/status` — текущий STATE; `/chatid` — узнать chat_id;
   `/approve` и `/reject` + inline-кнопки — решение владельца (пишется в decisions.json);
   `/pause` и `/resume` — поставить/снять паузу диспетчера;
   `/mute` и `/unmute` — замьютить рутинные уведомления (решения не глушатся).
6. Решение можно дать и из консоли: `python -m orchestrator respond approve`.

## Roadmap
- Phase A: dry-run скелет (готово).
- Phase B: реальный DeepSeek-executor (ключ в .env — единственная платная LLM-сессия).
  Роль My3 (review) выполняет владелец бесплатно — оркестратор НЕ делает второй платный вызов.
- Phase C: Telegram inline-кнопки для решений + команды /status /approve /pause /mute (готово).
- Phase D: торговый бот шлёт сигналы тем же notifier (категория `signal`, заглушка готова).

## Telegram в РФ (без VPN)
`api.telegram.org` блокируется провайдерами — бот упрётся в таймаут даже без VPN.
Решение: свой API-прокси (Cloudflare Worker, бесплатно). Задать в `backend/.env`:
```
TELEGRAM_API_BASE=https://your-worker.workers.dev
```
Библиотека сама добавит `/bot<TOKEN>/`. Polling и отправка пойдут через прокси.

## Две сессии Hy3 (бесплатно, без DeepSeek)
Архитектор и executor — две отдельные сессии opencode на модели Hy3. Связь между ними —
общая файловая система репозитория (обе сессии видят `backend/orchestrator/`). Бот пишет
задачу в `executor_task.md`, вторая сессия читает тот же файл по пути и пишет ответ в
`executor_response.md`. Никакого копирования содержимого не нужно — только путь.

ID сессий (для подсказок в файлах) задаются в `.env`:
`SESSION_ARCHITECT`, `SESSION_EXECUTOR`, `SESSION_BASE`.

Команды со стороны архитектора (сессия №1):
```
python -m orchestrator task "сформулируй задачу"   # положить задачу в state
python -m orchestrator dispatch "@task.md"          # записать executor_task.md + уведомить
python -m orchestrator status                       # посмотреть state
```
Команды со стороны executor (сессия №2, Hy3):
```
python -m orchestrator take        # показать текущую задачу (executor_task.md)
# ... вставить задачу в сессию Hy3, получить результат, сохранить в answer.md ...
python -m orchestrator submit "@answer.md"   # записать executor_response.md (daemon заберёт)
```
Если запущен `python -m orchestrator` (daemon), он сам пишет `executor_task.md` и ждёт
ответа, затем возвращается к архитектору. Режим `EXECUTOR_MODE=relay` (по умолчанию) —
бесплатный; `deepseek` — платный вызов API (legacy).
