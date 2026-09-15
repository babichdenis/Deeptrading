# AI_GATE_PROMPTS — все промпты AI-гейта (где лежат и как настраивать)

> Гейт: `backend/scripts/ai_approval_worker.py` (воркер) + MCP `backend/mcp_server/bot_server.py`.
> Провайдер на .3: **opencode / big-pickle** (бесплатный Zen через локальный `opencode serve`),
> режим **shadow** (решения не применяются, только пишутся в UI/лог).
> Смотреть в UI: правый сайдбар → вкладка **🤖 AI-гейт** → кнопка **📄** (промпт с бэкенда).

---

## 1. System-промпт воркера (главный — правила решений)

Файл: `backend/scripts/ai_approval_worker.py`, константа **`SYSTEM`**.

Текущий текст:

```
Ты — риск-менеджер торгового бота (MOEX, T-Invest). Бот прислал заявку на вход,
нужно решить: approve (одобрить) или reject (отклонить). Отвечай СТРОГО JSON:
{"decision": "approve"|"reject"|"skip", "reason": "коротко по-русски", "confidence": 0.0-1.0}

Правила (по приоритету):
0. IMOEX (индекс) рассчитывается ТОЛЬКО в основную сессию 09:50–19:00 МСК (пн-пт).
   Если guard.trading=false (утро 06:50–09:50, вечер 19:00–23:50, выходной) — данных индекса
   НЕТ, и это нормально: НЕ отклоняй заявку из-за отсутствия/устаревания индекса. Оценивай
   по риску, концентрации, серии стопов и времени суток. Правило 1 действует только при
   guard.trading=true.
1. REJECT, если guard.trading=true и вход идёт ПРОТИВ направления свежего всплеска IMOEX
   (guard.active=1 и сторона SELL, или guard.active=-1 и сторона BUY).
2. REJECT, если guard.stale=true (индекс не обновляется в основную сессию — инцидент)
   и заявка идёт против рынка/крупная.
3. REJECT, если risk.state != NORMAL или daily_pnl близок к лимиту дня.
4. REJECT при явно негативном контексте: серия стопов по этому тикеру в последних сделках,
   вход против режима рынка.
5. APPROVE, если противопоказаний нет: бот уже прошёл свои фильтры (кворум, режим, guard),
   а вход согласован с направлением индекса/трендом.
6. skip — если данных мало или случай спорный (пусть решит таймаут/человек).
Не выдумывай данные, опирайся только на переданный JSON. Учитывай сессию (МСК): утро/вечер
менее ликвидны, вечером движения чаще ложные.
```

**Как поменять правило:** отредактировать `SYSTEM` → перезапустить воркер (см. §5).
В UI после рестарта промпт обновится (воркер присылает его при старте).

---

## 2. Что уходит модели в каждой заявке (user-сообщение)

Формат: JSON `{"order": {...}, "context": {...}}` (до 12 000 символов).
Собирается в `_ctx(api)` из HTTP API бота:

| Ключ | Источник | Содержимое |
|---|---|---|
| `now_msk` | — | текущее время МСК (модель не гадает) |
| `order` | `/api/v1/bot/approvals` | ticker, side, qty, price, reason (фичи сигнала) |
| `context.positions` | `/api/v1/bot/state` | count, same_side, this_ticker (сторона/qty/pnl/SL/TP) |
| `context.equity` | `/api/v1/bot/state` | эквити |
| `context.guard` | `/api/v1/bot/status` | active/pct/move/trading/stale/last_candle |
| `context.risk` | `/api/v1/bot/status` | state (NORMAL/…), daily_pnl, лимит дня |
| `context.session` | `/api/v1/bot/status` | торговая сессия (утро/день/вечер) |
| `context.recent_trades_ticker` | `/api/v1/bot/trades` | последние 5 сделок ЭТОГО тикера |
| `context.candles_1m` | `/api/analysis/{figi}?interval_name=1min&limit=60` | последние 5 закрытых 1м свечей (t — МСК, o/h/l/c/v) |
| `context.volume` | там же | last, mean50, ratio (объём к среднему за 50) |
| `context.signal_features` | meta заявки | votes / volume_features / regime и т.п. |

**Важно про IMOEX:** индекс публикуется только **09:50–19:00 МСК** (в БД 547 бар/день).
Вне этого окна `guard.trading=false` — правила про всплеск/stale не применяются.

---

## 3. Промпты MCP-сервера (для opencode/Claude — ручное управление)

Файл: `backend/mcp_server/bot_server.py` (декораторы `@mcp.prompt`).
Вызываются в opencode как слэш-промпты MCP-сервера `deeptrading-bot`:

