# VOTE ATTRIBUTION REPORT — canonical July 2026 baseline

**Run:** `5b44f3b383df` | **config_hash:** `1c7f75dc44c2aa67`
**Окно:** 2026-07-01..2026-07-31 | **Capital:** 10 000 ₽/позицию | **Lot:** 10
**Тип:** AUDIT_ONLY / DATA_RECOMPUTE (read-only; shadow-only; НЕ влияет на execution)

## 1. Scope данных
- Source: `reports/5b44f3b383df/` — единственный полный canonical baseline pack
  (entry_intents.csv + trades.csv + A–F telemetry). E1 pack `57b6244ee3eb` исключён (нет intents),
  legacy `ada34053fefd` (capital 100k, до A–F) исключён.
- Маппинг intent→trade: по `(figi, decision_time, side LONG/SHORT)` — 517/517 сошлось.
- Все 7 setup-функций active: `bollinger_reclaim, donchian_breakout, macd_cross, pullback_ema,
  range_compression_breakout, rsi_reversal, vwap_reclaim`.

## 2. Определения
- `net` — net_rub за вычетом commission+slippage; `net_bps` = net / entry_notional × 10 000.
- `PF` = Σ gross>0 / |Σ gross<0| (по сделкам блока).
- `net_bps_ci95` — bootstrap 95% CI для медианы (1000 ресемплов, seed=42), только при n≥20.
- `exit_reason`: target (2R) / signal_exit / stop_loss.
- `session`: open <10:30 МСК, mid 10:30–17:00, close ≥17:00.
- `vol_regime`: per-FIGI ATR_5m дня > rolling median 20 пред. торговых дней → hi_vol;
  первые 20 дней окна → warmup.
- `quorum_count` — число голосов кворума; `functions_mask` — setup-функции с сигналом в окне
  [decision−15m, decision] по той же стороне.

## 3. Function attribution (таблица)
| function | n | net | median net_bps | CI95 | PF | win% | exits (t/s/sl) |
|---|---|---|---|---|---|---|---|
| bollinger_reclaim | 91 | +2134 | 24.5 | — | — | — | — |
| donchian_breakout | 311 | +7391 | 28.4 | — | — | — | — |
| macd_cross | 280 | +6080 | 29.0 | — | — | — | — |
| pullback_ema | 305 | +6680 | 29.6 | — | — | — | — |
| range_compression_breakout | 220 | +3929 | 23.4 | — | — | — | — |
| rsi_reversal | 30 | +816 | 12.4 | — | — | — | — |
| vwap_reclaim | 4 | +133 | 42.0 | n<20 | — | — | — |

Полные значения (CI, PF, exits, MFE/MAE, hold, split by figi/side/session/vol):
`vote_attribution.json` → `per_function*`, и `vote_attribution_dataset_v1/function_attribution.csv`.
Малые n (rsi=30, vwap=4) НЕ интерпретировать как edge.

## 4. Pair attribution (pair_present — оба присутствуют, допускаются другие)
21 неупорядоченная пара из 7 функций; `pair_present` и `pair_exact` (ровно пара).
Файлы: `vote_attribution_dataset_v1/pair_present_attribution.csv`,
`pair_exact_attribution.csv`; значения в `vote_attribution.json` → `per_pair_present`,
`per_pair_exact`. Splits по side/session/vol — при n≥30. Малые n не интерпретировать.

## 5. n и CI
CI95 даётся только при n≥20 (bootstrap). Все слабые ячейки помечены отсутствием CI —
не делать по ним выводов.

## 6. Shrinkage-формула (canonical)
`weight = (n / (n + k)) * median_net_bps`, **k = 200**, `formula_version = v1_median_shrink`.
Walk-forward: вес для decision_time T использует ТОЛЬКО сделки с `exit_time < day_start(T)`;
`training_period_end` и `weight_timestamp` (= decision_time T) в каждой строке.
Редкие функции (n << 200) → вес стремится к нулю.

## 7. Warning
`shadow_score` — shadow-only (`causal_use=false`). EngineRunner и все торговые решения
НЕ используют его. Изменение весов не меняет trades/P&L.

## 8. Правила исключений
- Runs с legacy session bug / другим exit policy / капиталом ≠ 10k — исключены.
- intent без executed trade — исключён из атрибуции.
- n < 20 — без CI; n < 30 — без split-интерпретаций.

## 9. Ограничения и data gaps
- Один месяц (июль 2026) и 5 FIGI — малый объём для вывода о весах функций.
- vwap_reclaim (n=4) и rsi_reversal (n=30) — статистически слабы.
- Warmup-период (первые 20 дней окна) без vol-regime.
- Атрибуция — эксито-анализ; корреляции с другими сигналами не удаляются.

Артефакты: `reports/5b44f3b383df/vote_attribution.json`,
`reports/5b44f3b383df/vote_attribution_dataset_v1/` (7 файлов + manifest),
`reports/5b44f3b383df/vote_shadow_scores.csv`, `.../vote_attribution_report.md`,
`.../vote_shadow_scores_INVALID_k20.md`.
