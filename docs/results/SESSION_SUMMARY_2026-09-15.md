# SESSION_SUMMARY_2026-09-15 — IMOEX guard: защита от всплесков индекса + непрерывная БД MOEX + исследование чувствительности

> **Дата:** 2026-09-15. Запрос: бот на .2 в вечер 14.09 (17:50–19:00 МСК) продолжал
> открывать SHORT при взлёте IMOEX на +59 пунктов (+2.5%) — все такие входы убыточны.
> Задача: запретить новые входы против направления индекса до стабилизации; вести БД
> значений MOEX постоянно; понять порог, при котором акции идут за индексом, и какие
> бумаги наиболее подвержены влиянию.

---

## 1. ЧТО СДЕЛАНО

### 1.1 IMOEX guard (защита входов)
Новый модуль `backend/app/bot/imoex_guard.py` (чистая машина состояний, 12 юнит-тестов):

- **Ход индекса** за окно `window_min` (по умолчанию 20 мин): `pts` и `pct`.
- **Активация всплеска**: `|pct| ≥ imoex_spike_pct` (0.8%) **или** `|pts| ≥ imoex_spike_points` (20).
- **Блок**: UP-всплеск → запрещены новые SELL; DOWN-всплеск → запрещены новые BUY.
  Входы ПО направлению индекса и все выходы — не трогаются.
- **Стабилизация (release)**: ход затух ниже `release_frac`×порога (0.4%) и прошло
  `min_block_min` (5 мин) с активации; смена знака — мгновенная переактивация.
- Гейт в `runtime._process_signal` (перед `_submit_order("open")`): лог
  `ПРОПУСК ВХОДА <ticker>: против IMOEX — ...`, событие `SIGNAL_REJECTED/IMOEX_GUARD`,
  счётчик `blocks` в `status["imoex_guard"]`.
- Конфиг в `BotConfig` (+ persist): `imoex_guard`, `imoex_spike_pct`,
  `imoex_spike_points`, `imoex_spike_window_min`, `imoex_release_frac`,
  `imoex_min_block_min`, `imoex_refresh_sec`.

### 1.2 Непрерывная БД значений MOEX
`backend/app/bot/moex.py`:
- `sync_imoex_recent(minutes)` — догрузка хвоста 1м свечей IMOEX с ISS (фикс `till`:
  ISS отдаёт данные только при `till > from` — теперь `till = завтра`).
- `_sync_imoex_gap(days)` / `ensure_imoex_candles(days)` — gap-fill от последней
  свечи в БД (а не «если данных нет 2 дня»), переживает рестарты и простои.
- `runtime._imoex_loop` — live: каждые 60с догрузка + пересчёт; replay: ряд берётся
  из БД на всё окно теста (без look-ahead — расчёт использует бары ≤ виртуальных часов).
- `main.py` — keepalive-задача: каждые 5 минут `sync_imoex_recent(180)` — БД ведётся
  даже когда бот остановлен.
- `_load_imoex_buf`: live — последние N баров `ORDER BY ts DESC LIMIT` (переживает
  выходные/праздники); replay — окно `[replay_start−6ч, replay_end]`.

### 1.3 Развёртывание на обеих машинах
- **.2 (тест-стенд)**: патч применён (runtime.py/moex.py/main.py + новый модуль + тесты),
  py_compile/import OK, 12 тестов passed. Попутно починен баг агента №2:
  `/status` падал 500 (`portfolio = _test_portfolio_digest(...)` без `await`) — после
  фикса `/status` 200 и виден `imoex_guard`. Рестарт через `wmic process call create`.
- **.3 (прод)**: код на шару, рестарт uvicorn (PID 87318 → новый). `/status` 200,
  guard виден, ряд загружен (`IMOEX GUARD: ряд 390 свечей загружен`). Рынок 15.09
  не торгуется (ISS `TRADINGSTATUS=N`, последняя сессия 14.09 23:50 МСК) — рестарт
  без потерь.

### 1.4 Проверка на событии 14.09 (реплей на .2)
Окно `2026-09-14 17:30–19:00 МСК`, test `imoex_guard_1409`:

