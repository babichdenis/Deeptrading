# AGENTS.md — гид для ИИ-агентов

> **Сначала прочитай `MEMORY.md`** — единая память проекта: карта файлов, текущий статус,
> договорённости команды, операционные заметки. Этот файл — гиды/ограничения, в `MEMORY.md` —
> актуальное состояние. Общаемся/планируем через `MEMORY.md`, переписка субагентов — в `chat.md`.

Проект: **Deeptrading** — исследовательская платформа + будущий торговый бот для акций
Мосбиржи через T-Invest API (Т-Банк). Полное ТЗ: `main_plan.md` (живой документ, может меняться).

## Инфраструктура (важно!)

**IP-адреса сдвинулись после ребута роутера (01.10.2026) — роли прежние, проверено:**

| Машина | IP | Роль |
|---|---|---|
| MacBook «код» | 192.168.1.6 | Здесь правим код (opencode); в старых документах числился как .7 |
| MacBook «сервер» | 192.168.1.7 | FastAPI backend + Vite frontend + git (бывший .3) |
| **БД (Postgres 16, Docker)** | **192.168.1.7 (сервер)** | **БД живёт на сервере: `deeptrading:deeptrading@192.168.1.7:5432/deeptrading`, доступна со всех машин. Старый адрес был `192.168.1.2` — больше не работает. Локальный dev-PG14 на маке-коде (`127.0.0.1:5432`) — песочница, не источник.** |
| Windows-раннер | 192.168.1.8 | живой бот/таски (`uvicorn_test`), ssh `nadts@192.168.1.8` (бывший .2); :8000 после ребута не поднимался |

Папка проекта `/Volumes/Dev/Deeptrading` — это сетевой диск с машины .7 (сервера; монтируется по mDNS-имени, шара пережила смену IP).
**Правила:**
- НЕ устанавливать node_modules/venv на сетевой диск с этой машины (SMB бьёт тысячи мелких файлов).
  - venv Python: `~/.venvs/deeptrading` (локально на каждой машине свой)
  - npm install запускать только на машине, где диск локальный
- Код правим здесь — он сразу виден на .3; фронт перезагружается сам (HMR),
  бэк запущен с --reload.

## Стек

- Backend: Python 3.11, FastAPI, SQLAlchemy 2 async, asyncpg, PostgreSQL 16 (боевая: `192.168.1.7:5432`, Docker; локальный dev-PG14 на маке — песочница)
- SDK: `t-tech-investments` → импорт `from t_tech.invest import Client` (НЕ tinkoff.invest!)
- Frontend: Vite + TypeScript + lightweight-charts v5

## Команды

```bash
# Backend (на .3 или локально для отладки)
cd backend && zsh dev.sh            # venv + uvicorn --reload на :8000
# локальный запуск из macOS:
~/.venvs/deeptrading/bin/uvicorn app.main:app --reload   # из папки backend/

# Тесты движка (golden scenarios)
cd backend && ~/.venvs/deeptrading/bin/python -m pytest tests -q

# Frontend (на .3)
cd frontend && npm install && npm run dev    # :5173
BACKEND_URL=http://<ip>:8000 npm run dev     # если бэк не локальный
```

## Деплой/рестарт на .8 (Windows runner, бывший .2, где крутится живой бот)

```bash
# Деплой файлов (sshpass локально установлен):
sshpass -p 0987 scp -o StrictHostKeyChecking=no <file> nadts@192.168.1.8:C:/Users/nadts/Dev/Deeptrading/<path>
# Рестарт backend — НЕ скрипт, а таск планировщика:
#   таск `uvicorn_test` → C:\Users\nadts\run_uvicorn.bat → uvicorn app.main:app :8000
sshpass -p 0987 ssh -o StrictHostKeyChecking=no nadts@192.168.1.8 \
  "powershell -NoProfile -Command \"Get-CimInstance Win32_Process -Filter \\\"Name='python.exe' AND CommandLine LIKE '%uvicorn%'\\\" | ForEach-Object { Stop-Process -Id \\\$_.ProcessId -Force -ErrorAction SilentlyContinue }; cmd /c schtasks /run /tn uvicorn_test\""
# После рестарта wait ~20с и проверка: curl http://192.168.1.8:8000/api/v1/bot/status (running=true)
# (сейчас :8000 отвечает на сервере .7 — там подняты параллельные дев-бэкенды)
# Vite на .2: таск `vitebot` (Не убивать kill — респавнится).
# Логи: /bot/logs держит ~300 записей (кольцо); история большего срока — из БД.
```

## Сброс sandbox-счёта (1 команда) — НОВЫЙ механизм (2026-09-18)

Сброс делает САМ бэкенд через `POST /api/v1/sandbox/reset`; скрипт — только клиент.
Это единственный правильный способ (ручной сброс на одной машине оставляет другой
хост на «мёртвом» аккаунте).

```bash
# локальный бэкенд:
/usr/local/bin/python3 ~/Dev/Deeptrading/backend/scripts/reset_sandbox_account.py
# или бэкенд на .8 (Windows):
/usr/local/bin/python3 ~/Dev/Deeptrading/backend/scripts/reset_sandbox_account.py --host http://192.168.1.8:8000
# опции: --cash 20000 --name NewBot --keep-history (не стирать сделки/историю)
```

