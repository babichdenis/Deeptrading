# Research Pack — read-only экспорт для внешнего анализа стратегии

## Назначение

Immutable research pack с данными по canonical стратегии `ensemble_main_v1` для
внешнего LLM-анализа. Pack отвечает на вопросы:

1. почему EngineRunner исполняет мало сделок;
2. какие gates режут entry intents;
3. отличия прибыльных/убыточных executable trades;
4. какие 3 offline-эксперимента стоит проверить;
5. без права изменять параметры или принимать торговые решения.

**Инварианты:**
- НЕ меняется EngineRunner, SignalPolicy, CostModel, thresholds, session policy, ML.
- НЕ отправляется данных во внешние API.
- НЕТ live-trading логики.
- Всё новое read-only и покрыто тестами (tests/test_research_pack.py, 10 шт).
- Oracle/future-поля НЕ используются как features/рекомендации и исключены из экспорта.

## Endpoint

```
GET /api/v1/research/pack?from_ts=2026-07-01T00:00:00Z&to_ts=2026-07-31T00:00:00Z
    &figis=RUAL,AFLT,SNGSP,MVID,NLMK
    &strategy_id=ensemble_main_v1
    &include_samples=true
    &max_trade_samples=100
    &max_rejection_samples=200
    &output_format=json        # json | csv_bundle
```

Параметры:

| Параметр | Обязательный | Дефолт | Описание |
|---|---|---|---|
| `from_ts` | да | — | ISO datetime начала периода (UTC) |
| `to_ts` | да | — | ISO datetime конца периода (UTC) |
| `figis` | нет | RUAL,AFLT,SNGSP,MVID,NLMK | тикеры или FIGI через запятую |
| `strategy_id` | нет | ensemble_main_v1 | только canonical |
| `include_samples` | нет | true | включать строки trades/intents/rejections |
| `max_trade_samples` | нет | 100 | максимум строк executable trades |
| `max_rejection_samples` | нет | 200 | максимум строк sampled rejections |
| `output_format` | нет | json | json или csv_bundle |

Ограничения:
- период > 92 дней → `400` (защита от случайных гигантских прогонов);
- endpoint read-only: пишет только в `reports/{seed}/`;

## Артефакты

```
reports/{seed}/research_pack.json           # основной pack (все секции A–M)
reports/{seed}/research_pack_manifest.json  # манифест: хэши, параметры, счётчики
reports/{seed}/trades.csv                   # при output_format=csv_bundle
reports/{seed}/entry_intents.csv
reports/{seed}/rejections.csv
reports/{seed}/daily_pnl.csv
```

`seed` — детерминированный хэш от (period, figis): повторный запрос с теми же
параметрами пишет в ту же папку и даёт одинаковую выборку rejections.

## JSON schema (секции)

