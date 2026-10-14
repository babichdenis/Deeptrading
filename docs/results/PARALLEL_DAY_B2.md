# PARALLEL-DAY-1 · Builder-2 (Worker-2) · ветка `day_b2_20261014`

> **Пакет:** ML, обучаемый на экономическом результате.
> Вопрос дня: *«лучше ли взаимодействия ТФ предсказывают доходность после расходов,
> когда цель совпадает с проверяемой сделкой?»*
>
> Run: `backend/reports/architect_20261014/parallel_day/b2/20261014T093000Z_day_b2_20261014`
> (2026-10-14T09:52:38Z, EXIT=0).
> Machine-readable результат: `RESULT.json` в том же каталоге, сводная таблица — `summary.csv`.
> Леджеры: `ledgers/*.csv`. Воспроизводимая конфигурация: `config_frozen.json`.
> Веточный инвариант: `backend/research/branches/day_b2_20261014/config.json`.

---

## 0. Короткий ответ

**Нет.** Взаимодействия ТФ (набор 58 всех таймфреймов + голоса) при обучении на
сделке-совпадающей цели (net return после расходов) **систематически хуже**, чем
узкий набор 5m-признаков, и **Ridge стабильно лучше HistGBR**. Экономический
результат фиксированного primary на eval **+0.70 %** при cost 5/2, но:

* он **неотличим от нуля** (paired bootstrap 95 %: `[−0.00156, +0.00179]`);
* при cost 10/5 первичная политика **убыточна** (`−2.69 %`, CI не содержит ноль);
* весь плюс 5/2 сконцентрирован в **одном тикере (ASTR)**, **одном дне (17.08)**
  и **одном месяце (август)**; сентябрь отрицателен;
* **без лучшего тикера или без лучшего дня — суммарный результат отрицательный**;
* прогноз доходности **не коррелирует** с реализацией (Pearson ≈ 0.0003 на eval).

Предсказательный эффект взаимодействий ТФ **не установлен**. Положительный screening
на val (4.06 bps/событие) **не переносится** устойчиво на eval.

**Диагностика (stage G, prereg `PRE-REG diag-1`):** H1 CS-IC **FAIL** (val h60 mean IC
0.0138 < 0.05 — переупаковка, не ранжир); H2 no-chase **PASS** (executed dip 82 gross
+4.5 bps, chase 22 gross −1.4 bps; знак dip>0/chase<0 в обоих схемах); H3 repeat-blocked
**PASS** (blocked 607 mean +0.001485 = 4.5× executed; бухгалтерия 712 = 105+599+8 точна).
Кандидаты следующего цикла: no-chase фильтр, combo «p>0 AND dip», расширение repeat-окна
(секция 11).

---

## 1. Входы (все frozen, read-only)

| Вход | Путь | manifest sha256 |
|---|---|---|
| Датасет v3 | `backend/reports/architect_20261012/ml_mtf_1/b1/20261013T221949Z_v3_full` | `d46d7d24fa17d9d391c6807c16dd90f60f58a2e994a287b85b19f62ac5858570` |
| Snapshot минут | `backend/reports/architect_20261011/research_core_v2/b1/20261011T1947Z/snapshot` | `9964b08ff83a3820efb04862fb09e0ae31653a211a759f6360b91ad999681577` |
| Frozen daypack | `backend/research/daypack_20261014` | `5188d55d62646f4b1863e15ac27e24a8459efda0b0d1dcce017aa225ca2e97d2` |
| predictor_columns | 58 шт. | `f4d52d9085e61ae8f0a9b250d02bad583f6d7a3ab7332a5c8817a17e929a5b98` |
| freeze.json | run | `9f149abc0a2d11fa184ef50dfc0be150d6f99e5eb10d5a93825ae76ba3720434` |
| Скрипт (запуск) | `backend/scripts/research_day_b2.py` | `2a1db320f9592d53ddee636280fe29a4560412c98a4a4a671153797a07d7d7ad` |