Эндпойнт: останавливает бота → закрывает ВСЕ счёта с именем `--name` (дубли/«сироты») +
аккаунт из `.env` → открывает новый → пополняет с ретраями (сверка по фактическому
балансу; при неудаче — откат: счёт закрывается) → переписывает `SANDBOX_ACCOUNT` в
`backend/.env` → чистит `get_settings`-кэш → стирает сделки/историю/lоги текущей сессии
(`sandbox_trades, paper_trades, paper_positions, paper_accounts, ai_decisions, bot_logs`)
→ автостарт бота → sync `paper_accounts`.

⚠️ Если эндпойнт возвращает 502 «не удалось пополнить» — это АВАРИЯ upstream T-Invest
(`SandboxPayIn UNAVAILABLE`, бывает ночами/вне сессии). Механизм сам сделает откат,
повторить команду в сессию (пн 06:50 MSK).

⚠️ Sandbox отклоняет ордера вне торговых часов (ошибка 30079) — закрывать позиции
ночью вручную нельзя, просто удаляйте счёт целиком (как делает reset).

## Структура

```
backend/app/
├── engine/           # ЯДРО: чистый детерминированный backtest-движок (без БД/API)
│   ├── models.py     # Candle, Signal, Position, Trade, enums
│   ├── costs.py      # CostModel: комиссия+slippage+тик
│   ├── exits.py      # ExitPolicy: fixed_sl_tp, atr_stop; правило STOP_LOSS_FIRST
│   ├── sessions.py   # SessionPolicy moex_intraday_v1 (cutoff, weekend, overnight)
│   ├── policies.py   # SignalPolicy (ignore_same_side, min_hold), Strategy protocol
│   ├── strategies.py # MacdCross, DonchianBreakout + STRATEGY_REGISTRY
│   ├── ledger.py     # TradeLedger (сделки + audit log + fingerprint)
│   └── runner.py     # EngineRunner: canonical loop
├── services/
│   ├── tinvest.py    # загрузка данных Т-Инвестиций (акции, свечи)
│   └── indicators.py # SMA/EMA/MACD/RSI для API-графика
├── api/routes/       # instruments, candles, analysis
├── models/           # ORM: Instrument, Candle (Postgres)
└── main.py
frontend/src/         # main.ts — график (свечи+SMA+MACD), api.ts
docs/PROGRESS.md      # статус работ — обновлять при каждом завершённом блоке!
tests/                # pytest: golden scenarios движка
```

## Canonical timing (святые правила движка)

1. Бар t закрывается → стратегия видит только candles[0..t]
2. SignalPolicy решает: entry/exit/ignore/reject (всё пишется в audit)
3. Исполнение на open бара t+1 (next_open), slippage против нас
4. Intrabar stop/target проверяются с bar t+1; конфликт в одном баре → STOP_LOSS_FIRST
5. Гэп через стоп → выход по open (хуже стопа)
6. Конец данных → принудительное закрытие end_of_data
7. Один FIGI = одна позиция; same-side игнор; усреднение/пирамидинг запрещены
8. Прогрев буферов НЕ холодный: бот на старте грузит ~3 дня 1m-свечей из БД
   (hot-add, warmup_bars=50, ensemble ≥120 баров, ~20–40с) и живёт всегда тёплым.
   **Parity/audit-реплей обязан греться ТАК ЖЕ из БД**, иначе холодные буферы →
   кворум не набирается → реплей даёт 0–3 сделок вместо реальных 35. Это
   артефакт теста, НЕ «бот не торгует». Parity-0/3 != повод чинить движок.

Новый код движка обязан покрываться golden-тестами (`backend/tests/test_engine_golden.py`).

## Соглашения

- Комментарии в коде запрещены; самодокументируемые имена + docstrings только где критично
- Время везде UTC (tz-aware); MSK только на границе UI
- Денежные расчёты движка — float (исследование); Decimal не нужен до live
- Каждый эксперимент должен быть воспроизводим: fingerprint(ledger) == fingerprint повторного прогона

## Поток рыночных данных (правило!)

```
T-Invest API → ensure_candles() [докачка ТОЛЬКО недостающего] → PostgreSQL → потребитель
```

- Любой блок (график, эксперимент, стратегия) берёт свечи ИЗ БД, никогда напрямую с биржи
- `POST /api/candles/{figi}/sync` — это ensure: проверяет покрытие в БД, качает лишь дыры;
  повторный вызов бесплатен (0 запросов к бирже)
- `GET /api/candles/{figi}/coverage` — что уже лежит по интервалам
- Лимиты истории по умолчанию ~3-4 мес (backend DEFAULT_DAYS=120; фронт ENSURE_DAYS)

## СЛЕДУЮЩАЯ СЕССИЯ — СТАРТ (обязательно)
Сделать БЕЗ вопросов, в этом порядке:
1. Запустить ТЕСТОВОГО БОТА на .8 (nadts@192.168.1.8, бывший .2): те же 20 тикеров, cash 10000, тот же runtime с прогревом из БД (3д 1m, ensemble≥120). НЕ audit_replay, НЕ parity — именно бота (main.py / run_bot_engine).
2. Снять его сделки за те же сутки, что берём у Live.
3. Сравнить построчно (real Live ↔ real .2): те же входы/выходы/сессии утро|день|вечер. Расхождения — тайминги стрима/прогрева/книг.
4. Месяц — после того как сутки сошлись.