```
18:08  IMOEX GUARD: всплеск ВВЕРХ +20.3п (+0.87% за 20м) — SELL-входы запрещены
18:32  ПРОПУСК ВХОДА SMLT: против IMOEX — IMOEX UP +0.77% (+18.2п)
18:33  ПРОПУСК ВХОДА SMLT ...          (всего 5 блокировок SELL)
18:37  IMOEX GUARD: стабилизация +6.7п (+0.28%) — входы разрешены
18:43  IMOEX GUARD: всплеск ВНИЗ -19.4п (-0.82% за 20м) — BUY-входы запрещены
18:48  IMOEX GUARD: стабилизация -9.3п — входы разрешены
```
Счётчики после реплея: `activations=2, releases=2, blocks=5`. Реальные сделки 14.09
(AFKS SELL 18:08, PLZL SELL 18:11, AFKS SELL 18:15) попали в окно блока.

---

## 2. ИССЛЕДОВАНИЕ: акции vs IMOEX (90 дней, 1м)

Скрипт `backend/scripts/imoex_sensitivity.py`, отчёт
`docs/results/IMOEX_SENSITIVITY_2026-09-15.md`, JSON
`backend/reports/imoex_sensitivity_2026-09-15.json` (26 ликвидных бумаг, 17.06–14.09).

**Главные выводы:**
1. **Акции идут за индексом синхронно, а не с задержкой.** Порог: ход IMOEX
   **0.2–0.3% за 5 мин** → 71% акций в стороне индекса; 0.3–1.0% → 74–77%.
   corr(1м) pooled = +0.537 (лучшее выравнивание — совпадение меток, shift=0).
2. **«Заглянуть в будущее» импульсом индекса нельзя**: forward-догон 1–5 мин ≈ 0;
   при |ход20| ≥ 1.0% — уже отрицательный (−1.0 бп), ≥ 1.5% — −4.7 бп (t=−7.6).
3. **Fade после экстремума есть**: |ход20| ≥ 1.5% → откат +8.7 бп за 5 мин (t=+11);
   ≥ 2.0% → +18.8 бп (t=+13.5). После комиссии 0.05%×2 остаётся только ≥ 2.0%;
   при тарифе 0.3% — не торгуется. Кандидат в отдельный бэктест.
4. **Самые индексозависимые** (beta): SMLT 1.63, VKCO 1.39, SFIN 1.33, PLZL 1.29,
   RUAL 1.21. **Самые «индексные» по R²**: LKOH 0.66, GAZP 0.63, T 0.60, NVTK 0.57,
   TATN 0.55, SBER 0.53. **Слабозависимые**: MVID (52.2% согласия — почти монетка),
   MTSS, SNGSP, LENT, ALRS.

---

## 3. ФАЙЛЫ

| Файл | Что |
|---|---|
| `backend/app/bot/imoex_guard.py` | **новый** — state machine (move_at, step, ImoexGuardState) |
| `backend/app/bot/runtime.py` | конфиг guard, ряд индекса, `_imoex_loop`, гейт входа, `/status` |
| `backend/app/bot/moex.py` | `sync_imoex_recent`, gap-fill, фикс `till`, `imoex_last_ts` |
| `backend/app/main.py` | keepalive IMOEX (5 мин) |
| `backend/tests/test_imoex_guard.py` | **новый** — 12 тестов |
| `backend/scripts/imoex_sensitivity.py` | **новый** — исследование |
| `docs/results/IMOEX_SENSITIVITY_2026-09-15.md` | **новый** — отчёт |
| `backend/reports/imoex_sensitivity_2026-09-15.json` | результаты исследования |
| `docs/roadmap/SCRIPTS_INDEX.md` | запись о новом скрипте |