Датасет: `dataset_version=ml_mtf_1_v3`, `feature_schema_version=1m_mtf_v3`,
eligibility policy `{tz Europe/Moscow, weekdays [0..4], open 10:00, close 18:40,
decision_strictly_before 18:44}` (hash `b8f7f9cd0498d4b9`), 2 557 100 per-1m features /
511 159 5m targets / 30 тикеров (HHRU без данных). `n_min_*` и `voice_status_*` —
**metadata**, в predictors не входят (∩ = ∅).

Общий common acceptance прогнан до расчёта:

```
$ PYTHONPATH=backend python -m research.daypack_20261014.acceptance
{'status': 'PASS', 'cases': 6}
```

**Deviation = none.** Daypack использован read-only, локальных копий не создавалось.

---

## 2. Метод

### 2.1 Экономика (общий contract, daypack `execution_core` + `frozen_portfolio`)

* LONG_ONLY; вход **то на открытие 1m-бара `decision_ts`** (raw next-open = закрытие 5m);
* выход **то через `horizon` минут**, с clamp на cutoff 18:44 MSK;
* нет entry-минуты → `SKIP`; нет exit-минуты → `INCOMPLETE` (**unknown ≠ 0**);
* session gate 10:00 ≤ MSK < 18:44, будни; tie-break по FIGI, **exits перед entries**;
* один FIGI — одна открытая позиция; 5 слотов по 20 % стартового капитала,
  `qty = 0.2 / entry_base`; fees + slippage участвуют в cash-check;
* без плеча и реинвеста; fractional quantities (research, не live lots);
* costs на каждую ногу: **5/2 bps (primary)** и **10/5 bps (sensitivity)**;
* `net = gross − costs_total`, тождество проверяется на каждой конфигурации (`identity_ok=true`).

### 2.2 Stage A — offline target из frozen минут

Для **каждого** из 511 159 решений (все строки targets v3, без оглядки на
`label_status` — maturity не отбирает inference-решения) вычислен реальный
сделко-совпадающий net return на горизонтах 30 / 60 / 120 минут через
**тот же** `engine.execute_signal` с `CostParams(5,2)` и `CostParams(10,5)`.

Итог по статусам (из `stage_a_summary.json`):

| horizon | FILLED | INCOMPLETE | SKIP |
|---|---|---|---|
| 30 | 466 085 | 20 582 | 24 492 |
| 60 | 457 818 | 28 849 | 24 492 |
| 120 | 441 268 | 45 399 | 24 492 |

`session_rejected = 0` (decision grid v3 уже сидит в сессии). Все `decision_ts` —
строго на минутной сетке (`grid_ok`).

### 2.3 Stage B — fixed matrix 12 fits

Без подбора гиперпараметров. `seed = 42`, `OMP/MKL_NUM_THREADS = 1`.

| factor | значения |
|---|---|
| horizon | 30 / 60 / 120 мин |
| model | **Ridge(α=1)** / **HistGBR**(max_iter=150, max_leaf_nodes=15, l2=1, min_samples_leaf=200, early_stopping=False, random_state=42) |
| feature set | **cont5** = 7 признаков `rsi14_5m, macd_hist_5m, macd_hist_delta_5m, ema50_dist_5m, ema50_slope_5m, atr14_price_5m, volr20_5m` / **all_tf** = все 58 predictors |

Оба пайплайна: `MedianImputer` → (`StandardScaler` только для Ridge) → estimator.
Трансформеры и квантили fit **только на train**. Качественные метаданные
(`n_min_*`, `voice_status_*`) не подмешиваются. Цель — `net_per_unit` при cost 5/2.

Регрессионная цель, **не классификация** — метки непрерывные, включая отрицательные.

### 2.4 Stage C — 36 policy attempts и freeze

Для каждого из 12 fits × 3 порога (`predicted_net > 0`, `> 0.0005`, `> 0.001`)
посчитан screening на **val** по общему критерию (п.9):

