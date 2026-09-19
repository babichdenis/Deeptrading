# GATES — все гейты на пути заявки (аудит и структура)

> Источник правды по составу гейтов: **`backend/app/bot/gates.py`** (реестр `GATES`).
> Живое состояние + статистика отказов: **`GET /api/v1/bot/gates`**.
> `key` в реестре = reason-код из логов (`ПРОПУСК ВХОДА ...`), событий (`SIGNAL_REJECTED`)
> и `skip_counts` (видно в `GET /bot/portfolio_summary`).

## Слои и ранжирование (порядок прохождения заявки)

Ранжирование: **если гейт выше рангом отклонил — остальные не выполняются**.
Дешёвые проверки (время/сессии) идут до запросов к брокеру (маржа/`get_max_lots`).

| Ранг | Слой | Где в коде | Что решает |
|------|------|------------|------------|
| 1 | **signal** | `services/ensemble.py`, `ensemble_strategy.py` | кворум, сетапы, объём, bias, режим сетапов, IMOEX-вето, 1м-подтверждения |
| 1.5 | **signal: STAGE S** | `runtime._submit_order` (сразу после времени) | **стакан/ликвидность для ВСЕХ входов**: imbalance против стороны, широкий спред, низкий оборот |
| 2 | **time** (STAGE A) | `runtime._submit_order` (начало) + `_process_candle` | пауза, сессии, последний час последней сессии, long/short, риск дня, loss-streak, already-held, IMOEX guard |
| 3 | **trend** (STAGE B) | `runtime._submit_order` | daily_bias, H1/TF-конфликт, legacy MTF, якорь кворума, рейтинг |
| 4 | **portfolio** (STAGE C + после сайзинга) | `runtime._submit_order` | max_positions, кластер сектора, баланс L/S; затем max_exposure и net/sector/margin/stress-лимиты |
| 5 | **sizing** (STAGE D) | `runtime._submit_order` | цена/лот/бюджет, `get_max_lots` (брокер), qty, лимит маржи |
| 6 | **approval** | `ai_approval_worker.py` + `/bot/ai_trade` + STAGE C2 | AI approve/reject, пауза после reject, чейзинг AI-ордеров, потолки SL/TP |

> **STAGE S (стакан)** — первый рыночный гейт после сигнала ансамбля: даже если ансамбль
> дал SHORT, а стакан перекошен в покупки (imbalance > `entry_ob_imbalance_max`) или
> спред шире `entry_ob_spread_max` — входа не будет. Работает для движка и AI.
> AI-гейты чейзинга (STAGE C2) выполняются после time/trend/portfolio и
> **до** запроса маржи; стакан берётся из `app/services/orderbook.py` (кэш 5с).
> STAGE A/B/C — без обращений к брокеру (только память/кэш). STAGE D — единственное
> место, где спрашивается маржа (`get_max_lots`), и только после прохождения всех
> гейтов выше. `_process_candle` дублирует часть STAGE A для входов движка (лог не
> дублируется: отклонённый движок до `_submit_order` не доходит).

## Полный список

### signal — внутри ансамбля
| key | Флаг конфига | Смысл |
|-----|--------------|-------|
| `no_entries` | `ensemble_quorum` | кворум не набрал вход (funnel_raw) |
| `no_fresh` | — | входы есть, но старше FRESH_MIN |
| `vol_thr` | `vol_thr` (ensemble) | объём < порога |
| `AGAINST_BIAS` | `bias` (ensemble) | вход против bias |
| `REGIME_MODE` | `regime_setups_filter` | режим запрещает сетап |
| `IMOEX_VETO` / `STOCH_FILTER` / `VOL_FLOW` / `SETUP_MISSING` | — | фильтры сетапов |
| `entry_confirm` | `entry_confirm_closes` | N 1м-закрытий по направлению |
| `entry_macd_1m` | — | 1м MACD подтверждает вход |

