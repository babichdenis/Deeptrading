# SESSION_SUMMARY_2026-09-08 — Сброс sandbox-счёта + фиксы графика

> **Дата:** 2026-09-08. Короткая сессия: сброс sandbox-счёта в 1 команду,
> исправление перевёрнутой оси времени на графике, MSK-время во фронте.

---

## 1. STATUS LIVE-БОТА

- **Аккаунт:** `5e4d9c6f-b777-410f-abb3-95794fde0d99` (V4_Bot_10k, 10 000 ₽)
- **Бот:** running=True, mode=polling, sandbox, ensemble_v4 (lifespan автостарт)
- **Счёт чистый:** cash=10000, positions_open=0, equity=10000
- **STOPPED**: старый счёт `413306e6-...` закрыт (вместе с позициями)

## 2. СБРОС СЧЁТА (1 СКРИПТ)

`backend/scripts/reset_sandbox_account.py` — полный сброс в одну команду:
удаляет старый счёт с позициями → открывает новый → пополняет → переписывает
ACC в sandbox.py + live_broker.py → перезапускает uvicorn → ждёт автостарт бота.

```bash
cd ~/Dev/Deeptrading/backend
.venv/bin/python3 scripts/reset_sandbox_account.py              # 10k, V4_Bot_10k
.venv/bin/python3 scripts/reset_sandbox_account.py --cash 20000
.venv/bin/python3 scripts/reset_sandbox_account.py --name NewBot
```

Вручную = 6 шагов (в AGENTS.md раздел "Как быстро сбросить sandbox-счёт").
⚠️ Позиции не переносятся; sandbox отклоняет заявки вне сессии (30079),
закрытие позиций вручную ночью не работает — просто удаляйте счёт.

## 3. ФИКСЫ ФРОНТЕНДА (frontend/src/main.ts, НЕ закоммичено)

1. **Перевёрнутая ось времени** — причины: visible-range уезжал за данные при
   `scrollToRealTime()` (свечи отставали на ~2.5ч), `setVisibleLogicalRange`
   мог получать from>to. Фиксы:
   - first-render: `rightOffset: 0` + `Math.min/max` границ окна
   - `ensureForViewport` и `__chartOverlay`: нормализация from/to (min/max)
   - auto-refresh: scrollToRealTime() только если last candle < 180с от now
2. **Auto-refresh embedded** — каждые 8с: fetchAnalysis → renderData(keepView)
   → восстановление маркеров/линий → scrollToRealTime (если данные свежие).
3. **MSK-время** — `Europe/Moscow, hour12:false` в bot.ts/lab.ts/labchart.ts/
   enslab.ts/test.ts/main.ts (9 вхождений). БД остаётся UTC, показ — МСК.
4. **Вертикальная шкала под инструмент** — priceScale autoScale reset в renderData.
5. tsc по main.ts — без ошибок.

Внимание: уточнить в следующей сессии — свечи отстают ~2-3ч даже днём
(последняя 19:31 МСК при 22:20 МСК). Причина не докопана.

## 4. СТАРЫЙ СЧЁТ (закрыт)

- ID: `413306e6-f634-4aef-a553-c84e764b298a`, имя пустое
- Позиций на закрытие было 7 (ALRS -110, SMLT 5, SFIN 1, NLMK -3, SBER 6,
  RUAL -40, ROSN 5) + RUB 10330; equities ~-768 ₽
- Удалён целиком через close_sandbox_account

## 5. ПРОВЕРКИ

- `curl http://127.0.0.1:8000/api/v1/sandbox/status` → cash=10000, positions=0
- `curl http://127.0.0.1:8000/api/v1/bot/status` → running=true
- uvicorn: PID в pgrep, лог /tmp/uvicorn_reset.log (скрипт), /tmp/uvicorn_new2.log

## 6. НОВАЯ СЕССИЯ (утро 08.09): «график постоянно скачет» — CDBG-инструментарий

**Симптом (пользователь):** график постоянно прыгает; при переключении между
акциями все свечи пропадают — видна только самая правая (одна-две свечи).

**Причина (найдена в headless + логах юзера):**
при смене figi авто-refresh (каждые 8с) вызывал `renderData(nd, true)` с
**keepView=true**, а lightweight-charts сохранял логический диапазон **старого
figi** для нового набора данных → диапазон улетал в отрицательные индексы
(`lg=-88..1` → `vr` на 2 минуты) → на экране только крайний бар.

**Фиксы (все в `frontend/src/main.ts`, НЕ закоммичено):**
1. `_renderedFigi` — renderData запоминает figi; если данные пришли для другого
   figi и вызван keepView → принудительный reset (keepView=false).
2. `broken`-детектор в авто-refresh: если `lg.from < -2 || lg.to >= bars+50 ||`
   `lg.from >= bars+50` → авто-сброс `setVisibleLogicalRange` на последние
   WINDOW_BARS.
3. Guard на устаревший fetch: `nd.figi !== wantFigi` → SKIP (рисование не
   мусорит).
4. `__chartOverlay` (runPreview/Lab): исправлен баг `setVisibleLogicalRange`
   с **Unix-таймстампами** вместо индексов → теперь `setVisibleRange({from,to})`
   во времени.