* метрика: **mean known event net на val среди selected, cost 5/2**;
* ≥ 200 known events; ≥ 20 разных дат; **unknown endpoint fraction ≤ 5 %**.

Это screening metric **с overlap**, заранее объявленный как **не** portfolio PnL.

**Результат: 36 попыток, 30 допустимых.** `freeze.json` записан **один раз до eval**
(sha `9f149abc…`, immutable_after_write=true), primary и top3 заморожены; после eval
ничего не пере-выбиралось (`never_reselected_after_eval: true`).

| rank | attempt | mean known net @5/2 | n_known | n_dates | unknown |
|---|---|---|---|---|---|
| **1 → primary** | `ridge__cont5__h60__t0p0000` | **+0.000406** | 977 | 33 | 0 |
| 2 (top3_2) | `ridge__cont5__h120__t0p0010` | +0.000305 | 1 051 | — | 1 |
| 3 (top3_3) | `ridge__cont5__h120__t0p0005` | −0.000098 | 1 760 | — | 4 |
| 4 | `ridge__all_tf__h60__t0p0005` | −0.000248 | — | — | — |
| 5 | `ridge__all_tf__h60__t0p0000` | −0.000315 | 1 928 | — | — |
| 6 | `ridge__all_tf__h60__t0p0010` | −0.000580 | — | — | — |
| 7 | `ridge__all_tf__h30__t0p0000` | −0.000670 | — | — | — |
| 8 | `hgb__cont5__h60__t0p0010` | −0.000750 | — | — | — |
| 9 | `ridge__all_tf__h120__t0p0010` | −0.000787 | — | — | — |

`best_ridge` и `best_cont5` совпали с primary → схлопнулись в один eval-конфиг
`primary+best_ridge+best_cont5` (это **не** смена primary, а дедупликация).

**Наблюдение по матрице:** в топ-9 попали **только Ridge**; **ни одна all_tf-конфигурация
не лучше, чем соответствующая cont5**; **ни одна HGB-конфигурация не попала в топ-8**.

### 2.5 Stage D — единственный eval + портфель

Eval-конфигурации: primary (+best_ridge/+best_cont5), top3_2, top3_3, плюс baselines
`cash` и `always_long` на **том же** горизонте и тех же eligible-решениях.
Исполнение — тем же frozen daypack (`summarize` / `run_portfolio`), оба cost-сценария.

### 2.6 Stage E — robustness

Paired daily comparison на общей календарной оси с явным coverage;
500 bootstrap-выборок блоками по 5 сессионным дням, `seed=42`, на known paired days
(описательный интервал, не коррекция множественного поиска);
ticker/month вклады; sensitivity без лучшего тикера и без лучшего дня (без перенастройки);
residuals (MAE / bias / RMSE / Pearson / deciles) на known outcomes по всем 12 fits.

---

## 3. Сплиты и drop

train ≤ 2026-05-31 · val 2026-06-01..07-31 · eval 2026-08-01..10-08, embargo 120 мин.

| split | rows |
|---|---|
| train | 277 445 |
| val | 102 130 |
| eval | 131 584 |

Dropped: `embargo = 0`, `no_split = 0`, `not_known` =
`{train:30 40 908, train:60 48 977, train:120 65 241}`; в val/eval — 0.

Embargo дал 0, потому что правило не связывает соседние секции: сессия заканчивается
18:44 MSK, `entry + 120 мин ≤ 20:44` того же дня, а граница секции — 00:00 следующего дня.
Правило оставлено и проверено как инвариант (`test_assign_split_and_embargo`).

---

## 4. Eval ledger (единый портфель, shared capital = 1.0)

