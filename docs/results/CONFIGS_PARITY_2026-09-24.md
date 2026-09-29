# Паритет конфигураций: LIVE / ТЕСТЫ / БЭКТЕСТЫ (зафиксировано 2026-09-24)

> ОБНОВЛЕНИЕ 2026-09-24 (решение юзера): тест снова на ОТДЕЛЬНОМ конфиге
> `ensemble_config.test.json` (10min setups, SL manual 2.0, veto + TREND-only
> bias_by_state) — намеренное отличие от live для тестового прогона.
> `load_ensemble_config(mode="test")` грузит .test.json, live/sandbox — .json.
> Файл `ensemble_config.test.json.bak_20260924` — старая версия тестового конфига.

---

## 1. ТЕКУЩАЯ конфигурация (после фикса, тест == live)

Источник правды: `data/ensemble_config.json` — единственный конфиг для live,
sandbox и test.

| Параметр | Значение |
|----------|----------|
| Quorum | 2 |
| Neutral mode | semi_flip |
| Setups | **8 стратегий, все enabled, все tf=5min**: rsi_reversal(16/30/80), bollinger_reclaim(15/1.0), pullback_ema(20/10), vwap_reclaim(2.0), range_compression_breakout(15/16/40), macd_cross(12/26/9), donchian_breakout(45), volume_drop(20/1.5) |
| Bias (base) | hour/50, bias_mode=**veto** |
| Bias per-regime (bias_by_state) | TREND_UP/TREND_DOWN → 30min/100; HIGH_VOLATILITY → 10min/300; NEUTRAL/RANGE → hour/50 |
| entry_tf (в файле) | 1min — ⚠ НЕ читается рантаймом! Фактический entry_tf берётся из `BotConfig.ensemble_entry_tf` = дефолт **5min** |
| entry_from_setups | true (в файле); фактически — дефолт `BotConfig.ensemble_entry_from_setups` = **true** (сторона из ансамблей) |
| SL/TP | sl_source=**optuna** → слушается `sl_override=2.0` / `rr_override=0` (⚠ rr_override=0 → фолбэк в `optuna_params.rr` per-ticker, иначе 4.0) |
| vol_thr | 0.0 (выкл) |
| min_bar_turnover / density | 100000 / 0.5 |
| reentry cooldown | 15 баров |
| drop_useless / use_all_setups | false / false |
| regime_setups_filter | {} (пусто) |

Таймфреймы (фактически):
- **1min** — движок, свечи, исполнение (interval_name=1min при use_ensemble).
- **5min** — все сетапы ансамбля (resample 1m→5m кешится) + entry триггер (micro-breakout, entry_tf=5min).
- **hour** — базовый bias + режимы (regime tf=hour).
- **30min / 10min** — per-regime bias (TREND / HIGH_VOLATILITY).

Runtime-параметры активного бота (`тест 23`, сейчас): sessions=[day, evening],
confirm_flip=0, reentry_cooldown_bars=15, overnight=false, commission=0.0005,
slippage=2bps, atr=14/4.0/4.0, top_n=20, ensemble_capital=2000,
trade_regimes=[TREND_UP, TREND_DOWN, HIGH_VOLATILITY].
⚠ ensemble_quorum=3 в BotConfig статуса — НЕ влияет на ансамбль (кворум берётся
из `ensemble_config.json` = 2).

---

## 2. LIVE до фиксов (что реально крутилось на live-контуре)

Файл `data/ensemble_config.json` — идентичен §1 (это и был live-конфиг).
Отличие live-контура от теста до фикса = расхождение см. §3.

---

## 3. ТЕСТЫ (активный конфиг `тест 23`, после настройки 2026-09-24)

Файл `data/ensemble_config.test.json` (перезаписан 2026-09-24, бэкап `.bak_20260924`).

## 3a. Текущий тестовый конфиг

| Параметр | Тест (сейчас) | Live (§1) | Расхождение |
|----------|---------------|-----------|-------------|
| Setups | **6 активных**: rsi, bollinger, vwap, macd, donchian, volume_drop | 8 активных | ⚠ выключены pullback_ema, range_compression_breakout |
| ТФ сетапов | **10min** | 5min | ⚠ осознанно другой ТФ сигналов |
| entry_tf (в файле) | 5min | 1min (не читается, факт. 5min) | фактически совпадает (5min) |
| entry_from_setups | true | true | совпадает |
| bias | hour/50, **veto** | hour/50, veto | совпадает |
| bias_by_state | **только TREND_UP/TREND_DOWN** (30min/100) → NEUTRAL/RANGE/HV запрещены (REGIME_OFF) | TREND 30min, HV 10min, NEUTRAL/RANGE hour/50 | ⚠ тест жёстче: HV не торгуется |
| SL/TP | **manual: sl_mult=2.0, rr=4.0** | optuna (sl_override=2.0) | ⚠ manual, не optuna |
| vol_thr / cooldown | 0.0 / 15 | 0.0 / 15 | совпадает |

