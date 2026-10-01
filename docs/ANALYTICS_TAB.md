# Analytics — вкладка анализа тестов (ТЗ и роадмап)

> Передача задачи новой сессии. Цель — вкладка **Analytics** в UI: просматривать результаты
> прогонов из `reports/` и БД, видеть максимум срезов. Не переписывать существующее без нужды.

## Методология владельца (главное!)

**Простой PnL — не показатель.** В одной вселенной бумаги по 200₽ и по 5000₽: случайный ход
дорогой акции перевесит любую стратегию. Смотреть:
- **количество сделок**, gross win/loss — **и в штуках, и в рублях**;
- разрезы **по режиму рынка и сессии** (в каком режиме КАКОЙ робот чувствует себя лучше);
- **состояние тикеров**: лидеры/аутсайдеры по доходу за день/неделю/месяц — рынок в целом;
- **полная информация по сделкам**: min/max SL/TP, MAE/MFE, время в сделке, причина выхода;
- любые влияющие факторы, которые вспомним — срезы расширяемые.

## Где что лежит

- **Прогоны харнесса** (на .7): `backend/reports/` — основные:
  - `bt_ose_real_*.json` — real-прогоны: `meta` (name/period/interval/universe), `raw` — строки
    по (strategy × exit × ticker): trades/wins/gw/gl/net/max_dd_pct/commission/…; при
    `artifacts` — `trades_detail` (side, entry/exit time+price, exit_reason, bars_held, net_pnl);
  - `experiments/WF_*.json` — walk-forward: `meta/phases/selected/oos_matrix/robustness`;
  - `experiments/EXP-*.json` — карточки экспериментов (id, конфиг, метрики, статус CANDIDATE/…);
  - `ose_cache/` — дисковый кэш прогонов (бит-в-бит воспроизводимость).
- **Проектные тесты бота**: таблица `sandbox_trades` (entry/exit, exit_reason, meta/exit_meta —
  включая rsi/bias, mae_atr/max_pnl, trail_info), `bot_logs`.
- **Движок**: таблицы `experiments` / `experiment_trades` (модели `app/models/experiments.py`) —
  НЕ трогать, это отдельный контур.
- **Вкладка сейчас**: `frontend/src/main.ts` — «Вкладка “Анализ”» (~строка 1728: статистика
  прогона, сравнение тестов, AI-гейт, движения). Просмотра `reports/` там нет — это и надо
  добавить (новая вкладка **Analytics** или расширение существующей).
- **Логика срезов** уже есть в `backend/scripts/trades_split.py` (сессии МСК + ADX-режимы по
  входам). Её ядро вынести в `app/services/report_slices.py`, чтобы переиспользовать скрипт + API.
- **EfficiencyRatio** — канон-индикатор хаба (`app/engine/indicatorhub.py::_efficiency_ratio`),
  готов для ER-срезов. Канонический ADX — `indicatorhub._adx` (Wilder).

## Схема БД (новые таблицы, старые не ломаем)

```
report_runs    (id, file_name, kind[real|wf|matrix|exp], name, created_at, meta jsonb,
                content_hash unique, mtime)
report_rows    (run_id FK, strategy, exit, ticker, trades, wins, gw, gl, net,
                pf, max_dd_pct, commission, sec, raw jsonb)
report_trades  (run_id FK, strategy, exit, ticker, side, entry_time, exit_time,
                entry_price, exit_price, exit_reason, bars_held, net_pnl,
                sl_price, tp_price, mae_atr, mfe_atr, session, regime_adx, er_in)
report_slices  (run_id FK, strategy, dim, bucket, trades, wins, gw, gl, net,
                gross_wins_n, gross_losses_n)   -- dim: session|regime_adx|er|ticker|hour|weekday
```
Импортёр: `backend/scripts/import_reports.py` — читает `reports/**/*.json`, пишет идемпотентно
(контент-хэш), считает срезы (переиспользует `report_slices.py`); для `bt_ose_real` с artifacts
дополнительно заполняет `report_trades` (SL/TP — из `EXITS`-политик прогона, MAE/MFE — по свечам).

## API (эскиз)

- `GET /api/v1/analysis/reports` — список прогонов (файл, kind, дата, robots, trades, net).
- `GET /api/v1/analysis/reports/{id}` — строки (strategy×exit×ticker) + сводка по стратегиям
  (trades, gross W/L шт и ₽, WR, PF, expectancy, max_dd).
- `GET /api/v1/analysis/reports/{id}/slices?dim=session|regime_adx|er|ticker|hour` — срезы.
- `GET /api/v1/analysis/reports/{id}/trades?strategy=&ticker=` — сделки с полной инфой
  (включая min/max SL/TP по выборке, MAE/MFE).
- `GET /api/v1/analysis/market/leaders?window=day|week|month` — тикеры-лидеры/аутсайдеры по
  доходу/волатильности (рыночный контекст для оценки роботов).

## UI (эскиз)

1. Вкладка **Analytics** (база — существующая «Анализ»): 
2. Слева список прогонов (kind, дата, имя); фильтры: стратегия, тикер, период.
3. Таблица стратегий: trades, W/L шт, gross W/L ₽, WR, PF, expectancy, DD — сортировка;
   НЕ сортировать «по net» по умолчанию (методология!).
