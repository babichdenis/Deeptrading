# HOWTO: стратегии V4-ансамбля и ML (для агентов и людей)

> Коротко: как менять состав/параметры стратегий, как добавить новую, как гонять ML.
> Машины: **тесты — на `.2`** (`C:\Users\nadts\Dev\Deeptrading`, ветка `v2-dev`),
> **UI/боевой — `.3`** (`~/Dev/Deeptrading`, ветка `second`, трогать только с разрешения).

---

## 0. Карта файлов

| Файл | Что там |
|------|---------|
| `app/engine/strategies.py` | реализации стратегий (rsi_reversal, bollinger_reclaim, vwap_reclaim, macd_cross, donchian_breakout, pullback_ema, range_compression_breakout, volume_drop, volume_climax, stochastic) |
| `app/engine/catalog.py` | реестр стратегий (id → класс, tf, params-схема) — для UI/бэктестов |
| `app/engine/ensemble_v2.py` | 10 функций-голосов (`M1_FUNCS` на 1м: micro_breakout/ema/macd/rsi; `M5_FUNCS` на 5м: donchian/pullback/range/volume/bollinger/atr_breakout) + `EnsembleVoteStrategy` |
| `app/engine/regime_ensembles.py` | 5 режимных ансамблей (trend_up/trend_down/range/hv/neutral) + `ENSEMBLE_CFG` |
| `app/bot/ensemble_strategy.py` | адаптер Strategy для live: строит `req` для `compute_ensemble` (входы/выходы/режим-гейт) |
| `app/services/ensemble.py` | `compute_ensemble` — главный конвейер (сетапы → кворум → входы → сделки) |
| `app/services/regime.py` | `RegimeDetector` (калиброван: slope 0.0005, adx 19, atr_pct 78, range_mult 2.75) |
| `data/ensemble_config.json` | состав кворума (UI-управляемый): `setups`, `quorum`, `vol_thr`, `bias`, `entry_tf`, `neutral_mode`, `regime_setups_filter` |
| `data/bot_config.json` | настройки бота: `top_n`, `sessions`, `trade_regimes`, `margin_*`, `invert_signals`, `ensemble_*` |

---

## 1. Поменять состав/параметры стратегий (без кода)

**Через UI** (вкладка «Ансамбль»): включить/выключить setups, поменять params, `quorum`,
`vol_thr` → «Применить». Runtime сам пересобирает стратегии (`reload_ensemble`).

**Через API:**
```bash
curl -X PATCH http://127.0.0.1:8000/api/v1/bot/ensemble -H "Content-Type: application/json" -d '{
  "quorum": 2, "vol_thr": 0.6, "neutral_mode": "semi_flip",
  "setups": [
    {"strategy_id":"rsi_reversal","tf":"5min","enabled":true,"params":{}},
    {"strategy_id":"bollinger_reclaim","tf":"5min","enabled":true,"params":{}}
  ],
  "regime_setups_filter": {"vwap_reclaim": ["TREND_UP","TREND_DOWN"]}
}'
```

**Через файл:** править `data/ensemble_config.json` → `POST /api/v1/bot/ensemble/reset`
(или рестарт бэкенда).

⚠️ **Важно:** `inc_<sid>` в optuna-параметрах = какие стратегии **тюнить**, а НЕ «кто торгует».
В кворуме всегда участвуют все включённые setups.

---

## 2. Добавить НОВУЮ стратегию (код)

1. Класс в `app/engine/strategies.py`: интерфейс
   `strategy_id: str`, `warmup_bars() -> int`, `on_bar(candles) -> Signal | None`.
2. Регистрация в `app/engine/catalog.py` (иначе не появится в UI и в `_all_strategies`).
3. Нужен голос в `ensemble_vote` → добавить функцию в `app/engine/ensemble_v2.py`
   (в `M1_FUNCS` или `M5_FUNCS`).
4. Нужен режимный ансамбль → `app/engine/regime_ensembles.py`.
5. Проверка:
   - `scripts/golden_ensemble.py` — golden-hash `7bfc8ea185097b19` (при осознанном изменении
     логики хеш меняется — фиксировать новое значение);
   - `scripts/parity_diff.py` — live vs backtest;
   - `scripts/functions_direction_analysis.py` — hit% функций (направление, объём, MOEX, режимы).
6. Тест на `.2`: `BOT_MODE=test`, `BOT_TEST_NAME=<имя>`, `BOT_TEST_START/END` (ISO UTC),
   `feed=replay` → статистика `/api/v1/bot/test_stats?test_name=<имя>`.

---

## 3. Параметры стратегий per-ticker (optuna)

- Хранятся в `instruments.optuna_params` (JSON): `sl_mult`, `rr`, `quorum`, `vol_thr`,
  `inc_<sid>`, `params` стратегий.
- Runtime: `_build_ensemble_params()` читает их. SL/TP берутся из `strat.p.sl_mult/rr`
  (НЕ из `cfg.atr_multiplier` — иначе UI-настройки перезапишут optuna).
