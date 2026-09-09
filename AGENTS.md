# AGENTS.md — гид для ИИ-агентов

> **Сначала прочитай `MEMORY.md`** — единая память проекта: карта файлов, текущий статус,
> договорённости команды, операционные заметки. Этот файл — гиды/ограничения, в `MEMORY.md` —
> актуальное состояние. Общаемся/планируем через `MEMORY.md`, переписка субагентов — в `chat.md`.

Проект: **Deeptrading** — исследовательская платформа + будущий торговый бот для акций
Мосбиржи через T-Invest API (Т-Банк). Полное ТЗ: `main_plan.md` (живой документ, может меняться).

## Инфраструктура (важно!)

Два ноутбука в одной сети:

| Машина | IP | Роль |
|---|---|---|
| MacBook «код» | 192.168.1.7 | Здесь правим код (opencode) |
| MacBook «сервер» | 192.168.1.54 | Postgres + FastAPI backend + Vite frontend |

Папка проекта `/Volumes/Dev/Deeptrading` — это сетевой диск с машины .54.
**Правила:**
- НЕ устанавливать node_modules/venv на сетевой диск с этой машины (SMB бьёт тысячи мелких файлов).
  - venv Python: `~/.venvs/deeptrading` (локально на каждой машине свой)
  - npm install запускать только на машине, где диск локальный
- Код правим здесь — он сразу виден на .54; фронт перезагружается сам (HMR),
  бэк запущен с --reload.

## Стек

- Backend: Python 3.11, FastAPI, SQLAlchemy 2 async, asyncpg, PostgreSQL 16 (.54:5432)
- SDK: `t-tech-investments` → импорт `from t_tech.invest import Client` (НЕ tinkoff.invest!)
- Frontend: Vite + TypeScript + lightweight-charts v5

## Команды

```bash
# Backend (на .54 или локально для отладки)
cd backend && zsh dev.sh            # venv + uvicorn --reload на :8000
# локальный запуск из macOS:
~/.venvs/deeptrading/bin/uvicorn app.main:app --reload   # из папки backend/

# Тесты движка (golden scenarios)
cd backend && ~/.venvs/deeptrading/bin/python -m pytest tests -q

# Frontend (на .54)
cd frontend && npm install && npm run dev    # :5173
BACKEND_URL=http://<ip>:8000 npm run dev     # если бэк не локальный
```

## Сброс sandbox-счёта (1 команда)

```bash
cd backend
.venv/bin/python3 scripts/reset_sandbox_account.py              # 10 000 ₽, имя V4_Bot_10k
.venv/bin/python3 scripts/reset_sandbox_account.py --cash 20000
.venv/bin/python3 scripts/reset_sandbox_account.py --name NewBot
```

Удаляет старый счёт (с позициями) → открывает новый → пополняет → переписывает
ACC в `app/api/routes/sandbox.py:11` и `app/bot/live_broker.py:22` → перезапускает
uvicorn → ждёт автостарт бота. Актуальный sandbox-аккаунт: `5e4d9c6f-b777-410f-abb3-95794fde0d99`.
Позиции НЕ переносятся; sandbox вне торговых часов отклоняет ордера (ошибка 30079),
закрывать позиции вручную ночью нельзя — просто удаляйте счёт целиком.

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