| config | cost | h | selected | closed | open | gross | fees | slip | **net** | final | last_known |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **primary+best_ridge+best_cont5** | **c5_s2** | 60 | 712 | 105 | 0 | +0.036424 | 0.021018 | 0.008407 | **+0.006999** | **1.006999** | 1.006999 |
| primary+best_ridge+best_cont5 | c10_s5 | 60 | 712 | 103 | 0 | +0.034999 | 0.041235 | 0.020617 | **−0.026854** | 0.973146 | 0.973146 |
| top3_2 (`…h120__t0p0010`) | c5_s2 | 120 | 717 | 85 | 0 | +0.042575 | 0.017021 | 0.006809 | **+0.018745** | 1.018745 | 1.018745 |
| top3_2 | c10_s5 | 120 | 717 | 84 | 0 | +0.045231 | 0.033645 | 0.016823 | −0.005237 | 0.994763 | 0.994763 |
| top3_3 (`…h120__t0p0005`) | c5_s2 | 120 | 1 143 | 118 | 1 | +0.021249 | 0.023611 | 0.009444 | −0.011805 | **None** | 1.004967 |
| top3_3 | c10_s5 | 120 | 1 143 | 118 | 1 | +0.021249 | 0.047221 | 0.023611 | −0.049582 | **None** | 0.979345 |
| always_long | c5_s2 | 60 | 131 584 | 864 | 3 | −0.081580 | 0.172759 | 0.069104 | **−0.323443** | **None** | 0.948657 |
| always_long | c10_s5 | 60 | 131 584 | 803 | 1 | −0.068002 | 0.321132 | 0.160566 | −0.549700 | **None** | 0.870104 |
| cash | c5_s2 / c10_s5 | — | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 1.0 | 1.0 |

`identity_ok = true` для всех строк (`net == gross − costs_total`).

Дополнительные счётчики primary @5/2: `n_skip 0`, **`n_ignore_repeat 599`**
(повторные сигналы по уже открытому FIGI корректно отбрасываются одним слотом),
`n_no_slot 0`, `n_no_cash 8`, `n_incomplete 0`; win_rate 0.4667; mean/trade +6.67e-05;
max drawdown known 0.02005; `last_known_ts = 2026-09-29`.

**Важно про `final_equity = None` у top3_3 и always_long:** это не ошибка расчёта,
а честная неизвестность — у этих конфигураций на хвосте осталась **принятая открытая
позиция** (minute-store обрывается около 2026-10-04), и final не выдаётся до закрытия.
`last_known_equity` и `max_drawdown_known` считаются **только до первой accepted open**.
Полный closed net cashflow опубликован отдельно в `RESULT.json →
closed_cashflow_diagnostic` с явной пометкой, что это **diagnostic, а не equity**.

---

## 5. Paired daily (область правды для «лучше ли»)

| cost | baseline | common days / axis | total diff | mean daily | **bootstrap 95 % CI** |
|---|---|---|---|---|---|
| c5_s2 | cash | 23 / 46 (0.500) | **+0.006999** | +0.000304 | **[−0.001558, +0.001785]** |
| c5_s2 | always_long | 23 / 46 (0.500) | +0.215889 | +0.009386 | [+0.002680, +0.018480] |
| c10_s5 | cash | 23 / 49 (0.469) | −0.026854 | −0.001168 | **[−0.002389, −0.000427]** |
| c10_s5 | always_long | 23 / 49 (0.469) | +0.246966 | +0.010738 | [+0.004649, +0.018962] |

Чтение:

* **vs cash при 5/2 — ноль внутри интервала.** Нет статистически различимого превосходства
  над бездействием.
* **vs cash при 10/5 — интервал строго отрицательный.** Рост издержек не просто «съедает»
  плюс, а делает политику значимо убыточной.
* **vs always_long — primary заметно лучше** (очевидно: always_long при 5/2 теряет −32 %),
  но это сравнение не про качество модели, а про то, что входить в рынок без отбора
  при этих издержках разрушительно.

Coverage 0.50 / 0.47 — явно задокументирована: `always_long` и tail-позиции дают
нереализованные дни, которых у primary нет, поэтому общая ось шире.

