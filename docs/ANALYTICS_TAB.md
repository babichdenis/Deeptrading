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

## Приёмка

- Прогон `reports/bt_ose_real_20260930_0145.json` (ансамбль 15, q1..q6 + MTF) виден во вкладке:
  таблица по стратегиям с gross W/L шт/₽, развороты по сессиям/режимам, сделки с SL/TP.
- Тот же результат доступен по API (проверяемо curl'ом).
- Старые таблицы `experiments` не изменены.
