# ML-MTF-1 / B2 — поиск взаимодействий признаков на нескольких таймфреймах

**Builder-2**, ветка ML-MTF-1, 2026-10-12.
Зона: `backend/scripts/ml_mtf_learn.py`, `backend/tests/test_ml_mtf_learn.py`,
артефакты `backend/reports/architect_20261012/ml_mtf_1/b2/<run_id>/`.

> ## ✅ СТАТУС: ОСНОВНОЙ ПРОГОН — `20261013T225653Z_v3_full` (датасет v3)
>
> Цифры §9–§11 относятся к пробегу на **DATA_READY v3**
> (`b1/20261013T221949Z_v3_full`, `dataset_version=ml_mtf_1_v3`,
> `feature_schema_version=1m_mtf_v3`) после единого цикла
> «исправления → новые данные → обучение → портфельный расчёт»
> (`chat.md` 5898). Тесты: **28 passed**.
>
> Статус предыдущих прогонов:
>
> - `20261012T1111Z_full` — **отозван навсегда** (нарушено соответствие
>   X↔y, метка привязана через `enumerate`). Run-каталог immutable,
>   `SUPERSEDED.txt` внутрь не дописывался. Отзыв закреплён в `chat.md`
>   5503–5527, proof: `architect/review_b1_b2/{REVIEW.md,proof.py,proof.json}`.
> - `20261013T194158Z_v2_full` — **устарел методологически**. X↔y там не
>   нарушен, но он обучался с метаданными покрытия в предикторах (в
>   `continuous_all_tf` входили `n_min_<tf>`, корень дерева победителя —
>   `n_min_3600s`, т.е. модель торговала плотностью данных, а не рынком)
>   и на данных, где B1 ещё пропускал выходные. Этих чисел в отчёте больше нет.
>
> Что изменено в этом цикле (все пункты покрыты тестами):
>
> 1. **Eligibility-gate** — будни / главная сессия с 10:00 MSK / решения
>    строго до 18:44 MSK. Читает **только** `decision_ts` (не метку и не
>    будущий `MATURE`), применяется и к supervised, и к inference-вселенной,
>    сверяется с `eligibility_policy` из `manifest.json` B1 и падает при
>    несовместимости.
> 2. **Whitelist признаков** — в X только 35 рыночных continuous + `bias` +
>    9 `vote_` + 9 `age_` + 4 aggregate = **58**; `n_min_*`,
>    `voice_status_*`, identity/timing остаются metadata
>    (`excluded_quality_columns`). Закрыт и suffix-fallback-канал.
>    Совпадение с `predictor_columns` B1 проверяется жёстко.
> 3. **flock-публикация** в `chat.md`: постоянный lock-файл,
>    `open(a)+flock(LOCK_EX)+flush/fsync+LOCK_UN`, пост перечитывается,
>    ошибка записи поднимается явно (прежний mkdir-lock молча отказывал).
> 4. **Инвариант join**: `rows_joined == rows_targets − only_in_targets` —
>    per-1m признаки не породили копий метки по 5m `row_id`.
> 5. **Отдельная отчётность**: support known targets vs eligible infer,
>    weekday counts, quality-strata (counts + per-stratum метрики на val/eval),
>    `primary_policy_sufficiency` (precision ≥ val base_up → явный вердикт).

Вопрос ветки: **даёт ли набор признаков нескольких ТФ (1/5/10/30/60 м) +
индивидуальных голосов добавочное качество предсказания метки triple_barrier
относительно 5m-непрерывных признаков и относительно фиксированного baseline
кворума**, и проявляются ли при этом объяснимые взаимодействия между ТФ.

Метка ≠ прибыль. Ответ на вопрос доходности — за B3 (отдельная ветка).

---

## 1. Входные данные (датасет B1 v3)

Источник: `backend/reports/architect_20261012/ml_mtf_1/b1/20261013T221949Z_v3_full/`
(`dataset_version=ml_mtf_1_v3`, `feature_schema_version=1m_mtf_v3`,
`manifest.json` + `feature_schema.json` — источник истины). Окно
`2025-10-01T04:00Z … 2026-10-09T00:00Z` (правая граница исключая),
30 тикеров (HHRU — `status=no_bars`, в манифест не входит),
`label_cfg_hash a898948ff9f0d027` (единственный),
`params_hash 7bc511c8…`, `input_manifest_sha256 9964b08f…`,
`eligibility_policy_hash b8f7f9cd0498d4b9`.

**Eligibility-policy B1** (записана в `manifest.json`, сверяется прогоном):
`{tz Europe/Moscow, weekdays [0..4], open 10:00, close 18:40,
decision_strictly_before 18:44}`. Источник `decision_ts`: `2025-10-01T07:05Z
… 2026-10-08T15:40Z`. На v3 `--validate` даёт `weekday_violations: 0`; на
предыдущем v2-full — 1 043 977 нарушений, `ok: false`.

В v3 разделение колонок задано явно: **58 `role=predictor` + 20
`role=metadata`**. Прогон жёстко проверяет
`set(default_feature_schema()['all_tf_votes']) == set(manifest['predictor_columns'])`
и иначе падает с сообщением про `predictor_columns`; B1-контракт
записывается в manifest как `b1_provenance` (dataset/feature versions,
`label_cfg_hash`, `code_sha256`, `params_hash`, `eligibility_policy_hash`,
`whitelist_matches_b1_predictors`).

Состав v3 (58 predictor): 6 identity-колонок **не входят**; входят 35 рыночных
continuous (`rsi14_/macd_hist_/macd_hist_delta_/ema50_dist_/ema50_slope_/
atr14_price_/volr20_` × `_1m/_5m/_600s/_1800s/_3600s`) + `bias` + 27 voice
(`vote_/age_/voice_status_` × 9 сетапов) + 4 aggregate (`voice_buy`,
`voice_sell`, `voice_quorum`, `voice_conflict`). Whitelist B2 даёт
`individual_votes 22 / continuous_5m 7 / continuous_all_tf 36 / all_tf_votes 58`;
`n_min_*` (5 колонок покрытия) в наборы не попадают.