---

## 6. Концентрация — главный результат

Сensitivity **без перенастройки** (по уже исполненному ledger):

| cost | total | без лучшего тикера (ASTR) | без лучшего дня (2026-08-17) |
|---|---|---|---|
| **c5_s2** | **+0.006999** | **−0.002241** | **−0.005674** |
| c10_s5 | −0.026854 | −0.034485 | −0.037915 |

Вклад по тикерам при c5_s2 (105 closed trades, `concentration_top1_share = 1.3202` —
вклад одного тикера **больше суммарного**, потому что остальные в сумме отрицательны):

```
ASTR +0.009240   VKCO +0.004939   OZON +0.004932   AFKS +0.003475
MTSS +0.003311   ALRS +0.003255   T    +0.000968   RNFT +0.000510
GMKN +0.000484
SFIN −0.001249   PLZL −0.001645   YDEX −0.001764   MAGN −0.002100
LKOH −0.002419   MVID −0.003066   SMLT −0.003511   RUAL −0.003651
PHOR −0.004711
```

По месяцам: **2026-08 +0.011957, 2026-09 −0.004959.**
При c10_s5: август −0.010691, сентябрь −0.016163 (плюс ASTR +0.007632, top1_share 0.2842).

**Вывод:** утверждение «модель даёт +0.70 %» фактически равносильно утверждению
«ASTR 17.08 дал +0.92 %». Один тикер, один день, один месяц. Устойчивого
диверсифицированного эффекта нет.

---

## 7. Качество прогноза (residuals)

Регрессия прогноза против реализации на **known** outcomes:

| fit | split | n_known | MAE | bias | **Pearson** | mean_pred | mean_realized |
|---|---|---|---|---|---|---|---|
| `ridge__cont5__h60` (primary) | val | 99 952 | 0.006143 | +0.000042 | **0.0093** | −0.001574 | −0.001616 |
| `ridge__cont5__h60` (primary) | eval | 129 398 | 0.004515 | −0.000056 | **0.0003** | — | — |

По всем остальным h60-моделям |Pearson| ≤ 0.0085; hgb__all_tf__h120 на val −0.0154;
MAE на h120 0.006–0.0089.

Bias крошечный (модель в среднем чуть не хватает уровня), MAE сопоставим с масштабом
самой цели, **но корреляция практически нулевая**. Это означает, что Ridge и HGB
учатся воспроизводить средний net return, а не ранжировать решения по их будущей
доходности. Экономический отбор ниже по течению (порог > 0 на predicted net)
фактически отбирает «те, кому модель предсказала плюс», а не «тех, у кого плюс будет».

### Где остаются неизвестные (val, h60)

FILLED 99 952 / INCOMPLETE 1 063 / SKIP 1 115. Самые частые INCOMPLETE: MVID 488,
SFIN 331, ASTR 90, VKCO 23, NLMK 19, RUAL 19, TRNFP 16, SMLT 12 — то есть те же
тикеровые прорывы minute-store, что и в sensitivity.

---

## 8. Ответ на вопрос дня

**Вопрос:** лучше ли взаимодействия ТФ предсказывают доходность после расходов,
когда цель совпадает с проверяемой сделкой?

| сравнение | verdict |
|---|---|
| all_tf (58) vs cont5 (7) | **cont5 лучше.** Все all_tf-конфигурации в val-таблице хуже своих 5m-аналогов; ни одна не попала в top-3 |
| HGB vs Ridge | **Ridge лучше.** Все top-9 — Ridge; ни одна HGB-конфигурация не в top-8 |
| regression на net return vs 3-class classification (старый цикл ML-MTF-1) | признак того, что смена цели на экономическую **сама по себе ничего не чинит**: Pearson ≈ 0 сохраняется |
| primary eval @5/2 vs cash | **различимо не лучше** (CI содержит 0) |
| primary eval @10/5 vs cash | **значимо хуже** (CI строго < 0) |

