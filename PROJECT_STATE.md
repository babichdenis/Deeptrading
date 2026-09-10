# Project State

Updated: 2026-08-27
Owner: My3 (research lead) + DeepSeek (executor) — утверждает человек
Mode: AUDIT / DISCOVERY / EXPERIMENT_DESIGN (GLOBAL_DISCOVERY завершён)
Location: корень проекта, рядом с chat.md. Старые копии docs/PROJECT_STATE.md и
textdocs/PROJECT_STATE.md удалены — этот файл ЕДИНСТВЕННЫЙ источник состояния.

## Current canonical baseline
- strategy_id: ensemble_main_v1
- status: RESEARCH ONLY / NOT LIVE
- signal: 5m
- execution: next 1m open
- entry session: main, 10:00–18:45 MSK
- quorum: 2
- cooldown: 15 bars
- capital: 10,000 ₽ per position (config declares 100000, effective=10000)
- commission: 5 bps/side
- slippage: 2 bps/side
- intrabar: conservative_stop_first
- ML: off
- universe: RUAL, SNGSP, AFLT, MVID, NLMK (5 FIGI)

## Last canonical run (baseline)
- run_id: 5b44f3b383df20260826191654
- generated_at: 2026-08-26T19:20:31Z
- commit: 1c20a604fd7b
- config_hash: 1c7f75dc44c2aa67
- period: 2026-07-01 to 2026-07-31
- trades: 517
- gross: +18,286.73 ₽
- commission: 5,069.33 ₽
- slippage: 2,027.75 ₽
- costs: 7,097.08 ₽
- net: +11,189.52 ₽
- net PF: 3.68
- gross PF: 8.79
- win_rate: 71.37%
- expectancy: 21.64 ₽/trade
- median net: 26.37 ₽
- max DD (realised-only): 202.83 ₽
- result type: exploratory, one-month only
- important: MTM equity 1m сгенерирован (audit A–F), но F-сводка
  reported realised-only (daily_mtm_availability флаг отсутствует в pack) =>
  DD НЕ полностью репрезентативен.

## Known open issues
1. MTM в F: A4 DONE (mtm_equity_1m.csv + mtm_pnl/mtm_dd в F, availability=True),
   но realised_only флаг отсутствует в pack — помечено.
2. G2 daily OHLCV 2025-08..2026-06: DONE (A2, reports/g2_daily_ohlcv.csv) —
   swing-трек и walk-forward БОЛЬШЕ НЕ заблокированы данными.
3. 10m/30m/1h/4h бары: DONE (A3) — intraday_trend эксперименты БОЛЬШЕ НЕ заблокированы.
4. functions_mask заполнен в intents, но meta-labeling/quorum-weights требуют
   SHADOW + disjoint history (не в execution path).
5. signal_exit cohort отрицательный (167 сделок, net -1,728.6) — проверен через
   E1 (signal_exit on/off); My3 REJECT (см. Research status). Больше не загадка.
6. Концентрация: RUAL 27.87% + MVID 21.27% = 49.1% net => concentration risk.
7. Нет L2 / earnings calendar / dividends / borrow (G6–G8) =>
   microstructure, PEAD, точность шорт-издержек заблокированы.