- join строго по `row_id`, one-to-one: `validate="one_to_one"` плюс диагностика
  `_validate_join` — дубли `row_id`, несколько `label_cfg_hash`, расхождение
  `decision_ts/figi/ticker` **на пересечении `row_id`** и нарушение
  `feature_max_available_ts ≤ decision_ts` валит прогон. Шарды разного размера
  (features на каждом 1m-решении — 2 557 100, targets на закрытиях 5m —
  511 159), поэтому сверка идёт по пересечению. Дополнительно проверяется
  инвариант **`rows_joined == rows_targets − only_in_targets`** (= 511 159):
  per-1m признаки не могут породить копий метки по 5m `row_id`.
- из targets берутся `row_id,label,label_status,label_cfg_hash,label_end_ts` +
  опционально `label_status_reason` — `figi,ticker,decision_ts` дублируются в
  обоих шардах и дали бы суффиксы `_x/_y` при merge.
- **supervised ≠ вселенная предсказаний**: `y_known = MATURE ∧ label ∈ LABEL_MAP
  ∧ label_end_ts известен` — только такие строки годятся для fit/scoring;
  CENSORED (в v3: 118 466, `label_end_ts=None`, причины
  `future_bucket_incomplete` 41 209 / `anchor_incomplete` 47 293 /
  `session_cutoff` 29 847 / `gap` 117) в обучение не попадает и нулём не
  подменяется, но **остаётся в inference-вселенной**.
- целевая метка: canonical triple_barrier на закрытых 5m-барах
  (`take=1.5ATR, stop=1.5ATR, max_bars=24, atr_period=14`).
- запрещённых колонок в X нет: `label/label_status/label_cfg_hash/label_end_ts/
  label_status_reason/y` отбрасываются на чтении; отдельный тест это фиксирует.

**Смена версий B1 (все предыдущие отозваны):**

| run B1 | статус | что изменилось |
|---|---|---|
| `20261012T084035Z_full` | SUPERSEDED | исходный; использован в отозванном пробеге B2 |
| `20261012T112650Z_full` | отозван архитектором | сдвиг индекса метки ~65 мин → X↔y нарушен |
| `20261013T221949Z_v3_full` | **используется** | i2 + **eligibility-policy** (будни/сессия, `eligibility_policy_hash`), явный split колонок 58 predictor / 20 metadata, новая причина CENSORED `future_bucket_incomplete` |

Фактические признаки v2 (78 колонок, все найдены):

| группа | колонки |
|---|---|
| метаданные (не признаки) | `row_id, figi, ticker, decision_ts, feature_max_available_ts, feature_schema_version` |
| непрерывные по ТФ | `rsi14, macd_hist, macd_hist_delta, ema50_dist, ema50_slope, atr14_price, volr20` × суффиксы `1m / 5m / 600s / 1800s / 3600s` |
| bias | `bias` (часовой, period=50, `BIAS_TF=3600`) |
| полнота корзины ТФ | `n_min_<суффикс>` ×5 |
| голоса | `vote_<стратегия>` и `age_<стратегия>` ×9 |
| статус голоса (строковые, не признаки) | `voice_status_<стратегия>` ×9 |
| кворум/расхождение | `voice_buy, voice_sell, voice_quorum, voice_conflict` |

Наборы признаков (фиксированы архитектором; числа — фактически разрешённый
состав на v2, проверено на реальной схеме):

| id | состав | кол-во в данных |
|---|---|---|
| `individual_votes` | 9 `vote_*` + 9 `age_*` + 4 `voice_*` | 22 |
| `continuous_5m` | 7 индикаторов `5m` + `n_min_5m` | 8 |
| `continuous_all_tf` | 7×5 ТФ + `bias` + 5 `n_min_*` | 41 |
| `all_tf_votes` | `continuous_all_tf` + `individual_votes` | 63 |

**Тихая ошибка совместимости, найденная при сверке с v2:** старая схема знала
суффикс `60s`, а v2 даёт `_1m`. Явные списки колонок в `feature_schema.json`
оставались непустыми (по остальным ТФ), поэтому fallback-разбор по маске не
запускался и **7 колонок самого мелкого ТФ молча выпадали**
(`continuous_all_tf` 29/36, `all_tf_votes` 51/58) — без единого предупреждения.
Исправлено: канонический суффикс `_1m` + алиас-разворот `_60s` при резолвинге
(обе версии дают идентичный состав), `tf_of_column` узнаёт оба варианта,
добавлен dtype-guard от строковых `voice_status_*`/мета-колонок (в df их 15)
и подключены `n_min_*`. Закрыто тестами
`test_v2_tf_suffix_resolves_all_timeframes` и
`test_non_numeric_status_columns_never_enter_feature_sets`.

**Особенность данных, которой нет в контракте:** `age_vwap_reversion` и
`age_kama_trend` целиком NaN в train-окне (стратегии не срабатывали в этот
период). `SimpleImputer(strategy="median")` по умолчанию выбрасывает такие
колонки. Это зафиксировано явно (`dropped_all_nan` в `metrics.json`,
`kept_features`/`dropped_all_nan` в `tree_leaves.json`), иначе имена признаков в
условиях листьев дерева съехали бы на одну позицию — молчаливый сдвиг имён
исправлен и закрыт тестом `test_all_nan_feature_dropped_without_name_shift`.

## 2. Временной split (без lookahead): purge + embargo

Окна (CLI-дефолты, фиксированы архитектором):

| split | окно по `decision_ts` |
|---|---|
| train | ≤ 2026-05-31 |
| val | 2026-06-01 … 2026-07-31 |
| eval | 2026-08-01 … 2026-10-08 |

Два независимых правила, оба обязательны:

1. **Embargo 120 мин ПЕРЕД границей применяется к предыдущему сплиту** — окно
   `[граница−120м, граница)` вычищается из train (перед началом val) и из val
   (перед началом eval). Прежняя проверка пересекала окно с mask **собственного**
   сплит, который начинается позже — пересечение было всегда пустым.
2. **Purge по `label_end_ts`** — target, чей путь заканчивается на/после границы
   СЛЕДУЮЩЕГО сплита, удаляется из предыдущего целиком (не переносится: решение
   принято в его окне). Train теряет `label_end ≥ 2026-06-01`, val —
   `label_end ≥ 2026-08-01`. Неизвестный `label_end_ts` в supervised недопустим
   (в v2 это CENSORED с `label_end_ts=None`).

Эти два правила дают **два разных набора масок**:

- `train/val/eval` — **supervised** (для fit и scoring): известный исход, purge и embargo выполнены;
- `train_infer/val_infer/eval_infer` — **inference-вселенная**: любое допустимое
  решение в окне сплита, независимо от `label_status`/значения метки (здесь
  embargo действует, purge — нет: purge свойство метки, а не решения).

Инференс обязан идти по вселенной, иначе доступ к сделкам отфильтрован по
будущему исходу. Scoring — только по supervised через отображение
`rel[split]` (позиция supervised-строки внутри infer-массива).

Счётчики строк нового пробега (supervised / inference) публикуются в
`model_manifest.json` как `n_rows_split` и `n_rows_split_infer`.

`model_fit_end_ts` = **max `label_end_ts`** использованных train-строк (не
`decision_ts`: модель знает метки только в момент их наступления);
`available_at_ts` = max(`model_fit_end_ts`, max `label_end_ts` val-строк,
участных в выборе порога) — публикуется в predictions и манифесте.

Препроцессинг (median-impute + z-score) fit **только на train** и трансформирует
val/eval — статистики val/eval не утекают. Финальный eval — temporal
out-of-training, **не** нетронутый holdout проекта (все прежние периоды уже
исследовались).

## 3. Фиксированная матрица (seed 42, без подбора по score)

3 модели × 4 набора признаков = **12 fits**, ровно один прогон:

| model | фиксированные гиперпараметры |
|---|---|
| `logreg` | `LogisticRegression(C=1)` |
| `dtree` | `DecisionTreeClassifier(max_depth=3, min_samples_leaf=200)` |
| `hgb` | `HistGradientBoostingClassifier(max_iter=150, max_leaf_nodes=15, l2_regularization=1, min_samples_leaf=200, early_stopping=False)` |

Классы фиксированы `[down, neutral, up]`; majority-класс берётся из train.
Победитель выбирается **по val log_loss** (первичный критерий). Отбора гиперпараметров
по метрикам нет — перебор только по порогу (см. §4).

## 4. Выбор порога

Для каждой модели на val перебираются `P(up) ∈ {0.50, 0.60, 0.70}`: из порогов,
прошедших фильтр `n_signals ≥ 200` **и** `n_dates ≥ 20`, берётся тот, у кого
**precision максимален** (не максимальный порог — в этом прогоне выиграл 0.50:
precision 0.4993 при 0.60 и 0.70 вообще не набирают ни одного сигнала).
Если ни один не проходит фильтр — `NO_SELECTION`:
**`selected_long` у такой модели равен False на всех строках** (отсутствие
входов, а не скрытый порог 0.5), вероятности при этом сохраняются, а причина
лежит в `threshold_selection.reason` и в колонке predictions
`selection_status`. Порог выбирается **только для победителя**; все 3 попытки и
их precision публикуются в `details` для каждой модели (число попыток поиска = 3).

Расхождение precision между порогами само по себе **не доказательство шума** —
это свойство выборки при падающем числе сигналов; на это прямо указывает §10.

## 5. Экспорт листьев малого дерева

Победитель текущего прогона — `dtree__individual_votes` (см. §9): листья
берутся из его bundle, **без повторного fit**. Для каждого листа публикуются
`node` (id узла — по нему сопоставляются `val_attr`/`eval_attr`), условия
(`feature <= t` / `feature > t`), `support`, `p_label`, `lift` к train base.
Те же фиксированные условия применяются к val и eval (`val_attr`/`eval_attr`) —
это воспроизведение правил, а не повторный подбор.

Дерево обучено на z-score-признаках, поэтому `conditions` — пороги в **z-шкале**.
Рядом публикуется `conditions_raw` — те же пороги, переведённые обратно в
исходные единицы (`raw = z·scale + mean`), иначе условие вроде
`rsi14_5m <= -0.42` нечитаемо и его нельзя сопоставить с индикатором.

Листья берутся из **сохранённого bundle** обученного дерева, без повторного fit
(дерево детерминировано при seed 42, но refit ради извлечения листьев нарушал
бы «один fit на модель»).

## 6. Ablation взаимодействий (нелинейная модель)

Для `hgb` на `continuous_all_tf`: 5 вариантов «выкинуть один ТФ»
(суффиксы `1m/5m/600s/1800s/3600s`; `bias` относится к `3600s`, потому что
считается по часовым). Для каждой — `delta_log_loss` к полной модели на val.
Полная модель **не переобучается**: берётся готовый результат
`hgb__continuous_all_tf` из матрицы (`reused_from_matrix: true`), поэтому
ablation добавляет ровно ≤5 fits и вписывается в лимит архитектора.
Фактическое число прогонов публикуется явно:
`n_fits_matrix` / `n_fits_ablation` / `n_fits_total` в `model_manifest.json`.

Отдельно: **важность признаков сама по себе не доказательство взаимодействия** —
доказательством считается только дельта метрики при удалении целой группы ТФ.

## 7. Заморозка и предсказания

Строго последовательно, три фазы:

1. **train + val**: fit всех 12 моделей, подбор порога на val; каждая модель
   сразу сохраняется в `bundles/<model_id>.joblib` (модель + препроцессор +
   список колонок + схема + порог + `model_fit_end_ts`/`available_at_ts` +
   train-prior). Результатов по eval в этой фазе **нет**.