Funnel живого теста (первые минуты): AGAINST_BIAS 10630, REGIME_OFF 8333,
COMBO_BIAS 142 — veto и TREND-gate реально гоняют.

### 3b. Старый базовый тестовый конфиг (до настройки, бэкап `.bak_20260924`)

Файл был: 6 стратегий 10min (rsi, bollinger, vwap, macd, donchian, volume_drop —
pullback/range выкл) + bias hour/50 veto БЕЗ bias_by_state + entry_tf 5min.
Отличие от текущего: не было per-regime combo (входы в NEUTRAL/RANGE были
разрешены при совпадении с базовым veto; HV тоже).

### 3c. Вариант `q2ref` (файл `ensemble_config.test.q2ref.json`, .env: BOT_TEST_VARIANT=q2ref)

⚠ Важно: `test_variant` в рантайме НЕ подхватывался `load_ensemble_config`
(гейтовые оверрайды TEST_VARIANTS/apply_test_overrides — мёртвый код), поэтому
«тест 23» реально стартовал с базовым `.test.json` (§3a), а не с q2ref.
Файл q2ref зафиксирован ниже как «задуманный» вариант:

| Параметр | q2ref (задуманный) | Live (§1) |
|----------|--------------------|-----------|
| Setups | 6 активных, **все tf=10min** | 8 активных, 5min |
| entry_tf | 1min | фактически 5min |
| entry_from_setups | true | true |
| bias | hour/50, veto, **без bias_by_state** | hour/50 + bias_by_state |
| SL/TP | **manual sl_mult=6.0, rr=4.0** (не optuna!) | optuna |
| vol_thr / cooldown | 0.0 / 15 | 0.0 / 15 |
| секция bot | confirm_flip=0, reentry=15, sessions=[day], overnight=false, rank_enabled=false, entry_h1_align/tf_conflict/last_hour_block=false | см. §1 (sessions=[day,evening] из сохранёнок) |
| exit_* | куча ручных exit-настроек (rsi_ob=70, volume_drop и пр.) | не используются (exit из AtrStopPolicy/optuna) |

---

## 4. БЭКТЕСТЫ (августовские серии: 648 trades / +6540₽, Phase2 B1, Optuna OOS)

Основные скрипты: `scripts/backtest_v2.py`, `scripts/backtest_full_bot.py`,
`scripts/portfolio_optuna_backtest.py`, `scripts/run_bot_engine_backtest.py`.

| Параметр | Бэктесты | Live (§1) | Расхождение |
|----------|----------|-----------|-------------|
| Setups | **V2_SETUPS = 7 стратегий, все tf=5min**: rsi, bollinger, pullback, vwap, range, macd, donchian — ⚠ **нет volume_drop** | 8 стратегий (V2 + volume_drop) | ⚠ отличается состав |
| Quorum | 2 (все скрипты) | 2 (из ec) | совпадает |
| entry_tf | 5min (`entry: {tf: 5min, lookback: 1}`) | фактически 5min | совпадает |
| entry_from_setups | **False** (дефолт EnsembleParams) → сторона из micro-breakout | **True** → сторона из ансамблей | ⚠ КРИТИЧНО — разная логика стороны входа |
| Bias | `bias_mode="info"` (только метка, НЕ veto) + hour/50; bias_by_state нет | **veto** + bias_by_state | ⚠ КРИТИЧНО — в бэктесте bias не блокирует |
| Bias per-regime | нет | есть (30min/10min) | ⚠ |
| SL/TP | **atr_multiplier=4.0, rr=4.0** (CFG) — либо **per-ticker optuna** (`portfolio_optuna_backtest`: sl_mult/rr из `instruments.optuna_params`, V2_BASE_P fallback 4.0/4.0) | optuna (sl_override=2.0) | ⚠ live слушает sl_override=2.0, бэктесты — 4.0 либо optuna |
| neutral_mode | не задан явно в бэктесте (→ дефолт compute) / semi_flip в optuna-скрипте | semi_flip | проверить |
| Commission | **0.003 (0.3% — тариф Investor)** | 0.0005 (0.05%) | ⚠ КРИТИЧНО ×6 — бэктесты платят в 6 раз больше |
| Slippage | 2bps | 2bps | совпадает |
| Sessions | ensemble_session="main" (входы только main), sessions=[morning,day,evening] | ensemble_session="all", sessions=[day,evening] | ⚠ входы в разные окна |
| Overnight | false (backtest_v2/full_bot) | false | совпадает |
| Margin | use_margin=true, max_margin_pct=80 (backtest_v2/full_bot) | margin_leverage=0.0 | ⚠ |
| Confirm flip / reentry | 2 / 15 (CFG) | 0 / 15 (сохранёнки теста) | ⚠ confirm_flip: 2 vs 0 |
| Period (эталонные прогоны) | Aug 1–Sep 1 (v2), Aug 18–25 (full_bot), Optuna w1/w2/w3 (авг) | реплей-окно теста 2026-09-23 | — |
| Тикеры | 20 eligible (Phase2 B1) / 5 pilot (Optuna) | top_n=20, universe 31–32 | примерно совпадает |

