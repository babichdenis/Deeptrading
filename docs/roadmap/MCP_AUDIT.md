# MCP-аудит бота (2026-09-14, обновлён 2026-09-15)

> Аудит MCP-обвязки торгового бота. Статус: **работает, протестирован на .3 и .2,
> подключён к opencode на .2**. Гайд: `docs/MCP_GUIDE.md`.

## Что это

MCP-сервер `backend/mcp_server/bot_server.py` — stdio-адаптер над HTTP API бота
(порт 8000). Нейросеть (opencode/Claude/локальная LLM) видит состояние, позиции,
сделки, логи, guard IMOEX и может ограниченно управлять ботом.

Запуск: отдельный venv `.venv-mcp` (mcp 2.2.0 + httpx). Основной venv не трогается.
Тест-клиент: `backend/mcp_server/test_client.py` (чек-лист, ожидаем `ИТОГ: PASS`).

## Инструменты (21)

### Чтение (14)
`get_status`, `get_state`, `get_positions`, `get_guard`, `get_risk`, `get_trades`,
`get_events` (структурная лента), `get_logs` (с фильтром), `get_tests`,
`get_test_stats` (срезы), `get_trading_status` (MOEX), `get_config`,
`get_screener` (рынок TQBR), `get_orders`.

### Запись (7) — все с гардами
`set_levels`, `close_position`, `close_all`, `pause_entries`, `cancel_pending`,
`run_test` (replay), `stop_bot`.

**Гарды:** dry-run превью при `confirm=false`; запрет записи в `mode=live`
(пока нет `MCP_ALLOW_LIVE=1`); аудит `audit.jsonl` (ts/tool/args/reason/dry_run/result).

## Промты и ресурсы

- Промты (6): `digest`, `positions_review`, `incident`, `guard_help`, `research_loop`,
  `safe_writes`.
- Ресурсы (4): `bot://state`, `bot://status`, `bot://guard`, `bot://config`.
- `instructions` сервера = системные правила для модели (см. MCP_GUIDE §4).

## Проверки

- **.3 (macOS)**: `test_client.py` → 21 tools, 9 read OK, 2 dry-run OK, 6 prompts,
  4 resources → **PASS**.
- **.2 (Windows)**: venv `.venv-mcp` (Python 3.12, mcp 2.2.0), `test_client.py` → **PASS**;
  конфиг opencode `~/.config/opencode/opencode.jsonc` → сервер `deeptrading-bot`
  (`BOT_API_URL=http://127.0.0.1:8000`).
- Запись: проверены dry-run превью (`pause_entries`, `close_all`); реальные записи
  на .2 (sandbox/paper) доступны с `confirm=true`.

## Закрытые проблемы (было 6)

1. ~~Нет конфигурации MCP-клиента~~ → opencode на .2 подключён; пример для .3 в гайде.
2. ~~Write без гардов~~ → confirm + live-блок + аудит.
3. ~~Стенный текст логов~~ → `get_events` (структурно) + фильтр в `get_logs`.
4. ~~Мелкий баг тикера в `/positions/close` sandbox-fallback~~ → закрытие идёт по
   `figi` из `get_state` (тикер виден в превью), сам роут не менялся.
5. Таймауты: per-call (`get_trading_status`/`get_screener` 60с), ошибки возвращаются
   структурно `{"ok": false, "error": ...}`.
6. Нет авторизации — **остаётся** (см. ниже).

## Остаётся

1. **Авторизация**: `BOT_API_TOKEN` прокидывается сервером, но бэкенд его не проверяет;
   порт 8000 открыт в LAN. Предложение: проверка токена для write-роутов `/api/v1/bot/*`
   (env `BOT_API_TOKEN` на бэкенде; если пусто — как сейчас).
2. **Push-алерты** (бота → агента) — только опрос.
3. **ML-инструменты** (`ml_predict`, `ml_conviction`) — после стабилизации ML-модели.

## Локальная LLM (.2)

- Железо: i5-4460, 6 ГБ RAM, GTX 750 Ti 2 ГБ (Vulkan, offload ~42%).
- `qwen2.5-coder:3b` — **не умеет tool-calling** (JSON текстом).
- `llama3.2:3b` — **умеет** (корректные `tool_calls`, полный цикл), ~8 tok/s, ~2 ГБ.
- Ollama: GUI-приложение падает, `ollama serve` работает; автозапуск — задача
  планировщика `ollama_serve` (ONSTART, SYSTEM).
- Рекомендация: локально — лёгкие сводки/мониторинг; сложные задачи — провайдеры
  (deepseek/openai/openrouter уже подключены в opencode).

## Файлы

- `backend/mcp_server/bot_server.py` — сервер (21 tool, 6 prompts, 4 resources)
- `backend/mcp_server/test_client.py` — тест-клиент (PASS на .3 и .2)
- `backend/mcp_server/audit.jsonl` — аудит write-вызовов (создаётся при записи)
- `docs/MCP_GUIDE.md` — гайд: инструменты, промты, системный промт, локальная LLM
- `.venv-mcp` — изолированный venv (mcp 2.2.0, httpx)