- Запись результатов: `scripts/save_optuna_to_db.py`.
- ⚠️ **Optuna стабильно переобучается** (4 независимых случая: OOS −1702/−274/−1903).
  Всегда проверять OOS; для выбора функций/порогов не использовать.

---

## 4. ML (meta-labeling)

**Модуль:** `app/services/ml_meta.py` — строит датасет из кандидатов (raw/quorum/entry-breakout),
признаки строго на `decision_ts` (без утечек), разметка ровно тем же движком
(`AtrStopPolicy` + `intrabar_exit` + `CostModel`), обучает LogisticRegression
walk-forward (train → val → frozen OOS) с purge/embargo.

**Обучение БЕЗ БД и БЕЗ токена (рекомендуется):**
```bash
cd backend
.venv/bin/python3 scripts/ml_train_csv.py \
    --csv-dir /tmp/hist --extra-dir /tmp/hist/2025 \
    --train-from 2025-08-01 --train-to 2026-05-31 \
    --val-from 2026-06-01 --val-to 2026-06-30 \
    --oos-from 2026-07-01 --oos-to 2026-08-25
```
Читает CSV `uid;ts;open;close;high;low;volume;` прямо из файлов — **сеть/токен не нужны**.

**Эталонный тест baseline vs ML:** `scripts/run_v4_mltest.py` (5 FIGI, 2026-05..07, кэш в `reports/`).
Ранние эксперименты: `scripts/h_train_ml.py`, `scripts/h057_train.py`.

**⚠️ Windows-консоль (`.2`): `UnicodeEncodeError: 'charmap' codec can't encode '\u20bd'`**
Скрипты печатают `₽`, а консоль в cp1251. Запускать с UTF-8:
```bat
cd /d C:\Users\nadts\Dev\Deeptrading\backend
set "PYTHONIOENCODING=utf-8"
.venv\Scripts\python.exe scripts\run_v4_mltest.py
```
(кавычки в `set "VAR=value"` обязательны — иначе в значение попадёт пробел)

**⚠️ Если скрипт «просит токен»:**
- путь через **CSV** (`ml_train_csv.py`) токена не требует вообще — используйте его;
- путь через **БД** (`run_v4_mltest.py`, `research_pack._load_candles`) берёт настройки из
  `backend/.env` (`POSTGRES_*`) — токен T-Invest там не нужен;
- токен T-Invest нужен **только** для скачивания CSV (`scripts/download_*.py`) — он берётся
  из `backend/.env` (`TINKOFF_TOKEN=`). Проверьте, что `.env` есть и в нём заполнен
  `TINKOFF_TOKEN`, `SANDBOX=` (sandbox-счёт), `POSTGRES_HOST/PORT/USER/PASSWORD/DB`.
- **На `.2` сейчас `tinkoff_token=` ПУСТОЙ** — поэтому любые скрипты, которым нужен
  T-Invest API (скачивание истории, live-фид), там падают/просят токен. Для ML это неважно:
  `ml_train_csv.py` (CSV) и `run_v4_mltest.py` (БД) токен не используют. Если всё же нужен
  API — впишите токен в `C:\Users\nadts\Dev\Deeptrading\backend\.env`.
- Если в логах светится сам токен — это утечка, сообщить; в скриптах не печатать `settings`.

---

## 5. Правила и грабли (проверено болью)

- **FIGI** (не путать!): SBER=`BBG004730N88`, GAZP=`BBG004730RP0`, LKOH=`BBG004731032`,
  ROSN=`BBG004731354`, RUAL=`BBG008F2T3T2`. В `scripts/run_bot_engine_backtest.py` FIGI
  НЕПРАВИЛЬНЫЕ (баг) — не копировать оттуда.
- `cached_resample` — ключ по **контенту** (не по `id()`), иначе утечка кэша между тикерами.
- `semi_flip` NEUTRAL: `position = self._close(...)` — иначе повторные закрытия одной сделки.
- Инверсия сигналов — на уровне **сигнала** (`invert_signals` в `_process_candle`),
  не в `_submit_order`.
- Сторона у брокера: `BUY/SELL` ↔ `LONG/SHORT` — нормализовать в обе стороны.
- Выходы (SL/TP) живут в in-memory (`_trail_stop`/`_exit_target`), а БД — лишь запись;
  при восстановлении `_ensure_exit_state` читает уровни из БД (`sandbox_trades`).
  Ручная правка: `POST /api/v1/bot/positions/levels` `{"ticker":"SMLT","sl":304,"tp":292}`.
- Быстрая проверка SL/TP между барами — `_intrabar_exit_loop` (`intrabar_check_sec=10`),
  цена из `broker.last_prices()`.
- Единый снапшот состояния — `GET /api/v1/bot/state` (позиции+цены+SL/TP+P&L+режим+алерты).
- Битая свеча (прыжки >50% между соседними барами ≥3 раз за день) — день отбрасывается целиком.
- Тесты гонять на `.2`, UI править на `.3`; `.3` без разрешения не трогать.