2. **freeze**: победитель = argmin val log_loss, ablation, затем
   `model_manifest.json` — с `freeze.stage="after_train_val_selection_before_eval"`,
   `eval_evaluated_after_manifest_write: true`, хешем кода, идентичностью
   входных данных (`dataset_identity`), диагностикой join, счётчиками строк и
   fits, sha256 каждого bundle.
3. **read-only eval**: bundles загружаются с диска, инференс идёт оттуда;
   повторная загрузка даёт **те же предсказания** (закрыто тестом
   `test_freeze_manifest_and_bundle_reload`).

`dataset_identity` дополнительно **валит прогон**, если в каталоге данных
появились `SUPERSEDED.txt`/`REVOKED.txt`/`REVOKE.txt`/`REWORK.txt` — работать
на отозванных данных больше нельзя.

Помимо прочего, в manifest этого цикла добавлены блоки, которые архитектор
требовал как отдельную отчётность:

| блок | что фиксирует |
|---|---|
| `policy_eligibility` | политика (будни/сессия/до 18:44), `gate_source = "decision_ts only"`, число отклонённых и причины по сплитам, `b1_eligibility_policy` + `b1_eligibility_policy_hash` + `consistent_with_b1_policy` |
| `weekday_counts_supervised` | число строк по дням недели в каждом сплите; любой `sat`/`sun` > 0 валит прогон |
| `quality_strata` | полнота `n_min_5m` по сплитам, отдельно supervised и inference |
| `excluded_quality_columns` | все metadata-колонки, которые **не** вошли в X |
| `b1_provenance` | dataset/feature versions, `label_cfg_hash`, `code_sha256` B1, `params_hash`, `eligibility_policy_hash`, `whitelist_matches_b1_predictors` |
| `feature_columns` / `feature_set_sizes` | точный замороженный состав признаков каждой модели (восстанавливается из manifest, а не из кода) |
| `primary_policy_sufficiency` | precision порога против `base_up_val` с явным вердиктом «НЕДОСТАТОЧНА», если precision ниже базы |

В `metrics.json` те же данные продублированы в `row_counts`
(`support_known_targets` против `eligible_infer_all_labels`), а в Phase C
добавлены метрики внутри quality-stratum (`<split>_by_stratum`: n, log_loss,
balanced_accuracy, accuracy, base_up).

`predictions.parquet` (потоковая запись, по батчу на модель×набор), 12 колонок:

| колонка | смысл |
|---|---|
| `row_id, figi, decision_ts, model_id, split` | ключ и окно |
| `p_down, p_neutral, p_up` | вероятности, сумма = 1 |
| `selected_long` | вход в long; **False при `NO_SELECTION`** |
| `model_fit_end_ts` | max `label_end_ts` train-строк (когда модель могла быть обучена) |
| `available_at_ts` | max(`model_fit_end_ts`, max `label_end_ts` val-строк выбора порога) |
| `selection_status` | `selected` / `NO_SELECTION` |

Строк в предсказаниях — по числу **inference-вселенной**
(`n_rows_split_infer`), а не supervised: инференс не должен быть отфильтрован
по будущей метке. Строки для scoring — `n_rows_split` (supervised).
As-of-времена детерминированы (из данных, не wall-clock) → повторный прогон
даёт байт-в-байт тот же файл.

## 8. Воспроизводимость и окружение

- `OMP_NUM_THREADS=1` по умолчанию (`ML_OMP_THREADS` — переопределение): на
  4-ядерном хосте многопоточный HGB в ~50× медленнее из-за спавна потоков на
  каждую итерацию (замер: 13.7 с против 0.77 с за 10 итераций). Побочно это
  обеспечивает стабильный порядок reduce → детерминизм.
- признаки приводятся к `float32` (хост 8 ГБ, активный swap) — пик памяти вдвое
  ниже; типы колонок predictions зафиксированы явной pyarrow-схемой, иначе
  float32/float64 из разных моделей расходились бы между батчами.
- память: predictions пишутся потоково (накопление дало бы десятки ГБ), модели
  не держатся в RAM все сразу — каждая сохраняется сразу после fit и заново
  загружается в фазе 3.

---

## 9. Результаты — прогон `20261013T225653Z_v3_full`

Единственный прогон, годный для выводов. Прогон `20261012T1111Z_full` отозван
(нарушено соответствие X↔y), `20261013T194158Z_v2_full` устарел методологически
(`n_min_*` входили в признаки) — их числа здесь не приводятся.

| | |
|---|---|
| run_id | `20261013T225653Z_v3_full` |
| датасет | `b1/20261013T221949Z_v3_full` (`ml_mtf_1_v3`, `1m_mtf_v3`) |
| `label_cfg_hash` | `a898948ff9f0d027` (единственный) |
| `eligibility_policy_hash` (B1) | `b8f7f9cd0498d4b9` |
| `params_hash` (B1) | `7bc511c8862b9fc1a56fbb0a32b60ca774c4e560eb3326c73c5d15a2f856faf7` |
| `identity_sha256` (features) | `74ec8848240c23b239630c33394da36fe1373a2681fd1152faa1b27955a91e7e` |
| `identity_sha256` (targets) | `8b3f4…` (31 файл, содержательно не хэширован) |
| `code_sha256` B1 | `121388e4dd46d575f71507e13f230e3e83da9a36900f5bbf7a32b1b8d48387fa` |
| `code_sha256` B2 | `4891291bd5f84486dbb5e69f7482101da3e444f8c327aaf3ee38ed2cd2691aee` |
| fits | **17** (матрица 12 + ablation 5) |
| строк всего / supervised | 511 159 / 392 693 |
| supervised train/val/eval | 195 088 / 86 295 / 111 310 |
| inference train/val/eval | 277 445 / 102 130 / **131 584** |
| predictions.parquet | 516 695 333 B, **6 133 908** строк (12 × 511 159), 12 колонок |
| `model_fit_end_ts` | 2026-05-29 15:40 UTC |
| `policy_available_at_ts` | 2026-07-31 15:40 UTC |
| freeze | `after_train_val_selection_before_eval`, 12 bundles записаны до eval |

