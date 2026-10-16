# MIGRATION_TO_2 — подготовка переезда бота на .2 (Live + БД)

> Создан 2026-09-15. Цель: полностью переехать на .2 (Windows), сохранив тестовую часть.
> Планируемое окно: ночь, после остановки торгов (вечерняя сессия до 23:50 МСК).

## 1. Итог сравнения кода (.3 second vs .2 v2-dev)

| Параметр | Значение |
|---|---|
| `second` относительно `v2-dev` | **+96 коммитов, 0 назад** (v2-dev полностью влит в second) |
| Значит | на .2 достаточно `git fetch central && git checkout second` — весь код .3 уже там |
| .2 сейчас | ветка `v2-dev` (13db5cf) + локальные незакоммиченные правки |
| Локальные правки .2 | `bot.py, sandbox.py, moex.py, replay_feed.py, runtime.py, config.py, main.py, mcp_server/bot_server.py, frontend/*` + untracked (`imoex_guard.py`, `test_client.py`, `.venv-mcp/`, `audit.jsonl`) |

**Что есть в `second`, но отсутствует в текущем .2** (ключевое):
IMOEX guard (+chase, beta, сессия 09:50–19:00), AI-гейт (worker, approvals, advice, orderbook,
parallel big-pickle+llama), тройное подтверждение входа (3×1м), loss-streak HOLD, held-тикер
(skip_entry_side + логи), MCP v2 (21 инструмент, промты/ресурсы/гарды), wall-pace реплея,
ML-фильтр из v2-dev (уже влит), скрипты исследований, UI (бейджи IMOEX, вкладка AI-гейт).

**Тестовая часть не теряется**: replay/test-режим, `sandbox.py` test-ветки, ML-эксперименты —
всё это в v2-dev и уже влито в second. Локальный wip агента №2 на .2 сохранить
(закоммитить в v2-dev или stash) до переключения на second.

## 2. База данных

| Параметр | .3 (сейчас) | .2 (цель) |
|---|---|---|
| Postgres | 16.15 (Docker `deeptrading-postgres`, postgres:16-alpine) | **не установлен** (ни Docker, ни Postgres) |
| Размер БД | **5.4 ГБ** (candles 5.2 ГБ, signals 132 МБ, bot_logs 50 МБ) | хватит: C: 38 ГБ free, X: 65 ГБ free |
| Подключение | 127.0.0.1:5432 | ⚠️ **ИТОГ (2026-09-28): БД в Docker на .2, для всех машин `postgres_host=192.168.1.2` (порт 5432)** |

**Установка на .2**: PostgreSQL 16 (native Windows, zip-архив EDB — без инсталлятора),
`initdb` в `X:\pgdata` (больше места), сервис/задача автозапуска, база `deeptrading` + роль
`deeptrading/deeptrading`, `listen_addresses='localhost'` (наружу не открывать).

**Перенос данных**:
```bash
# на .3
docker exec deeptrading-postgres pg_dump -U deeptrading -Fc deeptrading > /tmp/deeptrading.dump
# на .2 (после установки)
pg_restore -U deeptrading -d deeptrading --no-owner -j 4 C:\...\deeptrading.dump
```

## 3. .env на .2 (что перенести)

Сейчас на .2: `postgres_host=192.168.1.2` (**БД живёт в Docker на .2, обновлено 2026-09-28**), `tinkoff_token=` (пусто), `BOT_MODE=test`.
Перенести с .3 (значения не печатать):
- `TINKOFF_TOKEN` (используется и как feed/live: `feed_token = live or token`);
- `SANDBOX_ACCOUNT`, `LIVE_ACCOUNT`;
- `DEEPSEEK_API_KEY/BASE_URL/MODEL` (уже есть);
- `TELEGRAM_*` (если нужен релей);
- `POSTGRES_HOST=127.0.0.1` (после переезда БД), остальные POSTGRES_* как есть;
- `BOT_MODE=sandbox` (для live-старта; тесты — через UI/POST /mode).

## 4. Сервисы .2 (что уже есть)

| Задача/сервис | Команда | Статус |
|---|---|---|
| `uvboot` | `cmd /c C:\Users\nadts\uvboot.bat` → `backend\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000` | Ready |
| `vitebot` | `cmd /c C:\Users\nadts\vite_start.bat` → `npm run dev -- --port 5174 --host 0.0.0.0` | Running |
| `ollama_serve` | `ollama serve` (ONSTART, SYSTEM, OLLAMA_MODELS) | Ready |
| **+ postgres** | добавить службу/задачу автозапуска PostgreSQL | нет |

MCP: `.venv-mcp` + `mcp_server/bot_server.py` + opencode config (`deeptrading-bot`) — уже на .2.

## 5. Чек-лист переезда (ночь)

1. [ ] Остановить бота на .3 (вечерняя сессия закрыта, позиций нет/минимум) — `POST /bot/stop`.
2. [ ] Финальный `pg_dump` (5.4 ГБ, ~2–5 мин), scp на .2.
3. [ ] На .2: установить PostgreSQL 16, initdb (X:\pgdata), создать роль/базу, `pg_restore`.
4. [ ] Проверить БД на .2: `candles` count, последняя свеча, `sandbox_trades`.
5. [ ] .2 `.env`: `POSTGRES_HOST=127.0.0.1`, токены, `BOT_MODE=sandbox`.
6. [ ] .2 git: сохранить wip (commit/stash) → `git fetch central && git checkout second`.
7. [ ] Перезапустить `uvboot` на .2 → проверить `/api/health`, `/bot/status`, `/bot/state`,
       `/sandbox/status`, свечи идут, позиции восстановились.
8. [ ] Проверить фронт (vitebot 5174), MCP (`test_client.py` PASS), AI-гейт (worker на .2 или .3).
9. [ ] Убедиться, что бот на .3 ОСТАНОВЛЕН (один токен — нельзя два бота одновременно!).
10. [ ] Утром — контроль первой сессии: входы/выходы, guard, AI-гейт, логи.

**Откат** (устарело — план до переезда, НЕ применять к БД): вернуть на .2 `POSTGRES_HOST=192.168.1.3`, остановить uvboot, запустить бота на .3. ⚠️ Актуально с 2026-09-28: Postgres живёт в Docker НА .2 (`Denis@192.168.1.2`, пароль `0987`), доступен СО ВСЕХ машин — на .3 базы нет и не будет.

## 6. Риски

- **Один токен T-Invest**: нельзя одновременно гонять ботов на .3 и .2 (лимиты/дубли заявок).
- **Время**: дамп+restore 5.4 ГБ + установка Postgres — 30–60 мин, окно ночью есть.
- **Место**: PGDATA на X:\ (65 ГБ free), бэкапы дампов не хранить на C:.
- **TZ**: код работает в UTC, Windows-локаль МСК — на данные не влияет (проверить `candles.ts`).
- **Firewall**: 5432 слушать только localhost; 8000/5174 — LAN как сейчас.
- **Windows-venv**: `.venv` на .2 уже используется uvboot; после `checkout second` переустановить
  зависимости, если менялся requirements (`pip install -r requirements.txt`).