## 4. ПРОВЕРКИ
- `pytest tests/`: 231 passed, 2 failed — **оба падения пре-существующие**
  (`test_atr_stop_trailing_raises_stop`, `test_ensemble_static_contains_oracle_coverage`
  падают и на чистом HEAD — проверено checkout'ом).
- .2: `pytest tests/test_imoex_guard.py` — 12 passed; `/status` 200.
- .3: import OK, 12 passed, uvicorn поднят, guard в `/status`.

## 5. СЛЕДУЮЩИЕ ШАГИ (предложения)
1. **Per-ticker режим guard'а**: блокировать контр-входы только по индексозависимым
   (beta ≥ 1.0), оставляя mean-reversion по MVID/MTSS/SNGSP/LENT.
2. **Второй уровень**: при |ход20| ≥ 1.5% блокировать и «догоняющие» входы ПО индексу
   (forward −4.7 бп — ловушка на пике).
3. **Fade-стратегия** после экстремума ≥1.5–2.0% — отдельный бэктест с комиссией.
4. Сохранить beta/R² в БД (`instruments.imoex_beta`) для universe/guard-весов.
5. Проверить guard на других месяцах (июнь–август) — стабильность порога.

---

## 6. ВТОРАЯ ВОЛНА (в этой же сессии, по запросу «дальше всё друг за другом» + UI)

### 6.1 Per-ticker guard + chase (второй уровень)
- `imoex_guard.block_for(...)` — единая функция блокировки: контр-входы (UP→SELL,
  DOWN→BUY), фильтр `imoex_guard_min_beta` (0=все; >0 — только beta ≥ порога),
  `imoex_chase_block_pct` (при |ходе| ≥ N% блокировать и входы ПО индексу).
- `runtime`: загрузка `instruments.imoex_beta` (26 бумаг), гейт передаёт figi,
  счётчики beta/chase в `/status`; сброс счётчиков guard'а на каждый запуск.
- Тесты: 16 (добавлены block_for: стороны, min_beta, chase).
- БД: `instruments.imoex_beta/imoex_corr/imoex_r2` (ALTER+UPDATE, `--save-db`).

### 6.2 UI-индикатор (frontend — зона A)
- `index.html`: бейдж `IMOEX` в шапке (`#bs-imoex-guard`).
- `bot.ts` (`pollOnce`): состояния — **НЕТ СВЕЧЕЙ** (красный, stale в торговую сессию),
  **↑+X% блок SELL**, **↓−X% блок BUY** (красный), **ок** (зелёный); title — детали
  (порог, окно, chase, beta, счётчик блокировок, возраст свечи).
- `style.css`: `.imoex-dot` + красное состояние.
- Backend snapshot дополнен: `last_candle`, `age_sec`, `stale`, `trading`.
- Алерт в логе: `⚠ IMOEX: свечи НЕ ОБНОВЛЯЮТСЯ ...` (throttle 10 мин) + событие `IMOEX_STALE`.
- Синхронизировано на .2 (Python-скрипт точечных замен, CRLF сохранён) и .3; tsc — без
  новых ошибок (все ошибки pre-existing).

### 6.3 Парный лид-лаг (26 бумаг + IMOEX)
Отчёт `docs/results/PAIR_LEADLAG_2026-09-15.md`, скрипт `scripts/pair_leadlag.py`:
- Топ пар k=1: IMOEX→LENT +0.110, LKOH→LENT +0.099, IMOEX→MVID +0.096, T→LENT +0.095,
  MAGN→NLMK +0.093; на k=2–3 ~0 → **одноминутное запаздывание, а не многошаговый лид**.
- Догон: IMOEX→MVID +5.7 бп (3м, t=+7.2), MAGN→NLMK +4.4 (t=1.3), LENT-хвост +1.3–2.3 бп.
- **Экономически не торгуется** (издержки ~14 бп); LENT/MVID — «stale» данные
  (важно для parity live/backtest).

### 6.4 Fade-бэктест
`scripts/imoex_fade_backtest.py` (табл. в отчёте IMOEX_SENSITIVITY, §5):
- Положительный net (издержки бота 0.14%): T=1.5 retrace25 H=10 +15.9%; T=2.0
  retrace25 H=3 +11.3% (PF 8.2, WR 76.7%); T=2.0 immediate H=10 +26.2%. События редкие
  (86–225 за 90 дней). При тарифе 0.3% — не торгуется. Кандидат на OOS, не live.

### 6.5 Проверки
- .2: реплей 14.09 (test `imoex_chase_1409`) — 5 блокировок SELL (18:32–18:36), вход
  SMLT после релиза 18:38 прошёл; chase-блок не сработал только потому, что в окне
  |ход|≥1.5% не было BUY-сигналов (логика покрыта юнит-тестом).
- .3: guard, beta (26), chase/min_beta в `/status`; оба бэкенда перезапущены;
  старый зависший uvicorn (PID 87318) убит, чтобы не было двух ботов.
