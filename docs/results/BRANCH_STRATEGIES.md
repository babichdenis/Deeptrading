# BRANCH_STRATEGIES — состояние ветки Builder-3 (STRATEGIES)

Обновляет: Builder-3. Канон: B27 BRANCH DISPATCH 2026-10-10 (chat.md) + DISPATCH-20261011-01
(`docs/BUILDER_ASSIGNMENTS.md`). Результат ветки: два воспроизводимых кандидата C1/C2 и
development-ledger, принятый B2/A11. Это research/development, НЕ OOS и НЕ торговое решение.

## Итог на сейчас

**Статус ветки: market-RUN выполнен и принят (ACCEPTED_SCOPED). PLAN33 и COVERAGE приняты;
активна gate-wave (не RUN_AUTHORIZED) — см. последние разделы.**

- CLI прогона кандидатов реализован; наивный генератор A18 даёт **performance blocker** на
  реальном масштабе; подготовлена оптимизированная версия (fast, O(M)) с доказанным parity;
  собран один пакет: protocol (10 процессов), hashes, argv, output-схема, timeout-политика,
  bounded synthetic evidence.
- Market/development run НЕ выполнялся: нужен `RUN_AUTHORIZED` архитектора с конкретными
  hashes/лимитами. См. `backend/reports/architect_20261010/b3/branch_strategies/20261010T221746Z-b27/`.

## Что сделано по B27

| Артефакт | sha256 | Роль |
|---|---|---|
| `backend/scripts/day_candidate_replay.py` | `6f43af48d5ea93c8db9b96ec5b5d8576494b63d53f951acea8873a050c7dc0d4` | CLI: 1 процесс = ticker × cost, внутри C1 и C2; ledger A11-формата |
| `backend/scripts/day_candidate_signals_fast.py` | `51bab2a78ce8413d50b43d0135c6a14e34ad4e7fe474283e53d077db3de29e7b` | fast-генератор O(M), решение = принятый A18 `_evaluate_day`; новый hash, требует приёмки |
| `backend/scripts/day_candidate_execution.py` | `29f4d790427713a4459fea712585ca48de36dd8d5026375f38ea55f1511d6897` | принятый A24-adapter (не менялся) |
| `backend/scripts/day_candidate_signals.py` | `28bbea4f522d87e05d4a0482cf0ba80d751d2cfe3b01f4df54fef604b1aac67c` | принятый A18 r2 (не менялся) |
| `backend/scripts/day_strategies_core.py` | `46005b5e20365827f07dd68baa22e6b8d32b2f7c8f3d1c129f371bde483473e1` | A7 read-only (не менялся) |
| `backend/scripts/day_research_report.py` | `2da099696940c1de0b53351374e465479fa5599bdf955cfd6f92ff6072680322` | принятый A11 r3 (не менялся) |

Пакет B27 `backend/reports/architect_20261010/b3/branch_strategies/20261010T221746Z-b27/`:

- `protocol.json` (`8ee7a3e2f98a4409a9d254392666b4ec1476d8855c73e8fdb196ddd7a225f085`): 10 процессов
  = 5 тикеров A10 (GAZP/LKOH/SBER/SFIN/VKCO) × 2 cost-сценария (c5_s2 5/2, c10_s5 10/5), каждый
  процесс прогоняет оба кандидата; границы времени/строк; timeout-политика; output-схема; argv.
- `perf_results.json`: наивный A18 — blocker (8 синт. сессий ~18с/кандидат; ≥13 сессий >30с;
  экстраполяция ~44k мин/тикер ⇒ ~1957с/кандидат).
- `perf_results_fast.json`: fast — 88 синт. сессий (46 200 мин) за ~1.25с (C1) / ~2.04с (C2);
  линейно, укладывается в лимит процесса.
- `parity_results_fast.json`: fast ≡ A18 (полные события C1/C2, причинность/суффикс, reset,
  byte-идентичный CLI-ledger на синтетике).