5. scrollToRealTime() — только при `age<180s && pinned` (не выдёргивает из
   истории каждые 8с).

**Инструменты:** CDBG-лог (console + скрытый `div#cdbg` + `document.title`),
видимый оверлей `#cdbg-ov` (слева снизу, 4 последние строки). Для диагностики:
`?embedded=1&autofocus=<FIGI>` — авто-фокус + SNAP-самплер диапазонов каждые 3с.
Headless: `chrome --headless=new --dump-dom`, виртуальное время — см. AGENTS.md.

**Статус:** фиксы залиты на сервер, vite отдаёт новую версию. Юзеру нужен
жёсткий рефреш (Cmd+Shift+R) и повторное переключение между акциями — ожидается
подтверждение, что `lg=-88..1` не воспроизводится (в логе должны появиться
`broken=true` → `auto-broken-reset`).

## 7. ОТЛАДОЧНЫЙ КОД В main.ts (убрать после верификации)

- все `cdbg(...)`, `cdbgView(...)`, блок `autof` (autofocus), оверлей `#cdbg-ov`

## 8. МСК-ВРЕМЯ НА ОСИ ГРАФИКА (добавлено 08.09 днём)

Проблема: ось времени внизу показывала UTC, подсказка — МСК.
В lightweight-charts v5 метки **оси** форматирует `timeScale.tickMarkFormatter`,
а подсказку — `localization.timeFormatter` (разные хуки!). Был задан только
первый. Фикс в main.ts:
- `timeScale: { tickMarkFormatter: (t, type) => axisTickLabel(t, type) }`
- `axisTickLabel()` — мапит TickMarkType→формат: Year/Month/DayOfMonth/Time/
  TimeWithSeconds, всё через `_dtMSK*` (Europe/Moscow). Есть `_mskDateOf()` для
  number|string|BusinessDay. try/catch вокруг форматтеров (иначе исключение
  валит рендер → `lg=null`, пустой график).
- tsc main.ts — 0 ошибок.

Симптом «фронт не отображается» (`renderData keepView=false n=120 lg=null
vr=null`) на главной/дневном ТФ — вероятно был связан с исключением в
tickMarkFormatter; после try/catch и передеплоя должен исчезнуть.
Верификация: embedded headless показывает `renderData-end lg=60..149` (ок).

## 9. ВЕЧЕР 08.09: АУДИТ ПУТИ СВЕЧЕЙ + СТРИМ (продолжение)

Работали с поставкой свечей (журнал: `docs/roadmap/CANDLE_AUDIT_2026-09-08.md`).

**Этап A (логирование) — завершён:**
- `feed.on_log` → `runtime._log` с префиксом `TECHINFO [feed]`; счётчики
  `candles_received/seen/rejected`, флеши persist; `TECHINFO stat` раз в 60с;
  `TECHINFO persist ok` каждые 10 флешей; фильтр «Тех» на фронте (`.lg-tech`).
- **Критический фикс «фронт не видно»:** `/api/analysis/1min` больше НЕ
  блокирует запрос докачкой. `_spawn_ensure_1min` — фоновая задача
  (`asyncio.wait_for(ensure_candles(2h), 12s)`), ответ сразу из БД.
  Замеры: 1min direct 0.16с, через vite 0.39с, day 0.04с, /bot/status 0.004с
  (было: 15с+ timeout).

**Этап B (стрим) — закрыт решением «оставить polling»:**
- B1: retry 3 попытки (1с/3с/10с) + трассировка причины → в деплое.
- B2: `OrderStateStream` — figi резолвится из `instrument_uid` (маппинг
  `_uid_to_figi`), цена из money-полей; B3: битый ордер в try/except.
- **Полная проверка стрима T-Invest (боевой токен): НЕ поднимается НИ ОДНИМ
  транспортом** — bidirectional `CancelledError` 0.0с; менеджер подписок
  открывает стрим (open=1) но шлёт ноль ответов (30с+); server-side стрим —
  баг SDK 1.49.3 (`_UnaryStreamMultiCallable` unexpected 'request_iterator');
  WebSocket `/ws/` → 404. Сертификаты/тариф не при чём (OrderState-стрим на том
  же host работает). **→ Решение: живём на polling** (боевой REST GetCandles
  600/мин, хватает). Стрим-код остаётся fallback (SILENT-таймаут 15с×3 →
  polling).
- Бот подтверждён в `mode=polling`, received растёт (`562→587` за час),
  persist ok без PERSIST_ERR.

**Этап C:** C1–C2 (deque maxlen=2000 + popleft-дрейн) — в живом процессе с
17:50; persist ок. C4 (оптимизация compute_ensemble, источник CPU) — не начата.

**Выбор токена feed (по просьбе):** `Settings.feed_token = tinkoff_live_token
or tinkoff_token` (config.py) + runtime берёт `settings.feed_token`. В `.env`
два токена под комментами; активен `TINKOFF_TOKEN`; переключение =
раскомментировать нужный. `t_tech` верифицирует по вшитому НУЦ — сертификаты
на сервере не нужны.

**Приоритеты дальше:** C4 (compute_ensemble CPU) → либо MOEX-источник свечей
(уже есть `moex.py`), либо Этап D (фронт).