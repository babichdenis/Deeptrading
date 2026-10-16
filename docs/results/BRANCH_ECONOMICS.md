# BRANCH_ECONOMICS — Builder-2: есть ли экономический смысл (S1/S2/C1/C2)

Владелец ветки: Builder-2. Основание: `chat.md` (BRANCH DISPATCH) и
`docs/TRADING_RESEARCH_PLAN.md`. Состояние на 2026-10-10.

**Конечный результат ветки:** единая таблица решений по S1/S2/C1/C2, сопоставимая по
исполнению, расходам и качеству данных. Не «ещё отчёты», а: оставить для независимой
проверки / отвергнуть / нельзя оценить из-за конкретных данных.

## Статус

- **A14 r3 — ACCEPTED_SCOPED** (описательная экономика S1/S2; `research_candidate=0`).
- **A21 r2 — ACCEPTED_SCOPED** (локальный bounded news-journal; сетевой сервис/масштабирование
  НЕ принято). **Новости после A21 — DEFERRED** до baseline и пригодного архива.
- **A22 — ACCEPTED_SCOPED** (запас на издержки; 80 строк, 11 sign-change комбо).
- **B26 (пункты 1–3) — COMPAT_READY** (ниже). Пункт 4 (ECONOMICS_DONE) ждёт принятый ledger B3.

## B26 — совместимость и оценка (COMPAT_READY)

1. **Прямой A11 достаточен — wrapper НЕ создан.** Принятый `day_research_report.py`
   (A11 r3) принимает ledger C1/C2 напрямую: `scenario=(commission_bps,slippage_bps)`,
   `config` — произвольные имена (C1-A/C1-B/C2-A/C2-B), идентичность
   `(ticker,config,day,entry_ts)`. **Переименование C1/C2 → S1/S2 не требуется.**
   Контракт зафиксирован в `report_interface.json` (в артефакте B26).
2. **Synthetic contract-fixture** (A24 ещё в работе → явно synthetic): маленький
   A7-форматный ledger C1/C2 (VKCO; оба costs 5/2 и 10/5; weekday+weekend;
   FILLED/INCOMPLETE/SKIP; плюс копия с дублями). Прогон принятого A11:
   - config C1-A/C1-B/C2-A/C2-B читаются без переименования; два сценария;
   - closed/incomplete/skip видны; weekend-сделка отдельной строкой `day_type=weekend`;
   - `net` сходится с исходным fixture (reconcile 5/2: 3.5786004);
   - дедуп (`duplicate_trade_in_run`) и конфликт (`conflicting_duplicate_in_run`) — reject.
3. **`decision_template.json`** зафиксирован ДО рыночного результата: counts,
   gross/net/costs, месяцы/половины, концентрация, unknown-coverage, SBER исключён из
   экономического выбора; вердикты `reject_for_further_research` /
   `retain_for_independent_observation` / `inconclusive` — **без автоматического PASS по
   произвольному порогу**; постфактум выбранная сторона объявляется новой post-hoc гипотезой.

Артефакт B26: `backend/reports/architect_20261010/b2/branch_economics/<UTC>/`
(`manual_offline_examples.py`, `report_interface.json`, `decision_template.json`,
`results.json`, `cases/`, `manifest.json`, `report_snapshot.md`).

## interface_binding (DISPATCH-20261011-01) — READY

`interface_binding.json` в `backend/reports/architect_20261010/b2/branch_economics/<UTC>-interface-binding/`
связывает `report_interface.json` (B26) с A25 `ledger_schema.json`/`protocol.json`:
- реальные id — **только C1/C2** (2 кандидата), 5 тикеров A10, 2 сценария издержек;
- **максимум 10 процессов** ticker×scenario, каждый с **обоими** кандидатами (A25 `runs.count=20`
  устарел по DISPATCH-20261011-01; synthetic C1-A/C1-B/C2-A/C2-B из B26 — НЕ конфигурации исследования);
- A11 `day_research_report.py` (`2da09969…`) читает ledger напрямую; **wrapper не нужен**;
- точный report argv: `--run <10 каталогов> --out <report_dir> --phase development_unvalidated`;
- проверки, требующие принятого ledger, ПОМЕЧЕНЫ как NOT_YET_VERIFIED (не заявляются выполненными).

## Следующий шаг ветки (ECONOMICS_DONE, пункт 4)

Стартует автоматически при выполнении **всех** условий:
`A24 ACCEPTED` **и** `RUN_AUTHORIZED` архитектора **и** `RAW_DONE`(→`REPORT_READY`) от B3.
Ожидаемый артефакт B3: `backend/reports/architect_20261010/b3/<branch_strategies>/<UTC>/`
с `run_index.json` + 10 A7-форматными runs (config C1/C2) + `report/`.

Тогда: собрать отчёт принятым A11 напрямую, сопоставить с A12, дать ≤2 страницы «что
оставляем / отвергаем / не можем оценить»; исторические издержки условные, тикеры не
складываются в портфель; ошибки manifest/ledger → запрос B3 через chat (чужие файлы не править).

