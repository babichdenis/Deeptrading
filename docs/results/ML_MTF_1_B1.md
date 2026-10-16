# ML-MTF-1 / B1 — полный датасет «признаки × метки» (контракты v1 → v2 → v3)

> **АКТУАЛЬНО: версия v3 (раздел 8 ниже).** Разделы 1–6 — отозванный v1
> (`20261012T112650Z_full`, код `13b24930…`); раздел 7 — v2 (`20261013T164233Z_v2_full`, код `650070e3…`),
> тоже отозван (включал выходные). Рабочий артефакт — `20261013T221949Z_v3_full` (раздел 8).

**Builder-1**, ветка ML-MTF-1, 2026-10-12.
Зона: `backend/scripts/ml_mtf_dataset.py`, `backend/tests/test_ml_mtf_dataset.py`,
артефакты `backend/reports/architect_20261012/ml_mtf_1/b1/<run_id>/`.

Вопрос ветки для B1: построить воспроизводимый **полный датасет функций × разметки**
на всех таймфреймах (1/5/10/30/60 м) + индивидуальных голосах ensemble, на котором
B2 ищет взаимодействия, а B3 проверяет экономический результат. Датасет offline:
не пишет в БД, не трогает live/execution.

---

## 1. Команда и артефакт

Исполняемая команда (полный export, окно по контракту):

```bash
cd backend
.venv/bin/python scripts/ml_mtf_dataset.py \
  --snapshot reports/architect_20261011/research_core_v2/b1/20261011T1947Z/snapshot \
  --start 2025-10-01T00:00:00+00:00 --end 2026-10-08T00:00:00+00:00 \
  --out <RUN_DIR> --resume
```

- Каталог артефакта (DATA_READY): `backend/reports/architect_20261012/ml_mtf_1/b1/20261012T112650Z_full/`
- Код: `backend/scripts/ml_mtf_dataset.py` sha256 `13b24930d5…` (полный в `manifest.json`)
- Тесты: `backend/tests/test_ml_mtf_dataset.py` — 12 passed
- `params_hash` (код+окно+версии) и все счётчики — в `manifest.json`
- Runtime ≈ 50 мин (шард на тикер + `--resume`; один тикер в памяти), диск ≈ 561 МБ
- 30/31 тикеров ok; **HHRU = `no_bars`** (0 строк m1 в snapshot) — отражено в manifest

## 2. Контракт данных v1 (реализовано)

| пункт контракта | реализация |
|---|---|
| наблюдение = закрытый 5m бар | `decision_ts` = закрытие бара; `feature_max_available_ts` = `decision_ts` |
| features/targets раздельно | подкаталоги `features/`, `targets/`, `meta/`; шард на тикер; manifest перечисляет файлы |
| join строго по `row_id` | `row_id = sha256(figi|decision_ts|dataset_version)`; порядок в features = targets |
| primary target | canonical `triple_barrier` (take=stop=1.5ATR, max_bars=24, ATR14), stop-first при двойном касании |
| `label_status` | **`MATURE` / `CENSORED`** (верхний регистр по контракту); CENSORED ⇒ `label=null` (не 0) |
| supervised | только `MATURE`; CENSORED в обучение не попадает и нулём не подменяется |
| ТФ признаков | 60/300/600/1800/3600 с; `tf=300` накладывается на закрытые 5m-бары метки |
| признаки | RSI14, MACD12/26/9 hist+Δ, EMA50 dist/slope, ATR14/price, volr20 ×5 ТФ; `bias` (hourly, period50, закрытый час); 9 голосов×(vote,age) + voice_buy/sell/quorum/conflict |
| закрытость/прогрев | source bar END ≤ decision_ts; m1 до `start−10д`; строки только с `decision_ts ≥ start` |
| lookahead голосов | `generate_signals.ts` = начало бара ⇒ голос доступен с `ts+tf` (поймано тестом future-bar perturbation) |
| missing/zero | → `null` (например `volr20_5m` при нулевом объёме); не закрывается синтетической ценой/ATR |
| determinism | повторный прогон даёт те же `row_id` и значения; `--resume` пропускает тикеры с совпавшим `params_hash` |

Полный список колонок и типов: `feature_schema.json` и `manifest.json`
(`feature_columns` 64 / `target_columns` 8).

## 3. Цифры (валидировано 2026-10-12)