## Task queue (Anti-idle conveyor)
| Задача | Тип | Статус |
|---|---|---|
| A1: CANCELLED_BEFORE_FILL (40) | AUDIT | DONE — 28 exit-сигналы, 11 съедены след. входом, 1 edge |
| A2: G2 daily OHLCV 2025-08..2026-06 | DATA_PREP | DONE |
| A3: 10m/30m/1h/4h bars | DATA_PREP | DONE |
| A4: MTM equity в F | AUDIT | DONE (availability=True; realised_only флаг нет) |
| A5: invariants март-апр | AUDIT | DONE (net −232, режимная зависимость) |
| E1: signal_exit on/off (2026-03..04) | RUNNING_EXPERIMENT | **CLOSED/REJECT** — baseline ON несущий, оставить |
| E2: cooldown 15→8 (2026-05..06) | RUNNING_EXPERIMENT | **CLOSED/REJECT** — net +11.7% но DD +8% => REJECT; cooldown=15 |
| P0: Regime diagnostic (Июль vs Март–Апр) | RESEARCH | **DONE** — edge волатильностно-зависимый (не направленный); артефакт REGIME_DIAGNOSTIC.md |
| E3: swing (WORLD-H-016) | PREPARED | данные готовы (G2+A3), конфиг не писался |
| E4: TSM 30-240m (WORLD-H-009) | PREPARED | данные готовы, конфиг не писался |
| E5: trailing (atr_trailing) | CLOSED/REJECT | policy trailing 1R/1R failed on May–June (not proven class-wide); S1 net +989 vs S0 +9265 (DD 511 vs 245) |
| REGIME-GATED EXIT (trailing) | CLOSED / REJECTED WITHOUT RUNNING | owner confirmed trailing branch closed; diagnostic proved trailing harms all vol buckets (high-vol +9868 vs +2777); no run |
| EXP-002a regime-gated trailing | **CLOSED / REJECTED** | hi_vol baseline +9868 → trailing +2777 (diagnostic); E5 REJECT. Ветка закрыта (=P3b). |
| EXP-002b high-vol entry gate | **DRAFT** | предв. 2025-08..12 OOS: S1 net +9783<S0 +14641 (REJECT как замена) но DD −68%, PF↑ all FIGI; НЕ фиксировать REJECT — нужна point-in-time + disjoint + MTM. |
| EXP-003 prune RSI/VWAP | **RESEARCH CANDIDATE / BLOCKED BY ATTRIBUTION RECOMPUTE** | вывод о слабости преждевременен до pair_present + k=200. |
| E6: RR 2→3 | BLOCKED | deprioritized — tuning без regime-обоснования (риск подгонки) |
| E7: ATR mult 2→1.5 | BLOCKED | deprioritized — то же |

## Work in progress
- EXP-002 (volatility entry gate) handed to executor (после подтверждения 2025 OOS):
  `backend/orchestrator/executor_task.md`, gate = entry_volatility_gate=rolling_atr_high_only.
  Окно 2025-08-01..2025-12-31. После прогона — executor_response.md → My3 REVIEW.
- GLOBAL_DISCOVERY завершён: docs/research/WORLD_METHODS_BACKLOG.md, RESEARCH_MAP.md,
  DATA_GAPS.md, STRATEGY_FAMILIES.md.

## Approved next action
- EXP-002 (volatility entry gate) PRE-REGISTERED; окно 2025-08-01..2025-12-31 (ждёт подтверждения
  владельца, что 2025 — OOS). После подтверждения — executor (DeepSeek, .3) реализует
  entry_volatility_gate и прогоняет S0/S1; My3 верифицирует по критериям успеха. Одновременно ≤1 RUNNING_EXPERIMENT.
- Не менять стратегические параметры без явного утверждения.

## Explicitly blocked
- ML в execution path
- live trading
- pyramiding
- weighted voting (quorum≠2)
- grid search threshold/EMA/RR/cooldown
- смешивание intraday и swing equity в одну кривую
- (signal_exit experiment — E1 уже прогнан и My3 REJECT; блок снят)

## Research status (My3)
- latest: E2 cooldown 15→8 — **My3 REJECT**. S0(cd15) net 9265.2 / 973 / DD 245.5;
  S1(cd8) net 10349.8 / 1069 / DD 265.2. net +11.7%, trades +9.9%, но DD +8% (~20₽)
  => pre-reg "DD не хуже" не выполнен. Cooldown=15 оставить. (E1 signal_exit — тоже REJECT,
  baseline ON несущий.)
- УСТАНОВЛЕНО: baseline режимно-зависим (Июль +11k, Март–Апр ~0/−232). signal_exit нужен.
- P0 Regime diagnostic — **DONE** (REGIME_DIAGNOSTIC.md). Вывод: edge зависит от
  ВОЛАТИЛЬНОСТИ (внутридневной диапазон IMOEX), а не от направления. Июль 3.1% vs
  Март–Апрель 1.1% диапазон; high-vol дни +802 vs low-vol +341 ₽. Кандидат-признак №1
  — волатильность; будущий эксперимент — REGIME-GATED EXIT (по вол-ти) поверх E5.