Оба timestamp'а выведены из данных (последний `label_end_ts` train и val),
а не из wall-clock. Purge и embargo на этих данных не удалили ни одной
supervised-строки: сессия заканчивается в 15:40 UTC, окна `[22:00, 00:00)` UTC
пусты, 120-минутный горизонт барьеров не пересекает границы сплитов.
Логика покрыта тестами (`test_purge_*`, `test_embargo_*`).

### 9.0 Контрольные точки конвейера (до матрицы)

| проверка | результат |
|---|---|
| `policy_eligibility.rejected_total` | **0** — B1 уже отфильтровал, gate не нашёл ни выходных, ни решений вне сессии |
| `policy_eligibility.consistent_with_b1_policy` | **true** (источник `b1/…/manifest.json`) |
| `weekday_counts_supervised` sat/sun | **0/0 во всех сплитах** |
| `b1_provenance.whitelist_matches_b1_predictors` | **true** (58 predictor / 20 metadata) |
| `excluded_quality_columns` | 5×`n_min_*` + 9×`voice_status_*` + identity/timing/label — **ни одна не в X** |
| `join_diagnostics` | features 2 557 100 / targets 511 159 / only_in_features 2 045 941 / only_in_targets **0** / `rows_joined` **511 159** / `feature_available_late` **0** |
| `primary_policy_sufficiency` | precision 0.4993 ≥ `base_up_val` 0.4683 → **sufficient = true** (но это не утверждение о прибыльности) |
| `quality_strata` (`n_min_5m`) | supervised: partial = **0 во всех сплитах**; inference: partial 40 514 / 3 387 / 3 392 → неполная 5m-корзина ⇒ `CENSORED`, в scoring не попадает |

### 9.1 Матрица 3×4 (победитель — по val log_loss)

| model | feature_set | n_feat | balacc_val | logloss_val | balacc_eval | logloss_eval | acc_eval | thr |
|---|---|---|---|---|---|---|---|---|
| dtree | **individual_votes** | 18 | 0.3350 | **0.7847** ← победитель | 0.3310 | 0.8094 | 0.4751 | 0.5 |
| logreg | individual_votes | 18 | 0.3341 | 0.7852 | 0.3345 | 0.8089 | 0.4798 | NO_SELECTION |
| hgb | individual_votes | 18 | 0.3370 | 0.7905 | 0.3328 | 0.8157 | 0.4789 | 0.6 |
| logreg | continuous_5m | 7 | 0.3425 | 0.7903 | 0.3358 | 0.8111 | 0.4782 | 0.5 |
| dtree | continuous_5m | 7 | 0.3333 | 0.7951 | 0.3333 | 0.8068 | 0.4780 | NO_SELECTION |
| hgb | continuous_5m | 7 | 0.3395 | 0.8100 | 0.3396 | 0.8125 | 0.4805 | 0.5 |
| logreg | continuous_all_tf | 36 | 0.3405 | 0.7892 | 0.3356 | 0.8348 | 0.4785 | 0.5 |
| dtree | continuous_all_tf | 36 | 0.3348 | 0.7910 | 0.3328 | 0.8128 | 0.4774 | 0.5 |
| hgb | continuous_all_tf | 36 | 0.3392 | 0.8138 | **0.3411** | 0.8154 | **0.4895** | 0.7 |
| logreg | all_tf_votes | 54 | 0.3407 | 0.7896 | 0.3379 | 0.8358 | 0.4788 | 0.5 |
| dtree | all_tf_votes | 54 | 0.3309 | 0.7895 | 0.3343 | 0.8113 | 0.4808 | NO_SELECTION |
| hgb | all_tf_votes | 54 | 0.3396 | 0.8128 | 0.3388 | 0.8180 | 0.4859 | 0.7 |

Случайный трёхклассовый классификатор: balacc 0.3333.
`n_feat` — фактически вошедшие в матрицу колонки (whitelist даёт 22/7/36/58;
4 all-NaN колонки `vote_vwap_reversion`, `vote_kama_trend`,
`age_vwap_reversion`, `age_kama_trend` отбрасываются доводчиком, и это
фиксируется в `dropped_all_nan`).

**Честные находки:**

1. **Победитель не переносится на eval.** `dtree__individual_votes` выигрывает
   по val log_loss, но на eval он **ниже случайного** по balacc (0.3310 <
   0.3333) и хуже обеих базовых линий (см. 9.2). Критерий отбора был
   зафиксирован до прогона и не менялся — это свойство отбора, а не дефект кода.
2. **Лучший по eval — `hgb__continuous_all_tf`** (balacc 0.3411, acc 0.4895),
   но он выбирался не по этому критерию, и его порог 0.7 даёт всего 561 сигнал
   на val. Перевыбор первичной модели после eval запрещён протоколом.
3. **Взаимодействия ТФ не дают прироста.** Paired `all_tf vs 5m` на eval:
   logreg Δbalacc −0.0002 / Δlogloss **+0.0237** (хуже), dtree −0.0006 / +0.0059,
   hgb +0.0015 / +0.0029. То есть добавление ТФ к 5m-набору статистически
   неразличимо на eval — гипотеза ветки о взаимодействиях ТФ **не подтверждена**.

### 9.2 Базовые линии (победитель `dtree__individual_votes`)

Train-prior: {down 0.5247, neutral 0.0287, up 0.4466}; majority = down.