| метрика | значение |
|---|---|
| feature_rows = target_rows | 1 594 155 |
| supervised (`label_status=MATURE`) | 1 593 838 |
| `CENSORED` (`label=null`) | 317 |
| тикеров ok | 30 / 31 (HHRU = no_bars) |
| метки MATURE | +1 = 678 380, 0 = 157 738, −1 = 757 720 |
| bias | −1 = 871 115, +1 = 722 906, null = 134 |
| разрезы по `decision_ts` (без embargo) | train 924 808 / val 275 716 / eval 393 314 |
| min `decision_ts` | 2025-10-01T04:00Z (≥ start) |

Совпадает со split в `ml_mtf_learn.py::temporal_split` и отчётом B2. embargo+purge
применяет потребитель по `label_end_ts`.

## 4. Правильность (focused-регрессии, 12 тестов)

1. Closed-only по каждому ТФ; правка баров **после** `decision_ts` не меняет X (побайтово).
2. 10m/30m признаки присутствуют и конечны (иерархический ресемпл 5m 300→600/1800, побайтово равно прямому).
3. Maturity/stop-first: двойное касание = stop-first; хвост без горизонта = CENSORED.
4. Missing/zero → null; all-NaN-колонки не сдвигают имена признаков.
5. Два противоположных голоса → conflict-флаг, без BUY-приоритета.
6. Join по `row_id`, схемы совпадают с манифестом, дублей нет, `--resume` идемпотентен.
7. Прогрев: первый отданный бар тёплый (индикаторы не NaN).
8. Layout: `features/` и `targets/` разъединены; `feature_schema.json` эмитится.

## 5. Исправления интеграции (2026-10-12, по сообщению B2/архитектора)

Найдено на реальных данных B1 и исправлено в источнике (не workaround-ом потребителя):

1. **Регистр `label_status`**: писалось `mature`/`censored` строчными, контракт — `MATURE`/`CENSORED`.
   Исправлено в билдере; артефакт пересобран (значения бит-в-бит те же, меняется только enum-case).
2. **Каталог**: `features_*`/`targets_*`/`meta_*` лежали в одном плоском каталоге, что ломало
   слепой glob-merge (`0 строк`). Исправлено: раздельные подкаталоги `features/`, `targets/`, `meta/`;
   manifest перечисляет относительные пути.
3. Добавлен обязательный контрактом `feature_schema.json` (полный список признаков/типов/конфигов).

## 6. Границы и что мешает следующему этапу

- Покрытие ранних месяцев неравномерное: полный год m1 есть только у 17 тикеров
  (GAZP/LKOH с 2026-01, TATN 2026-06, ALRS/PLZL 2026-07, RNFT 2026-08, часть с 2026-08-22; VKCO/YDEX с 03-03).
  Это свойство входного snapshot, не ошибка датасета.
- Corporate actions UNVERIFIED (наследовано из snapshot) — возможны дыры меток у дивидендов/сплитов.
- `phase=development_unvalidated` — результаты исследовательские.
- Сплиты/embargo применяет потребитель по `label_end_ts`; датасет сырой + `label_end_ts`.

---

## 7. v2 — REWORK архитектора исполнен (2026-10-13)

**Статус: REWORK (6 пунктов) исполнен, датасет пересобран, DATA_READY. v1 отозван.**

- Артефакт (полный export): `backend/reports/architect_20261012/ml_mtf_1/b1/20261013T164233Z_v2_full/`
- Shard для B2/B3 (schema, SBER/GAZP × 2026-09): `.../20261013T163418Z_v2_schema/`
- Код: `backend/scripts/ml_mtf_dataset.py` sha256 `650070e36b36ae0413bf370b3fdf54a4e79e2ce965ab071abd76df055dc3976a`
- Тесты: `backend/tests/test_ml_mtf_dataset.py` — **20 passed**
- Версии: `dataset_version = ml_mtf_1_v2`, `feature_schema_version = 1m_mtf_v2`
- Окно: start `2025-10-01T04:00:00+00:00`, end `2026-10-09T00:00:00+00:00` (end-exclusive, 8 окт включён)
- Хэши: `params_hash = 614baefbc7e1272a22f2a3bd2f7e5a688ff80f96f39170fce7683c62a6421760`,
  `label_cfg_hash = a898948ff9f0d027`, `input_manifest_sha256 = 9964b08ff83a3820efb04862fb09e0ae31653a211a759f6360b91ad999681577`
- Диск ≈ 642 МБ; `--validate` → `ok: true, errors: [], warnings: []`

### 7.1 Что исправлено (по пунктам REWORK)

1. **Сдвиг метки.** Метка привязывается по `lr["index"]` канонического лейблера, а не по позиции
   строки в ресемпле. Совпадение бар-признак ↔ бар-метка: канон `08:10` ↔ экспорт `08:10`
   (в v1 было `07:05`), `canonical_index 13`. Покрыто тестом `test_label_alignment_canonical_index`.