- E5 trailing — BLOCKED (движок: atr_trailing, не trail_* на atr_stop); P3, после regime.
- BLOCKED: E6 RR2→3, E7 ATR2→1.5 — tuning без regime-обоснования (риск подгонки).
- СТАТУС 2026-08-27 (коррекция): E5 **REJECT** (policy trailing 1R/1R не прошла на May–June;
  проверено по артефакту: net −89%, win −24pp, DD ×2). Корректный вывод — НЕ «trailing класс не
  работает». REGIME-GATED EXIT возвращён в **DRAFT/BLOCKED_PENDING_REGIME_DIAGNOSTIC**: запуск
  преждевременен (тот же May–June + утечка медианы). Сначала read-only диагностика (point-in-time
  buckets baseline vs E5), затем — при сигнале — ОДИН pre-reg на НОВЫЙ disjoint период. Baseline не менялся.
  ВАЖНО: DeepSeek ошибочно запустил P3b (regime_gated_exit_202605_202606_DRAFT.json, S1 +4497/DD245/
  win55%) — **VOID** (преждевременно + leaky гейт: IMOEX daily range > медиана всего окна). Игнорировать;
  требуется read-only диагностика с point-in-time гейтом.
- ИТОГ 2026-08-27 (REGIME DIAGNOSTIC): read-only telemetry (point-in-time) показала, что trailing 1R/1R
  вреден во ВСЕХ бакетах; весь edge в hi_vol=True (+9868 из +9265), где trailing режет net до +2777 (−72%).
  Гейт применил бы trailing к единственному прибыльному режиму → P3b **CLOSED WITHOUT RUNNING**.
  Гипотеза «улучшить exits через trailing» исчерпана (E5 REJECT + диагностика; signal_exit нужен по E1).
  Дальше — входы / другие regime-семьи (E3 swing, E4 TSM, P2 IMOEX-shadow) или признать baseline оптимальным по exits.
- blocked directions: ML execution, live, pyramid, weighted vote, grid search;
  L2/earnings/dividends/borrow (G6–G8) данных нет.
- 2026-08-27 (EXP-002): trailing-ветка окончательно ЗАКРЫТА (P3b CLOSED/REJECTED WITHOUT RUNNING).
  Следующий тест — EXP-002 (volatility entry gate), PRE-REGISTERED. Окно: Jan–Feb 2026 in-sample
  (исключён), Sep–Oct 2026 будущее (нет данных) → владелец подтвердил весь 2025 свободен → заморожен
  2025-08-01..2025-12-31. ПОДТВЕРЖДЕНО владельцем: 2025 — OOS, валидация валидна; EXP-002 готов к
  запуску executor.
  См. docs/research/EXP-002_VOL_GATE.md.
- 2026-08-27 (executor + My3 verify): EXP-002 RUNNING_COMPLETED на 2025-08..2025-12 (окно корректно
  ограничено, без данных вне окна — deviation снят). Результат: REJECT как замена (net 9783<14641,
  coverage 38.5%<40%, conc 46.4%>45%), НО DD −68%, PF↑ all 5 FIGI, win 67.9% vs 62.5% → мощное
  подтверждение, что edge сконцентрирован в hi_vol. VOTE_ATTRIBUTION PARTIAL: per_function(7 функций,
  edge у trend/breakout; rsi/vwap слабы) + by_slices + exact_combos + shadow готовы; НЕТ pair_present,
  vote_attribution_dataset_v1/ и report md; shadow посчитан с k=20 вместо зафиксированного k=200.
  Владелец авторизовал AUDIT_ONLY/DATA_RECOMPUTE (k=200 + pair_present + dataset_v1/ + report md);
  задача: executor_task_vote_attribution_recompute.md. См. EVIDENCE_LEDGER.md.
- 2026-08-27 (owner + My3, reframe очереди): EXP-002a (regime-gated trailing) = CLOSED/REJECTED;
  INSIGHT-002 (volatility asymmetry) = CONFIRMED_ON_DISCOVERY_WINDOW_ONLY (не deployed gate); EXP-002b
  (high-vol entry gate) = DRAFT (НЕ REJECT; нужна point-in-time + disjoint + MTM); EXP-003 (prune RSI/VWAP)
  = RESEARCH CANDIDATE / BLOCKED BY ATTRIBUTION RECOMPUTE (нельзя вывод до pair_present + k=200). Очередь:
  VOTE_ATTRIBUTION recompute → My3 audit → EXP-002b validation → EXP-003a/3b. Gate и prune не одновременно.
  Старый vote_shadow_scores.csv (k=20) помечен INVALID FOR RANKING. См. EVIDENCE_LEDGER.md + EXPERIMENT_QUEUE.md.
