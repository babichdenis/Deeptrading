# MCP_GUIDE — обвязка бота для ИИ: инструменты, промты, безопасность, локальная LLM

> Обновлено 2026-09-15. Сервер: `backend/mcp_server/bot_server.py` (stdio, MCP SDK 2.2.0).
> Тест-клиент: `backend/mcp_server/test_client.py`. Аудит: `docs/roadmap/MCP_AUDIT.md`.

## 1. Что это

MCP-сервер — тонкий stdio-адаптер над HTTP API бота (по умолчанию `http://127.0.0.1:8000`).
Нейросеть через него **видит** состояние бота и **ограниченно управляет** им: SL/TP,
закрытие позиций, пауза входов, запуск replay-тестов. Движок, лимиты риска и
переключение в live через MCP недоступны (см. `docs/MCP_idea.md`).

Где живёт:
- код в репозитории (`backend/mcp_server/`), отдельный venv `.venv-mcp` (mcp + httpx);
- на **.2** уже подключён к opencode (`~/.config/opencode/opencode.jsonc`, сервер `deeptrading-bot`);
- на .3 — та же схема, поменять `BOT_API_URL`.

## 2. Инструменты (21)

### Чтение (14) — безопасно, без гардов
| Инструмент | Что отдаёт |
|---|---|
| `get_status()` | running/mode/error, метрики, портфель, риск, конфиг |
| `get_state()` | позиции (вход/цена/P&L/SL/TP/дистанции/режим/трейлинг) + алерты + guard |
| `get_positions()` | компактно только позиции + equity |
| `get_guard()` | IMOEX guard: active/pct/blocks/stale + человеческая подсказка |
| `get_risk()` | risk.state, daily_pnl, лимит, equity, свободные средства |
| `get_trades(limit)` | последние сделки с P&L и exit_reason |
| `get_events(limit, kind)` | структурная лента: ORDER_FILLED, SIGNAL_REJECTED, IMOEX_GUARD, IMOEX_STALE… |
| `get_logs(limit, grep)` | строки лога с фильтром |
| `get_tests()` | все replay-прогоны со статистикой |
| `get_test_stats(name, from, to)` | срезы: side/regime/ticker/exit_reason/session/quorum |
| `get_trading_status()` | статус MOEX (NORMAL/DISCRETE_AUCTION/закрыто) |
| `get_config()` | текущий конфиг бота |
| `get_screener(limit, sort_by)` | рынок TQBR: цена/оборот/волатильность |
| `get_orders(limit)` | заявки/lifecycle |

### Запись (7) — с гардами
| Инструмент | Действие |
|---|---|
| `set_levels(ticker, sl, tp, confirm, reason)` | правка SL/TP позиции |
| `close_position(ticker, confirm, reason)` | закрытие одной позиции |
| `close_all(confirm, reason)` | закрытие всех позиций |
| `pause_entries(paused, confirm, reason)` | пауза/возобновление новых входов |
| `cancel_pending(confirm, reason)` | снять pending-заявки |
| `run_test(name, replay_start, replay_end, confirm, reason)` | запуск replay-теста (paper) |
| `stop_bot(confirm, reason)` | остановка бота (позиции остаются) |

**Гарды записи (реализованы в сервере):**
1. `confirm=false` → возвращается **dry-run превью** (что именно изменится). Модель
   обязана показать превью человеку и выполнить с `confirm=true` только после «да».
2. **live-блок**: если бот в `mode=live` (реальные деньги), запись запрещена, пока в
   окружении MCP-сервера не выставлено `MCP_ALLOW_LIVE=1`.
3. **Аудит**: каждый вызов пишется в `backend/mcp_server/audit.jsonl`
   (ts, tool, args, reason, dry_run, результат).

## 3. Промты (6) и ресурсы (4)

Промты — готовые сценарии, которые клиент подставляет в диалог:
`digest` (сводка), `positions_review` (разбор позиций), `incident` (диагностика),
`guard_help` (объяснить IMOEX guard), `research_loop` (правила исследований),
`safe_writes` (правила безопасных записей).

Ресурсы: `bot://state`, `bot://status`, `bot://guard`, `bot://config` — JSON-снапшоты
для клиентов, поддерживающих resources.

## 4. Как объяснять нейронке (системный промт)

MCP-сервер уже отдаёт клиенту `instructions` (их видит модель). Для opencode/Claude
можно дополнительно положить файл-инструкцию и подключить его в `opencode.jsonc`:
`"instructions": ["docs/MCP_GUIDE.md"]`.

Готовый текст (можно вставлять в системный промт агента):