| split | balacc | log_loss | acc | brier | prior_log_loss | majority_acc | ll − prior | acc − majority | base_up |
|---|---|---|---|---|---|---|---|---|---|
| train | 0.3374 | 0.7984 | 0.5273 | 0.5234 | — | — | **−0.00182** | +0.00257 | 0.4466 |
| val | 0.3350 | 0.7847 | 0.5099 | 0.5213 | — | — | **−0.00017** | +0.00050 | 0.4683 |
| eval | 0.3310 | 0.8094 | 0.4751 | 0.5326 | — | — | **+0.00119** | **−0.00291** | 0.4931 |

**На eval модель хуже и train-prior, и majority.** Положительный знак
`ll − prior` означает, что log_loss модели больше, чем у константы-приора.
Это сильнейшее свидетельство против работоспособности выбранной
первойичной модели на горизонте вне обучения — и оно получено без всякого
учёта издержек.

### 9.3 Пороги (val supervised)

| model_id | 0.50 (сиг/дат/prec) | 0.60 | 0.70 | выбран |
|---|---|---|---|---|
| **dtree__individual_votes** (победитель) | **4176 / 45 / 0.4993** | 0 / — / — | 0 / — / — | 0.50 |
| dtree__continuous_all_tf | 1279 / 21 / 0.5309 | 1279 / 21 / 0.5309 | 1279 / 21 / 0.5309 | 0.50 |
| dtree__all_tf_votes | 0 | 0 | 0 | **NO_SELECTION** |
| dtree__continuous_5m | 0 | 0 | 0 | **NO_SELECTION** |
| logreg__individual_votes | 32 / 5 / 0.6875 | 0 | 0 | **NO_SELECTION** |
| logreg__continuous_5m | 1029 / 45 / 0.4684 | — | — | 0.50 |
| logreg__continuous_all_tf | 3655 / 45 / 0.4802 | — | — | 0.50 |
| logreg__all_tf_votes | 4703 / 45 / 0.4888 | 99 / 24 / 0.3838 | 7 / 5 / 0.1429 | 0.50 |
| hgb__continuous_5m | 8012 / 45 / 0.4819 | 282 / 40 / 0.4468 | 4 / 4 / 0.2500 | 0.50 |
| hgb__individual_votes | 13002 / 45 / 0.4872 | 1164 / 44 / 0.5086 | 40 / 13 / 0.6750 | 0.60 |
| hgb__continuous_all_tf | 18235 / 45 / 0.4790 | 3377 / 45 / 0.4842 | 561 / 38 / 0.5276 | 0.70 |
| hgb__all_tf_votes | 19517 / 45 / 0.4788 | 4065 / 45 / 0.5109 | 644 / 37 / 0.5435 | 0.70 |

Правила отбора порога не менялись: ≥200 сигналов **и** ≥20 дат, иначе
`NO_SELECTION` → `selected_long = False` всегда (не порог 0.5).
`logreg__individual_votes` выглядит заманчиво (precision 0.6875), но это
32 сигнала на 5 дат — правило 200/20 его справедливо отбрасывает.
У `dtree__continuous_all_tf` все три порога дают идентичные числа —
вырожденные листья при `max_depth=3, min_samples_leaf=200`.

### 9.4 Precision на supervised-строках

Соединение `predictions` ↔ `targets` по `row_id`: 4 712 316 = 12 × 392 693 ✓.
`edge = precision − base_up` той же модели на том же сплите.

| model_id | val: n / prec / edge | eval: n / prec / edge |
|---|---|---|
| **dtree__individual_votes (победитель)** | 4176 / 0.4993 / **+0.031** | 3693 / 0.4449 / **−0.048** |
| dtree__continuous_all_tf | 1279 / 0.5309 / +0.063 | 1089 / 0.4509 / **−0.042** |
| dtree__all_tf_votes | 0 (NO_SELECTION) | 0 |
| dtree__continuous_5m | 0 (NO_SELECTION) | 0 |
| logreg__individual_votes | 0 (NO_SELECTION) | 0 |
| logreg__continuous_5m | 1029 / 0.4684 / +0.000 | 967 / 0.4819 / −0.011 |
| logreg__continuous_all_tf | 3655 / 0.4802 / +0.012 | 4125 / 0.4919 / −0.001 |
| logreg__all_tf_votes | 4703 / 0.4888 / +0.021 | 5336 / 0.4955 / +0.002 |
| hgb__individual_votes | 1164 / 0.5086 / +0.040 | 881 / 0.4472 / **−0.046** |
| hgb__continuous_5m | 8012 / 0.4819 / +0.014 | 9616 / 0.5047 / **+0.012** |
| hgb__continuous_all_tf | 561 / 0.5276 / +0.059 | 468 / **0.5321** / **+0.039** |
| hgb__all_tf_votes | 644 / 0.5435 / +0.075 | 791 / 0.4766 / **−0.017** |

Базовые уровни `base_up`: train 0.4466, val 0.4683, eval 0.4931.
Колонка train (строки fit) приведена в `summary.md` и **не является
доказательством**: hgb там даёт 0.95–0.98 precision на 627–842 строках —
это переобучение, а не сигнал.

**Главный результат:** знак edge у победителя **меняется между val и eval**
(+0.031 → −0.048). Положительные на обоих сплитах остаются только
`hgb__continuous_all_tf` (+0.059 / +0.039, n = 561 / 468) и
`hgb__continuous_5m` (+0.014 / +0.012, n = 8012 / 9616) — оба ниже потолка
издержек (см. 9.8) и оба являются одним из 12×3 = 36 комбинаций
модель×набор×порог, т.е. результатом мульти-теста.

### 9.5 Ablation (val log_loss, `full` переиспользован из матрицы)

База: `hgb__continuous_all_tf`, 36 признаков, `reused_from_matrix = true`.

| вариант | n_feat | val log_loss | Δ к full | val balacc |
|---|---|---|---|---|
| full | 36 | 0.8137953 | — | 0.3392 |
| drop 1m (`tf60`) | 29 | 0.8149912 | **+0.001196** | — |
| drop 5m (`tf300`) | 29 | 0.8121839 | **−0.001611** | **0.3411** |
| drop 10m (`tf600`) | 29 | 0.8134360 | −0.000359 | — |
| drop 30m (`tf1800`) | 29 | 0.8141887 | +0.000393 | — |
| drop 60m (`tf3600`) | 28 | 0.8136013 | −0.000194 | — |