### pre_order — `_process_candle`
| key | Флаг конфига | Смысл |
|-----|--------------|-------|
| `cooldown` | `reentry_cooldown_bars` | пауза после выхода |
| `already_held` | — | позиция уже есть |
| `session_filter` | `sessions` | вне разрешённых сессий |
| `entries_paused` | — (UI) | ручная пауза входов |
| `long_disabled` / `short_disabled` | `long_allowed` / `short_allowed` | направление выключено |
| `regime_off` | `trade_regimes` | режим не разрешён |
| `trend_alignment` | `trend_alignment` | вход против TREND_UP/TREND_DOWN |
| `loss_streak_hold` | `loss_streak_hold` | HOLD после серии убытков |
| `imoex_guard` | `imoex_guard` | вход против всплеска IMOEX |
| `risk_limit` | `daily_loss_limit` | risk.state != NORMAL |

### order — `_submit_order`
| key | Флаг конфига | Смысл |
|-----|--------------|-------|
| `daily_bias` | `daily_bias` (+`daily_bias_mode`) | против дневного bias (veto/info) |
| `last_hour` | `entry_last_hour_block` | последний час ПОСЛЕДНЕЙ сессии бота |
| `h1_align` | `entry_h1_align` | H1 MACD против стороны входа |
| `tf_conflict` | `entry_tf_conflict` | daily и H1 противоречат |
| `mtf_h1_align` / `mtf_m5_trigger` | `mtf_align` / `mtf_trigger` | legacy MTF-фильтры |
| `require_member` | `ensemble_require_member` | нет обязательного голоса |
| `rank_filter` | `rank_enabled` | тикер не в top-N |
| `market_reversal` | — | режим reversal против книги |
| `max_exposure` | `max_exposure_pct` | свои деньги в позициях > X equity |
| `portfolio_limit` | `max_net_exposure_pct`, `max_sector_pct`, `max_margin_use_pct`, `max_stress_loss_pct` | портфельные лимиты |
| `max_positions` | `max_positions` | лимит одновременных позиций |
| `sector_cluster` | `max_sector_positions` | лимит позиций в секторе |
| `ls_balance` | `max_short_share` | перекос в шорт |
| `budget` / `margin_limit` | `pos_pct`, `max_margin_pct` | сайзинг: нет денег/лимит маржи |
| `queue` | `queue_enabled` | слабый вход отложен в очередь |
| `policy_reject` | — | SignalPolicy отклонил сигнал |

### approval — AI-слой
| key | Флаг конфига | Смысл |
|-----|--------------|-------|
| `ai_approval` | `ai_approval` (+`_timeout_sec`, `_default`) | заявка ждёт решения модели |
| `ai_reject_cooldown` | `ai_reject_cooldown_min` | пауза после отказа AI |
| `ai_already_pending` | — | заявка уже в гейте |
| `ai_chase` | `ai_chase_pct` | AI-вход после хода >X% за день |
| `ai_orderbook` | `ai_ob_imbalance_max`, `ai_ob_spread_max` | стакан против входа / спред |
| `ai_sl_tp` | `ai_sl_max_pct`, `ai_tp_max_pct` | потолки SL/TP AI-ордера |
| `ai_watch` | `ai_approval` | вахтёр позиций (close/tighten) |

### data
| key | Смысл |
|-----|-------|
| `bad_candle` | OHLC/объём не прошли валидацию |
| `stale_candle` | бар не закрыт / вне окна свежести |

## Логирование (не «молча»)

Каждый отказ пишет:
1. лог бота: `ПРОПУСК ВХОДА {ticker}: ...` (в UI → Логи);
2. событие: `SIGNAL_REJECTED` с `reason=<key>` и `detail`;
3. счётчик: `skip_counts` (в UI/portfolio_summary);
4. ошибка самого гейта — `TF_GATE_ERROR` + `⚠ TF-ГЕЙТ ...` (не проглатывается).

## Проверка живьём

```bash
curl -s http://127.0.0.1:8000/api/v1/bot/gates | python3 -m json.tool
# enabled = состояние флага; rejects = сколько раз гейт отклонил (с последнего старта)
```

## Что дальше (если захотим физический рефакторинг)

Сейчас реестр — «паспорт» гейтов, а сами проверки остаются на своих местах (live-код
не двигаем без нужды). Следующий безопасный шаг: вынести группы проверок в
`gates.py` по одной (TF → portfolio → AI) с тестами, вызывая их из `_submit_order`.