```
Ты — ассистент торгового бота Deeptrading (T-Invest, MOEX). Твои инструменты — MCP deeptrading-bot.

ПРАВИЛА:
1. Сначала читай (get_status → get_state → get_guard → get_events), потом делай выводы.
2. Любой write: сначала confirm=false (превью), покажи его человеку, и только после
   явного согласия — confirm=true с параметром reason.
3. Никогда не переключай бота в live, не меняй код движка, лимиты риска и параметры
   стратегий — это только вручную человеком.
4. IMOEX guard — защита от всплесков индекса. Блокировки входов против направления —
   норма, а не ошибка. Если guard показывает stale («НЕТ СВЕЧЕЙ») — это инцидент:
   данные MOEX не идут, защита не работает; сообщи человеку.
5. Отвечай кратко, цифрами и фактами: позиции и P&L в ₽, дистанции до SL/TP в %.
6. Инструмент вернул {"ok": false, "error": ...} — не повторяй вслепую, объясни проблему.
```

Примеры диалогов:
- «Что с ботом?» → `digest`: running/mode, equity, позиции, guard, алерты, 1-2 рекомендации.
- «Закрой SMLT» → `close_position("SMLT", confirm=false)` → превью с P&L → после «да» —
  `confirm=true` → `get_state()` для проверки.
- «Почему бот не входил в 18:30?» → `get_events(kind="SIGNAL_REJECTED")` +
  `get_guard()` → «блокировал IMOEX guard: всплеск +1.2%, SELL-входы против индекса».

## 5. Локальная LLM на .2 — результаты тестов (2026-09-15)

Железо .2: i5-4460 (4 ядра), **6 ГБ RAM** (после чистки свободно ~3.9 ГБ),
GTX 750 Ti 2 ГБ (Ollama использует Vulkan, offload ~42%), Windows 10.

| Модель | Размер | Tool-calling | Скорость | Вывод |
|---|---|---|---|---|
| `qwen2.5-coder:3b` (стояла) | 1.8 ГБ | **нет** (JSON текстом, не `tool_calls`) | ~9 tok/s | не годится для MCP-агента |
| `llama3.2:3b` | ~2.0 ГБ | **да** (корректные `tool_calls`, полный цикл с ответом) | ~8 tok/s | рабочий локальный вариант |

Проверено API-тестами: `llama3.2:3b` вызывает `get_positions`, принимает результат и
формирует ответ («3 позиции, SMLT −120.5, ROSN +45.2, GAZP −8.0»). Первый ответ ~60с
(включая загрузку модели), последующие ~10с.

Рекомендация:
- **Локально (llama3.2:3b)** — лёгкие задачи: сводки, мониторинг, «что с ботом»,
  простая диагностика. Бесплатно, офлайн, без утечки данных.
- **Провайдеры (deepseek/openai/openrouter/… уже подключены в opencode)** — сложные
  многошаговые задачи, длинные логи, планирование, надёжные tool-call цепочки.
- Апгрейд: 16 ГБ RAM позволит 7-8B Q4 (llama3.1:8b/qwen3:8b) локально (~3-4 tok/s),
  но провайдеры всё равно быстрее и умнее.

Ollama на .2: GUI-приложение падает (`Unable to init instance`), но `ollama serve`
работает. Создана задача планировщика `ollama_serve` (ONSTART, SYSTEM, OLLAMA_MODELS),
API: `http://127.0.0.1:11434`.

## 6. Перенос на другую машину

```bat
:: Windows
cd C:\Users\<user>\Dev\Deeptrading\backend
python -m venv .venv-mcp
.venv-mcp\Scripts\python.exe -m pip install mcp httpx
:: конфиг opencode: ~/.config/opencode/opencode.jsonc (mcp.deeptrading-bot, BOT_API_URL)
:: проверка:
set BOT_API_URL=http://127.0.0.1:8000
.venv-mcp\Scripts\python.exe mcp_server\test_client.py   :: ожидаем "ИТОГ: PASS"
```
```bash
# macOS/Linux
python3 -m venv .venv-mcp && .venv-mcp/bin/pip install mcp httpx
BOT_API_URL=http://127.0.0.1:8000 .venv-mcp/bin/python3 mcp_server/test_client.py
```

## 7. Ограничения / TODO

- **Нет авторизации** HTTP API бота (порт 8000 открыт в LAN). Варианты: токен
  (`BOT_API_TOKEN` уже прокидывается сервером) + проверка на бэкенде для write-роутов.
- **Push-алерты** (бота → агента) не реализованы: сейчас только опрос.
- `get_logs` отдаёт текст; структурное — в `get_events`.
- ML-инструменты (`ml_predict`, `ml_conviction`) — после стабилизации модели.