2. **`label_end_ts` = CLOSE бара t1 + сессионный путь.** Целевой путь ограничен main-окном
   `10:00–18:40 MSK`; горизонт wall-clock 120 мин (24 × 5м), без сжатия гэпов; выход за границу
   → `CENSORED` с причиной (`session_cutoff` при пересечении дня/закрытия, `gap` при пропуске слота
   внутри дня, `anchor_incomplete` при неполном 5м-баре-якоре < 5 мин). ATR прогрет сквозь сессии
   каноническим `indicatorhub._atr`. Тест: time-exit = `decision_ts + 120 мин`; overnight → CENSORED.
3. **Полнота ТФ через coverage.** Для каждой корзины считается `n_min_<tf>` (число 1m-минут в
   `[bar_start, bar_start+tf)`); неполная корзина → все 7 непрерывных ячеек `None` + `n_min`.
   Прогревы задекларированы: ATR→idx 13, EMA50→idx 49, RSI14→idx 14, volr20→idx 19.
   Переиспользованы канонические индикаторы (`app.engine.indicatorhub`); локальные дубли удалены.
4. **Голоса не протухают.** Срок — из замороженного конфига ensemble `FRESH_MIN=30`
   (`voice_fresh_min` в манифесте). `vote_<sid>` = только свежие активные голоса, `age_<sid>` — отдельно
   (возраст независим от активности), `voice_status_<sid>` = `ok` / `error:<Class>` (ошибка стратегии
   видима, а не «нет голоса»). id 9 стратегий сверены с реальными.
5. **Версии/хэши/immutable.** `row_id = sha256(figi|decision_ts|dataset_version)`; входные хэши
   (snapshot manifest + per-ticker CSV + module shas) в `manifest.json` и в resume-ключе `meta_<T>.json`
   (`status`/`params_hash`/`csv_sha256`); прогон immutable — иной `params_hash` без `--resume/--force` → отказ.
6. **Ветка доделана.** `source_inventory.json` (read-only инвентарь БД/parquet + rationale, 4009 Б)
   в каталоге run; признаки — на КАЖДОМ 1m-решении, управляемые метки — ТОЛЬКО на закрытиях 5м;
   shard + schema отданы B2/B3 ДО полного export.

### 7.2 Цифры v2 (`20261013T164233Z_v2_full`)

| метрика | значение |
|---|---|
| feature_rows (1m-решения) | **3 426 902** |
| target_rows (5m-закрытия, эмитированные) | 685 334 |
| supervised (`MATURE`, 1:1 join) | **527 952** |
| CENSORED | 157 382 |
| причины CENSORED | `anchor_incomplete` 114 598 / `session_cutoff` 41 182 / `gap` 1 602 |
| `skipped_no_1m_anchor` (5м-бар есть, закрывающей 1m-минуты нет) | 97 331 |
| тикеров ok | **30 / 31** (HHRU = `no_bars`, 0 строк m1) |
| метки MATURE | +1 = 232 898, 0 = 35 150, −1 = 259 904 |
| `decision_ts` min/max | 2025-10-01T07:05Z .. 2026-10-08T15:40Z (10:05 .. 18:40 MSK) |

Схема: 78 feature-колонок (6 мета + 35 непрерывных = 7 × 5 ТФ + 5 `n_min_<tf>` + `bias`
+ 27 голосовых = 9 sid × {`vote_`,`age_`,`voice_status_`} + `voice_buy/sell/quorum/conflict`);
9 target-колонок. Суффиксы ТФ: **`_1m`, `_5m`, `_600s`, `_1800s`, `_3600s`**
(`tf_seconds = [60,300,600,1800,3600]`). В features НЕТ колонки `label`; join строго по `row_id`.

`CENSORED`: `anchor_incomplete` доминирует на разреженных тикерах (ASTR/MVID/SFIN — десятки тысяч
пропущенных подряд 1m-минут в snapshot), `session_cutoff` — решения после ~16:40 MSK, чей
120-мин горизонт упирается в закрытие сессии (≈23 % от всех решений — свойство сессионного контракта).

### 7.3 Проверки v2

- `--validate` полного run: `ok: true`, `errors: []`; join 1:1 проверен для SBER/GAZP на полном run
  (`only_in_targets=0`, дублей `row_id` нет), 78/9 колонок.
