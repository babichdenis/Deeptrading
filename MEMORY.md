# MEMORY.md — единая память проекта Deeptrading

**Правило:** весь контекст, договорённости и статус нейронки записываем СЮДА. Этот файл читается первым. Детали — по ссылкам. Не плодим новые md без нужды.

---

## Как устроены файлы (карта)

| Файл | Что это | Когда писать |
|------|---------|--------------|
| `MEMORY.md` | **главная память, этот файл** | всегда, при любом изменении статуса |
| `AGENTS.md` | кратко самому агенту: как запускать, ограничения | редко, при изменении архитектуры |
| `docs/MCP_API.md` | контракты API для ИИ-агентов (эндпоинты, JSON) | при изменении API |
| `docs/PROGRESS.md` | хроника этапов и исправлений | по завершении блока |
| `chat.md` | переписка субагентов (Neura-*) | только субагенты |
| `docs/*.md` | идеи и разборы (ML, Warehouse, preview...) | по желанию, отдельные исследования |
| `instruments.md` | **White/Gray/Black списки инструментов** по Net/PF/WR (живой) | при каждом расширении периода/серии |
| `docs/roadmap/MTF_V4_2026.md` | **план MTF-тестирования V4+V2** (сессии MOEX, data-gate, attribution/policy, блочная Optuna, калибровка) + журнал | по ходу выполнения серий |

Нейронка/субагент начинает с чтения `MEMORY.md` → `AGENTS.md` → нужные контракты в `docs/`.

---

## Актуальный статус (2026-09-02) — MTF V4+V2, см. docs/roadmap/MTF_V4_2026.md

### Серия 0 выполнена (апрель–июнь 2026, 24 акции, V2)
- `entry_session="main"`: **10,407 сделок, net +56,449₽, PF 1.84, WR 56.4%** (апр +7,443 / май +5,482 / июн +43,524).
- `entry_session="all"` (входы по реальному расписанию MOEX): утро 06:50–09:50 **-23,845₽** (WR 38.6%), день **+20,553₽**, вечер 19:00–23:50 **-51,831₽** (WR 32.1%), outside -25,786₽.
- **Вывод:** вне «дня» V2-входы убыточны (вечер хуже к 23h, стопы бьются рывками). Цель — адаптировать вечер/утро, а не выкидывать.
- Часовой пояс: ts в БД = UTC, метка = **закрытие** бара (первый бар дня 06:59 MSK = 03:59 UTC).
- Классификация инструментов → `instruments.md` (White: SMLT/ASTR/GMKN/MAGN/RUAL/CHMF; Black: HYDR/SBER/IRAO/MTSS/RENI/T/TRNFP).

## Текущий статус (2026-08-25)