- `parity_results.json`: независимо — ledger CLI (`--generator a18`) совпал с прямым модульным
  путём A18+A24 (13/13 сделок, 0 расхождений полей; skip_parity, reset — true).
- `.../20261010T225642Z-b27-edge/` (`MANIFEST.sha256` `43b239d2…`): **edge-parity** — gaps
  (неполные бары), выходные бары, внесессионные минуты, обрыв данных (INCOMPLETE), reset посреди
  потока; `parity_edge_results.json` parity=true; CLI a18↔fast байт-идентичен. Хэши A18 (`28bbea4f…`)
  и fast (`51bab2a7…`) **неизменны** — это дополнение к замороженному пакету, не новая версия кода.
- `interface_binding_b3.json`: интерфейс B3→B2 (identity, required_manifest, A11 argv, phase).
  **Подтверждён B2** (binding r2, `6b9c45a6…`): расхождений с `report_interface.json` нет;
  провенанс-колонки в `trades.csv` не требуются (ровно 32 колонки A12; при необходимости —
  sidecar `run_index.json`); weekday-weekend `insufficient_context` принят; `configs=['C1','C2']`
  в одном manifest рана (ticker×scenario) подтверждён; `config_hash` совпадает с пересчётом A11.
- Manual synthetic smoke: CLI → `day_research_report.py` (A11) на синтетике, `smoke_results.json`
  (A11 exit 0; scenarios=2, metrics=234, trades=18, rejects=0) — ledger принимается A11 без правок.

## Гейты (по B27)

1. Упаковка hashes/argv/лимитов/интерфейса — **сделано** (этот пакет).
2. Приёмка fast-генератора или явный отказ — **ожидает архитектора** (пакет `READY_FOR_REVIEW`).
3. `RUN_AUTHORIZED` с конкретными hashes/лимитами — **ожидает архитектора**.
4. После авторизации: один bounded offline run → `RAW_DONE` → отчёт B2 → `STRATEGIES_DONE`.

## Ограничения (соблюдены)

A7/A18/A24/engine/runtime не менялись; сеть, боевая БД, ордера, деплой, suites, стратегии — не
трогались. Market/A10-scan и PnL без `RUN_AUTHORIZED` не выполнялись. Старые артефакты (A24/A25,
A16 r3) неизменяемы.

## Следующий шаг владельцу/архитектору

Принять или отклонить `day_candidate_signals_fast.py` (parity-доказательство приложено), затем
выдать `RUN_AUTHORIZED` с hashes из `protocol.json`. При отказе от fast — потребуется отдельная
оптимизация (наивный A18 не влезает в лимит процесса).

## RUN-C1C2-20261011-01 — RAW_DONE (выполнено)

Архитектор выдал `RUN_AUTHORIZED` (`run_authorization.json` `bf51aa40…`), fast-генератор принят
`ACCEPTED_SCOPED`; CLI принят с hash `c3626a6c…` (заменяет `6f43af48…`, точечные поправки:
warmup/outside_eval не увеличивают skipped eval-периода; `signals.json` sidecar; REJECT →
`rejected_signal.json` и остановка; `net_verdict` — описательный).

Один авторизованный offline-пакет выполнен целиком, без ретраев и смены параметров:
`backend/reports/architect_20261011/b3/RUN-C1C2-20261011-01/` (`MANIFEST.sha256` `9bfc6ff9…`,
87 файлов; `run_index.json` `23c314cb…`; `integrity_check.json` `7c8eaeb9…` `all_ok=true`).

- 10 процессов ticker×cost (внутри C1+C2), sequential, argv из protocol; subprocess без shell,
  process group, `wait(timeout=600)`, SIGTERM→SIGKILL→wait; заменены только `--out` (абсолютные
  каталоги) и `--data-note`. Предстартовая сверка code/input hashes: 6/6 и 5/5 match.