- 2026-08-27 (owner + My3, параллельные ветки): запуск 3 независимых линий — (B1) time-of-day attribution
  (shadow, monthly canonical packs), (B2) IMOEX context + lead-lag/stop-concept (shadow, при наличии IMOEX),
  (C1) high_vol conviction sizing (pre-reg, prepare не run, на unused 2025-месяце). Плюс будущие: entry-frequency
  expansion, IMOEX как опережающий предсказатель/стоп, 30m/1h intraday trend + swing 2–5д — НОВЫЕ families
  (design+data prep only, не P&L test). Задачи: executor_task_time_of_day.md / _imoex_context.md / _vol_sizing.md.
  Основной меняющий-стратегию эксперимент в очереди — только один (EXP-002b); остальное shadow/new-family.
- 2026-08-27 (My3 verify recompute): VOTE_ATTRIBUTION recompute ВАЛИДЕН — k=200 подтверждён (shrinkage_k=200,
  weight=(n/(n+200))*median_net_bps, walk-forward по exit_time<day_start), pair_present+pair_exact добавлены,
  dataset_v1/ + report md готовы, trades.csv не тронут (AUDIT_ONLY). Вывод: сильнейшие пары (donchian+macd,
  donchian+pullback, macd+pullback) НЕ содержат rsi/vwap ⇒ доминирующий edge в trend/breakout; пары с rsi слабы,
  vwap n≤2. ⇒ EXP-003a (remove RSI) и EXP-003b (remove VWAP) РАЗБЛОКИРОВАНЫ (edge не пострадает при quorum=2),
  запуск после EXP-002b на disjoint 2025. Shadow k=200 ВАЛИДЕН для ранжирования; старый k20 INVALID.
- 2026-08-27 (My3 audit B1/B2): B1 DONE — нет повторяемого time-of-day pattern (Jul uniform +, Mar–Apr mixed)
  → time-filter НЕ рекомендуется. B2 PARTIAL: context+lead-lag готовы, STOP-концепт НЕ реализован (gap).
  Находки: alignment НЕ улучшает edge (counter_market PF 11.55 > aligned 7.89); index_vol_hi net/trade 41 vs 18.7
  (корроборирует volatility-тезис); lead-lag 2m 74.5%/5m 80.9%/10m 79.2% → IMOEX ЛИДИРУЕТ акции (интуиция
  владельца ПОДТВЕРЖДЕНА). STOP-концепт → follow-up executor_task_imoex_stop_concept.md. C1 — prepare-only, не run.
- 2026-08-27 (owner synthesis + My3): vote-attribution = research direction only (НЕ веса execution, НЕ gate).
  EXP-002b разблокирован (B1/B2 не противоречат volatility-тезису). Порядок: EXP-002b → EXP-003a(RSI) →
  EXP-003b(VWAP при основании). HARD CONSTRAINTS: НЕ quorum=3 / НЕ weighted votes / НЕ VWAP removal / НЕ sizing
  до OOS. VWAP insufficient sample ⇒ оставить. EXP-002b task: executor_task_exp002b.md (PRIMARY 2025-08..12,
  2-й период подтверждения).   Status: next_action_owner=executor (B2b shadow + EXP-002b primary).
- 2026-08-27 (owner: заменить B1 на entry-analysis): B1 SUPERSEDED (inconclusive, результат сохранён). Добавлены
  B3 entry-confluence / quorum opportunity-cost и B4 entry-quality micro-analysis (shadow/read-only, замена B1).
  Кормят будущую теорию расширения числа входов. Задачи: executor_task_entry_confluence.md / executor_task_entry_quality.md.
  STATUS next_action расширен: shadow-parallel B2b + B3 + B4 → EXP-002b (primary).
- 2026-08-27 (My3 audit B3/B4/B2b): B3 DONE — confluence↑ монотонно (q2→q4+ net/trade 20→51, PF 8→39); Part B —
 1153 из 1438 отброшенных 2-сигнальных setups заблокированы SESSION-гейтом (главный leverage entry-frequency).
 B4 DONE — pullback depth сильный предиктор (shallow weak → deep best); trend-alignment НЕ полезно. B2b PARTIAL —
 частоты IMOEX-разворотов готовы, fill-sim нет → follow-up executor_task_imoex_stop_fill.md. Future candidates:
 session-expansion / require-deeper-pullback / ≥3-signals / IMOEX-stop-fill (нужен 2-период + pre-reg).