Все |Δ| ≤ 0.0016, т.е. **ни один таймфрейм не влияет**; удаление 5m-набора
даже немного улучшает результат. `drop_tf3600` уносит и `bias` (он живёт на
60m), поэтому 28 вместо 29. Число fits посчитано явно: 12 + 5 = 17,
`full` не дообучался.

### 9.6 Листья дерева — артефакт покрытия устранён

В прогоне v2 корневым признаком была `n_min_3600s` (плотность данных).
В v3 корень — **`macd_hist_delta_1800s ≤ 0.0479908`**, рыночный признак;
другие условия — `rsi14_1800s ≤ 35.8648`, `age_rsi_reversal ≤ 1367.5`,
`rsi14_3600s ≤ 89.6426`. Whitelist отработал: в `conditions_raw` нет ни
одной `n_min_*`/`voice_status_*` колонки, условия выгружены в исходной
(натуральной) шкале.

`kept_features` 54, `dropped_all_nan` = [vote_vwap_reversion, vote_kama_trend,
age_vwap_reversion, age_kama_trend]. Примеры листьев (model `dtree__all_tf_votes`):
node3 sup 18 805, P(up) 0.4978, lift 1.11; node4 sup 9 430, P(down) 0.5178;
node6 sup 134 748, P(down) 0.5253. Lift ≈ 1.1 — это слабое разделение,
что и объясняет нулевой edge.

### 9.7 Разбивка по тикерам и месяцам (eval, supervised)

По 30 тикеров в каждом наборе; месяцы: 2026-08 n=48 344, 2026-09 n=58 084,
2026-10 n=4 882.

| model_id | тикеров >0.40 balacc | тикеров <0.3333 | топ-5 | низ-5 |
|---|---|---|---|---|
| **dtree__individual_votes** (победитель) | **0** | **22 из 30** | SNGSP 0.3372, SMLT 0.3366, PHOR 0.3344, PLZL 0.3341, AFKS 0.3338 | LKOH 0.3258, MAGN 0.3263, GAZP 0.3270, ALRS 0.3275, ROSN 0.3279 |
| hgb__continuous_all_tf | **0** | 8 из 30 | SFIN 0.3723, OZON 0.3592, VTBR 0.3586, SBER 0.3580, AFKS 0.3571 | MVID 0.3234, RUAL 0.3248, ALRS 0.3250, GMKN 0.3251, YDEX 0.3252 |
| hgb__continuous_5m | **0** | 16 из 30 | T 0.3881, OZON 0.3829, GMKN 0.3749, NLMK 0.3537, SFIN 0.3492 | RNFT 0.3147, ALRS 0.3190, YDEX 0.3240, MVID 0.3257, MTSS 0.3258 |

Помесячно (eval balacc): победитель 0.3260 / 0.3353 / 0.3316;
`hgb__continuous_all_tf` 0.3419 / 0.3404 / 0.3460;
`hgb__continuous_5m` 0.3476 / 0.3329 / 0.3351.

**Ни одна модель не даёт ни одного тикера с balacc > 0.40 на eval.**
Любые пер-тикерные утверждения допустимы только как кандидаты под
мульти-тест, а не как найденный эффект (30 тикеров × 12 моделей = 360 попыток).

### 9.8 Прибыльность: потолок издержек

Симметричные барьеры ±1.5 ATR дают

`EV ≈ 1.5·ATR·(2p − 1) − cost`,  ⇒  `p_be = 0.5 + cost / (3·ATR)`.

При roundtrip 14 bps и ATR ≈ 1%: **p_be ≈ 0.547**.

| кандидат | precision eval | vs p_be |
|---|---|---|
| победитель `dtree__individual_votes` | 0.4449 | **ниже на 0.102** |
| лучший вообще `hgb__continuous_all_tf` | 0.5321 | **ниже на 0.015** |
| `hgb__continuous_5m` (самый массовый) | 0.5047 | ниже на 0.042 |

**Ни одна конфигурация не проходит порог издержек на eval.** Это
**честный отрицательный результат**: на этой матрице, этом сплите и этих
издержках edge не подтверждён. Это НЕ утверждение, что edge не существует, —
оно не проверялось на других семействах признаков и других горизонтах.

---

## 10. Ограничения

1. **eval — не holdout проекта**, а временной выход за пределы обучения
   (2026-08-01…10-08). Он был прочитан один раз, после записи manifest.
2. **Метка ≠ прибыль.** Положительный исход triple_barrier — не результат
   торговли. Стоимость (≈14 bps roundtrip) в метриках не участвует;
   §9.8 — отдельный аналитический расчёт, не бэктест.
3. **CENSORED ≠ ноль.** 118 466 строк (23.2 %) не имеют исхода; они есть
   в inference universe и отсутствуют в scoring. Это скрытый фильтр
   допуска к сделкам, который теперь открыт: `n_rows_split_infer` ≠
   `n_rows_split`. Причина `future_bucket_incomplete` (41 209) — как раз тот
   дефект «метка достоверна при неполной будущей свече», который закрыл B1:
   такие строки теперь честно CENSORED, а не «достоверные».
4. **Отбор и порог подобраны на val** (12 моделей × 3 порога). Никакой
   компенсации множественных сравнений не делается — все 36 сочетаний
   опубликованы.
5. **Критерий отбора не переносится**: победитель по val log_loss на eval
   ниже случайного по balanced accuracy (0.3310 < 0.3333) и хуже обеих
   базовых линий, хотя по log_loss остаётся чуть лучше `hgb` (§9.1, §9.2).