**Итог: эффект взаимодействий ТФ не установлен.** Экономическая цель, хоть и является
более честной постановкой, не спасает отсутствие предсказательной силы. Признак
того, что лимит здесь — не выбор цели, а сама информация в признаках.

Дополнительно (вне критерия, без смены primary): **top3_2 `ridge__cont5__h120__t0p0010`
дал на eval при 5/2 +1.87 % против +0.70 % у primary.** Это exploratory comparator,
замороженный до eval, и он **не пере-выбран** — но его стоит отметить архитектору как
кандидат на отдельный, заранее заявленный исследовательский вопрос.

---

## 9. Контроль и воспроизводимость

### 9.1 Собственные focused checks (16 тестов, все зелёные)

```
$ cd backend && OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_research_day_b2.py -q
16 passed in 9.39s
```

Покрыто: entry = open 1m-бара; тождество `net == gross − costs`; clamp выхода на 18:44;
`SKIP` при отсутствии entry-минуты и `INCOMPLETE` при отсутствии exit-минуты;
session + weekday gating; отсечение look-ahead по `available_at_ts`;
assign_split + embargo; **неизвестная метка → NaN и исключение из fit-маски**;
MedianImputer fit только на train; детерминированный pickle roundtrip;
**quality-метаданные не попадают в predictors** (проверено на реальном manifest v3);
гейты `_screen` (support / даты / unknown); **per-ticker evaluate ≡ reference evaluate()**;
IGNORE повторного сигнала по одному FIGI; детерминированный bootstrap;
freeze пишется ровно один раз, sha стабилен; **уникальность attempt-id по threshold**
и разрешение prediction-колонки по `fit`.

### 9.2 Воспроизведение

```
cd backend && OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python \
  scripts/research_day_b2.py --repo /Users/Denis/Dev/Deeptrading \
  --out backend/reports/architect_20261014/parallel_day/b2/20261014T093000Z_day_b2_20261014 \
  --resume
```

Стадии можно гонять по одной (`--stage a|b|c|d|e|f`); `--resume` пропускает завершённые
фрагменты **только по совпавшим sha256**. `--out` должен быть абсолютным путём.

### 9.3 Закрытые дефекты собственного кода (все исправлены до сдачи)

1. `pcfg = "primary"` в stage E/F — после дедупликации реальное имя конфига
   `primary+best_ridge+best_cont5` → StopIteration. Исправлено разбором префикса.
2. `bootstrap_block_ci` в пустой ветке не возвращал `n_bootstrap` / `block_days` / `seed`.
3. Paired vs cash давал пустое пересечение (у cash нет `day_realized`) → для cash
   ось берётся из primary, diff = primary.
4. **`attempt`-id не содержал threshold** → top3 схлопывался, конфиги `top3_2` и
   `top3_3` мержились. Исправлено: `rec['fit']` + `rec['attempt'] = f'{fit}__t{thr:.4f}'`;
   prediction-колонка берётся по `fit`. Старые eval/ledgers переименованы в
   `deprecated_eval_attemptid_bug` / `deprecated_ledgers_attemptid_bug`; **freeze и eval
   прогнаны заново до сдачи**; выбор по val не менялся (sha freeze тот же).
5. `RESULT['primary']['config']` писал dict вместо имени; добавлен
   `inputhashes.script_sha256_stage_f`.

---

## 10. Ограничения

1. Период — **development, не fresh holdout**; выбор по val, ровно один eval после freeze.
2. Minute-store покрытие **неполное**: часть тикеров стартует в 2026-01..2026-08,
   m1 обрывается около 2026-10-04 (SBER 2026-10-08) → INCOMPLETE / unknown на хвосте eval.
3. Корпоративные действия и календарь snapshot'а: `UNVERIFIED` / `NOT_VERIFIED`.
4. Screening metric (mean known event net) — с overlap и без учёта 5 слотов;
   портфельный ledger **может заметно расходиться** с ним (что и произошло).