```jsonc
{
  "A_metadata": {
    "run_id": "string",
    "schema_version": "research_pack_v1",
    "generated_at": "ISO UTC",
    "period": {"from": "ISO", "to": "ISO"},
    "timezone": "Europe/Moscow",
    "data_source": "candles table (interval=1min), read-only",
    "canonical": true
  },
  "B_strategy_config": {
    "strategy_id": "ensemble_main_v1",
    "signal_tf": "5m",
    "execution_tf": "1m",
    "fill": "next available 1m open",
    "functions": ["range_compression_breakout", "...", "macd_cross"],
    "quorum": 2,
    "bias_mode": "info",
    "bias": {"tf": "hour", "period": 50},
    "cooldown_bars": 15,
    "entry_session": "main",
    "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
    "one_position_per_figi": true,
    "capital_per_position": 100000,
    "lot_size": 10,
    "lot_rounding": "floor(capital / (entry_fill * lot)) * lot",
    "commission_rate": 0.0005,
    "slippage_bps": 2.0,
    "ml_mode": "off",
    "allow_short": true,
    "carry_overnight": true
  },
  "C_portfolio_assumptions": {
    "capital_per_position": 100000,
    "reference_gross_exposure": 500000,
    "maximum_concurrent_positions": 5,
    "cash_constraint_enforced": false,
    "mtm_equity": "realized-only (no intraday MTM in canonical runner)"
  },
  "D_funnel": {
    "raw_setup_candidates": {"count": 0, "pct_prev": null, "pct_entry_candidates": 0.0},
    "quorum_candidates": {...},
    "entry_candidates": {...},
    "entry_intents": {...},
    "rejected_bias": {...},
    "rejected_quorum": {...},
    "rejected_regime": {...},
    "rejected_cooldown": {...},
    "rejected_session": {"count": null, "note": "недоступно из compute_ensemble API"},
    "rejected_already_in_position": null,
    "rejected_duplicate_episode": null,
    "rejected_no_next_bar": null,
    "rejected_price_or_lot": null,
    "executed_entries": {...},
    "closed_trades": {...},
    "open_trades_at_end": 0
  },
  "E_rejection_attribution": {
    "primary_reason_counts": {"SETUP_MISSING": 0, "AGAINST_BIAS": 0, ...},
    "per_figi": {"BBG...": {...}},
    "per_hour_utc": {"9": 0, ...},
    "note": "только entry rejections"
  },
  "F_performance_summary": {
    "gross_rub": 0.0, "commission_rub": 0.0, "slippage_rub": 0.0,
    "total_costs_rub": 0.0, "net_rub": 0.0,
    "gross_pf": null, "net_pf": null,
    "pf_definitions": {"gross_pf": "...", "net_pf": "..."},
    "win_rate": null, "expectancy_rub": 0.0, "median_net_rub": 0.0,
    "max_drawdown_rub": 0.0, "max_drawdown_pct": null,
    "max_consecutive_wins": 0, "max_consecutive_losses": 0,
    "trade_count": 0,
    "avg_hold_bars": null, "median_hold_bars": null,
    "avg_round_trip_cost_bps": 0.0, "cost_to_gross_ratio": null,
    "daily_mtm_availability": false,
    "note": "только executable EngineRunner trades; НЕ candidate-audit"
  },
  "G_per_figi_summary": {
    "BBG008F2T3T2": {
      "ticker": "RUAL", "entry_intents": 0, "executed_trades": 0,
      "gross_rub": 0.0, "total_costs_rub": 0.0, "net_rub": 0.0,
      "gross_pf": null, "rejection_distribution": {}, "concentration_of_total_net_pct": null
    }
  },
  "H_daily_pnl": [
    {"date": "2026-07-01", "gross": 0.0, "commission": 0.0, "slippage": 0.0,
     "costs": 0.0, "net": 0.0, "trade_count": 0,
     "cumulative_realized_net": 0.0, "mtm_equity": null, "open_positions_eod": 0}
  ],
  "I_executable_trades": [
    {"trade_id": "...", "figi": "...", "ticker": "RUAL", "side": "LONG",
     "signal_time": null, "decision_time": "ISO", "entry_time": "ISO", "exit_time": "ISO",
     "entry_price": 0.0, "exit_price": 0.0, "qty": 4000, "lot_size": 10,
     "entry_notional": 0.0, "exit_notional": 0.0,
     "gross_rub": 0.0, "commission_rub": 0.0, "slippage_rub": 0.0,
     "total_cost_rub": 0.0, "net_rub": 0.0,
     "exit_reason": "target", "hold_bars": 0, "hold_minutes": 0.0,
     "break_even_bps": 0.0, "mfe_r": 0.0, "mae_r": 0.0}
  ],
  "J_entry_intents": [
    {"intent_id": "...", "figi": "...", "ticker": "RUAL", "side": "BUY",
     "decision_time": "ISO", "source_stage": "entry_breakout",
     "quorum_count": 2, "engine_decision": "executed|rejected",
     "primary_reject_reason": null, "failed_gates": [], "linked_trade_id": null}
  ],
  "K_sampled_rejections": [/* как J, детерминированная выборка */],
  "L_diagnostics": {
    "simultaneous_intents": 0,
    "top10_trades_by_net": [...], "bottom10_trades_by_net": [...],
    "note_top_bottom": "дескриптивно, не рекомендация",
    "hold_time_distribution_bars": {...}
  },
  "M_leakage_and_data_quality_checks": {
    "feature_timestamps_lte_decision_time": {"pass": true, "evidence": "..."},
    "execution_next_available_1m_bar": {"pass": true, "evidence": "..."},
    "oracle_fields_excluded": {"pass": true, "evidence": "..."},
    "costs_not_double_counted": {"pass": true, "evidence": "..."},
    "lot_rounding_applied": {"pass": true, "evidence": "..."},
    "trades_non_overlapping_per_figi": {"pass": null, "evidence": "..."},
    "no_future_candles_in_decisions": {"pass": true, "evidence": "..."}
  }
}
```

## Пример запроса

```bash
curl -s "http://127.0.0.1:8001/api/v1/research/pack?from_ts=2026-07-01T00:00:00Z&to_ts=2026-07-08T00:00:00Z&figis=RUAL" | python -m json.tool
```

Ответ:

```json
{
  "run_id": "<seed><timestamp>",
  "schema_version": "research_pack_v1",
  "strategy_id": "ensemble_main_v1",
  "out_dir": ".../reports/<seed>",
  "manifest": { "...": "..." },
  "read_only": true
}
```

## UI

Кнопка «Экспорт research pack» (без автоматической отправки) — Lab → результаты
конфигурации. Данные никуда не отправляются, файлы остаются локально.

## Тесты

`backend/tests/test_research_pack.py` — 10 тестов:
1. read-only (только immutable артефакты)
2. нет credentials/токенов/URL БД в JSON/CSV
3. oracle-поля исключены из executable_trades / entry_intents / rejections
4. funnel reconcile (executed_entries >= closed_trades, intents >= executed >= closed)
5. total_cost = commission + slippage ровно один раз
6. qty кратен lot_size
7. entry_time > decision_time
8. детерминированная выборка rejections (одинаковый seed → одинаковый sample)
9. timezone = Europe/Moscow в daily output
10. период > 92 дней отклоняется

## Исследовательские секции (N–R, добавлены 2026-08-26)

Pack теперь включает не только сделки, но и данные для анализа «как увеличить доход»:

| Секция | Содержимое | Назначение |
|---|---|---|
| `N_candles_5m` | 5m свечи (ts/open/high/low/close/volume) по каждой FIGI | контекст цены вокруг сделок |
| `O_setup_signals` | ВСЕ raw-сигналы 7 функций (ts, side, reason) по FIGI | какие функции голосовали |
| `P_quorum_signals` | quorum-события (прошедшие кворум): ts, side, votes | порог кворума |
| `Q_entry_candidates` | entry-кандидаты (микро-брейкаут): ts, side, reason, breakout_level | входной триггер |
| `R_session_audit` | session-инварианты: сделки по session_at(entry), intents по session_at(decision), fills_after_main_boundary, exits_outside_main, overnight | верификация main-политики |

Пример использования для исследования:
- Сопоставить сделки (`I_executable_trades`) с сигналами (`O_setup_signals`) по времени,
  чтобы понять, какие функции дают прибыльные/убыточные входы;
- `Q_entry_candidates` → сколько раз триггер срабатывал, но не было кворума;
- `R_session_audit` → влияет ли время входа на результат.

Каждый тест (`backend/tests/test_research_pack.py`) теперь создаёт собственный
`research_pack.json` в `backend/reports/test_research_pack/<test_name>/`.