- 2026-08-27 (owner + My3): следующая read-only задача ПОСЛЕ EXP-002 — VOTE_ATTRIBUTION (per-vote/per-pair
  attribution + walk-forward shadow weighted score, shadow_only, без изменения движка). Спецификация:
  docs/research/VOTE_ATTRIBUTION.md; задача: executor_task_vote_attribution.md. Weighted voting сейчас
  BLOCKED → любое будущее применение весов = отдельный pre-reg эксперимент на disjoint 2025.
- 2026-08-27 (owner + My3): ещё одна read-only диагностика ПОСЛЕ EXP-002 — EXIT_ORACLE_AUDIT (post-entry
  «рентген» exit policy на canonical Июль 5b44f3b383df; capture_ratio + post_exit adverse/favorable по
  exit_reason; oracle diagnostic_only, НЕ правило выхода). Спецификация: docs/research/EXIT_ORACLE_AUDIT.md;
  задача: executor_task_exit_oracle.md. Отделяется от oracle_coverage (вход). Результат → гипотезы для
   будущих каузальных тестов (partial exit / stop geometry / signal-exit фильтрация), НЕ immediate change.
  - 2026-08-27 (owner final synthesis + My3): B-серия финализирована — B1 TIME_OF_DAY CLOSED/REJECTED
   (time-filter REJECTED); B2 IMOEX alignment CLOSED/REJECTED (counter_market PF 11.55 > aligned 7.89);
   B3 ENTRY_CONFLUENCE CLOSED → CANDIDATE FOR FUTURE EXPERIMENT; B4 ENTRY_QUALITY CLOSED → CANDIDATE;
   B2b IMOEX STOP SHADOW/RUNNING (fill-sim follow-up). NEW B5 IMOEX TREND REGIME & STRENGTH ATTRIBUTION
   (shadow, заменяет грубый alignment на regime+strength) — задача executor_task_imoex_trend_attribution.md,
   параллельно с очередью. EXP-002b = NEXT PRIMARY (единственный RUNNING_EXPERIMENT). C1 PREPARED ONLY.
   EXP-003a after EXP-002b. Future pre-reg candidates: session-expansion, deeper-pullback, ≥3-signals, IMOEX-stop-fill.
   NEW B5b ENSEMBLE VOTE BEHAVIOR AT ENTRY/EXIT (shadow, companion to B5): per-function vote direction+strength на
   входе/выходе и горизонте ±1/2/3/5 бар (5m, точка «5+1»); задача executor_task_entry_exit_vote_behavior.md,
   параллельно с B5. **QUEUE (2026-08-27):** B2b/B5/B5b QUEUED (shadow parallel); EXP-002b QUEUED как primary RUNNING_EXPERIMENT.

## Useful docs
- Agent instructions: docs/agents/minimax/minimax/MY3_RESEARCH_LEAD.md,
  MY3_GLOBAL_DISCOVERY.md, RESEARCH_WORKFLOW.md
- Live handoff (watchdog): STATUS.md (корень) — читать в начале каждого шага.
- Единый список файлов для чтения: раздел «ФАЙЛЫ ДЛЯ ПОСТОЯННОГО ЧТЕНИЯ» в RESEARCH_WORKFLOW.md
- Latest manifest: backend/reports/5b44f3b383df/research_pack_manifest.json
- Latest research pack: backend/reports/5b44f3b383df/research_pack.json
- Last DeepSeek report (E1): backend/reports/e1_signal_exit_202603_202604.json
- My3 E1 review: backend/reports/e1_signal_exit_202603_202604_MY3_review.md
- GLOBAL_DISCOVERY: docs/research/WORLD_METHODS_BACKLOG.md, RESEARCH_MAP.md,
  DATA_GAPS.md, STRATEGY_FAMILIES.md

## Правила обновления PROJECT_STATE.md
- Файл ЕДИНСТВЕННЫЙ, лежит в корне рядом с chat.md.
- DeepSeek обновляет только тех. раздел (tests, commits, artifacts, bugs, status).
- My3 готовит research status.
- Итог подтверждает человек.
- Меняется ТОЛЬКО после завершённого run ИЛИ утверждённого plan.