---

## 5. Сводная таблица расхождений (главное)

| # | Параметр | Live | Тест (активный, осознанно отличен) | Бэктесты | Критичность |
|---|----------|------|--------------------------------------|----------|-------------|
| 1 | ТФ сетапов | 5min | **10min** | 5min | 🟡 (осознанно) |
| 2 | Кол-во активных сетапов | 8 | 6 | 7 (нет volume_drop) | 🟡 |
| 3 | bias_by_state (per-regime) | есть (all states) | есть (только TREND_UP/DOWN) | нет (optuna_ensembles: info+ROUTER) | 🟡 |
| 4 | bias_mode | veto | veto | **info** (optuna_ensembles) / veto (full_bot default) | 🔴 |
| 5 | entry_from_setups (сторона) | true | true | **false** (full_bot default) / true (optuna_ensembles) | 🔴 |
| 6 | SL/TP источник | optuna | **manual 2.0** | 4.0 / optuna | 🟡 |
| 7 | Commission | 0.0005 | 0.0005 | **0.003** | 🔴 |
| 8 | Sessions (входы) | all / [day,evening] | day | main / [morning,day,evening] | 🟠 |
| 9 | Confirm flip | 0 (сохранёнки) | 0 (q2ref bot) | 2 | 🟠 |
| 10 | entry_tf (файл vs факт) | файл 1min / факт **5min** | файл 5min | 5min | 🟡 (файл врёт) |

## 6. Trailing stop (проверено 2026-09-24)

Трейлинг в коде ЕСТЬ, но **ВЫКЛЮЧЕН** в текущей конфигурации.

**Рукавичка включения:** `BotConfig.trail_activation_comm_mult` (runtime.py:175) =
`None` → трейлинг не активен (работают только SL/TP + сигнальные выходы).
В `/api/v1/bot/status` подтверждено: `trail_activation_comm_mult: None`.

**Реализация (engine/exits.py, класс AtrStopPolicy, policy_id="atr_stop"):**
- `plan_entry()` — SL = entry ∓ ATR×multiplier, TP = ± ATR×risk_reward.
- `trailing_activated()` — активация при PnL (по последнему close) ≥
  `trail_activation_comm_mult × commission_входа`. Без qty/commission → false.
- `update_stop()` — rachet-стоп (только в плюс). Two режима:
  - комиссионный (бот, `trail_activation_comm_mult != None`): dist = trail_distance_atr × ATR (чистый ATR, а не risk);
  - ATR-режим (`trail_activation_r`/`trail_distance_r`): dist = trail_distance_r × risk.

**Динамический трейлинг (настройки):**
- `trail_distance_atr` = 2.5 (базовая дистанция за ценой, в ATR)
- `trail_compress_r` = 1.0 (сжатие дистанции по прибыли, в R)
- `trail_min_factor` = 0.3 (мин. множитель сжатия)
- `trail_min_atr` = 0.5 (мин. дистанция в ATR)
- `trail_vol_boost` = 0.3 (объём: высокий → шире, низкий → теснее)

**Как включить:** выставить `trail_activation_comm_mult` (напр. 2.0 — активация при
PnL ≥ 2× комиссии входа). Значение > 0, не None.

**Куда передаётся:** runtime.py L3102-3107, L5625-5630, L5749-5754 →
`AtrStopPolicy(trail_activation_comm_mult=cfg.trail_activation_comm_mult, ...)`.

## 6. Открытые вопросы (решить, чтобы паритет был полным)

1. `entry_tf: "1min"` в `ensemble_config.json` — мёртвое поле (читается
   `BotConfig.ensemble_entry_tf` = 5min). Убрать из файла или начать читать?
2. Бэктесты: bias_mode=info vs live veto, commission 0.003 vs 0.0005,
   entry_from_setups false vs true — бэктестовые результаты (август) получены
   при ДРУГОЙ логике, чем текущий live. Нужен перегон эталона под live-конфигом.
3. `rr_override: 0` в live-конфиге — при sl_source=optuna RR берётся из
   `optuna_params.rr` per-ticker (фолбэк 4.0); `rr: 4.0` в файле не
   используется. Проверить фактические per-ticker rr в `instruments.optuna_params`.
4. Активный тест `тест 23` перезапущен после фикса (2026-09-24 11:00 UTC) —
   прогон идёт уже под live-конфигом. Старые прогоны (тест 22 и ранее) —
   под 10min-конфигом, их результаты НЕ сравнимы с новыми.