- На shard: MATURE `label_end_ts − decision_ts ∈ [5, 120]` мин; `CENSORED ⇒ label_end_ts = None`
  (0 нарушений); частичная корзина ⇒ 7 ячеек `None` (0 нарушений из 6 607 SBER + 3 528 GAZP);
  `feature_max_available_ts == decision_ts` (нет будущего).
- 20 focused-тестов (в т.ч. регрессии на выравнивание метки, ATR-невалидность без сдвига,
  stop-first −1, overnight→session_cutoff, gap→CENSORED, частичный якорь, канонические прогревы
  13/49/14, stale/fresh голос + `error:RuntimeError`, 1m/5m join CLI, стабильность fingerprint,
  immutable (`SystemExit` при смене params, `--force` работает), resume при смене CSV, closed-only,
  sparse-anchor без 1m-закрытия, bias, отсутствующий тикер).
- `ml_mtf_learn.load_contract(features, targets)` (потребитель B2): 1:1 join, `only_in_targets=0`,
  `feature_available_late=0`, един `label_cfg_hash`.

### 7.4 Уточнения к сообщению DATA_SCHEMA_READY

- Суффиксы ТФ: `_1m/_5m/_600s/_1800s/_3600s` (в посте были `_10m/_30m/_60m` — неверно).
- Голосовых колонок 27 (9 стратегий × 3), а не 7×3.
- Финальный `code_sha256 = 650070e3…` (после фикса БАГА 3: пустой тикер HHRU ломал unpack);
  `params_hash` shard = `85ff5b55…` — он включает окно+start, поэтому у shard и полного run
  он РАЗНЫЙ ПО ДИЗАЙНУ; сверять надо `label_cfg_hash` + `code_sha256` + `modules`.
- `HHRU`: `status=no_bars`; в `features/` остаётся пустой `features_HHRU.parquet` (0 строк),
  в `manifest.parts` он не входит.

### 7.5 Границы (унаследовано и новое)

- Покрытие входного snapshot неравномерно: полный год m1 только у ~17 тикеров
  (GAZP/LKOH с 2026-01, TATN 2026-06-24, ALRS/PLZL 2026-07-15, RNFT 2026-08-06, AFKS/OZON/PHOR/SIBN/VTBR 2026-08-22,
  VKCO/YDEX 2026-03-03, HHRU 0 строк) — свойство snapshot, не ошибка датасета.
- Corporate actions UNVERIFIED (наследовано из snapshot).
- `phase=development_unvalidated` — результаты исследовательские.
- Сплиты/purge/embargo применяет потребитель (B2) по `label_end_ts` (embargo 120 мин).

---

## 8. Версия v3 (приёмка 2026-10-14) — исправления выходных и будущих корзин

### 8.1 Что исправлено (по 4 пунктам приёмки архитектора)

1. **Единая eligibility-policy ветки** — один predicate `_in_session(ts)` (будни `_WEEKDAYS=(0..4)`
   И главная сессия `10:00 ≤ MSK < 18:40`, решения строго `< 18:44` MSK) применяется ОБОИМ входам:
   и признакам (`_continuous_features_1m`), и меткам (`_session_label_rows`). Политика зафиксирована
   константой `ELIGIBILITY` и хэшем `ELIGIBILITY_POLICY_HASH = b8f7f9cd0498d4b9` (sha256[:16]),
   публикуется в `manifest.json` и `feature_schema.json`. B2/B3 применяют её как defensive gate.
   Выходные — явное ограничение исследования: субботние/воскресные свечи в snapshot полноценные
   (объём>0, 1440 мин/сутки, `source=db`), их в снапшоте даже больше, чем будних; БД не чистится,
   рыночные данные ошибочными не объявляются.
2. **Completeness каждой будущей 5m-корзины до low/high** — в цикле слота сначала проверяется
   `n_min5[j] < SUPERVISED_TF//60`, и только потом барьеры. Неполная будущая корзина →
   `CENSORED(label=null, label_end_ts=null, reason=future_bucket_incomplete)`. Ранний hit ДО первой
   неизвестной корзины остаётся MATURE (реализованный выход). Timeout по 24 слотам — только если все 24
   корзины полные. Якорь по-прежнему проверяется отдельно (`anchor_incomplete`). ATR/close-time/index —
   канонические, синтетические свечи не вставляются.