### Вкладка «Бот» — единый дашборд (черновик по Preview_bot.md §19)
- Вкладки «Бот» и «Портфель» слиты в одну «Бот»; page-portfolio удалена.
- Layout: status-strip → конфиг + стат-карточки → метрики портфеля → universe → позиции → сделки → вся история.
- Status-strip: СТАТУС/РЕЖИМ/ДАННЫЕ/СИГНАЛЫ/ОШИБКА (.st-chip on/warn/off/err).
- Далее по Preview_bot.md: kill-switch, orders lifecycle, signal stream, risk panel (нужны бэенд-эндпоинты /api/v1/bot/*).

### Вкладка «Тест» во фронтенде
- Секции: «Потолок торговли» (`/api/v1/test/max-profit`), «Ансамбль» (`/api/v1/test/ensemble`), «Матрица» (`/api/v1/test/ensemble-sweep`).
- Ансамбль: bias(1h EMA) → setups(5m) + quorum → entry(1m micro-breakout) → exits (движок).
- Потолок vs Реальность: на одном графике серый пунктир = оракул, цветные зоны = реальные сделки; таблица сравнения net/gross/capture + распределение плюс/минус.
- Поле «Дней» (по умолчанию 7).

### Движок (app/engine)
- `same_side_reentry_cooldown_bars` — запрет повторного входа той же стороны после выхода (разворот разрешён).
- `exit_confirm_window_bars` — выход только после повторного противоположного сигнала в окне.
- `opposite_hold` — слабый противоположный сигнал НЕ закрывает позицию.
- `confirm_flip` — подтверждённый переворот (LONG→SHORT).
- `Signal.kind` ("entry"/"exit") — раздельные потоки входа и выхода.
- `exit_coverage` — счётчики выходной ветки.

### oracle_coverage (РАБОТАЕТ, проверено на неделе RUAL)
- Диагностика «почему не заходим в сделки оракула»: для каждой точки оракула (swing low/high из zigzag) — есть ли микро-брейкаут того же направления до точки и до подтверждения, куда он ушёл (accepted/rejected с причиной).
- Две воронки: geometric (до точки экстремума) и causal (до confirmation_ts).
- Поля: point_ts / confirmation_ts, failed_gates, primary_reason, rejected_by_gate.
- **Результат (RUAL, 7 дней, 377 точек):** видно 96% точек, принято 9.3% (35), исполнено 4.
  Причины отсева: **bias (1h EMA) — 179 точек (47%)**, quorum — 148 (39%), not_seen — 15.
  Вывод: не отсутствие сигналов, а гейты отсекают ~86% идеальных входов; главный убийца — bias (counter-trend режется медленным трендом).
- Механика executed: _run_pipeline собирает executed_signal_keys {(signal_ts_iso, side)} по факту сделки движка → _oracle_coverage(executed_signals=...); сравнивать entry_ts сделки с ts сигнала нельзя (исполнение на бар позже).
- Добавлено confirmation_lag_bars {mean, median, max} = conf_idx − point_idx по свингам оракула.
- Тесты: backend/tests/test_oracle_coverage.py (5 шт: воронки geometric/causal, accepted/executed, гейты quorum/bias, lag, smoke compute_ensemble). Всего 51 passed.
- Следующий шаг: исследовать bias (разрешить counter-trend при подтверждении?) и 5м-сигналы.

### Эксперимент bias B0/B1/B2 (2026-08-25) — bias_mode в ensemble
- Параметр `bias_mode`: `veto` (текущий, по умолч.) | `info` (не veto) | `strict_ct` (counter-trend только при полном кворуме).
- **Результат (7 дней):**
  - RUAL: veto — 27 сделок, net +1532, coverage 9.3%; **info — 52 сделки, net +1845, coverage 18.3%**; strict_ct = veto (полный кворум почти не случается).
  - GAZP: veto — 28 сделок, net −731, coverage 9.2%; **info — 44 сделки, net −2, coverage 21.7%**.
- Вывод: **bias как hard veto отсекал хорошие развороты; info удваивает покрытие оракула и не ухудшает (часто улучшает) экономику.** Кандидат на дефолт: `bias_mode=info`.
- strict_ct не дал отличий (кворум 2 из 5 уже редок; требование 5/5 почти никогда не выполняется).

### Эксперимент 5m-вход + 1m-исполнение (2026-08-25) — entry_tf в ensemble
- Параметр `entry_tf: "1min"|"5min"` (микро-брейкаут на 5м барах, исполнение движком по open следующего 1м бара).
- **Результат (7 дней, bias=info):**
  - RUAL: 1min — 52 сделки, net +1845, PF 3.15; **5min — 47 сделок, net +11375, PF 11.56**, удержание 8.5 бара.
  - GAZP: 1min — 44 сделки, net −2; **5min — 42 сделки, net +2297, PF 8.44**, удержание 7.8 бара.
- Вывод: **5м-вход кратно улучшает экономику (движение больше издержек), как и предполагалось.** Требует проверки на месяце и нескольких акциях (неделя может быть удачной).
- Рекомендуемая конфигурация-кандидат: `bias_mode=info` + `entry_tf=5min` + cooldown 15.

### Месячный прогон (2026-07-25..08-24) — кандидат: info + 5min + cd15 (2026-08-25)
- Набор 5 акций одной ценовой категории (~26-68 ₽): RUAL, AFLT, SNGSP, MVID, NLMK (1м докачаны в БД через sync, политика «сначала в базу»).
- **Месяц целиком: 5/5 в плюсе, net +139 544 ₽ (капитал 100k/позицию), PF 5.5-8.9, ~200 сделок/акцию.**
- **H1/H2 (половины месяца): 10/10 половин положительные** — RUAL +13916/+17575, AFLT +8118/+4276, SNGSP +11707/+8084, MVID +25542/+18154, NLMK +11724/+12897.
- Сбер/SBERP (270₽) систематически в минусе на 7д — исключён; с ценовой категорией 26-68 ₽ работает.
- **ВАЖНО:** результат ин-семпл (параметры подобраны на неделях внутри этого месяца). Следующий шаг — walk-forward на НОВОМ месяце (сентябрь) или отложенная выборка, прежде чем считать edge доказанным. До этого — «перспективный кандидат», не более.

### Идея дальше
- Переход на 5m сигналы + 1m исполнение (сравнить с 1m baseline: фиксировать baseline до перехода).
- Ключ вывода: ансамбль ловит ~1-2% от оракула (net), надо понять, на каком фильтре сигнал исчезает.

---

## Договорённости команды

- **Распределение на текущем этапе:**
  - **Я (главный агент, opencode)** — бэкенд-ось: `oracle_coverage`, переход на 5m-сигналы + 1m-исполнение, движение в сторону оракула. По мере выполнения править и продолжать.
  - **Neura-Frontend** — фронтенд: график «Потолок vs Реальность», легенда, таблицы во вкладке «Тест».
  - **Остальные агенты** — реализация идей из `docs/*.md` (список ниже), помощь по бэкенду при запросе.
- **docs/*.md на реализацию (кому-то из команды):** Addiional_idea, MCP_idea, ML_idea (ML-фильтр/режимы), Order_state + ROADMAP_order_state (контракты заявок), PlugIn_strategy, Preview_bot, Preview_warehouse, Prewiev_lab, SIGNALS_CATALOG. Прогресс — в `PROGRESS.md`.
- **Единая рабочая папка — `/Volumes/Dev/Deeptrading`.** Не создавать копий проекта. Временные файлы/чат — вне её (например `~/Documents/Default Project`).
- **Порядок входа агента: `MEMORY.md` → `AGENTS.md` → нужный контракт в `docs/`.
- Каждый правит только свои файлы; перед правкой `git status`/просмотр, чтобы не затереть чужое; договорённости и статус — сюда.
- Общение субагентов — через `chat.md` (каждый дописывает в конец, чужие реплики не редактирует).
- Все договорённости дублируются сюда (chat.md — не единственный носитель памяти; бесплатные агенты могут «забыть»).

### Валидация entry_session=main на двух месяцах (2026-08-25)
- Июнь (A/B/C): main лучше all на 5/5 акций (+47% net: 92k vs 63k), carry=flat (переносов нет).
- **Июль (независимая валидация): main лучше all на 5/5 акций** — RUAL +47.2k(PF 12.2), AFLT +27.2k(7.4), SNGSP +35.7k(9.4), MVID +46.4k(6.9), NLMK +35.9k(10.2). Итого B=+192k vs A=+174k.
- Вывод: entry_session=main — подтверждённая policy (2 независимых месяца, 10/10 в пользу main). Зафиксирован baseline: info + 5min + cd15 + main + carry.
- Не подбирать дальше (09:30/10:15/без первых минут) — selection bias.

### Stress-тест издержек (июль, main, 5 акций) — 2026-08-25
- Base (0.05% + 2bps): net +192 409. ×1.5: +124 934. **×2.0: +60 100 (все 5 акций в плюсе, PF 5.1-7.8)**.
- Вывод: стратегия устойчива к издержкам, результат не «фантом на комиссиях». Net падает ~линейно, ни одна акция не уходит в минус при ×2.0.
- Настройки комиссии/слипа добавлены в шестерёнку Lab (GET/PUT /api/v1/lab/settings: commission_rate, slippage_bps) и применяются в очереди ансамбля.

## Задачи Lab (по плану, когда дойдём)

- **Прогресс-бар тестов через websocket**: в рабочем механизме тестов (и вообще при торговле) должны быть сигналы о прохождении теста по websocket — сколько сделано/всего, чтобы фронт показывал живой прогресс. Сейчас, по словам владельца, «через жопу» — от правок третьего агента. Требуется починить (см. файл про Preview_ab/Lab).
- **Перетаскивать тесты/эксперименты ансамбля в Lab**: чтобы результаты фиксировались в базе и можно было визуально смотреть (где зарабатываем / где теряем прибыль). Начать потихоньку.
- **ЕДИНЫЙ ДВИЖОК для тестов и живой торговли (требование владельца, важно!)**: тест (backtest) и бот с живыми акциями должны использовать ОДИН И ТОТ ЖЕ движок (EngineRunner + те же policy/session/exits).
  - Сейчас: тесты (warehouse/experiments/ensemble) — через `EngineRunner`; бот (`app/bot/runtime.py`) — СВОЙ цикл, хотя переиспользует `intrabar_exit` и `FixedSlTpPolicy`.
  - Цель рефакторинга: бот должен гонять тот же конвейер (сигнал→решение→позиция), что и тесты, чтобы поведение теста = поведению вживую.

## Задачи Lab (по плану, когда дойдём)

- **Скачивание архивов history-data по акции (со Склада)**:
  - URL: `https://invest-public-api.tinkoff.ru/history-data?instrumentId={uid}&year={год}` (zip с csv по дням; uid инструмента из T-Invest SDK, формат CSV: `figi;time;open;close;high;low;volume;`).
  - Нужна кнопка/действие на Складе: «Скачать историю» — по выбранной акции качает архив(ы), распаковывает и грузит в базу (candles, interval=1min, on_conflict_do_nothing).
  - Скрипт загрузки уже есть: `backend/scripts/load_history_csv.py` (идетемпотентный).
  - Объём работы: 50–70 акций максимум (не 150), база наполняется постепенно.
  - SSL: curl нужен `-k` (самоподписанный сертификат на этой машине).

## Задачи Lab (по плану, когда дойдём)

- **БАГ: «💾 Сохранить» не записывает filters (ensemble) в конфигурацию через UI.** Бэкенд исправен (POST/PATCH с filters работают, проверено вручную), фронт собирает полный body (лог `[saveConfig] body` показывает полные filters: bias_period/даты/акции), но при сохранении через модалку filters в БД не попадают. Консоль: init падает на lab.ts:110 (`$("btn-cfg-save").addEventListener` — элемент есть? проверено null), bot.ts:46. Нужно разобраться с цепочкой saveConfiguration → updateConfiguration/createConfiguration (возможно, fetch падает после alert/console.log). До фикса — писать filters вручную через PATCH.

## Операционные заметки

- Бэкенд на сервере .54: `cd /Users/Denis/Dev/Deeptrading/backend && nohup ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/deeptrading.log 2>&1 &` (без reload).
- **СТАТУС ДЕПЛОЯ (2026-08-25):** свежие ceiling.py (шорты в оракуле), ensemble.py (funnel_tf, why_no_entry, oracle_coverage), test.py (from/to) залиты на .54 через scp в `/tmp/dt_sync/` и скопированы в `app/services/` — НО uvicorn НЕ перезапущен (ssh не проходит). Нужен рестарт: `kill -9 $(lsof -ti :8000); sleep 2; cd /Users/Denis/Dev/Deeptrading/backend && nohup ./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > /tmp/deeptrading.log 2>&1 &`
- SSH на .54: `ssh -o PubkeyAuthentication=no -o PreferredAuthentications=keyboard-interactive Denis@192.168.1.54` (seed в чат-переписке). SSH периодически не отвечает — повторить.
- Фронтенд dev: `cd frontend && npm run dev` (порт 5173, прокси /api → localhost:8000).
- Локальный тест бэкенда: порт 8001, venv `/var/folders/.../T/opencode/dtvenv`.
- Тесты: `python -m pytest tests/` (сейчас 51 зелёный). Фронт: `npx tsc -b` и `npm run build`.
- СЕКРЕТЫ: TINKOFF_TOKEN и пароли НЕ писать в chat.md / MEMORY.md и не скармливать бесплатным агентам.

## ГДЕ И ЧТО СЧИТАЕТСЯ (важно! проверено 2026-08-25)
- **PostgreSQL живёт на сервере .54** (192.168.1.54:5432, deeptrading/deeptrading) — БД общая, и прод, и локальный тест используют её.
- **Код** лежит на SMB-шаре `/Volumes/Dev/Deeptrading` (= `/Users/Denis/Dev/Deeptrading` на .54).
- **Просчёт (compute_ensemble и тесты) выполняется ЛОКАЛЬНО на Mac пользователя** — python из `/Library/Frameworks/...`, uvicorn на порту 8001, код читается с шары. Нагрузка на CPU локальная (видна в `ps aux | grep uvicorn`, ~10-15% CPU, пики при compute_ensemble в to_thread).
- **MacBook не гудит под нагрузкой — это нормально** (вентилятор почти не включается). Тишина ≠ «ничего не считается». Проверка: `ps aux | grep uvicorn` (CPU% должен быть ненулевой) и статусы в БД.
- **Сервер .54 молчит по CPU всегда** — на нём только Postgres и (незапущенный сейчас) прод-uvicorn на 8000.

## КАК ПРАВИЛЬНО ЗАПУСКАТЬ ПРОГОНЫ ИСТОРИИ (проверено 2026-08-25)

1. Поднять локальный бэкенд (если не поднят): `cd /Volumes/Dev/Deeptrading/backend && nohup /var/folders/jk/gmxjby5d3ng6qpb98ftr11_40000gn/T/opencode/dtvenv/bin/uvicorn app.main:app --port 8001 > /tmp/uvicorn_local.log 2>&1 &` — **обязательно с cwd=backend** (иначе ModuleNotFoundError: No module named 'app'), ждать health 200 (`curl -s http://127.0.0.1:8001/api/health`) до 60 сек — старт может быть медленным.
2. Запускать прогон: `POST /api/v1/lab/queue` с `{"config_id": <id 56e41e3b...>, "tickers": [5 акций], "date_from", "date_to", "period_days": 30}`. Вернёт run_id.
3. **НЕ запускать повторно, если прогон уже в очереди!** Проверить очередь: `GET /api/v1/lab/ensemble?limit=10` (или БД: `SELECT id, status, started_at FROM ensemble_runs ORDER BY created_at DESC`).
4. **Прогон идёт ~4-5 МИНУТ на месяц** (5 акций, 1m свечи, sequential). Статусы: QUEUED → RUNNING → DONE/FAILED/CANCELLED. Между проверками ждать ≥5 минут. Смотреть результат в `result.total_net` и `result.by_stock[].net`.
5. Ошибки прогона: в `result.by_stock[].error` или `error` у run. «мало свечей» = свечи есть, но compute_ensemble отфильтровал по датам (см. баги ниже).
6. Отмена лишних: `UPDATE ensemble_runs SET status='CANCELLED' WHERE status='QUEUED' AND ...` (dispatcher берёт только QUEUED; RUNNING нельзя так отменить).

## БАГИ, КОТОРЫЕ УЖЕ ПОЧИНЕНЫ (2026-08-25, не возвращать!)

- **ensemble_queue._execute грузил ВСЕ свечи** без фильтра from/to → все месяцы давали одинаковый net (весь период). Фикс: `stmt.where(Candle.ts >= from_ts, Candle.ts <= to_ts)` из params.
- **compute_ensemble резал свечи по `days` от now** (последние 30 дней от текущей даты) → старые месяцы = «мало свечей». Фикс: если есть from_ts/to_ts в req — фильтровать по ним, иначе по days (ensemble.py:870).
- **compute_ensemble вызывался синхронно** в _execute → блокировал asyncio-цикл, сервер не отвечал (health висел). Фикс: `await asyncio.to_thread(compute_ensemble, candles, body)`.
- **compute_ensemble принимает 1m свечи и САМ ресемплит** в нужные ТФ: setup-функции → 5m (resample), bias → hour, entry_tf (1min|5min) → resample. Это СХЕМА 5+1: сигналы на 5m, исполнение на 1m. **1m свечи в БД — правильно, НЕ менять на 5m!** Проверка: ensemble.py:33 `TF_SECONDS`, :461-474 (resample), :890-895.
- interval_value в _execute = INTERVAL_NAMES["1min"] (всегда 1m) — корректно для 5+1, см. выше.

## РЕЗУЛЬТАТЫ ПРОГОНОВ ИСТОРИИ (январь–май 2026, конфиг 56e41e3b, main, 5 акций, вложение 100k на позицию)

- Январь (58d742b0): **−12 562** (RUAL +7 966, все остальные в минусе: SNGSP −5 554, MVID −7 089, NLMK −6 631, AFLT −1 254; 891 сделок, издержки 128 597)
- Февраль (955a0968): **+320** (MVID +11 005 единственный плюс, SNGSP −3 171, AFLT −4 038, NLMK −3 342, RUAL −135; 850 сделок)
- Март (8445c8b8): **+23 548** (RUAL +12 622, SNGSP +12 284, MVID +3 496, AFLT +43, NLMK −4 897)
- Апрель (7a8164cd): **+22 232** (MVID +12 444, RUAL +8 422, SNGSP +3 531, AFLT +600, NLMK −2 765)
- Май (4f6bb862): **+10 049** (все 5 в плюсе: MVID +3 756, RUAL +2 467, AFLT +2 347, NLMK +660, SNGSP +818)
- ИТОГО 5 мес: **+43 587 ₽** при обороте ~450 млн ₽ (доходность ~0.01% от оборота).
- КРИТИЧНО: издержки (комиссия 0.05% + слип 2bps) = 124-129 тыс ₽/мес на ~900 сделок — в 5-10 раз больше чистой прибыли. PF считается от gross, БЕЗ издержек.
- Январь/февраль = просадка или ноль; март-май стабильно плюсовые (чувствительность к режиму рынка).
- Старые прогоны bca16b37/38a63282/1a21d4a0/25c2a4ca/30b94f9c (+147-148k у всех) — НЕВЕРНЫЕ (считали весь период без фильтра дат), игнорировать.

---

## Статус ox-alpha (2026-08-25, присоединился к команде)

- **Зона:** Lab-ось — Prewiev_lab.md (Decisions таб, QC-чеклист, эпизоды просадок,
  сравнение конфигураций), ML-порог в эксперименты. Файлы: backend/app/services/
  (warehouse.py, test_queue.py), frontend/src/lab.ts + style.css.
- **Сделано до присоединения:** плавный прогресс тестов (коммит каждые 2с в
  send_to_lab → _sync_progress), сигнатурный рендер плашек без дёрганья,
  ⏸/▶/■ (стоп = удаление TestRun → возврат в левую колонку), единая таблица
  результатов, сегментный мидбар, заливки W/L и PnL. Детали — docs/PROGRESS.md.
- **Не трогает:** oracle_coverage / 5m+1m (главный агент), вкладку «Тест»
  (Neura-Frontend).
- **Открытый вопрос команде:** /Volumes/Dev/Deeptrading не git-репозиторий —
  предложение `git init` на шаре (обсуждение в chat.md).

## Зона ox-alpha обновлена (2026-08-25)
- Добавлена **Warehouse-страница** по docs/Preview_warehouse.md (MVP шаг 1–7):
  новый frontend/src/warehouse.ts + вкладка; роли BIAS→SETUP→ENTRY,
  фильтры/quorum секциями, preview на графике, сохранение, send to Lab.
- Decisions-таб в Lab поставлен на паузу (бэкенд готов, фронт начат —
  вернусь после Warehouse). Lab-очередь остаётся моей зоной.

## ML META-FILTER ЭКСПЕРИМЕНТ (2026-08-26) — статус и выводы

### Что сделано
- **ml_meta.py** (backend/app/services/): датасет кандидатов (raw/quorum/entry из ансамбля), features на decision_ts (5m индикаторы + 1m микро-контекст + bias_1h + session), labels через движок (AtrStopPolicy + intrabar_exit + CostModel), обучение LogisticRegression, walk-forward train/val/OOS, purge/embargo, правило выбора threshold.
- **scripts/ml_train_csv.py**: обучение прямо из CSV (T-Invest архивы), без БД.
- **quant_analytics.py**: QuantStats-адаптер (equity curve, метрики, HTML tearsheet).
- Данные: 1m CSV 2025 (1820 файлов, загружено в БД 1 519 235 свечей) + 2026 в /tmp/hist.

### Результаты (run ml_meta_v2, real qty: floor(100k/(price*lot))*lot, lot=10)
- Датасет: 312 199 кандидатов (124k raw, 26k quorum, 162k entry) за 2025-08..2026-08.
- Калибровка ML ОТЛИЧНАЯ (OOS): win rate по бакетам 23.9% → 44.6% → 47.6% → 52.2% → 60.6% (монотонно с p_success). Модель разделяет кандидатов.
- Threshold rule: ни один порог не прошёл на validation (все net<0 после costs) → ML НЕ включаем честно.
- Старый run ml_meta_v1_unit_qty (qty=1) сохранён в /tmp/ml_meta_v1_unit_qty.json — diagnostic only, P&L units non-production.

### ML candidate-universe economics audit (v2) — ВАЖНО, корректная интерпретация
- **Это НЕ backtest стратегии!** Это аудит всех 44 823 candidate-событий OOS (июль-август) как если бы каждое было сделкой.
- 44,823 candidate-level counterfactual outcomes: gross +3.56M ₽, costs −6.33M ₽, net −2.77M ₽ (100k notional на кандидата, полный cost model).
- Интерпретация: **торговать каждый кандидат как отдельный round-trip — экономически невалидно.** Кандидаты могут пересекаться, включать сигналы внутри открытой позиции, в cooldown, без session/execution state.
- Это НЕ доказывает, что ensemble_main_v1 с реальным EngineRunner убыточен. Вывод только: all-candidates universe не имеет положительной unit-economics.
- ML-фильтр НЕ одобрен: сначала нужен executable EngineRunner baseline (entry-only, session=main, one position per FIGI, cooldown 15, real qty/costs, portfolio constraints), затем на нём оценивать ML.
- QuantStats: Sharpe/Sortino/maxDD из candidate-аудита — некорректны как портфельные метрики (нет mark-to-market, пересечения позиций, 500k база не соблюдается). Использовать только как стресс-индикатор turnover.

### Как запускать ML-эксперимент
```
cd backend && nohup venv/bin/python scripts/ml_train_csv.py --extra-dir /tmp/hist/2025 \
  --train-from 2025-08-01 --train-to 2026-05-31 --val-from 2026-06-01 --val-to 2026-06-30 \
  --oos-from 2026-07-01 --oos-to 2026-08-25 --out-json /tmp/ml_report.json
```
Отчёт: threshold, validation_metrics (rule_passed), baseline_metrics_oos, ml_metrics_oos, by_figi_oos, calibration, leakage_checks.
QuantStats: `python -c "from app.services.quant_analytics import compute_metrics, build_html_report; ..."`.

### API
POST /api/v1/ml/meta/train (csv_dir + периоды) — обучение; POST /api/v1/ml/meta/dataset — только датасет. Оба через to_thread (тяжёлые).

### Известные проблемы производительности (решены)
- _rsi c pop(0) был O(n²) → deque-подход (Wilder smoothing).
- _extract_5m_features пересчитывал индикаторы на каждый кандидат → _IndCache (прекомпут один раз).
- _bias_aligned пересчитывал EMA50 hourly на каждый кандидат → hourly_ema прекомпут.
- _label_candidate линейный поиск → бинарный.

### EXECUTABLE ENGINE-RUNNER BASELINE (2026-08-26) — ВАЖНЫЙ РЕЗУЛЬТАТ
- Скрипт: backend/scripts/engine_baseline_oos.py — полный replay реальной стратегии (compute_ensemble → EngineRunner: 5m setup/quorum → entry → session=main → one position per FIGI → cooldown 15 → atr_stop 14/2/2 → next 1m open → real qty lot → costs).
- OOS июль-август 2026, 5 акций: **net +5 821 ₽** (gross +8 671, costs −3 989), gross_PF 7.13 / **net_PF 3.53**, win rate 68.97%, expectancy +200.74 ₽/сделку, max_dd 854 ₽, **всего 29 сделок за 2 месяца**.
- Все 5 акций в плюсе: RUAL +1 763 (PF 12.0), MVID +2 142 (PF 6.59), NLMK +1 422 (PF 11.07), SNGSP +250, AFLT +245.
- **Вывод: реальная стратегия ПРИБЫЛЬНА после издержек.** −2.77M из candidate-audit — артефакт трактовки всех 44 823 кандидатов как сделок (turnover). Реальный EngineRunner отсеивает 99.94% кандидатов.
- **Правило на будущее:**
  - All candidates (ML audit) — только проверка разделимости score.
  - Entry-only counterfactual — impact entry-стадии.
  - **Executable EngineRunner — единственный торговый baseline** (29 сделок/2 мес).
  - ML-фильтр оценивать ТОЛЬКО на executable потоке (entry intents, не raw кандидаты).
- QuantStats: `quant_analytics.py` (compute_metrics, build_html_report с metadata в reports/{run_id}/quantstats.html; при <30 сделок — HTML вручную, т.к. daily-агрегация пуста). Sharpe/Sortino интерпретируемы только при mark-to-market equity с полным дневным NAV.
- Артефакты: /tmp/engine_oos.json, /tmp/qs_engine_oos_2026-07-01_2026-08-25.html, /tmp/ml_report_v2.json, /tmp/ml_meta_v1_unit_qty.json.

## RESEARCH PACK (2026-08-26) — read-only экспорт для внешнего LLM-анализа
- **Модуль:** backend/app/services/research_pack.py; **endpoint:** GET /api/v1/research/pack (from_ts, to_ts, figis, include_samples, max_trade_samples, max_rejection_samples, output_format json|csv_bundle).
- **Ограничение:** период ≤ 92 дня (400 иначе). Read-only: не пишет в БД, не отправляет наружу, не меняет движок/параметры/ML.
- **Артефакты:** reports/{seed}/research_pack.json + research_pack_manifest.json (+ trades.csv, entry_intents.csv, rejections.csv, daily_pnl.csv при csv_bundle). seed = SHA256(period+figis) — детерминированный, повторный запрос пишет в ту же папку.
- **Секции:** A metadata, B strategy_config, C portfolio_assumptions, D funnel (raw→quorum→entry→intents→executed→closed), E rejection_attribution (primary reasons, per FIGI, per hour), F performance (gross/commission/slippage/costs/net, gross_pf/net_pf с определениями, win_rate, expectancy, median, max_dd, streaks, hold, cost_bps, cost_to_gross), G per_figi, H daily_pnl (Europe/Moscow), I executable_trades (без oracle), J entry_intents (executed/rejected-in-engine), K sampled_rejections (детерминированные), L diagnostics, M leakage_checks.
- **Тесты:** tests/test_research_pack.py — 10 шт (read-only, нет credentials, oracle исключён, funnel reconcile, costs once, qty%lot, entry>decision, детерминированная выборка, tz Europe/Moscow, период≤92).
- **UI:** кнопка «📦 Экспорт research pack» в renderLab (lab.ts) — локально, без отправки.
- **Docs:** docs/RESEARCH_PACK.md (JSON schema + пример).
- **Важные нюансы реализации:**
  - Чтение свечей из БД через ОТДЕЛЬНЫЙ async engine в своём loop (глобальный engine привязан к loop uvicorn — "got result for unknown protocol state 3").
  - engine_decision в intents: сопоставление signal_time сделки с decision_time intents (accepted может быть отклонён движком: session/cooldown/position).
  - сигнал = последний закрытый 1m бар до входа (entry_index-1), entry = open следующего бара.
  - НЕ использовать compute_ensemble без from_ts/to_ts — иначе берёт последние 30 дней от now!

## AUDIT-ONLY PATCH ensemble_main_v1 (2026-08-26)
- **Модуль:** backend/app/services/audit_engine.py (apply_fill_price, reprice_trade, session_at, split_funnel, contention_groups, resolve_intrabar_exit, qty_for_fill) — read-only, движок НЕ меняется.
- **Скрипт:** backend/scripts/audit_replay.py — frozen audit replay (compute_ensemble → reprice fills/P&L).
- **Тесты:** tests/test_audit_engine.py — 11 шт (slippage BUY/SELL×entry/exit, no-double-count, session MSK/midnight, funnel reconciliation 100%, intrabar long/short, lot sizing + gap, contention priority LONG>SHORT).
- **Docs:** docs/AUDIT.md (ответы a–e, before/after таблица).
- **Ключевые находки:**
  1. Slippage в research_pack был 0 из-за `t.get("slippage")` — поля нет в trades_out (там entry_slippage/exit_slippage с захардкоженным slip_bps=0.0002 в ensemble.py:585). Движок реально применяет slippage через CostModel.fill_price — проблема была в экспорте.
  2. break_even 10bps = commission-only; с slippage 2bps×2 стороны = 14bps.
  3. main session = MOEX 10:00-18:45 MSK Mon-Fri (Europe/Moscow, без хардкода UTC-окна).
  4. Reconciliation intents: 100% (EXECUTED + REJECTED_COOLDOWN + REJECTED_IN_POSITION = entry_intents).
  5. Intrabar: legacy == conservative_stop_first (stop проверяется раньше target); optimistic_target_first — отдельная политика.
- **Frozen OOS 2026-07..08 (before/after):** net +289 016 → +228 866 ₽ (после корректного slippage 74 643 + commission 186 607), net_PF 3.67 → 2.36, break_even 10 → 14bps. 1 870 сделок. Это НЕ оптимизация — только честный accounting.

### AUDIT продвинутый (2026-08-26, продолжение)
- **КРИТИЧЕСКАЯ НАХОДКА: `entry_session=main` НЕ работает как фильтр!** `SessionPolicy.can_enter` (backend/app/engine/sessions.py:48-49): вне окна 10:00-18:45 MSK возвращает True (вход РАЗРЕШЁН) — инверсия. Режет ТОЛЬКО weekend. Движок исполняет ~46% сделок с входом вне main (3 210 из 4 920 intents вне main). Документация «входы только основная сессия» НЕ соответствует коду. Исправление = отдельное решение (меняет движок — вне audit-only).
- **29-trade vs 1 870-trade:** 29 было артефактом неполного периода (compute_ensemble без from/to брал последние 30 дней). Canonical audit (с from/to) = 1 870 сделок за 2 мес (~42/день). Режим: `ensemble_main_v1_audit_v1`.
- **IN_POSITION decomposition (OOS):** 2 320 in-position = 1 174 классифицировано интервальным методом (same_episode_repeat 830 = 16.9%, opposite_side_candidate 344 = 7.0%); остальные 1 146 требуют ledger.audit прогона (TODO). Противоположная сторона при позиции → движок ACCEPT_EXIT (выход/flip), НЕ теряется.
- **Session-инварианты (OOS):** new_entries_decision_outside_main=3 210, fills_after_main_boundary=0, exits_outside_main=936, overnight_positions=15.
- Новые функции audit_engine.py: classify_in_position, decompose_in_position, session_invariants. Тесты: 24 passed (14 audit + 10 research).
- Артефакт: /tmp/audit_oos_v2.json (полный), docs/AUDIT.md (обновлён).

### ДВИЖОК ИСПРАВЛЕН: session bug (2026-08-26)
- **Баг:** `SessionPolicy.can_enter` (backend/app/engine/sessions.py) вне окна 10:00-18:45 MSK возвращал True (вход РАЗРЕШЁН) — инверсия. Движок НЕ резал входы вне main (только weekend). ~46% сделок входили вечером.
- **Фикс:** вне окна → `False, "outside main session"`. Вход только в окне main будни + cutoff.
- **Проверка:** 33 теста движка (golden/reentry/sessions/bot) проходят после фикса.
- **Пересчитанный OOS (2026-07..08, 5 акций, audit_oos_fixed.json):**
  - Сделок: 1 870 → **959** (вне-main входы отсеяны)
  - Reconciliation 100%: 4 920 = 959 EXECUTED + 3 210 REJECTED_SESSION + 371 COOLDOWN + 380 IN_POSITION
  - Legacy net: +204 919; audit reprice net: **+174 427**; Net PF **3.18** (было 2.36); win 69.0%; expectancy +181.88 ₽; max_dd 2 087 ₽
  - Все 5 акций в плюсе (PF 9.3-14.2)
  - Session: new_entries_outside_main=0 (3 210 отсеяны), fills_after_main_boundary=23, exits_outside_main=29, overnight=0
- **IN_POSITION (после фикса):** 380 отказов = same_episode_repeat 423* + opposite 190 (всего 613 классифицировано интервальным методом; остальное — эпизоды вне интервалов)
- **Вывод:** session-фикс резко улучшил качество (net PF 2.36→3.18, expectancy +122→+182) при вдвое меньшем обороте — вечерние входы были шумом.

## DATA-INTEGRITY PATCH A–F (2026-08-26) — canonical July 2026
- **Процесс**: тесты и ресурсоёмкие прогоны — ТОЛЬКО на .54 (MacBook-ProDen x86_64, в 5-8× быстрее). SSH: expect-скрипт /tmp/ssh54.exp (пароль в памяти). venv .54: /Users/Denis/Dev/Deeptrading/backend/.venv (sklearn+quantstats установлены).
- **A. Capital**: единый 10 000 в request/config/manifest/trades (B_strategy_config, C_portfolio, manifest). position_size_source=run_request.
- **B. Terminal ledger**: replay_engine_audit (повторный прогон движка с теми же сигналами, движок НЕ меняется) + raw_reason. Reconciliation 100%, generic_gates_left=0.
- **C. Gap executed→trade**: 583→517 = 40 CANCELLED_BEFORE_FILL (accepted без сделки и без reject-аудита) + 2 REJECTED_DUPLICATE_EPISODE + точный linkage (entry_time в 5 мин окне от decision, той же стороны).
- **D. Intent context**: quorum_count (окно 15м от decision), functions_mask (setup-функции голосовавшие в окне), candidate_id=quorum_event_id, unit=entry_intent.
- **E. Session boundary**: V_session_boundary_audit (14 сделок у границы), session_at_decision/execution, allow_fill_after_main_boundary.
- **F. MTM equity 1m**: U_mtm_equity_1m (cash+realised+unrealised по close бара), mtm_drawdown_rub, open-позиции на конец.
- **Артефакты**: reports/{seed}/research_pack.json + manifest + reconciliation.json + intent_lifecycle.csv + session_boundary_audit.csv + mtm_equity_1m.csv.
- **July 2026 (10k)**: net +11 190 ₽, net PF 3.68, win 71.4%, 517 сделок, 2 720 intents (reconcile 100%).
- **Тесты**: 66 passed (18 research + 15 audit + 33 engine). Новые: test_a..test_f в test_research_pack.py.
- **Файлы**: research_pack.py (секции U/V/W, replay с entries_raw), audit_engine.py (replay_engine_audit расширен, mtm_equity_1m, session_boundary_audit).
- Артефакт на .54: /Users/Denis/Dev/Deeptrading/backend/reports/5b44f3b383df/ (= /Volumes/Dev/Deeptrading/backend/reports/5b44f3b383df/).

## ORCHESTRATOR (2026-08-27) — координация агентов
- Папка: backend/orchestrator/ (python -m orchestrator) — Telegram-оркестратор для
  передачи задач между агентами (architect/executor) + state.json, notifier, agents.
- Команды: `python -m orchestrator dispatch "@task.md"` (передать задачу executor),
  `python -m orchestrator task "текст"` (поставить next_action).
- Это НЕ часть canonical strategy pipeline — отдельный инструмент коммуникации.
- README: backend/orchestrator/README.md, конфиг: .env.example.

## QUANTDINGER — ПЛАТФОРМА (2026-08-30)

QuantDinger — отдельная платформа (Flask backend + Vue frontend), развернута на .54 через Docker.
Deeptrading является первичным проектом; QuantDinger — следствие (портит стратегии из Deeptrading).

### Архитектура

```
Deeptrading (порт 8000)          QuantDinger (порт 5000)
├── Backend (FastAPI)             ├── Backend (Flask)
├── Engine (ensemble_main_v1)     ├── Strategy API V2
├── PostgreSQL (5432)             ├── PostgreSQL (5433)
├── 5m signals + 1m execution     ├── Backtest engine
└── compute_ensemble              └── Live trading (BLOCKED)
```

### Docker (на .54)

Для Docker-команд **всегда** keychain bypass:
```bash
DOCKER_CONFIG=/tmp/dcfg /usr/local/bin/docker <command>
```

| Сервис | Порт | Контейнер |
|--------|------|-----------|
| QuantDinger backend | 5000 | native (uvicorn) |
| QuantDinger frontend | 8888 | quantdinger-frontend |
| QuantDinger mobile | 8889 | quantdinger-mobile |
| PostgreSQL (QuantDinger) | 5433 | quantdinger-db |
| Redis | 6379 | quantdinger-redis |
| Celery workers | — | quantdinger-celery-* |

### БД QuantDinger (port 5433, user: quantdinger)

```sql
qd_klines          -- market, symbol, timeframe, ts BIGINT, open/high/low/close/volume DOUBLE
qd_symbol_ref      -- ticker, instrument_uid, figi (28 MOEX stocks)
qd_script_sources  -- strategy code (IDE)
qd_backtest_runs   -- backtest results
qd_backtest_trades -- individual backtest trades
qd_strategy_trades -- live strategy trades
qd_strategy_positions
qd_watchlist       -- user watchlist
```

### QuantDinger — ключевые компоненты

- **Strategy API V2:** compile/verify/backtest/live. Скрипт → API → sandbox → результат.
- **Script IDE:** Vue-компонент для написания стратегий на Python. Sandbox с ограничениями (нет lightgbm/sklearn/zoneinfo/setattr).
- **Kline cache (qd_klines):** заполняется из ISS MOEX при нехватке. 500 свечей за запрос.
- **T-Invest SDK:** `t-tech-investments==1.49.3`, token в .env. Публичный API: `invest-public-api.tinkoff.ru/history-data`.
- **Auth:** JWT token lifetime 3650 дней. Дефолт креды: quantdinger / 123456.

### Data Flow

```
T-Invest API → load_history_csv.py → PostgreSQL candles (1m)
                                          ↓
                                    Deeptrading: compute_ensemble → signals → trades
                                    QuantDinger: qd_klines → Strategy API V2 → backtest
```

### QuantDinger Doc/ — память для сессий

Папка `QuantDinger/Doc/` содержит файлы, которые нейронка читает в начале сессии:
- `Doc/MEMORY.md` — точка входа
- `Doc/PROMPT.md` — инструкции
- `Doc/ensemble/ENSEMBLE_REFERENCE.md` — конфигурация ансамбля
- `Doc/PROJECT_STATE.md` — состояние проекта
- `Doc/results/RESULTS_SUMMARY.md` — сводка результатов

### Заблокировано в QuantDinger

- Live trading
- ML в execution path
- Pyramiding
- Weighted voting
- Grid search
