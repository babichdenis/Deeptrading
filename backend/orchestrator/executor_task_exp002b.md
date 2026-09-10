# executor_task_exp002b.md — EXP-002b: HIGH-VOL ENTRY GATE (pre-reg experiment)

**Выдал:** My3 (research lead), 2026-08-27
**Исполнитель:** DeepSeek (`.3`)
**Тип задачи:** PRE-REGISTERED EXPERIMENT (одна переменная; меняет торговлю при прохождении)
**Статус:** CLEARED TO RUN (B1/B2 telemetry НЕ противоречит volatility-тезису).

**Обоснование разблокировки (2026-08-27):** B1 inconclusive (нет повторяемого time-of-day pattern) → не задерживает;
B2 corroborates volatility-тезис (index_vol_hi net/trade 41.0 vs index_vol_lo 18.7, и alignment НЕ улучшает edge).
⇒ high-vol entry gate логично запустить как следующий основной тест.

**Единственная переменная:** `entry_volatility_gate: off → rolling_atr_high_only`.
- hi_vol = ATR_5m(decision_ts) > rolling median ATR_5m за пред. 20 завершённых торг. дней per-FIGI (point-in-time).
- WARMUP: первые 20 дней окна — новые входы запрещены (нет истории median).
- В hi-vol новые входы разрешены; в low-vol — НЕТ. Открытые позиции управляются baseline exit policy (без изменений).

**Immutable (ровно canonical July config, config_hash=1c7f75dc44c2aa67):**
- 5m signal / 1m next-open exec; quorum 2; cooldown 15; session 10:00–18:45 MSK;
- exit atr_stop(period14, mult2.0, RR2.0); signal_exit on; capital 10 000 ₽/pos; comm 5bps + slip 2bps;
- short on; carry_overnight; ML off; 7 functions (НЕ удалять RSI/VWAP); pyramid НЕТ.
- **НЕ использовать** shadow_vote_score / weighted votes / quorum≠2 / sizing change.

**Окно PRIMARY:** `2025-08-01 .. 2025-12-31` (pre-chosen, disjoint от 2026 baselines; единственный чистый 2025 OOS).
Сравнение на ОДНОМ окне: S1 (gate on) vs immutable S0 (gate off, тот же window).

**Pre-reg критерии успеха (gate КАК ФИЛЬТР, не замена baseline):**
- S1 net ≥ 0.90 × S0 net
- S1 MTM max DD ≤ 0.80 × S0 MTM max DD
- S1 aggregate net PF ≥ S0 PF
- S1 win rate ≥ S0 win rate
- S1 coverage (trades kept / S0 trades) ≥ 35%
- S1 concentration (max FIGI share of net) ≤ 50%

**Интерпретация:** если ВСЕ 6 критериев выполнены → EXP-002b PASSES (primary). Если нет → rejected-for-now (НЕ деплоить).

**ВАЖНО — не деплоить сразу:** при прохождении primary запустить подтверждение на 2-м независимом периоде
(2026 non-baseline month, напр. 2026-03 или 2026-05/06, как robustness check — 2025 это единственный чистый OOS,
2026 месяцы использовались под другие гипотезы, пометить как stability check). Только после 2-го прохождения — кандидат в baseline.

**Деливераблы:**
- `backend/reports/exp002b_primary_202508_202512.json` + `exp002b_primary_..._MY3_review.md`
- MTM-report обязателен (per-FIGI MTM DD, net/trade, PF, coverage, concentration).
- При прохождении: `exp002b_confirm_<period>.json`.

**Верификация My3 (до принятия):** immutability (config == canonical, НЕТ drift policy кроме gate),
point-in-time hi_vol (нет look-ahead median), disjoint window, корректность MTM math, S0 действительно immutable.

**Guardrails:** single variable; gate + function-set НЕ менять одновременно (иначе нельзя атрибутировать);
НЕ запускать одновременно с EXP-003. Shadow score НЕ влияет на execution.