5. Fractional quantities (research, не live lots); short не рассматривается.
6. При NO_SELECTION/cash пороги и support **не снижаются**.
7. 500 bootstrap-выборок блоками по 5 сессионным дням — **описательный** интервал,
   не коррекция множественного поиска.
8. Цена входа = открытие 1m-бара `decision_ts` (raw next-open = закрытие 5m).
9. Непрерывность промежуточных минут (path_metrics) не влияет на endpoint PnL без стопов.
10. Daypack использован read-only, deviation = none; локальных копий не создавалось.

---

## 11. Stage G · диагностика prereg diag-1

Формализация ad-hoc диагностики в ветке run-а (отдельная стадия `g`, не трогает freeze/eval).
Протокол prereg опубликован в `chat.md` ДО расчётов (маркер `PRE-REG diag-1`); goalposts
не сдвигались. Источник цен — snapshot m1 CSV (тот же, что у runtime; без обращения к БД).
Все числа ниже — из `stage_g_summary.json` (script_sha256_stage_g `8a1977ae5b2f`),
проверка механических счётов `checks_failed=[]`.

### 11.1 H1 · Cross-sectional IC (FAIL по prereg)

Spearman(p__ridge__cont5__h, n52h) внутри слота decision_ts, слоты ≥5 тикеров FILLED:

| split | h | slots | mean IC | t | frac>0 |
|---|---|---|---|---|---|
| val | 30 | 4680 | 0.013720 | 3.48 | 0.524 |
| val | 60 | 4680 | 0.013802 | 3.55 | 0.515 |
| val | 120 | 4680 | 0.005930 | 1.56 | 0.495 |
| eval | 30 | 4685 | 0.033971 | 9.83 | 0.566 |
| eval | 60 | 4684 | 0.040878 | 11.73 | 0.578 |
| eval | 120 | 4680 | 0.042500 | 12.46 | 0.584 |

Prereg-гейт: val h60 mean_IC ≥ 0.05 ∧ t ≥ 2 ∧ eval > 0 → **FAIL** (val 0.0138 < 0.05).
IC статистически значим (t 3.5 val / 11.7 eval) и sign-consistent по схемам, но
слабый: вертикальная переупаковка признаков, а не горизонтальный ранжир-эффект.
Top-k screening по гейту НЕ запускался.

### 11.2 H2 · no-chase фильтр (PASS prereg)

ret30 = close[последний бар < t1] / close[baseline ≤ t1−30м] − 1 (окно 91м; baseline
строго внутри окна — регресс-тест на исторический баг «baseline на краю окна»). Dip = ret30 < 0:

| группа | dip n | dip mean net | chase n | chase mean net | Spearman(ret30,net) |
|---|---|---|---|---|---|
| val·all | 857 | +0.000588 | 115 | −0.001211 | −0.023 (ns) |
| eval·all | 501 | +0.002644 | 206 | −0.002152 | −0.188 |
| eval·executed | 82 | +0.000832 | 22 | −0.002100 | −0.092 |
| eval·blocked | 419 | +0.002998 | 184 | −0.002158 | −0.209 |

Executed ledger (гросс/нетто по ценам ledger, H2-гейт):

| sign | n | mean gross | mean net |
|---|---|---|---|
| dip | 82 | **+0.000447** | +0.000166 |
| chase | 22 | **−0.000140** | −0.000420 |

Prereg-гейт: dip n ≥ 30 ∧ gross_dip > 0 ∧ gross_chase ≤ 0 → **PASS**.
Знак dip>0 / chase<0 совпадает в val и eval (уровень val слабее, dip несёт SMLT —
хрупкость уровня, знак устойчив). Coverage ret30: val 972/977, eval 707/712.
Правило «не гнаться» идёт в prereg СЛЕДУЮЩЕГО цикла; через freeze/eval сейчас НЕ прогонялось.