- Итог: 10/10 rc=0, 0 timeout, 0 overflow, hash-drift нет. Trades 1322 (filled 1228, incomplete 94),
  skips 1078, DQ 18142 (≤2000/run). DQ — `session_bars_weekend` (выходные бары входа A10, A7 core),
  A10 immutable.
- Это development на просмотренной истории, не OOS; прибыльность UNKNOWN. SBER — known-bad
  диагностика, SFIN incomplete — не P&L.

Далее — B2: `ECONOMICS_DONE` (один вызов принятого A11 `2da09969…`, timeout 120с, сверка с A12;
стратегия повторно не запускается).

## ECONOMICS_DONE B2 — исход (gate 4 закрыт)

B2 собрал отчёт (`.../b2/branch_economics/20261011T051239Z-economics-done/`): 10/10 hashes приняты,
один вызов A11 (`scenarios=2, metrics=1674, trades=1322, rejects=0` — совпало с числом B3),
сверка с A12 с явными знаменателями. Деньги ex-SBER (1 акция): C1 net −1056.52 (5/2) / −1671.48 (10/5);
C2 gross +357.84, net −175.64 (5/2) / −785.34 (10/5) — расходы превышают gross в обоих бюджетах.
**Вердикт по обоим: `reject_for_further_research`.** Кандидатов нет; `retain_for_independent_observation`
не выдан. Решение о следующей волне — за архитектором/владельцем. development, не OOS; прибыльность
UNKNOWN.

## PLAN33 + COVERAGE — ACCEPTED_SCOPED (архитектор, 2026-10-11)

- **PLAN33 ACCEPTED_SCOPED**: `backend/reports/architect_20261011/b3/data_request_plan_33/20261011T062608Z/`
  (8/8 hashes; 47 identities; 33 targets; окна −5/+6). Канонические уточнения записью архитектора
  (файлы не менялись): исходы источника — `SOURCE_PRESENT/SOURCE_ABSENT/CONFLICT/UNAVAILABLE`
  (отсутствие свечи ≠ отсутствие торгов); 21120 с — IO-бюджет, отдельно 66×5 с launcher grace.
- **COVERAGE ACCEPTED_SCOPED**: `backend/reports/architect_20261011/b3/coverage_canon_input_contract/20261011T070600Z/`
  (356 day×ticker на метке 15:44Z = бар 18:44 MSK сверены с A10, 0 mismatch; логика D−1/D−2 по 89 дням
  совпала; 71 eval-день `factor_available`). Принимается coverage/схема, не gate/PnL.
- **Поправка знаменателей (действует записью, старые файлы не менялись):** 511 — ex-SBER события;
  614 FILLED / 47 INCOMPLETE относятся к 5 тикерам; **ex-SBER (корзина) 466 FILLED / 45 INCOMPLETE**
  на сценарий. Не смешивать знаменатели.
- **INDEPENDENT HANDOFF CHECK — ВЫПОЛНЕН (READY_FOR_REVIEW):** B2 r2
  `backend/reports/architect_20261011/b2/20261011T080157Z-majority-gate-r2/` сверена только чтением с
  принятым `ledger_event_contract.json`. Пакет B3:
  `backend/reports/architect_20261011/b3/handoff_check_gate_r2/20261011T083238Z/` (`handoff_check.py` —
  воспроизводимый read-only чекер, `handoff_check.json`, `handoff_report.md`, манифест). Хэши B2 r2
  6/6 совпали; canon `51075303…` verified; evidence 25/25. Все 5 обязательных пунктов **CONFIRMED**
  (scenario в grouping, event-identity без scenario; оба бюджета обязательны→`INVALID`; unknown/missing
  exposure→`INCONCLUSIVE`; raw-поля сохранены; второго сигнала нет). Вердикт —
  `CONFIRMED_WITH_MINOR_DEVIATIONS` (только номенклатура/аддитив: `coverage_unknown_ratio` не выводится
  колонкой, имена метрик + `group_kind`, единый `unknown_factor`+`invalid_side`, `marked_rows.csv`,
  ссылка на мой контракт по пути). Чужой код не менялся; реальные factor/PnL не считались.