Статус ветки: **WAITING_LEDGER_FROM_B3** → закрыт 2026-10-11 (`RUN_AUTHORIZED` → `RAW_DONE`
→ **ECONOMICS_DONE**, см. ниже). Новую инфраструктуру ради занятости не
создаём, A21/A22 не повторяем; новости и следующая волна гипотез отложены.

## Если волна завершится без кандидата (пункт 5) — ВЫПОЛНЕНО 2026-10-11

Условие наступило (C1 → reject, C2 → reject, см. `ECONOMICS_DONE`). Подготовлены максимум
**две** спецификации, **только документ** (без сети/БД/сканов/подбора/прогонов):

Артефакт: `backend/reports/architect_20261010/b2/branch_economics/20261011T052131Z-next-wave-specs/`
- `spec1_price_regime_gate.md` `12345a3a…` — **ценовая**: дневной режим-гейт по корзине из тех же
  5 бумаг (фактор `B(D-1)` = доля бумаг с положительным дневным ретёрном предыдущей сессии,
  ровно 2 предрегистрированных варианта порога), на ОБОИХ C1/C2 без выбора «лучшего».
  Экономика: у C2 gross > 0, но издержки превышают его → гейт режет и убыточные дни, и число
  сделок. **PROPOSAL / NOT_STARTED.**
- `spec2_factor_fx_commodity.md` `f977f2f4…` — **факторная** (валюта/нефть/золото как допуск
  направления, каркас принятого A4 §9): нейтральная зона первой, `sign(F)=side`, «нет свежего
  фактора → нет сделки». **PRECONDITION_NOT_MET**: офлайн есть только 5 equity-CSV A10,
  coverage внешних рядов = UNKNOWN в A4, инжест не разрешён → документ не является разрешением.
- `manifest.json` `1196c81b…`

Обе спецификации содержат записанные **заранее** условия `reject / retain / inconclusive` и
запрет на автопроход по произвольному порогу. Запуск любой из них — только после отдельного
`RUN_AUTHORIZED` архитектора.

## Ограничения

- `phase=development_unvalidated`, `holdout=NOT_ASSIGNED`, прибыльность UNKNOWN.
- Условные издержки 5/2 и 10/5 bps — не тариф; суммы на 1 акцию, тикеры не складываются.
- Новости DEFERRED; DeepSeek-улучшение не доказано; текущая волна от него не зависит.

---

## ECONOMICS_DONE (2026-10-11) — C1/C2 против базлайна A12

**Гейты:** A24 + `day_candidate_signals_fast` + interface `ACCEPTED_SCOPED` → `RUN_AUTHORIZED`
(`bf51aa40…`) → B3 `RAW_DONE` (`RUN-C1C2-20261011-01`, 10/10 rc=0, `integrity all_ok`).
Отчёт собран **одним** вызовом принятого A11 (`2da09969…`, timeout 120с, rc=0,
`trades=1322, rejects=0`); стратегия повторно не запускалась; `output_sha256` 10/10 пересчитаны.