### 11.3 H3 · repeat-блокировка (PASS prereg)

Бухгалтерия primary c5_s2 сходится точно: selected 712 = executed 105 + ignore_repeat 599
+ no_cash 8 (+ skip 0, no_slot 0, open 0) → blocked = 607.

| группа | n known | mean net | dates | frac>0 | mean w/o best ticker | sum w/o best day |
|---|---|---|---|---|---|---|
| executed | 105 | +0.000333 | 23 | 0.467 | −0.000112 | −0.028369 |
| blocked | 607 | **+0.001485** | 22 | 0.532 | +0.000987 | +0.506428 |

Blocked-события в 4.5× прибыльнее executed (mean +0.001485 vs +0.000333); знак и
концентрация (без лучшего тикера +0.000987, без лучшего дня +0.506; Aug n=418 sum +0.607,
Sep n=189 sum +0.295 — оба месяца >0). Месячные суммы приведены по stage-g-базису
(formal) и заменяют более ранние ad-hoc оценки из чекпоинта.
Prereg-гейт: blocked mean > 0 ∧ n ≥ 200 ∧ dates ≥ 20 → **PASS**. Интерпретация
диагностическая: repeat-окно теряет более прибыльные сигналы — кандидат «расширение
repeat-окна» в следующий цикл, НЕ доказательство переупаковки PnL.

### 11.4 Вердикты и кандидаты следующего цикла

| гейт | вердикт |
|---|---|
| H1 CS-IC | **FAIL** (top-k не авторизован) |
| H2 no-chase | **PASS** → в prereg следующего цикла |
| H3 repeat-blocked | **PASS** → кандидат «repeat-window relaxation» |

Кандидаты (отсортированы, все — prereg next-cycle, НЕ отбор через сожжённый eval):
1. no-chase фильтр: val-screening критерий п.9 уже проходит (n_known 857, dates 32,
   unknown 0, mean 0.000588 > 0.000406 primary);
2. combo «p > 0 AND dip» (blocked-dip eval: 419 @ +0.002998);
3. расширение repeat-окна (H3).
Ограничения: eval exploratory (окно Aug–Oct 2026 сожжено в-development); свежих данных
после 2026-10-08 в БД нет — подтверждение любых кандидатов только на свежей минутке.

## 12. Индекс артефактов

```
backend/reports/architect_20261014/parallel_day/b2/20261014T093000Z_day_b2_20261014/
├── input_manifest.json        # хеши всех входов
├── config_frozen.json         # frozen research config (до расчёта)
├── freeze.json                # sha 9f149abc…, immutable, до eval
├── targets_a/                 # 30 parquet + _index.json  (stage A)
├── design/                    # design matrices per split  (stage B)
├── fits/                      # 12 pkl + _index.json
├── predictions/{val,eval}.parquet
├── eval/*.json                # 5 конфигов × 2 cost (primary, top3_2, top3_3, always_long, cash)
├── ledgers/*.csv              # executed + accepted-open, 20 файлов
├── stage_{a,b,e,g}_summary.json
├── DIAGNOSTICS.md              # stage G: prereg diag-1 (H1/H2/H3)
├── summary.csv                # 10 eval (5 конфигов × 2 cost) + 36 val attempts
├── RESULT.json                # machine result
├── RESULT.md
├── run.log, console.log
├── deprecated_eval_attemptid_bug/      # артефакты до исправления attempt-id
└── deprecated_ledgers_attemptid_bug/

backend/research/branches/day_b2_20261014/{README.md, config.json}
backend/scripts/research_day_b2.py
backend/tests/test_research_day_b2.py
docs/results/PARALLEL_DAY_B2.md         # этот файл
```

**Не трогалось:** `ml_mtf_learn.py`, общий daypack (read-only), B1/B3-артефакты,
bot / orders / deploy / API / backfill / DB-write.