4. Развороты: срезы (сессия/режим/ER/час) × стратегия; подсветка «лучший робот в режиме».
5. Карточка сделки: SL/TP уровни, MAE/MFE, время, причина выхода; сводка min/max SL/TP по выборке.
6. Рыночная панель: тикеры день/неделя/месяц (лидеры, аутсайдеры), их состояния.

## Как добавлять новое

- Новый срез: добавить расчёт в `app/services/report_slices.py` и `dim` в `report_slices`
  (миграция не нужна — dim строковый); импортёр пересчитает.
- Новый формат отчёта: регистрация kind в импортёре (`parse_<kind>`).
- Новые метрики — в `report_rows` (nullable-колонки) + агрегация в API.

## Статус (2026-09-30) ✅

Всё из ТЗ сделано: таблицы + импортёр + API + UI (подвкладки «Живая» / «Прогоны»
в существующей «Анализ»), 25 pytest, тайпчек/сборка зелёные, ручной прогон в браузере.
⚠️ `frontend/src/index.html` — несервируемая копия; правки только в `frontend/index.html`.
⚠️ SL/TP/ADX/ER и «Рынок» заполняются при наличии свечей в БД (на .7 их нет).

## Приёмка

- Прогон `reports/bt_ose_real_20260930_0145.json` (ансамбль 15, q1..q6 + MTF) виден во вкладке:
  таблица по стратегиям с gross W/L шт/₽, развороты по сессиям/режимам, сделки с SL/TP.
- Тот же результат доступен по API (проверяемо curl'ом).
- Старые таблицы `experiments` не изменены.

---

# Этап 2 — заказ владельца (30.09): теги настроек + порядок в реплее

Требования для сессии аналитики (харнесс-часть — `docs/CONFIG_PRESETS.md`,
сайдкар `reports/presets/<test_name>.json` уже пишется `scripts/preset.py`):

1. **Реплейные тесты — в аналитику.** Список прогонов Analytics должен включать и проектные
   тесты (`sandbox_trades` по `test_name`), и харнесс-отчёты (`reports/`). Пути/папки — пробросить
   или завести отдельную папку; реплейные тесты видны наравне с прогонами.
2. **Карточка теста тегами.** При выборе теста на панели показывать тегами ВСЕ настройки:
   - роботы (движок + параметры), какие именно тестировались;
   - период/даты, таймфрейм;
   - сессии: утро / день / вечер (какие включены);
   - SL/TP (режим и значения), выходы (x-коды);
   - капитал на старте, размер позиции, лимиты/маржа, макс. число позиций;
   - bias, entry, кворум и число голосов;
   - overnight (переход через ночь) — вкл/выкл;
   - гейты, включённые в тесте; включённые режимы;
   - комиссия/слиппедж.
   Источник тегов — сайдкар пресета `reports/presets/<test_name>.json` (пишется при запуске
   из `scripts/preset.py`); если сайдкара нет — собрать из `bot_logs`/payload, чем богаты.
3. **Порядок в реплее (фронт).** Список прошедших тестов сейчас — куча; нужно:
   - упорядочить/сгруппировать список (дата, пресет, статус), быстрый выбор теста →
     сразу все сделки этого теста;
   - перезапуск теста → **новый тест** (имя с таймстампом; `preset.py replay` уже так делает),
     исторический сохраняется;
   - удаление тестов: по одному и массово.


---

## Статус Этапа 2 — ✅ закрыт 01.10.2026

Реализовано и проверено (см. `docs/PROGRESS.md`, раздел «Этап 2»):

- **Реплеи в Analytics**: источник `Реплеи (тесты бота)` (`#rep-source`) читает живую БД
  (`app/api/routes/analysis_replays.py`: list/detail/slices/trades), карточка, срезы,
  сделки и фильтры — те же панели, что у харнесса.
- **Теги из сайдкара**: `app/services/preset_tags.py` — единый источник тегов/имени
  (`<id пресета> <YYYYMMDD-HHMM>`), 10 групп в шапке карточки (`renderRepTags`).
  Нет сайдкара → явная подсказка «настройки теста неизвестны».
- **Порядок в фронте**: «Бот» → таблица тестов (поиск, сортировка по 10 колонкам,
  чекбоксы + массовое удаление, клик по строке → дроуэр со сделками, кнопка
  «📊 В аналитику»), перезапуск из дроуэра/Аналитики = новый тест (`POST /tests/{name}/restart`),
  удаление по одному и массово (`DELETE /tests/{name}`, `POST /tests/delete`).
- **Модалка запуска**: селект пресетов `#ts-preset` (`GET /bot/presets`), чекбокс
  `лог в БД` (`#ts-logdb`), автo-имя от пресета, в payload уходит `preset`.
- **Проверки**: `backend/tests/test_replay_analytics.py` — 17 passed (список/карточка/
  теги/срезы/фильтры/админка/delete/restart); живые curl + CDP-смоук UI
  (строка→дроуэр, сортировка, модалка, теги, действия в карточке).
- **Ограничения локально**: полный запуск реплея невозможен (нет `instrument_info`/
  свечей в БД .7); `192.168.1.2`/`.3` недоступны → обогащение ADX/ER/MAE/MFE и
  «Рынок» проверяются на живой БД.