6. **Артефакт покрытия устранён, но остался риск мотивации**: в прогоне v2
   корнем была `n_min_3600s`; в v3 `n_min_*` исключены whitelist'ом и корень
   рыночный (`macd_hist_delta_1800s`), §9.6. Остаётся то, что сам датасет
   уже отфильтрован B1 по `eligibility_policy`, и этот фильтр невозможно
   снять постфактум.
7. **Ablation маломощен**: эффекты ≤ 0.0016 log_loss (§9.5); отсутствие
   эффекта не доказывает отсутствие взаимодействия.
8. **`calibration_up` — не калибровка** (среднее p_up при истинном up
   минус база). Настоящая калибровка — `reliability_bins_up` +
   `brier_multiclass` в metrics.json.
9. **`tree_leaves.json` и `breakdown.json` — пост-фактум артефакты**,
   записанные после freeze; выбор их не влиял на отбор, но и не является
   подтверждением гипотез.
10. **4 признака выпадают как all-NaN** в `all_tf_votes`:
    `vote_vwap_reversion`, `vote_kama_trend`, `age_vwap_reversion`,
    `age_kama_trend` (по одному на набор в остальных).
11. **Гиперпараметры не подбирались** — по договорённости (матрица
    фиксирована, seed 42). Любые улучшения потребуют нового зафиксированного
    плана.
12. **Датасет B1 `phase=development_unvalidated`, corporate actions
    UNVERIFIED** — дивиденды/сплиты могут искажать и метки, и ATR.
13. **Одна единица наблюдения = 5m-решение одного тикера**; 511 159 строк
    сильно скоррелированы внутри дня и внутри тикера — стандартные ошибки
    завышены, если считать их независимыми.
14. **Покрытие данных неравномерно** (B1: uneven input coverage), но
    `n_min_*` в X больше не попадают: они сидят в `excluded_quality_columns`
    и служат только quality-stratum. Известно, что неполная 5m-корзина
    ⇒ `CENSORED`, т.е. попадание в scoring само по себе уже отфильтровано
    по полноте данных (§9.0). Остаточный риск: whitelist не убирает
    корреляцию между полнотой данных и тикером/периодом.

---

## 11. Артефакты (run `20261013T225653Z_v3_full`)

Каталог: `backend/reports/architect_20261012/ml_mtf_1/b2/20261013T225653Z_v3_full/`
(лог рядом: `…/b2/20261013T225653Z_v3_full.log`).

| файл | содержимое |
|---|---|
| `model_manifest.json` (13 042 B) | freeze-блок, split, fit/avail, `b1_provenance`, `policy_eligibility`, `weekday_counts_supervised`, `quality_strata`, `excluded_quality_columns`, `feature_columns`/`feature_set_sizes`, `primary_policy_sufficiency`, sha256 всех bundles |
| `metrics.json` (152 941 B) | manifest + все 12 results (train/val/eval + per-stratum) + paired + `row_counts` |
| `bundles/` (12 joblib) | модель + импьютер/скейлер + список колонок + порог + статус отбора; перезагрузка даёт байт-в-байт те же предсказания |
| `predictions.parquet` (516 695 333 B) | 6 133 908 строк × 12 колонок = inference-вселенная всех 3 сплитов |
| `ablation.json` (2 010 B) | 5 drop-TF fits, `full` переиспользован |
| `tree_leaves.json` (8 037 B) | листья с `conditions_raw` (натуральная шкала) |
| `breakdown.json` (971 460 B) | по тикерам и месяцам, val/eval |
| `summary.md` (2 627 B) | краткая сводка для человека |

Схема `predictions.parquet` (12 колонок, порядок фиксирован):
`row_id, figi, decision_ts, model_id, split, p_down, p_neutral, p_up,
selected_long, model_fit_end_ts, available_at_ts, selection_status`.

### Фильтры для B3 (точные)

Все строки одного набора:

```
split == "<split>" AND model_id == "<model_id>"
```

- **первичная модель**: `model_id = "dtree__individual_votes"`,
  `selection_status = "selected"`, `threshold = 0.50` → входы только там,
  где `selected_long == True`; `fit_end = 2026-05-29 15:40 UTC`,
  `available_at = 2026-07-31 15:40 UTC`.
- **лучшая линейная модель**: `model_id = "logreg__continuous_5m"`
  (val log_loss 0.7903 — минимум среди logreg), `threshold = 0.50`.
- **5m-компаратор**: `model_id = "hgb__continuous_5m"`, `threshold = 0.50`.
- **NO_SELECTION-модели** (`dtree__all_tf_votes`, `dtree__continuous_5m`,
  `logreg__individual_votes`) → передаются как **наличные деньги без входов**;
  изобретать им записи нельзя.

Числа для контроля сверки:

| величина | значение |
|---|---|
| supervised (support known targets) train/val/eval | 195 088 / 86 295 / 111 310 |
| inference (eligible infer, все метки) train/val/eval | 277 445 / 102 130 / 131 584 |
| строк в `predictions.parquet` | 6 133 908 = 12 × 511 159 |
| supervised-строк в `predictions` | 4 712 316 = 12 × 392 693 |
| набор признаков | whitelist 22 / 7 / 36 / 58; реально в матрице 18 / 7 / 36 / 54 |

### Воспроизведение

```bash
cd backend && OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 .venv/bin/python \
  scripts/ml_mtf_learn.py \
  --features reports/architect_20261012/ml_mtf_1/b1/20261013T221949Z_v3_full/features \
  --targets  reports/architect_20261012/ml_mtf_1/b1/20261013T221949Z_v3_full/targets \
  --out reports/architect_20261012/ml_mtf_1/b2/<new_run_id> --no-chat

cd backend && OMP_NUM_THREADS=1 .venv/bin/python -m pytest tests/test_ml_mtf_learn.py -q
```

Активных предупреждений о неполном датасете нет; `SUPERSEDED/REVOKED`-маркеры
в исходном датасете отсутствуют (иначе запуск жёстко прерывается).