3. **Новые версии и роли колонок** — `DATASET_VERSION=ml_mtf_1_v3`, `FEATURE_SCHEMA_VERSION=1m_mtf_v3`,
   `row_id=sha256(figi|decision_ts|version)`; политика eligibility входит в `params_hash`.
   Обучаемый X = только рыночные непрерывные (35) + `bias` + индивидуальные голоса (9 `vote_`,
   9 `age_`, `voice_buy/sell/quorum/conflict`) = **58 predictors**. Quality metadata
   (6 identity + 5 `n_min_<tf>` + 9 `voice_status_`) = **20 metadata**, не предикторы.
   В `manifest.json`/`feature_schema.json` каждая колонка помечена `role` (`predictor`/`metadata`);
   `validate_run` ругается, если в features есть `label`.
4. **Автоматический цикл** — export → `validate` → DATA_READY без отдельного разрешения; старые run не
   изменяются. `source_inventory.json` переиспользован из v2 (повторная БД-инвентаризация не делалась).

### 8.2 Цифры (полный v3: `20261013T221949Z_v3_full`)

| метрика | значение |
|---|---|
| feature_rows (1m-решения, будни+сессия) | **2 557 100** |
| target_rows (5m-закрытия) | 511 159 |
| supervised (MATURE, 1:1) | **392 693** |
| CENSORED | 118 466 |
| причины CENSORED | future_bucket_incomplete 41 209 / anchor_incomplete 47 293 / session_cutoff 29 847 / gap 117 |
| MATURE +1 / 0 / −1 | 182 440 / 10 718 / 199 535 |
| тикеров ok | 30 / 31 (HHRU = no_bars) |
| predictors / metadata / всего признаков | 58 / 20 / 78 |
| decision_ts min/max | 2025-10-01T07:05Z .. 2026-10-08T15:40Z |

Идентичность: `dataset_version ml_mtf_1_v3`, `feature_schema_version 1m_mtf_v3`,
`code_sha256 121388e4dd46d575f71507e13f230e3e83da9a36900f5bbf7a32b1b8d48387fa`,
`params_hash 7bc511c8862b9fc1a56fbb0a32b60ca774c4e560eb3326c73c5d15a2f856faf7`,
`eligibility_policy_hash b8f7f9cd0498d4b9`, `label_cfg_hash a898948ff9f0d027`,
`input_manifest_sha256 9964b08f…`, окно 2025-10-01T04:00Z .. 2026-10-09T00:00Z (end-exclusive).

Отличие от v2 — только отсечение выходных и новый `future_bucket_incomplete` (прогон из ТОГО ЖЕ frozen
snapshot `20261011T1947Z`); суффиксы ТФ `_1m/_5m/_600s/_1800s/_3600s`; в features НЕТ `label`.

### 8.3 Проверки

- `--validate` полного run: `ok: true`, `errors: []`, `warnings: []`, **`weekday_violations: 0`**.
- **Гейт не вакуумный**: тот же `--validate` на старом v2-full даёт `weekday_violations = 1 043 977`
  и `ok: false` — то есть выходные в v2 отбраковываются автоматически.
- `future_bucket_incomplete` по тикерам: MVID 8 528, SFIN 6 768, ASTR 6 551, NLMK 3 418, RUAL 2 601,
  SMLT 1 920, VKCO 1 775, CHMF 1 267, RNFT 1 190, MTSS 1 055 …— там, где 1m в snapshot разрежен.
- `CENSORED` ⇒ `label_end_ts=None` (0 нарушений); `MATURE` ⇒ `label_end_ts` задан (392 693);
  `decision_ts` только в будни и в окне сессии; `feature_max_available_ts == decision_ts`.
- Тесты: `backend/tests/test_ml_mtf_dataset.py` — **28 passed**
  (включая выходные/неполную будущую корзину/ранний hit/timeout/роли колонок/validate-флаги).

### 8.4 Уточнения и границы

- Разреженные 1m дают сильную цензуру supervised: MVID 5 316/17 673, SFIN 7 867/13 654,
  ASTR 9 714/12 882 (sup/cens) — это свойство входных данных snapshot, не дефект логики.
- Покрытие входного snapshot неравномерно (GAZP/LKOH с 2026-01-02, TATN 2026-06-24,
  ALRS/PLZL 2026-07-15, RNFT 2026-08-06, AFKS/OZON/PHOR/SIBN/VTBR 2026-08-24, VKCO/YDEX 2026-03-03,
  HHRU 0 строк); corporate actions UNVERIFIED; `phase=development_unvalidated`.
- Сплиты/purge/embargo применяет потребитель (B2) по `label_end_ts` (embargo 120 мин).
- Shard для B2/B3: `20261013T221630Z_v3_schema` (SBER/GAZP × 2026-09; feat 22 329 / targ 4 456 /
  sup 3 877 / cens 579; `weekday_violations 0`).