- **Приёмка:** **B3 handoff — ACCEPTED_SCOPED** (архитектор, 3/3 manifest hashes; замечания номенклатуры
  не блокируют). B2 r2 — ACCEPTED_SCOPED для frozen A10/accepted ledger.
- **Следующий блок — REVIEW_OUTPUT (после `GATE_DONE` B2):** B2 авторизован на `RUN-GATE-20261011-01`
  (ровно 1 factor + 8 apply + 1 report на реальных сохранённых CSV). Мой блок — только сверка
  сохранённых выходов с raw ledger/coverage: идентичность и неизменность raw-полей; counts KEEP/DROP/UNKNOWN;
  86 weekdays / 71 available factor days; 511 unique/scenario; обе cost-identity и одно и то же keep-решение;
  пересуммирование закрытых net/entry-ratios; unknown и итоговый verdict. Фактор/стратегии заново не
  запускать, чужие файлы не менять, один concise evidence report. Нет GATE_DONE → ждать без повторных ACK.
- **REVIEW_OUTPUT — ВЫПОЛНЕН (PASS 9/9):** `backend/reports/architect_20261011/b3/review_gate_output/20261011T094611Z/`
  (поверх B2 `RUN-GATE-20261011-01`, `status.json=GATE_DONE`). 12 входов hash==авторизация `e2e0b555…`;
  factor 86 будних / 71 available (независимый A10-пересчёт == B3 coverage); marked 1022 строки,
  511 identity/сценарий, raw-поля идентичны ledger; KEEP/DROP/UNKNOWN 203/246/62 на сценарий (одинаково
  в обоих бюджетах; 511/511 общих identity — одно keep-решение); пересуммирование kept/baseline ==
  decision.json; вердикт пересчитан == `INCONCLUSIVE` (kept INCOMPLETE>0: C1 6/C2 5; n_closed=96<100;
  kept-net <0). INCONCLUSIVE — результат, не подбор; RETAIN не выдаётся. Сеть/БД не использовались.
- **SPARSE MINUTE EXECUTION CONTRACT — DRAFT СДАН (read-only):**
  `backend/reports/architect_20261011/b3/sparse_minute_execution_contract/20261011T110240Z/`
  (contract.md sha `aa40471917ae0c8e62181ac5794bf5cf927c9b06f201c04631b1e753ba0a6bf0`). Один документ,
  без правок A7/A18/A24/данных, без запусков/сети/БД. Вывод: A7/A24 не различают missing_data и
  SOURCE_ABSENT — любая отсутствующая минута пути → INCOMPLETE (`_walk` day_strategies_core.py:513-560,
  :532-534; docstring :516-525 «последующие свечи путь не делают достоверным»; A24 :211, :220-222).
  3 SOURCE_ABSENT (VKCO 12:09Z/09:23Z/14:45Z) = first_missing трёх из 47 INCOMPLETE-путей (все
  interior_gap_later_candles_present, path_gap_after_entry; 11 new_gap); 11 kept-unknown C1 6/C2 5
  (SFIN 6/VKCO 5). Контракт: data-loss→UNKNOWN; source-absent без proof of no-trade→UNKNOWN
  (согласие двух OHLC-источников необходимо, не достаточно); proven no-trade — доказывает ТОЛЬКО
  биржевая лента сделок (per-trade prints) → event-time путь: минута пропускается, оценка по
  first actual next trade, SL/TP точно (1 print) или диапазоном (агрегированный бар), slippage —
  полосой. Boundary: результат познаваем ⇔ каждая минута от входа до выхода имеет бар либо
  доказанно бесторговая с identified first-next-trade ценой; иначе UNKNOWN навсегда. Пока лента не
  сохраняется — все 47 дыр (3 SOURCE_ABSENT + 11 kept-unknown) остаются UNKNOWN. Это проект
  будущего исполнения, не пересчёт истории/не объявление неизвестных сделок прибыльными.