**Артефакт:** `backend/reports/architect_20261010/b2/branch_economics/20261011T051239Z-economics-done/`
(`ECONOMICS_DONE.md 07208143…`, `economics_decision_table.csv 104be820…`,
`summary.json 19e5a00d…`, `manifest.json bb1cd680…` (10/10 файлов, включая вложенный
`a11_report/manifest.json cea2b934…`; первая версия `a0d9b64f…` его исключала — исправлено,
см. chat).

**Знаменатели (сопоставимы только closed/incomplete):** C1/C2 — 614 closed / 47 incomplete /
skipped 199 weekday + 340 weekend; A12 — 653 / 120 / 887 weekday + 0 weekend. Разница skipped —
артефакт календарного знаменателя нового CLI, **не** экономическое улучшение.

**Деньги, 1 акция, ex-SBER (`known_bad_input`):**

| Конфиг | n_closed | gross | net 5/2 | net 10/5 | net/trade 5/2 |
|---|---|---|---|---|---|
| C1 | 239 | −518.43 | **−1056.52** | **−1671.48** | −4.42 |
| C2 | 227 | +357.84 | **−175.64** | **−785.34** | −0.77 |
| A12 S1-A / S1-B / S2-A / S2-B | 167/205/98/65 | −76/+150/−184/+8 | −422/−341/−388/−108 | −818/−902/−620/−240 | −2.53/−1.66/−3.96/−1.66 |

**Вердикты (по `decision_template.json`, без автопрохода по порогу):**
- **C1 → `reject_for_further_research`**: gross и net отрицательны в обоих бюджетах, все 5
  месяцев и обе половины отрицательные, прибыльных закрытых 34.5%→33.2%.
- **C2 → `reject_for_further_research`**: gross положителен (+357.84), но расходы превышают
  его в обоих бюджетах → net < 0 при n=227; `best_*_share` не определены (net ≤ 0).
- `retain_for_independent_observation` не выдан ни одному кандидату. SFIN/VKCO-`INCOMPLETE`
  (6.6%/7.7% покрытия) — открытая позиция на конце данных, не P&L.
- Положительный gross C2 — **post-hoc наблюдение** (см. `post_hoc_note`): исследование границы
  по расходам требует отдельной регистрации и в эту волну не входит.

**Осторожно при сравнении:** C1 хуже всех конфигов A12 по net/trade; C2 — наименее
отрицательный (−0.77 против −1.66 у S2-B) при n 227 против 65, **без тестов значимости и без
утверждения преимущества**. Положительных независимых периодов ни у C1, ни у C2 нет.

Статус ветки: **ECONOMICS_DONE**. `phase=development_unvalidated`, `holdout=NOT_ASSIGNED`,
прибыльность UNKNOWN.

---

## NEXT-WAVE SPEC addendum (2026-10-11) — REWORK по замечаниям архитектора

**Приёмка:** `B2 ECONOMICS ACCEPTED_SCOPED` (chat, `architect_branch_review_0610/review.json`);
10/10 manifest-файлов совпали, 40 строк decision_table сверены с raw ledger, 0 расхождений.
Решение архитектора: **текущие фиксированные C1/C2 не продвигать к наблюдению/торговле**
(решение о приоритете исследования; полный результат при unknown не доказан). Суммы по одной
акции разных тикеров — описательная бухгалтерская сводка, **НЕ** портфель/доходность.

**Новый блок:** `NEXT-WAVE SPEC`, **только документ**; запуск **не принят**. Старые manifest
**не редактировались** — всё в новом addendum-каталоге:

`backend/reports/architect_20261010/b2/branch_economics/20261011T054502Z-next-wave-specs-addendum/`
- `spec_final_majority_gate.md` `a1a33e0a9e1c8bf8e8d28c91f1a947552e40c7940a9fd5fcfef10e9b3c76c762` — **одна** финальная ценовая спецификация;
- `factor_preconditions.md` `39587fff15a995a15df029a0a0565220878d076dec5d9127d43919299534457b` — факторные предусловия **отдельно**, `PRECONDITION_NOT_MET`;
- `manifest.json` `cb117e49f0a4d0e48476c01ed58b2828e0f5eca01a006f32f21ea45e8f6b17f4`.

**Что исправлено (5 пунктов REWORK):**
1. **Один majority gate, одно испытание.** При 4 бумагах `B ∈ {0,.25,.5,.75,1}` и
   `B > 0.5 ⟺ B ≥ 0.75` (для 5 бумаг — `B > 0.5 ⟺ B ≥ 0.6`) → V0/V1 тождественны; второй порог
   и «сетка `B ∈ {0,.2,…,1}`» сняты, два испытания не считаются.
2. **Отозвано ложное утверждение** о «минусе всех семей в сентябре»: у C2 `2026-09 = +41.88`
   (ex-SBER `+44.00`) при 5/2; общий для всех четырёх семей отрицательный месяц — только
   **2026-08**. Причинный вывод о «рыночном режиме» и обещание «издержки упадут быстрее gross»
   убраны — теперь это проверяемая гипотеза.
3. **Корзина без SBER, зафиксирована до расчётов:** `GAZP, LKOH, SFIN, VKCO`; SBER исключён и из
   корзины, и из PnL. Exact close: **18:45 MSK = 15:45 UTC** по `SessionPolicy moex_intraday_v1
   1.0.0`; fallback на «последний случайно имеющийся бар» **запрещён**. Предыдущие **две**
   eligible-сессии (`D−1`, `D−2` — ретёрн требует двух close); календарь = будни SessionPolicy
   (праздников в нём нет → будний день без баров = UNAVAILABLE). Инвентаризация (только
   доступность): 86 сессий; close есть у GAZP 82, LKOH 82, SFIN 76, VKCO 81; дней со всеми 4
   close за `D−1` и `D−2` = **67/86**. `B` и PnL **не считались**.
4. **`retain` требует положительного net** (`> 0` в обоих бюджетах + `net_gate > net_base`), а не
   улучшения отрицательного; missing/`INCOMPLETE` не обнуляются; критерии **непересекающиеся**
   (inconclusive при `n_closed < 100` → retain при всех условиях → иначе reject). Сравнение —
   per-ticker + `net/Σentry_base` (нормировка капитала), cross-ticker сумма = описательная.
5. **SPEC2 → `PRECONDITION_NOT_MET`** с exact instrument/source/timezone/publication availability
   (CNY/RUB MOEX — class VERIFIED / тикер `UNVERIFIED`; курс ЦБ — раз в день, только T-1;
   Brent FORTS и золото — `UNVERIFIED`); coverage `UNKNOWN` **без сетевого поиска и инжеста**.

`B`/`z`/PnL/gate не вычислялись; сеть и БД не трогались; чужие зоны не тронуты.
Вся идея **post-hoc на development**, даже после фиксации правил — **не OOS**. Запуск — только по
отдельному `RUN_AUTHORIZED`.