| Промпт | Для чего |
|---|---|
| `digest` | сводка состояния бота (running/equity/позиции/guard/алерты) |
| `positions_review` | разбор открытых позиций, уровни, что делать |
| `incident` | диагностика: guard/stale, ошибки, отклонённые сигналы |
| `guard_help` | объяснить IMOEX guard простыми словами |
| `research_loop` | правила исследований через API (что можно/нельзя) |
| `safe_writes` | правила write-действий (превью → подтверждение → аудит) |
| `approval_gate` | политика решений approve/reject по заявкам гейта |

Плюс **instructions** сервера (`INSTRUCTIONS`) — системные правила для модели-клиента MCP.
Полный гайд: `docs/MCP_GUIDE.md` §4 (готовый системный промт агента + примеры диалогов).

---

## 4. Где смотреть, что модель реально ответила

- UI: вкладка **AI-гейт** — решения с причиной, уверенностью, моделью, latency, shadow-флагом.
- API: `GET /api/v1/bot/ai_decisions?limit=30`, `GET /api/v1/bot/ai_prompt`.
- Журнал воркера: `backend/reports/ai_approval_log.jsonl` (JSONL: ts/order/decision/reason/latency).
- Лог процесса: `/tmp/ai_gate.log` (на .3).

---

## 5. Настройка и перезапуск (на .3)

```bash
# параметры гейта (таймаут/дефолт/вкл-выкл)
curl -X PATCH http://127.0.0.1:8000/api/v1/bot/config \
  -H 'Content-Type: application/json' \
  -d '{"ai_approval": true, "ai_approval_timeout_sec": 15, "ai_approval_default": "approve"}'

# перезапуск воркера (shadow)
cd ~/Dev/Deeptrading/backend
pkill -f ai_approval_worker
nohup .venv-mcp/bin/python3 scripts/ai_approval_worker.py --dry-run > /tmp/ai_gate.log 2>&1 &

# боевой режим (реально применять решения):
nohup .venv-mcp/bin/python3 scripts/ai_approval_worker.py > /tmp/ai_gate.log 2>&1 &

# проверить провайдера без бота:
.venv-mcp/bin/python3 scripts/ai_approval_worker.py --selftest
# другая модель Zen: --model deepseek-v4-flash-free | mimo-v2.5-free | nemotron-3-ultra-free
```

Требуется запущенный `opencode serve` (порт 4096) — бесплатный tier Zen работает только
через opencode. Ключ Zen: `~/.local/share/opencode/auth.json`.

---

## 6. TODO (настройка)

- [ ] Выбрать модель (A/B: big-pickle vs deepseek-v4-flash-free vs mimo-v2.5-free).
- [ ] Донастроить правила в `SYSTEM` под свои приоритеты (концентрация, серия стопов, время суток).
- [ ] Опционально: вынести правила в `backend/data/ai_gate_config.json` (пороги) вместо текста.
- [ ] Фолбэк-цепочка: big-pickle → DeepSeek API → локальный Ollama (.2).
- [ ] После наблюдения shadow — включить боевой режим (убрать `--dry-run`). ✅ **включён на .3**

## 7. Саморегуляция: совет (advice) → правила бота

Модель отвечает не только решением, но и **советом** (`advice`, 1 короткая фраза):

```json
{"decision": "reject", "reason": "3 убытка подряд по RNFT", "advice": "пауза по тикеру 1ч", "confidence": 0.72}
```

Советы копятся в `/api/v1/bot/ai_decisions` и показываются во вкладке AI-гейт (строка 💡).
**Сейчас совет не исполняется автоматически** — это витрина для человека.

Роадмап саморегуляции (постепенно):
1. **Сбор** — накопить советы по типам (`hold_ticker`, `reduce_size`, `tighten_sl`,
   `pause_entries`, `check_data`, `ok`) + статистика «совет → исход».
2. **Типизация** — попросить модель возвращать `advice_type` из фиксированного списка
   (enum), чтобы можно было маппить в действия.
3. **Полуавтомат** — для безопасных действий (например, `hold_ticker` = тот же loss-streak
   hold, но с порогом от AI) бот исполняет совет, с уведомлением в UI.
4. **Авто** — AI сам выставляет `loss_streak_n/hold_min`, паузы по тикерам, размер позиции
   (в пределах лимитов риска), с полным аудитом в `ai_approval_log.jsonl` и UI.

Уже реализовано как первые «правила»: loss-streak HOLD (2 убытка → пауза 1ч),
тройное подтверждение входа (3×1м растущих закрытий для BUY), IMOEX guard,
per-ticker beta-фильтр, chase-блок. AI-советы должны постепенно стать
следующим слоем поверх этих детерминированных правил.
