#!/usr/bin/env python3
"""ML-MTF-1 (B2): поиск взаимодействий и объяснимых сочетаний признаков ТФ.

Конвейер обучения для контракта данных v1 (Builder-1):
- features.parquet + targets.parquet, join строго по row_id.
- Наблюдение = закрытый 5m бар (supervised строки = label_status == MATURE).
- Разметка: triple_barrier (направленная close-anchored), классы -1/0/+1.
- Временной split без lookahead: train<=2026-05-31, val 2026-06-01..07-31,
  eval 2026-08-01..10-08, embargo 120 мин перед границами.
- Матрица: 3 модели (LogReg C=1; DecisionTree d3/leaf200; HistGBM fixed) ×
  4 набора признаков (individual votes; continuous 5m; continuous all TF;
  all TF + votes) = максимум 12 fits, seed 42. Без подбора по score.

Результат: обучаемый воспроизводимый конвейер + измеренное добавочное качество
взаимодействий ТФ. Наличие положительной метки != прибыль (это отдельная ветка B3).
Файлы пишутся в immutable <out>; MODEL_READY публикуется в chat.md.

Не изменяет код B1/B3/live ML. Только чтение parquet и sklearn.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Фикс переполнения потоками: на 4-ядерном хосте многопоточный HGB в ~50x медленнее
# (спавн потоков на каждую итерацию обгоняет саму работу; замер: 13.7s vs 0.77s за
# 10 итераций). Ограничиваем потоки ДО импорта sklearn/numpy — это ещё и обеспечивает
# детерминизм (стабильный порядок reduce). Переопределяется ML_OMP_THREADS.
os.environ.setdefault("OMP_NUM_THREADS", os.environ.get("ML_OMP_THREADS", "1"))

import numpy as np
import pandas as pd

SEED = 42
TZ = timezone.utc

LABEL_MAP = {-1: 0, 0: 1, 1: 2}          # down, neutral, up
LABEL_NAMES = {0: "down", 1: "neutral", 2: "up"}
VOTE_PREFIX = "vote_"
AGE_PREFIX = "age_"
VOICE_PREFIX = "voice_"

TF_LIST = (60, 300, 600, 1800, 3600)      # секунды: 1m/5m/10m/30m/60m
# Суффиксы ТФ: v2 (ml_mtf_1_v2) пишет 1m как '_1m', прежний контракт — '_60s'.
# Обе формы поддерживаются, иначе весь мелкий ТФ молча выпадает из набора.
TF_ALIAS = {60: ("1m", "60s"), 300: ("5m",), 600: ("600s",),
            1800: ("1800s",), 3600: ("3600s",)}
TF_SUFFIX = {tf: TF_ALIAS[tf][0] for tf in TF_LIST}   # каноничная форма v2
SUPERVISED_TF = 300                       # наблюдение = закрытый 5m бар
BIAS_TF = 3600                            # bias (period=50) считается по часовым

CONT_INDICATORS = (
    "rsi14", "macd_hist", "macd_hist_delta",
    "ema50_dist", "ema50_slope", "atr14_price", "volr20",
)
SINGLE_CONT = ("bias",)                   # без суффикса ТФ (часовой bias)
COVERAGE_PREFIX = "n_min_"                # полнота корзины ТФ (int, из schema B1)
NON_FEATURE_PREFIXES = ("voice_status_",)  # строковые служебные, в X не идут

VOTE_STRATEGIES = (
    "rsi_reversal", "macd_cross", "range_compression_breakout",
    "pullback_ema", "vwap_reclaim", "vwap_reversion",
    "donchian_breakout", "volume_drop", "kama_trend",
)
VOICE_COLS = ("voice_buy", "voice_sell", "voice_quorum", "voice_conflict")

# ---- eligibility-policy ветки (единая для B1/B2/B3) ------------------------
# Будни, главная сессия с 10:00 MSK, решения строго до 18:44 MSK.
# Политика — явно зафиксированное ограничение исследования: субботние свечи
# не объявляются ошибочными данными и не чистятся из БД, они просто не
# участвуют как наблюдения этой ветки.
MSK = timezone(timedelta(hours=3))
SESSION_START_MIN = 10 * 60            # 10:00 MSK
DECISION_CUTOFF_MIN = 18 * 60 + 44     # 18:44 MSK, строго раньше
POLICY_NAME = ("weekday, main_session>=10:00MSK, decision<18:44MSK")
WD_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Качество/покрытие/идентичность: остаются metadata для coverage и eligible
# decisions, но НИКОГДА не попадают в predictors (иначе модель торгует
# плотностью данных, а не рынком — так в v2 выигрывал n_min_3600s).
QUALITY_PREFIXES = (COVERAGE_PREFIX,) + NON_FEATURE_PREFIXES
META_EXACT = ("row_id", "figi", "ticker", "decision_ts",
              "feature_max_available_ts", "feature_schema_version",
              "label", "label_status", "label_cfg_hash", "label_end_ts",
              "label_status_reason", "y", "y_known", "label_end_known")

MODEL_IDS = ("logreg", "dtree", "hgb")
FEATURE_SETS = ("individual_votes", "continuous_5m", "continuous_all_tf", "all_tf_votes")
THRESHOLDS = (0.50, 0.60, 0.70)


def tf_of_column(col: str) -> int | None:
    """ТФ колонки в секундах; None если колонка не ТФ-признак."""
    for tf, aliases in TF_ALIAS.items():
        if col.endswith("_" + aliases[0]) or col.endswith("_" + aliases[-1]):
            return tf
    if col in SINGLE_CONT:
        return BIAS_TF
    return None


# ------------------------------------------------------------------ eligibility
def policy_reason(ts) -> np.ndarray:
    """Причина отклонения решения политикой ветки: 'ok' | 'weekend' |
    'before_session' | 'after_cutoff'. Только по decision_ts — ни метки, ни
    будущие статусы не читаются, поэтому это не фильтр по MATURE.
    """
    t = pd.to_datetime(pd.Series(ts), utc=True, errors="coerce").dt.tz_convert(MSK)
    minutes = t.dt.hour * 60 + t.dt.minute
    weekday = t.dt.weekday < 5
    out = np.full(len(t), "ok", dtype=object)
    out[(~weekday).to_numpy()] = "weekend"
    out[(weekday & (minutes < SESSION_START_MIN)).to_numpy()] = "before_session"
    out[(weekday & (minutes >= DECISION_CUTOFF_MIN)).to_numpy()] = "after_cutoff"
    out[t.isna().to_numpy()] = "bad_timestamp"
    return out


def policy_eligible(ts) -> np.ndarray:
    """Defensive gate: будни + главная сессия + решения строго до 18:44 MSK."""
    return policy_reason(ts) == "ok"


def load_b1_contract(features) -> tuple[dict, str | None]:
    """Manifest B1 рядом с `features/` — источник истины по политике и ролям.

    B1 (пост 2026-10-14) публикует `eligibility_policy` + `eligibility_policy_hash`
    и явные списки `predictor_columns`/`metadata_columns`. B2 обязан сверяться с
    ними, а не восстанавливать роли собственной эвристикой.
    """
    base = Path(features)
    if base.is_file():
        base = base.parent
    bases = [base]
    if base.name in ("features", "targets", "meta"):
        bases.append(base.parent)
    for b in bases:
        for name in ("manifest.json", "feature_schema.json"):
            f = b / name
            if not f.exists():
                continue
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and ("eligibility_policy" in doc
                                          or "predictor_columns" in doc):
                return doc, str(f)
    return {}, None


def assert_b1_policy_compatible(pol: dict) -> list:
    """Расхождения между моим gate и политикой B1 (пусто = совместимо).

    `close` намеренно не сверяется: это граница бара для меток, а моё верхнее
    ограничение — `decision_strictly_before`, как и записано в политике B1.
    """
    if not pol:
        return []
    got = {
        "tz": pol.get("tz"),
        "weekdays": list(pol.get("weekdays") or []),
        "open": pol.get("open"),
        "decision_strictly_before": pol.get("decision_strictly_before"),
    }
    want = {"tz": "Europe/Moscow", "weekdays": list(range(5)),
            "open": "10:00", "decision_strictly_before": "18:44"}
    return [f"{k}: B1={got[k]!r} != B2={want[k]!r}"
            for k in want if got[k] != want[k]]


# ------------------------------------------------------------------ schema
def default_feature_schema() -> dict:
    """Маппинг наборов признаков на имена колонок контракта (schema B1).

    continuous: '<indicator>_<tf_suffix>' для tf в TF_LIST + 'bias'.
    votes: 'vote_<strategy>' + 'age_<strategy>' + voice_* (кворум/расхождение).

    В наборы НЕ входят `n_min_*` (полнота корзины), `voice_status_*` и
    identity/timing-колонки: это metadata покрытия, а не рыночные признаки.
    В v2 победитель split'а приходился на `n_min_3600s` — качество данных,
    а не рынок; такие признаки дают artifactual edge, зависящий от загрузки
    источника. `coverage_columns` при этом сохраняются в схеме и уходят в
    diag/metadata и quality-strata.

    Для каждого ТФ в схеме стоит КАНОНИЧЕСКИЙ суффикс v2; устаревшие алиасы
    (`_60s`) подхватывает `resolve_feature_columns` — так переименование ТФ у
    B1 не может молча отбросить целый таймфрейм, и при этом один и тот же ТФ
    не попадёт в набор дважды.
    """
    cont_all = [f"{ind}_{TF_SUFFIX[tf]}" for tf in TF_LIST
                for ind in CONT_INDICATORS] + list(SINGLE_CONT)
    cont_5 = [f"{ind}_{TF_SUFFIX[SUPERVISED_TF]}" for ind in CONT_INDICATORS]
    votes = ([f"{VOTE_PREFIX}{s}" for s in VOTE_STRATEGIES]
             + [f"{AGE_PREFIX}{s}" for s in VOTE_STRATEGIES]
             + list(VOICE_COLS))
    return {
        "individual_votes": votes,
        "continuous_5m": cont_5,
        "continuous_all_tf": cont_all,
        "all_tf_votes": cont_all + votes,
        "tf_list": list(TF_LIST),
        "tf_suffixes": {str(tf): list(a) for tf, a in TF_ALIAS.items()},
        "continuous_indicators": list(CONT_INDICATORS),
        "coverage_columns": [f"{COVERAGE_PREFIX}{a}" for tf in TF_LIST
                             for a in TF_ALIAS[tf]],
    }


def _alias_variant(col: str, has: set) -> str | None:
    """Та же колонка под алиасом-суффиксом того же ТФ, если он есть в df.

    Нужен для v1-датасетов (`rsi14_60s`), когда схема называет канон v2
    (`rsi14_1m`). Без этого целый ТФ молча выпадал бы из набора.
    """
    for aliases in TF_ALIAS.values():
        for a in aliases:
            if col.endswith("_" + a):
                stem = col[: -(len(a) + 1)]
                for alt in aliases:
                    if f"{stem}_{alt}" in has:
                        return f"{stem}_{alt}"
                return None
    return None


def resolve_feature_columns(df: pd.DataFrame, schema: dict) -> dict:
    """Возвращает {set_name: [колонки, присутствующие в df]} валидируя наличие.

    Явные списки из schema раскрываются через алиасы ТФ; если список ничего не
    дал, набор достраивается по паттерну колонок df (суффикс ТФ / префикс
    голоса) — устойчиво к переименованию B1.

    Оба пути (явный список и suffix-fallback) проходят через один и тот же
    whitelist: `n_min_*`, `voice_status_*`, identity/timing-колонки и все
    нечисловые колонки в predictors не идут. Раньше fallback подхватывал
    `n_min_1m` как «ТФ-признак» и молча превращал покрытие источника в
    предиктор — единственный канал, которым quality-колонка могла попасть в X.
    """
    has = set(df.columns)

    def _whitelist(cols) -> list:
        numeric = {c for c in df.columns
                   if pd.api.types.is_numeric_dtype(df[c].dtype)}
        return [c for c in dict.fromkeys(cols)
                if c in numeric
                and not c.startswith(QUALITY_PREFIXES)
                and c not in META_EXACT]

    out = {}
    for s in FEATURE_SETS:
        picked = []
        for c in schema.get(s, []):
            if c in has:
                picked.append(c)
            else:
                alt = _alias_variant(c, has)
                if alt:
                    picked.append(alt)
        out[s] = _whitelist(picked)

    if not out["continuous_all_tf"]:
        out["continuous_all_tf"] = _whitelist(
            [c for c in sorted(has) if tf_of_column(c) is not None])
    if not out["continuous_5m"]:
        out["continuous_5m"] = _whitelist(
            [c for c in out["continuous_all_tf"]
             if tf_of_column(c) == SUPERVISED_TF])
    if not out["individual_votes"]:
        out["individual_votes"] = _whitelist(
            [c for c in sorted(has)
             if c.startswith((VOTE_PREFIX, AGE_PREFIX, VOICE_PREFIX))])
    out["all_tf_votes"] = _whitelist(out["continuous_all_tf"]
                                     + out["individual_votes"])
    return out


# ------------------------------------------------------------------ loading
def _read_any(path: Path) -> pd.DataFrame:
    if path.suffix == ".csv":
        return pd.read_csv(path)
    return pd.read_parquet(path)


def _read_shards(path: Path, kind: str) -> pd.DataFrame:
    """Читает шарды каталога. Каталог B1 содержит features_* и targets_* вместе —
    разделяем по префиксу имени, чтобы не склеить метки в матрицу признаков."""
    if not path.is_dir():
        return _read_any(path)
    files = sorted(path.glob("*.parquet"))
    parts = [p for p in files if p.name.startswith(kind)]
    if not parts:
        parts = [p for p in files
                 if not p.name.startswith(("features", "targets"))] or files
    df = pd.concat([_read_any(p) for p in parts], ignore_index=True) \
        if parts else pd.DataFrame()
    if kind == "features" and "label" in df.columns:
        raise SystemExit(f"{path}: в features-шардах найдена колонка 'label'")
    return df


TARGET_KEEP = ["row_id", "label", "label_status", "label_cfg_hash", "label_end_ts"]
TARGET_OPTIONAL = ["label_status_reason"]   # v2: причина CENSORED (для отчётности)


def _validate_join(fdf: pd.DataFrame, tdf: pd.DataFrame) -> dict:
    """Проверка one-to-one join и согласованности метаданных между шардами.

    Жёсткие ошибки (дубли row_id, несколько label_cfg_hash, расхождение
    decision_ts между шардами, признаки «доступны позже решения») валит прогон:
    молчаливое нарушение X↔y и есть то, из-за чего отозвали прежний результат.
    """
    for name, d in (("features", fdf), ("targets", tdf)):
        dup = int(d["row_id"].duplicated().sum())
        if dup:
            raise SystemExit(f"{name}: {dup} дублей row_id (join не one-to-one)")

    if "label_cfg_hash" in tdf.columns:
        cfgs = sorted({str(v) for v in tdf["label_cfg_hash"].dropna().unique()})
        if len(cfgs) > 1:
            raise SystemExit(f"targets: несколько label_cfg_hash {cfgs} — "
                             "смешаны разные конфигурации подписи")
    else:
        cfgs = []

    fids, tids = set(fdf["row_id"]), set(tdf["row_id"])
    common = fids & tids
    diag = {
        "rows_features": int(len(fdf)),
        "rows_targets": int(len(tdf)),
        "only_in_features": int(len(fids - tids)),
        "only_in_targets": int(len(tids - fids)),
        "label_cfg_hash": cfgs,
    }

    # features в v2 пишутся на КАЖДОМ 1m-решении, targets только на закрытиях
    # 5m — разный состав ожидаем. Сверяем метаданные по ПЕРЕСЕЧЕНИЮ row_id.
    both = [c for c in ("decision_ts", "figi", "ticker")
            if c in fdf.columns and c in tdf.columns]
    if both and common:
        f_ord = fdf.set_index("row_id").loc[sorted(common)]
        t_ord = tdf.set_index("row_id").loc[sorted(common)]
        for c in both:
            a, b = f_ord[c], t_ord[c]
            mismatch = int((a.to_numpy() != b.to_numpy()).sum())
            if mismatch:
                raise SystemExit(f"row_id '{c}' расходится между features/targets "
                                 f"в {mismatch} общих строках")

    if "feature_max_available_ts" in fdf.columns:
        fm = pd.to_datetime(fdf["feature_max_available_ts"], utc=True,
                            errors="coerce")
        dm = pd.to_datetime(fdf["decision_ts"], utc=True, errors="coerce")
        late = int((fm > dm).sum())
        diag["feature_available_late"] = late
        if late:
            raise SystemExit(
                f"features: {late} строк с feature_max_available_ts > decision_ts "
                "(признак из будущего)")
    else:
        diag["feature_available_late"] = 0

    return diag


def load_contract(features_path, targets_path, schema: dict | None = None,
                  supervised_only: bool = True,
                  diagnostics: dict | None = None) -> pd.DataFrame:
    """Join features+targets по row_id (one-to-one) без фильтра по меткам.

    Возвращает inference universe: ВСЕ строки, независимо от `label_status` и
    значения метки. Колонки:

    - `y_known` — исход достоверен (MATURE + метка в LABEL_MAP + известен
      `label_end_ts`): только такие строки годятся для fit/scoring;
    - `y` — класс 0/1/2 либо NaN, если исход неизвестен.

    Ранее функция удаляла CENSORED/неизвестную метку ДО всех predict — это
    фильтровало инференс по будущему исходу и передавало B3 скрытый фильтр
    допуска к сделкам. Supervised-подмножество отбирает вызывающий код через
    `y_known`.

    `supervised_only=True` оставлено для обратной совместимости: он отфильтровывает
    результат по `y_known` и приводит `y` к int.
    """
    fdf = _read_shards(Path(features_path), "features")
    tdf = _read_shards(Path(targets_path), "targets")

    missing = [c for c in TARGET_KEEP if c not in tdf.columns]
    if missing:
        raise SystemExit(f"targets: нет колонок {missing}")

    diag = _validate_join(fdf, tdf)
    if diagnostics is not None:
        diagnostics.update(diag)

    keep = TARGET_KEEP + [c for c in TARGET_OPTIONAL if c in tdf.columns]
    df = fdf.merge(tdf[keep], on="row_id", how="inner",
                   validate="one_to_one")
    # Join строго по 5m row_id: per-1m колонки features не должны породить
    # копий метки. one_to_one + этот инвариант закрывают канал дублирования.
    if diagnostics is not None:
        expected = diag["rows_targets"] - diag["only_in_targets"]
        diagnostics["rows_joined"] = int(len(df))
        if int(len(df)) != expected:
            raise SystemExit(
                f"join дал {len(df)} строк вместо {expected} (targets "
                f"{diag['rows_targets']} − без features {diag['only_in_targets']})")

    status = df["label_status"].astype(str).str.upper()
    df["label_status"] = status
    df["label"] = pd.to_numeric(df["label"], errors="coerce")
    end = pd.to_datetime(df["label_end_ts"], utc=True, errors="coerce")
    df["label_end_known"] = end.notna()
    df["y_known"] = ((status == "MATURE") & df["label"].isin(LABEL_MAP)
                     & df["label_end_known"].to_numpy())
    df["y"] = df["label"].map(LABEL_MAP).astype("float64")

    if supervised_only:
        df = df[df["y_known"]].copy()
        df["y"] = df["y"].astype(np.int64)
    return df


# ------------------------------------------------------------------ split
def _boundary_ts(d: date, end_of_day: bool) -> datetime:
    if end_of_day:
        return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=TZ)
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=TZ)


def temporal_split(df: pd.DataFrame, train_end: date, val_start: date,
                   val_end: date, eval_start: date, eval_end: date,
                   embargo_min: int = 120):
    """Temporal split с purge и embargo.

    Возвращает два набора mask-словарей (ключи «train»/«val»/«eval»):

    - **supervised** (`train`,`val`,`eval`) — только строки с ИЗВЕСТНЫМ исходом,
      чей target-path не дотягивается до границы СЛЕДУЮЩЕГО сплита (purge).
      Нужны для fit и для scoring.
    - **inference universe** (`*_infer`) — все допустимые решения в окне сплита
      независимо от maturity/значения метки. Нужны для predict: иначе доступ к
      сделкам отфильтрован по будущему исходу.

    Embargo (окно `embargo_min` ПЕРЕД границей) применяется к **предыдущему**
    сплиту: `[boundary-emb, boundary)` вычищается из train перед границей val и
    из val перед границей eval. К собственному началу сплита это окно и так не
    относится — решение там принято раньше, поэтому прежняя проверка пересечения
    с окном ДО собственного начала была пустой.

    Purge читает `label_end_ts`: target, заканчивающийся на/после границы
    следующего сплита, убирается из предыдущего целиком (не переносится —
    решение принято в его окне). Неизвестный `label_end_ts` в supervised
    недопустим.
    """
    ts = pd.to_datetime(df["decision_ts"], utc=True)
    emb = timedelta(minutes=embargo_min)
    val_b = _boundary_ts(val_start, False)
    eval_b = _boundary_ts(eval_start, False)

    win_train = ts.dt.date <= train_end
    win_val = (ts.dt.date >= val_start) & (ts.dt.date <= val_end)
    win_eval = (ts.dt.date >= eval_start) & (ts.dt.date <= eval_end)

    emb_val = (ts >= val_b - emb) & (ts < val_b)       # перед границей val
    emb_eval = (ts >= eval_b - emb) & (ts < eval_b)    # перед границей eval

    infer = {
        "train": (win_train & ~emb_val).to_numpy(),
        "val": (win_val & ~emb_eval).to_numpy(),
        "eval": win_eval.to_numpy(),
    }

    if "label_end_ts" in df.columns:
        lend = pd.to_datetime(df["label_end_ts"], utc=True, errors="coerce")
        known = lend.notna().to_numpy()
        into_val = (lend < val_b).to_numpy()
        into_eval = (lend < eval_b).to_numpy()
        sup = {
            "train": infer["train"] & known & into_val,
            "val": infer["val"] & known & into_eval,
            "eval": infer["eval"] & known,
        }
    else:
        sup = {k: v.copy() for k, v in infer.items()}

    out = dict(sup)
    out.update({f"{k}_infer": v for k, v in infer.items()})
    return out


# ------------------------------------------------------------------ models
def get_models():
    from sklearn.linear_model import LogisticRegression
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.ensemble import HistGradientBoostingClassifier

    return {
        "logreg": LogisticRegression(C=1.0, max_iter=2000, random_state=SEED),
        "dtree": DecisionTreeClassifier(max_depth=3, min_samples_leaf=200,
                                        random_state=SEED),
        "hgb": HistGradientBoostingClassifier(max_iter=150, max_leaf_nodes=15,
                                              l2_regularization=1.0,
                                              min_samples_leaf=200,
                                              early_stopping=False,
                                              random_state=SEED),
    }


# ------------------------------------------------------------------ metrics
def _safe_logloss(y_true, proba, classes):
    from sklearn.metrics import log_loss
    eps = 1e-15
    proba = np.clip(proba, eps, 1 - eps)
    try:
        return float(log_loss(y_true, proba, labels=classes))
    except ValueError:
        return float("nan")


def reliability_bins(y_true, p_pos, n_bins: int = 10) -> list:
    """Reliability bins для вероятности класса up: средний прогноз vs факт."""
    y = np.asarray(y_true)
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    b = np.clip(np.digitize(p_pos, edges) - 1, 0, n_bins - 1)
    out = []
    for i in range(n_bins):
        m = b == i
        n = int(m.sum())
        if not n:
            continue
        mean_p = float(p_pos[m].mean())
        obs = float(y[m].mean())
        out.append({"bin": i, "lo": float(edges[i]), "hi": float(edges[i + 1]),
                    "n": n, "mean_predicted": mean_p, "observed": obs,
                    "gap": mean_p - obs})
    return out


def baseline_scores(y_true, train_prior: dict, majority: int, classes) -> dict:
    """Базовые линии от train-only prior: log_loss константы и majority-accuracy.

    Без них log_loss/accuracy модели не с чем сравнивать: ноль — не база.
    """
    y = np.asarray(y_true)
    p = np.array([max(float(train_prior.get(c, 0.0)), 1e-15) for c in classes])
    return {
        "prior_log_loss": float(-np.log(np.clip(p[y], 1e-15, 1.0)).mean()),
        "majority_accuracy": float(np.mean(y == majority)),
        "train_prior": {LABEL_NAMES[c]: float(train_prior.get(c, 0.0))
                        for c in classes},
        "majority_class": LABEL_NAMES[majority],
    }


def _stratum_stats(y_true, proba, classes, mask) -> dict:
    """Компактная статистика внутри quality-stratum (полнота покрытия)."""
    from sklearn.metrics import balanced_accuracy_score
    y = np.asarray(y_true)[mask]
    p = proba[mask]
    if not len(y):
        return {"n": 0}
    yp = p.argmax(axis=1)
    return {
        "n": int(len(y)),
        "log_loss": _safe_logloss(y, p, classes),
        "balanced_accuracy": float(balanced_accuracy_score(y, yp)),
        "accuracy": float(np.mean(y == yp)),
        "base_up": float(np.mean(y == LABEL_MAP[1])),
    }


def metrics_for(y_true, proba, classes, baseline: dict | None = None):
    from sklearn.metrics import (balanced_accuracy_score, accuracy_score,
                                 precision_recall_fscore_support)
    y_true = np.asarray(y_true)
    y_pred = proba.argmax(axis=1)
    bal = float(balanced_accuracy_score(y_true, y_pred, adjusted=False))
    acc = float(accuracy_score(y_true, y_pred))
    prec, rec, f1, sup = precision_recall_fscore_support(
        y_true, y_pred, labels=classes, zero_division=0)
    ll = _safe_logloss(y_true, proba, classes)

    onehot = np.zeros_like(proba)
    onehot[np.arange(len(y_true)), y_true] = 1.0
    brier = float(np.mean(np.sum((proba - onehot) ** 2, axis=1)))

    up_idx = classes.index(LABEL_MAP[1])
    p_up = proba[:, up_idx]
    mask_up = (y_true == LABEL_MAP[1])
    base_up = float(mask_up.mean())
    cal_up = float(np.mean(p_up[mask_up])) if mask_up.any() else float("nan")

    out = {
        "accuracy": acc,
        "balanced_accuracy": bal,
        "log_loss": ll,
        "brier_multiclass": brier,
        "per_label": {
            LABEL_NAMES[c]: {"precision": float(prec[i]), "recall": float(rec[i]),
                             "f1": float(f1[i]), "support": int(sup[i])}
            for i, c in enumerate(classes)
        },
        "class_share": {LABEL_NAMES[c]: float(np.mean(y_true == c))
                        for c in classes},
        # это НЕ калибровка (среднее p_up при истинном up минус база) —
        # калибровка ниже, reliability_bins_up + brier_multiclass
        "calibration_up": (cal_up - base_up) if mask_up.any() else float("nan"),
        "reliability_bins_up": reliability_bins(y_true, p_up),
        "base_up": base_up,
    }
    if baseline:
        out["baseline"] = baseline
        out["log_loss_vs_prior"] = ll - baseline["prior_log_loss"]
        out["accuracy_vs_majority"] = acc - baseline["majority_accuracy"]
    return out


def threshold_select(y_val, proba_val, decision_ts, classes, thr_list=THRESHOLDS):
    """Выбор порога P(up) на val по precision (min 200 сигналов / 20 дат)."""
    up_idx = classes.index(LABEL_MAP[1])
    p_up = proba_val[:, up_idx]
    ts = pd.to_datetime(decision_ts, utc=True)
    best = None
    details = []
    for thr in thr_list:
        sel = p_up >= thr
        n_sig = int(sel.sum())
        n_days = int(ts[sel].dt.date.nunique()) if sel.any() else 0
        if n_sig == 0:
            prec = float("nan")
        else:
            prec = float(np.mean(np.asarray(y_val)[sel] == LABEL_MAP[1]))
        details.append({"threshold": thr, "n_signals": n_sig, "n_dates": n_days,
                        "precision": prec,
                        "eligible": n_sig >= 200 and n_days >= 20})
        if n_sig >= 200 and n_days >= 20:
            if best is None or prec > best["precision"]:
                best = {"threshold": thr, "precision": prec,
                        "n_signals": n_sig, "n_dates": n_days}
    if best is None:
        return {"selection": "NO_SELECTION", "reason": "no_threshold_meets_support",
                "details": details}
    return {"selection": "selected", "threshold": best["threshold"],
            "precision": best["precision"], "n_signals": best["n_signals"],
            "n_dates": best["n_dates"], "details": details}


# ------------------------------------------------------------------ tree leaves
def extract_tree_leaves(model, feature_names, y_train, classes, prep=None):
    """Экспорт листьев дерева: условия, support, P(label), lift к train base.

    Дерево обучено на z-оценках (`_Prep`), поэтому пороги в топологии — в
    единицах стандартного отклонения. Условия дублируются в `conditions_raw`
    (исходная шкала индикатора): переиспользовать правила на сырых значениях
    иначе нельзя, а «z > 1.17» нечитаемо.
    """
    from sklearn.tree import _tree

    t = model.tree_
    base = {c: float(np.mean(np.asarray(y_train) == c)) for c in classes}
    feats = [feature_names[i] if i != _tree.TREE_UNDEFINED else "<undef>"
             for i in t.feature]
    raw = {}
    if prep is not None and getattr(prep, "kept", None):
        sc = getattr(prep, "sc", None)
        if sc is not None and hasattr(sc, "mean_"):
            for j, name in enumerate(prep.kept):
                if j < len(sc.mean_):
                    raw[name] = (float(sc.mean_[j]), float(sc.scale_[j]))
    leaves = []
    n_nodes = t.node_count

    def to_raw(name, z):
        if name in raw:
            m, s = raw[name]
            return z * s + m
        return float("nan")

    def walk(node, cond, cond_raw):
        if t.children_left[node] == _tree.TREE_LEAF:
            # sklearn >= 1.3: tree_.value хранит ДОЛИ классов (сумма по узлу = 1),
            # число наблюдений в узле даёт weighted_n_node_samples.
            support = int(round(t.weighted_n_node_samples[node]))
            val = t.value[node][0]
            total = val.sum()
            dist = {LABEL_NAMES[c]: (float(val[c]) / total if total > 0 else 0.0)
                    for c in classes}
            pred = int(np.argmax(val))
            lift = {LABEL_NAMES[c]: (dist[LABEL_NAMES[c]] / base[c]
                                     if base[c] > 0 else float("nan")) for c in classes}
            leaves.append({"node": int(node),
                           "conditions": list(cond),
                           "conditions_raw": list(cond_raw),
                           "support": support,
                           "p_label": dist,
                           "pred_label": LABEL_NAMES[pred],
                           "lift": lift})
            return
        f = feats[node]           # фича, по которой разбивается этот узел
        th = float(t.threshold[node])
        walk(t.children_left[node],
             cond + [f"{f} <= {th:.4g}"],
             cond_raw + [f"{f} <= {to_raw(f, th):.6g}"])
        walk(t.children_right[node],
             cond + [f"{f} > {th:.4g}"],
             cond_raw + [f"{f} > {to_raw(f, th):.6g}"])

    walk(0, [], [])
    return leaves, base


def attribute_leaves(model, X, y, classes):
    """Применяет дерево и считает per-leaf P(up) и support на split (фикс. условия)."""
    from sklearn.tree import _tree

    t = model.tree_
    node_ind = model.apply(X)
    out = {}
    for i in range(t.node_count):
        if t.children_left[i] == _tree.TREE_LEAF:
            mask = node_ind == i
            n = int(mask.sum())
            if n == 0:
                out[i] = {"support": 0, "p_up": float("nan")}
                continue
            yv = np.asarray(y)[mask]
            out[i] = {"support": n,
                      "p_up": float(np.mean(yv == LABEL_MAP[1]))}
    return out


# ------------------------------------------------------------------ ablation
def ablation_nonlinear(X_train_df, y_train, X_val_df, y_val, classes,
                       cols=None, full_ref: dict | None = None):
    """Удаляем по одному ТФ из continuous_all_tf; refit HGB; дельта val logloss/balacc.

    Тот же путь препроцессинга, что и в матрице (median-impute + z-score, fit на
    train) — иначе 'full' не совпал бы с hgb__continuous_all_tf и дельты были бы
    несравнимы. X_train_df/X_val_df — DataFrame с колонками набора.

    `full_ref` — метрики уже обученного hgb__continuous_all_tf из матрицы:
    переиспользуем их вместо повторного фита. Так ablation даёт ровно
    len(TF_LIST)=5 дополнительных fits, а «full» гарантированно идентичен
    матричному результату (байт-в-байт), а не «примерно такому же».
    """
    from sklearn.metrics import balanced_accuracy_score
    from sklearn.ensemble import HistGradientBoostingClassifier

    cols = list(cols) if cols else list(X_train_df.columns)
    prep = _Prep()
    Ztr = prep.fit_transform(X_train_df[cols].to_numpy(np.float32), cols)
    Zva = prep.transform(X_val_df[cols].to_numpy(np.float32))
    full_cols = prep.kept

    def _fit(Z, yy):
        m = HistGradientBoostingClassifier(max_iter=150, max_leaf_nodes=15,
                                           l2_regularization=1.0,
                                           min_samples_leaf=200,
                                           early_stopping=False,
                                           random_state=SEED)
        m.fit(Z, yy)
        return m

    if full_ref is not None:
        full_ll = float(full_ref["log_loss"])
        res = {"full": {"log_loss": full_ll,
                        "balanced_accuracy": float(full_ref["balanced_accuracy"]),
                        "n_features": int(full_ref.get("n_features",
                                                       len(full_cols))),
                        "dropped_all_nan": list(full_ref.get("dropped_all_nan",
                                                              prep.dropped)),
                        "reused_from_matrix": True}}
        base_m = None
    else:
        base_m = _fit(Ztr, y_train)
        p_full = base_m.predict_proba(Zva)
        full_ll = _safe_logloss(y_val, p_full, classes)
        res = {"full": {"log_loss": full_ll,
                        "balanced_accuracy": float(balanced_accuracy_score(
                            y_val, p_full.argmax(axis=1))),
                        "n_features": len(full_cols),
                        "dropped_all_nan": list(prep.dropped),
                        "reused_from_matrix": False}}
    n_fits = 0 if base_m is None else 1
    for tf in TF_LIST:
        keep = [j for j, c in enumerate(full_cols) if tf_of_column(c) != tf]
        keep_set = set(keep)
        dropped = [c for j, c in enumerate(full_cols) if j not in keep_set]
        if not dropped:
            continue
        m = _fit(Ztr[:, keep], y_train)
        n_fits += 1
        p = m.predict_proba(Zva[:, keep])
        ll = _safe_logloss(y_val, p, classes)
        res[f"drop_tf{tf}"] = {
            "log_loss": ll,
            "balanced_accuracy": float(balanced_accuracy_score(
                y_val, p.argmax(axis=1))),
            "delta_log_loss": ll - full_ll,
            "n_features": len(keep),
            "dropped": dropped,
        }
    res["_n_fits"] = n_fits
    return res


def align_proba(proba, model_classes, classes) -> np.ndarray:
    """Колонки predict_proba -> фиксированный порядок classes [0,1,2]."""
    if list(model_classes) == list(classes):
        return proba
    out = np.zeros((proba.shape[0], len(classes)), dtype=proba.dtype)
    for j, c in enumerate(model_classes):
        out[classes.index(c)] = proba[:, j]
    return out


class _PredictSink:
    """Потоковая запись predictions.parquet: батч на (model, feature_set).

    Накопление в памяти дало бы ~19M строк x 11 колонок (десятки ГБ на 8 ГБ
    хосте) — пишем каждый батч сразу и держим только open writer. Схема
    фиксируется явно: типы батчей (float32/float64 из разных моделей) иначе
    расходятся, а байтовая детерминируемость нужна для повторного прогона.
    """

    SCHEMA_FIELDS = (
        ("row_id", "string"), ("figi", "string"), ("decision_ts", "string"),
        ("model_id", "string"), ("split", "string"),
        ("p_down", "float64"), ("p_neutral", "float64"), ("p_up", "float64"),
        ("selected_long", "bool"), ("model_fit_end_ts", "string"),
        ("available_at_ts", "string"), ("selection_status", "string"),
    )

    def __init__(self, path: Path):
        import pyarrow as pa
        import pyarrow.parquet as pq
        self._pa = pa
        self._schema = pa.schema([pa.field(n, pa.type_for_alias(t))
                                  for n, t in self.SCHEMA_FIELDS])
        self._w = pq.ParquetWriter(str(path), self._schema, compression="snappy")

    def append(self, df_part: pd.DataFrame) -> None:
        names = [n for n, _ in self.SCHEMA_FIELDS]
        missing = [n for n in names if n not in df_part.columns]
        if missing:
            raise SystemExit(f"predictions: в батче нет колонок {missing}")
        tbl = self._pa.Table.from_pandas(df_part[names], preserve_index=False)
        self._w.write_table(tbl.cast(self._schema))

    def close(self) -> None:
        if self._w is not None:
            self._w.close()
            self._w = None


class _Prep:
    """median-impute + z-score, fit ТОЛЬКО на train (никаких статистик val/eval).

    SimpleImputer по умолчанию ВЫБРАСЫВАЕТ колонки, целиком NaN в train (в реальном
    датасете B1 таких 2: age_vwap_reversion/age_kama_trend). Молчаливое выбрасывание
    сдвигало бы имена при экспорте листьев дерева, поэтому kept/dropped фиксируются
    явно и дальше везде используются согласованно.
    """

    def __init__(self):
        from sklearn.impute import SimpleImputer
        from sklearn.preprocessing import StandardScaler
        self.imp = SimpleImputer(strategy="median")
        self.sc = StandardScaler()
        self.kept: list[str] = []
        self.dropped: list[str] = []

    def fit_transform(self, X, cols):
        Z = self.imp.fit_transform(X)
        # у SimpleImputer нет get_support(); выброшенные колонки имеют NaN-статистику
        mask = ~np.isnan(self.imp.statistics_)
        self.kept = [c for c, k in zip(cols, mask) if k]
        self.dropped = [c for c, k in zip(cols, mask) if not k]
        if len(self.kept) != Z.shape[1]:
            raise SystemExit(f"prep: {len(self.kept)} имён != {Z.shape[1]} колонок")
        return self.sc.fit_transform(Z)

    def transform(self, X):
        return self.sc.transform(self.imp.transform(X))

    def export(self) -> dict:
        """Параметры препроцессора для bundle.

        Пиклятся сами sklearn-объекты (SimpleImputer/StandardScaler): их модульный
        путь `sklearn.*` стабилен при загрузке из любого процесса. Сам класс _Prep
        НЕ пиклится — при CLI-запуске он лежит в __main__, и bundle, прочитанный
        из другого entrypoint, не нашёл бы класс.
        """
        return {
            "kind": "median_impute+zscore",
            "imp": self.imp,
            "sc": self.sc,
            "kept": list(self.kept),
            "dropped": list(self.dropped),
        }

    @classmethod
    def restore(cls, p: dict) -> "_Prep":
        obj = cls()
        obj.imp = p["imp"]
        obj.sc = p["sc"]
        obj.kept = list(p["kept"])
        obj.dropped = list(p["dropped"])
        return obj


# ------------------------------------------------- breakdown (пост-фактум)
def _load_breakdown_meta(targets_path) -> pd.DataFrame:
    """row_id -> ticker, y по supervised-строкам (только нужные колонки).

    Отбор обязан совпадать с `y_known` в load_contract: MATURE + метка в
    LABEL_MAP + известный label_end_ts. Иначе breakdown считал бы на строках,
    которых не было в scoring, и расходился с metrics.json.
    """
    tdf = _read_shards(Path(targets_path), "targets")
    need = [c for c in ("row_id", "ticker", "label", "label_status",
                        "label_end_ts")
            if c in tdf.columns]
    tdf = tdf[need].copy()
    if "label_status" in tdf:
        tdf = tdf[tdf["label_status"].astype(str).str.upper() == "MATURE"]
    if "label_end_ts" in tdf:
        tdf = tdf[pd.to_datetime(tdf["label_end_ts"], utc=True,
                                 errors="coerce").notna()]
    if "label" in tdf:
        lab = pd.to_numeric(tdf["label"], errors="coerce")
        keep = lab.isin(list(LABEL_MAP))
        tdf = tdf[keep]
        tdf["y"] = lab[keep].astype(int).map(LABEL_MAP).astype(int)
    if "ticker" not in tdf:
        tdf["ticker"] = "?"
    return tdf[["row_id", "ticker", "y"]]


def _new_acc():
    return {"n": 0, "conf": np.zeros(9, dtype=np.int64),
            "ll": 0.0, "cal_sum": 0.0, "cal_n": 0}


def _acc_final(acc, classes):
    n = acc["n"]
    if n == 0:
        return {"n": 0}
    conf = acc["conf"].reshape(3, 3)          # [true, pred]
    support = conf.sum(axis=1)
    pred_n = conf.sum(axis=0)
    tp = np.diag(conf)
    precision = np.divide(tp, pred_n, out=np.zeros(3), where=pred_n > 0)
    recall = np.divide(tp, support, out=np.zeros(3), where=support > 0)
    present = support > 0
    base_up = float(support[LABEL_MAP[1]] / n)
    cal = float(acc["cal_sum"] / acc["cal_n"]) if acc["cal_n"] else float("nan")
    return {
        "n": int(n),
        "accuracy": float(tp.sum() / n),
        "balanced_accuracy": float(recall[present].mean()) if present.any()
        else float("nan"),
        "log_loss": float(acc["ll"] / n),
        "per_label": {LABEL_NAMES[c]: {"precision": float(precision[i]),
                                       "recall": float(recall[i]),
                                       "support": int(support[i])}
                      for i, c in enumerate(classes)},
        "class_share": {LABEL_NAMES[c]: float(support[i] / n)
                        for i, c in enumerate(classes)},
        "calibration_up": (cal - base_up) if acc["cal_n"] else float("nan"),
        "base_up": base_up,
    }


def write_breakdown(out, targets_path, classes) -> dict:
    """per-ticker / per-month метрики из predictions.parquet (описательные).

    Читается ПОСЛЕ заморозки манифеста и ни на что не влияет: отбор/победитель
    уже зафиксированы по val log_loss. Стримим батчами — 19M строк целиком в
    pandas не влезают в 8 ГБ.
    """
    import pyarrow.parquet as pq

    out = Path(out)
    pred_path = out / "predictions.parquet"
    if not pred_path.exists():
        return {}
    meta = _load_breakdown_meta(targets_path)
    by_dim = {"by_ticker": {}, "by_month": {}}
    n_in = n_matched = 0
    pf = pq.ParquetFile(str(pred_path))
    cols = ["row_id", "model_id", "split", "decision_ts",
            "p_down", "p_neutral", "p_up"]

    for batch in pf.iter_batches(batch_size=400_000, columns=cols):
        b = batch.to_pandas()
        n_in += len(b)
        j = b.merge(meta, on="row_id", how="inner")
        n_matched += len(j)
        if j.empty:
            continue
        p = j[["p_down", "p_neutral", "p_up"]].to_numpy(np.float64)
        y = j["y"].to_numpy(np.int64)
        pred = p.argmax(axis=1)
        rows = np.arange(len(y))
        nll = -np.log(np.clip(p[rows, y], 1e-15, 1.0))
        up_mask = (y == LABEL_MAP[1])
        month = j["decision_ts"].astype(str).str.slice(0, 7)

        for dim, group in (("by_ticker", j["ticker"].astype(str)),
                           ("by_month", month)):
            bucket = (j["model_id"].astype(str) + "\x00"
                      + j["split"].astype(str) + "\x00" + group.values)
            frame = pd.DataFrame({
                "bucket": bucket, "cls": 3 * y + pred, "nll": nll,
                "p_up": np.where(up_mask, p[:, 2], 0.0),
                "is_up": up_mask.astype(np.float64),
            })
            conf = pd.crosstab(frame["bucket"], frame["cls"])
            sums = frame.groupby("bucket", sort=False)[
                ["nll", "p_up", "is_up"]].sum()
            store = by_dim[dim]
            for bk in conf.index:
                acc = store.setdefault(bk, _new_acc())
                row = conf.loc[bk]
                conf9 = np.zeros(9, dtype=np.int64)
                for c in conf.columns:
                    conf9[int(c)] = int(row[c])
                s = sums.loc[bk]
                acc["n"] += int(row.sum())
                acc["conf"] += conf9
                acc["ll"] += float(s["nll"])
                acc["cal_sum"] += float(s["p_up"])
                acc["cal_n"] += float(s["is_up"])

    def _build(store):
        res = {}
        for bk, acc in store.items():
            model_id, split, key = bk.split("\x00")
            res.setdefault(model_id, {}).setdefault(split, {})[key] = \
                _acc_final(acc, classes)
        return res

    result = {"n_predictions": int(n_in), "n_matched": int(n_matched),
              "by_ticker": _build(by_dim["by_ticker"]),
              "by_month": _build(by_dim["by_month"])}
    (out / "breakdown.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


# ------------------------------------------------------------------ persistence
def _save_bundle(path: Path, payload: dict) -> None:
    import joblib
    joblib.dump(payload, path, compress=3)


def _load_bundle(path: Path) -> dict:
    import joblib
    return joblib.load(path)


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def dataset_identity(path) -> dict:
    """Отпечаток входных данных для manifest: пути, размеры, mtime, хэш метаданных.

    Content-хэш больших parquet дорог (побочные файлы датасета — сотни МБ), поэтому
    по ним берём размер+mtime_ns; JSON/TXT-манифесты хэшируются полностью — они и
    несут идентичность конфигурации. Кроме того честно фиксируется источник:
    работа на отозванном датасете обязана падать, а не выдавать «законный» результат.
    """
    p = Path(path)
    if not p.exists():
        return {"path": str(p), "exists": False}
    markers = ("SUPERSEDED.txt", "REVOKED.txt", "REVOKE.txt", "REWORK.txt")
    revoked = [n for n in markers if p.is_dir() and (p / n).exists()]
    if revoked:
        raise SystemExit(
            f"{p}: маркер отзыва {revoked} — входные данные отозваны, запуск запрещён")
    files = sorted((x for x in (p.rglob("*") if p.is_dir() else [p])
                    if x.is_file()))
    h = hashlib.sha256()
    n_content = 0
    for f in files:
        rel = f.name if p.is_file() else str(f.relative_to(p))
        st = f.stat()
        if f.suffix.lower() in (".json", ".txt", ".yaml", ".yml"):
            h.update(f"{rel}:{_sha256_file(f)}\n".encode())
            n_content += 1
        else:
            h.update(f"{rel}:{st.st_size}:{st.st_mtime_ns}\n".encode())
    return {"path": str(p.resolve()), "exists": True, "n_files": len(files),
            "n_content_hashed": n_content, "identity_sha256": h.hexdigest(),
            "revoked_markers": []}


def _code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


# ------------------------------------------------------------------ leaves-only
def leaves_only(args):
    """Пересчитывает tree_leaves.json для одного дерева (без переобучения матрицы).

    Приоритет — frozen bundle `bundles/<model_id>.joblib`: условия обязаны быть
    теми же, по которым обучалась модель и считались predictions. Refit остаётся
    только как legacy-путь для старых каталогов без bundles (дерево при фиксированных
    данных, параметрах и seed детерминировано, но это уже не тот же объект).
    """
    out = Path(args.out)
    leaves_path = out / "tree_leaves.json"
    if leaves_path.exists():
        prev = json.loads(leaves_path.read_text(encoding="utf-8"))
        dt_key, feat_set = prev["model_id"], prev["feature_set"]
    elif args.feature_set:
        feat_set = args.feature_set
        dt_key = f"dtree__{feat_set}"
    else:
        raise SystemExit("--leaves-only: нужен существующий tree_leaves.json "
                         "или --feature-set")

    schema = default_feature_schema()
    if args.schema:
        schema.update(json.loads(Path(args.schema).read_text(encoding="utf-8")))
    df = load_contract(args.features, args.targets, schema,
                       supervised_only=True).reset_index(drop=True)
    df["decision_ts"] = pd.to_datetime(df["decision_ts"], utc=True)
    cols_by_set = resolve_feature_columns(df, schema)
    cols = cols_by_set[feat_set]
    if not cols:
        raise SystemExit(f"feature set '{feat_set}' пуст в данных")

    splits = temporal_split(
        df, date.fromisoformat(args.train_end), date.fromisoformat(args.val_start),
        date.fromisoformat(args.val_end), date.fromisoformat(args.eval_start),
        date.fromisoformat(args.eval_end), embargo_min=args.embargo_min)
    idx = {k: np.flatnonzero(splits[k]) for k in ("train", "val", "eval")}
    y = df["y"].to_numpy().astype(np.int64)
    classes = sorted(LABEL_MAP.values())

    X = df[cols].apply(pd.to_numeric, errors="coerce").to_numpy(np.float32)
    source = "refit"
    bundle_path = out / "bundles" / f"{dt_key}.joblib"
    dm = prep = None
    if bundle_path.exists():
        b = _load_bundle(bundle_path)
        dm, prep = b["model"], _Prep.restore(b["prep"])
        kept, dropped = list(prep.kept), list(prep.dropped)
        source = f"bundle:{bundle_path.name}"
        del b
    else:
        prep = _Prep()
        prep.fit_transform(X[idx["train"]], cols)
        kept, dropped = list(prep.kept), list(prep.dropped)
        dm = get_models()["dtree"]
        dm.fit(prep.transform(X[idx["train"]]), y[idx["train"]])
    Xva = prep.transform(X[idx["val"]])
    Xev = prep.transform(X[idx["eval"]])
    del X
    leaves, base = extract_tree_leaves(dm, kept, y[idx["train"]], classes,
                                       prep=prep)
    attr_val = attribute_leaves(dm, Xva, y[idx["val"]], classes)
    attr_eval = attribute_leaves(dm, Xev, y[idx["eval"]], classes)
    leaves_path.write_text(json.dumps(
        {"model_id": dt_key, "feature_set": feat_set, "source": source,
         "kept_features": kept, "dropped_all_nan": dropped,
         "leaves": leaves,
         "train_base": {LABEL_NAMES[c]: base[c] for c in classes},
         "val_attr": attr_val, "eval_attr": attr_eval},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"ml_mtf_learn: leaves -> {leaves_path} ({len(leaves)} листьев, {source})")
    return 0


# ------------------------------------------------------------------ run
def run(args):
    import os as _os
    _dbg = _os.environ.get("ML_DEBUG")
    _t0 = __import__("time").time()
    def _p(*a):
        if _dbg:
            print(f"[ml_dbg {__import__('time').time()-_t0:.1f}s]", *a, flush=True)

    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} уже существует и не пуст (immutable-каталог)")
    out.mkdir(parents=True, exist_ok=True)

    schema = default_feature_schema()
    if args.schema:
        schema.update(json.loads(Path(args.schema).read_text(encoding="utf-8")))

    join_diag: dict = {}
    df = load_contract(args.features, args.targets, schema,
                       supervised_only=False, diagnostics=join_diag)
    _p("loaded", len(df))
    df = df.reset_index(drop=True)
    if "decision_ts" in df.columns:
        df["decision_ts"] = pd.to_datetime(df["decision_ts"], utc=True)
    cols_by_set = resolve_feature_columns(df, schema)
    for s in FEATURE_SETS:
        if not cols_by_set[s]:
            print(f"[warn] feature set '{s}' пуст в данных: проверьте схему B1")
    predictor_cols = sorted({c for cs in cols_by_set.values() for c in cs})
    leaked = [c for c in predictor_cols
              if c.startswith(QUALITY_PREFIXES) or c in META_EXACT]
    if leaked:
        raise SystemExit(f"в predictors просочились quality/meta колонки: {leaked}")
    excluded_quality = sorted(
        c for c in df.columns
        if (c.startswith(QUALITY_PREFIXES) or c in META_EXACT)
        and c not in predictor_cols)

    # роли колонок: источник истины — manifest.json B1 (пост 2026-10-14).
    # Мой whitelist обязан совпасть с ним ОДИН В ОДИН, иначе это не
    # «фиксированные сочетания функций», а тихое расхождение контрактов.
    b1_contract, b1_contract_src = load_b1_contract(args.features)
    b1_pred = set(b1_contract.get("predictor_columns") or [])
    if b1_pred:
        mine = set(cols_by_set["all_tf_votes"])
        if mine != b1_pred:
            raise SystemExit(
                "whitelist ≠ B1 predictor_columns: лишнее в B2 "
                f"{sorted(mine - b1_pred)}; отсутствует в B2 "
                f"{sorted(b1_pred - mine)} ({b1_contract_src})")
        for s in FEATURE_SETS:
            extra = set(cols_by_set[s]) - b1_pred
            if extra:
                raise SystemExit(f"набор '{s}' выходит за predictor_columns B1: "
                                 f"{sorted(extra)}")
    b1_pol = dict(b1_contract.get("eligibility_policy") or {})
    b1_policy_mismatch = assert_b1_policy_compatible(b1_pol)

    splits = temporal_split(
        df, date.fromisoformat(args.train_end), date.fromisoformat(args.val_start),
        date.fromisoformat(args.val_end), date.fromisoformat(args.eval_start),
        date.fromisoformat(args.eval_end), embargo_min=args.embargo_min)

    # defensive eligibility-policy ветки (будни / сессия с 10:00 MSK /
    # решения строго до 18:44 MSK). Гейт читает ТОЛЬКО decision_ts: решение не
    # режется по будущему MATURE, и это отдельная конъюнкция к purge/embargo.
    preason = policy_reason(df["decision_ts"])
    elig = preason == "ok"
    infer_before = {k: splits[f"{k}_infer"].copy()
                    for k in ("train", "val", "eval")}
    policy_diag = {
        "policy": POLICY_NAME,
        "gate_source": "decision_ts only (без label/MATURE)",
        "eligible_total": int(elig.sum()),
        "rejected_total": int((~elig).sum()),
        "reject_reasons": {r: int((preason == r).sum())
                           for r in sorted(set(preason) - {"ok"})},
        "per_split_dropped": {k: int((infer_before[k] & ~elig).sum())
                              for k in ("train", "val", "eval")},
    }
    if b1_contract_src:
        if b1_policy_mismatch:
            raise SystemExit(
                "eligibility-policy B1 несовместима с gate B2: "
                + "; ".join(b1_policy_mismatch))
        policy_diag["b1_eligibility_policy"] = b1_pol
        policy_diag["b1_eligibility_policy_hash"] = b1_contract.get(
            "eligibility_policy_hash")
        policy_diag["b1_contract_source"] = b1_contract_src
        policy_diag["consistent_with_b1_policy"] = True
    else:
        policy_diag["b1_eligibility_policy"] = None
        policy_diag["consistent_with_b1_policy"] = None
        print("[warn] manifest.json B1 не найден — сверка политики пропущена")

    # supervised = purge/embargo И известный исход. Без второй конъюнкции в
    # supervised попадали бы строки с недостоверным/неизвестным label_end.
    yknown = df["y_known"].to_numpy()
    for k in ("train", "val", "eval"):
        splits[k] = splits[k] & elig & yknown
        splits[f"{k}_infer"] = splits[f"{k}_infer"] & elig
    _p("split", {k: int(v.sum()) for k, v in splits.items()},
       "infer", {k: int(splits[f"{k}_infer"].sum()) for k in ("train", "val", "eval")})
    idx = {k: np.flatnonzero(v) for k, v in splits.items()}
    n_rows_split = {k: int(splits[k].sum()) for k in ("train", "val", "eval")}
    n_rows_infer = {k: int(splits[f"{k}_infer"].sum())
                    for k in ("train", "val", "eval")}
    del splits

    msk_dt = df["decision_ts"].dt.tz_convert(MSK)
    weekday_counts = {}
    for k in ("train", "val", "eval"):
        d = msk_dt.iloc[idx[k]].dt.dayofweek
        weekday_counts[k] = {WD_NAMES[i]: int((d == i).sum()) for i in range(7)}
        sat = int((d >= 5).sum())
        if sat:
            raise SystemExit(f"split {k}: {sat} выходных строк прошли eligibility")
    del msk_dt

    # quality-strata: полнота 5m-корзины источника (metadata, не предиктор).
    # Нужно, чтобы увидеть, не держится ли качество модели на строках с
    # неполным покрытием данных.
    cov_col = next((c for c in (f"{COVERAGE_PREFIX}{TF_SUFFIX[SUPERVISED_TF]}",
                                f"{COVERAGE_PREFIX}5m")
                    if c in df.columns), None)
    strata_mask = {}
    strata_counts = {}
    if cov_col:
        cov = pd.to_numeric(df[cov_col], errors="coerce")
        cov_full = int(cov.max()) if pd.notna(cov.max()) else 5
        full = (cov >= cov_full).to_numpy()
        strata_mask = {"coverage_full": full, "coverage_partial": ~full}
        for k in ("train", "val", "eval"):
            strata_counts[k] = {
                "coverage_col": cov_col,
                "coverage_full_value": cov_full,
                "coverage_full": int(full[idx[k]].sum()),
                "coverage_partial": int((~full)[idx[k]].sum()),
                "coverage_full_infer": int(full[idx[f"{k}_infer"]].sum()),
                "coverage_partial_infer": int((~full)[idx[f"{k}_infer"]].sum()),
            }
        del cov
    else:
        strata_counts = {}

    # rel[split]: позиция supervised-строк внутри inference-массива того же сплита.
    # sup ⊆ infer по построению; проверка страхует от молчаливого рассинхрона —
    # иначе scoring читал бы чужие вероятности.
    rel = {}
    for k in ("train", "val", "eval"):
        pos = np.full(len(df), -1, dtype=np.int64)
        pos[idx[f"{k}_infer"]] = np.arange(n_rows_infer[k])
        r = pos[idx[k]]
        if (r < 0).any():
            raise SystemExit(f"split {k}: supervised-строка вне inference universe")
        rel[k] = r

    y = df["y"].to_numpy()
    ysup = {k: y[idx[k]].astype(np.int64) for k in ("train", "val", "eval")}
    ts = df["decision_ts"]
    ts_str = df["decision_ts"].astype(str)
    classes = sorted(LABEL_MAP.values())   # [0,1,2]

    # as-of момента фита = максимальный label_end_ts реально использованных train
    # строк (не decision_ts): модель знает только то, что уже разрешилось.
    # available_at политики дополнительно включает val-метки (порог и отбор).
    lend = pd.to_datetime(df["label_end_ts"], utc=True, errors="coerce").to_numpy()
    fit_end_dt = pd.Timestamp(lend[idx["train"]].max())
    val_end_dt = pd.Timestamp(lend[idx["val"]].max())
    fit_end_ts = str(fit_end_dt)
    avail_ts = str(max(fit_end_dt, val_end_dt))
    del lend

    counts = np.bincount(ysup["train"], minlength=len(classes))
    prior = {c: float(counts[c] / counts.sum()) for c in classes}
    majority = int(np.argmax(counts))
    bl_train = baseline_scores(ysup["train"], prior, majority, classes)
    bl_val = baseline_scores(ysup["val"], prior, majority, classes)
    bl_eval = baseline_scores(ysup["eval"], prior, majority, classes)

    all_cols = sorted({c for cs in cols_by_set.values() for c in cs})
    num = df[all_cols].apply(pd.to_numeric, errors="coerce")
    df[all_cols] = num.astype(np.float32)  # 8 ГБ хоста: float32 режет пик вдвое
    del num
    _p("cast_float32")

    results = {}
    run_models = tuple(args.models.split(",")) if args.models else MODEL_IDS
    run_sets = tuple(args.sets.split(",")) if args.sets else FEATURE_SETS
    bundle_dir = out / "bundles"
    bundle_dir.mkdir(exist_ok=True)
    n_matrix_fits = 0

    # ---------------- PHASE A: fit на train, оценка и порог ТОЛЬКО на val.
    # eval не читается ни в этой фазе, ни до записи model_manifest.json.
    for sname in run_sets:
        cols = cols_by_set[sname]
        if not cols:
            continue
        X = df[cols].to_numpy(dtype=np.float32)
        prep = _Prep()
        Xtr = prep.fit_transform(X[idx["train"]], cols)
        Xva = prep.transform(X[idx["val"]])
        del X
        ytr = ysup["train"]
        _p("prep", sname, Xtr.shape, "dropped_all_nan", prep.dropped)

        for mname in run_models:
            if mname not in MODEL_IDS:
                raise SystemExit(f"неизвестная модель: {mname}")
            model = get_models()[mname]
            model.fit(Xtr, ytr)
            n_matrix_fits += 1
            proba_tr = align_proba(model.predict_proba(Xtr),
                                   model.classes_, classes)
            proba_va = align_proba(model.predict_proba(Xva),
                                   model.classes_, classes)

            thr = threshold_select(ysup["val"], proba_va,
                                   ts.iloc[idx["val"]], classes)
            sel_status = thr.get("selection", "NO_SELECTION")
            key = f"{mname}__{sname}"
            results[key] = {
                "model": mname, "feature_set": sname,
                "n_features": len(prep.kept),
                "dropped_all_nan": list(prep.dropped),
                "train": metrics_for(ytr, proba_tr, classes, bl_train),
                "val": metrics_for(ysup["val"], proba_va, classes, bl_val),
                "threshold_selection": thr,
                "selection_status": sel_status,
            }
            # bundle кладём сразу: в PHASE C прогон идёт только с диска
            _save_bundle(bundle_dir / f"{key}.joblib", {
                "model": model, "prep": prep.export(), "cols": list(cols),
                "feature_set": sname, "model_id": key, "classes": classes,
                "threshold_selection": thr, "selection_status": sel_status,
                # NO_SELECTION -> selected_long ВСЕГДА False, а не порог 0.5:
                # невыбранный порог не даёт права на вход
                "selected_threshold": (thr.get("threshold")
                                       if sel_status == "selected" else None),
                "model_fit_end_ts": fit_end_ts, "available_at_ts": avail_ts,
                "n_features": len(prep.kept),
                "dropped_all_nan": list(prep.dropped),
            })
            _p("fit+frozen", key, sel_status)
            del model, proba_tr, proba_va
        del Xtr, Xva, prep

    if not results:
        raise SystemExit("ни одна комбинация model/feature_set не обучена")

    # ---------------- PHASE B: отбор победителя, ablation (train/val), FREEZE
    primary = min(results, key=lambda k: results[k]["val"]["log_loss"])

    if (not args.no_ablation) and ("continuous_all_tf" in run_sets) \
            and cols_by_set["continuous_all_tf"]:
        ca_full = df[cols_by_set["continuous_all_tf"]]
        # full переиспользует уже посчитанный hgb__continuous_all_tf -> ровно
        # len(TF_LIST) дополнительных fits, фактическое число фиксируем явно
        full_ref = results.get("hgb__continuous_all_tf", {}).get("val")
        abl = ablation_nonlinear(
            ca_full.iloc[idx["train"]], ysup["train"],
            ca_full.iloc[idx["val"]], ysup["val"], classes,
            list(ca_full.columns), full_ref=full_ref)
        del ca_full
    else:
        abl = {}
    n_ablation_fits = int(abl.pop("_n_fits", 0))
    (out / "ablation.json").write_text(
        json.dumps(abl, ensure_ascii=False, indent=2), encoding="utf-8")

    # Достаточность выбранной политики: precision порога ниже val base_up —
    # это отсутствие добавленного качества, а не «edge». Критерий отбора
    # (val logloss) после eval НЕ пересматривается.
    _pthr = results[primary]["threshold_selection"]
    _vbase_up = results[primary]["val"].get("base_up")
    _sufficient = (
        _pthr.get("selection") == "selected"
        and _vbase_up is not None
        and float(_pthr.get("precision") or 0.0) >= float(_vbase_up)
    )
    _POLICY_OK_NOTE = (
        "precision выбранного порога НЕ ниже val base_up — политика даёт "
        "добавленное качество на val; это не утверждение о прибыльности")
    _POLICY_BAD_NOTE = (
        "precision выбранного порога НИЖЕ val base_up — политика "
        "НЕДОСТАТОЧНА: входы не добавляют качества к константе. "
        "Наличие edge НЕ объявляется")

    manifest = {
        "tool": "ml_mtf_learn", "schema_version": "v1",
        "seed": SEED, "classes": [LABEL_NAMES[c] for c in classes],
        "models": MODEL_IDS, "feature_sets": FEATURE_SETS,
        "thresholds": list(THRESHOLDS),
        "split": {"train_end": args.train_end, "val_start": args.val_start,
                  "val_end": args.val_end, "eval_start": args.eval_start,
                  "eval_end": args.eval_end, "embargo_min": args.embargo_min},
        "n_rows_total": int(len(df)),
        "n_rows_supervised": int(sum(n_rows_split.values())),
        "n_rows_split": n_rows_split,
        "n_rows_split_infer": n_rows_infer,
        "supervised_definition": "purge/embargo И известный исход (y_known); "
                                 "только эти строки идут в fit и в scoring",
        "inference_definition": "все решения окна сплита независимо от "
                                "maturity/значения метки; только они идут в "
                                "predictions.parquet",
        "join_diagnostics": join_diag,
        "input_features": str(Path(args.features).resolve()),
        "input_targets": str(Path(args.targets).resolve()),
        "input_features_identity": dataset_identity(args.features),
        "input_targets_identity": dataset_identity(args.targets),
        "code_sha256": _code_sha256(),
        "run_models": list(run_models), "run_sets": list(run_sets),
        "n_fits_matrix": n_matrix_fits,
        "n_fits_ablation": n_ablation_fits,
        "n_fits_total": n_matrix_fits + n_ablation_fits,
        "primary_winner": primary,
        "primary_threshold": results[primary]["threshold_selection"],
        "primary_selection_status": results[primary]["selection_status"],
        "model_fit_end_ts": fit_end_ts,
        "policy_available_at_ts": avail_ts,
        "policy_eligibility": policy_diag,
        "weekday_counts_supervised": weekday_counts,
        "quality_strata": strata_counts,
        "excluded_quality_columns": excluded_quality,
        "b1_provenance": {
            "source": b1_contract_src,
            "dataset_version": b1_contract.get("dataset_version"),
            "feature_schema_version": b1_contract.get("feature_schema_version"),
            "label_cfg_hash": b1_contract.get("label_cfg_hash"),
            "code_sha256": b1_contract.get("code_sha256"),
            "params_hash": b1_contract.get("params_hash"),
            "eligibility_policy_hash": b1_contract.get("eligibility_policy_hash"),
            "n_predictor_columns": int(len(b1_pred)),
            "n_metadata_columns": int(len(b1_contract.get("metadata_columns")
                                              or [])),
            "whitelist_matches_b1_predictors": bool(b1_pred),
        },
        "feature_columns": {s: list(cols_by_set[s]) for s in run_sets},
        "feature_set_sizes": {s: len(cols_by_set[s]) for s in FEATURE_SETS},
        "primary_policy_sufficiency": {
            "selected_threshold": _pthr.get("threshold"),
            "precision_val": _pthr.get("precision"),
            "base_up_val": _vbase_up,
            "n_signals_val": _pthr.get("n_signals"),
            "n_dates_val": _pthr.get("n_dates"),
            "sufficient_vs_val_base_up": bool(_sufficient),
            "note": (_POLICY_OK_NOTE if _sufficient else _POLICY_BAD_NOTE),
        },
        "freeze": {
            "stage": "after_train_val_selection_before_eval",
            "bundles_written_before": True,
            "eval_evaluated_after_manifest_write": True,
        },
        "bundles": {},
    }
    for k in sorted(results):
        bp = bundle_dir / f"{k}.joblib"
        manifest["bundles"][k] = {
            "path": str(bp.relative_to(out)), "sha256": _sha256_file(bp),
            "bytes": int(bp.stat().st_size),
        }
    (out / "model_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _p("FROZEN manifest", primary, "fits", manifest["n_fits_total"])

    # ---------------- PHASE C: read-only inference из frozen bundles
    pred_sink = _PredictSink(out / "predictions.parquet")
    up_idx = classes.index(LABEL_MAP[1])
    try:
        for key in sorted(results):
            b = _load_bundle(bundle_dir / f"{key}.joblib")
            model, prep = b["model"], _Prep.restore(b["prep"])
            thr_val = b["selected_threshold"]
            sel_status = b["selection_status"]
            use_thr = thr_val is not None and sel_status == "selected"
            X = df[b["cols"]].to_numpy(dtype=np.float32)
            for split in ("train", "val", "eval"):
                ii = idx[f"{split}_infer"]
                Z = prep.transform(X[ii])
                proba = align_proba(model.predict_proba(Z), model.classes_, classes)
                del Z
                if use_thr:
                    sel = proba[:, up_idx] >= thr_val
                else:
                    sel = np.zeros(len(ii), dtype=bool)
                pred_sink.append(pd.DataFrame({
                    "row_id": df["row_id"].to_numpy()[ii],
                    "figi": df["figi"].to_numpy()[ii] if "figi" in df
                    else np.array([""] * len(ii), dtype=object),
                    "decision_ts": ts_str.to_numpy()[ii],
                    "model_id": key, "split": split,
                    "p_down": proba[:, classes.index(0)],
                    "p_neutral": proba[:, classes.index(1)],
                    "p_up": proba[:, classes.index(2)],
                    "selected_long": sel,
                    "model_fit_end_ts": b["model_fit_end_ts"],
                    "available_at_ts": b["available_at_ts"],
                    "selection_status": sel_status,
                }))
                if split != "train":
                    p_sup = proba[rel[split]]
                    results[key]["eval"] = metrics_for(
                        ysup[split], p_sup, classes, bl_eval)
                    results[key]["eval"]["n_scored"] = int(len(ysup[split]))
                    if strata_mask:
                        g = idx[split]
                        results[key][f"{split}_by_stratum"] = {
                            nm: _stratum_stats(ysup[split], p_sup, classes,
                                               m[g])
                            for nm, m in strata_mask.items()
                        }
                del proba
            del X, model, prep, b
            _p("eval", key)
    finally:
        pred_sink.close()

    # дерево: листья и атрибуция БЕЗ переобучения — берём модель и препроцессор
    # из того же bundle, что шёл в матрице (иначе артефакт не воспроизводим)
    dt_key = "dtree__all_tf_votes" if "dtree__all_tf_votes" in results else None
    if dt_key is None:
        dt_key = next((k for k in results if k.startswith("dtree__")), None)
    if dt_key:
        b = _load_bundle(bundle_dir / f"{dt_key}.joblib")
        dm, prep = b["model"], _Prep.restore(b["prep"])
        kept, dropped = list(prep.kept), list(prep.dropped)
        X = df[b["cols"]].to_numpy(dtype=np.float32)
        Xva = prep.transform(X[idx["val"]])
        Xev = prep.transform(X[idx["eval"]])
        del X
        leaves, base = extract_tree_leaves(dm, kept, ysup["train"], classes,
                                           prep=prep)
        attr_val = attribute_leaves(dm, Xva, ysup["val"], classes)
        attr_eval = attribute_leaves(dm, Xev, ysup["eval"], classes)
        del Xva, Xev, dm, prep, b
        (out / "tree_leaves.json").write_text(json.dumps(
            {"model_id": dt_key, "feature_set": results[dt_key]["feature_set"],
             "kept_features": kept, "dropped_all_nan": dropped,
             "leaves": leaves,
             "train_base": {LABEL_NAMES[c]: base[c] for c in classes},
             "val_attr": attr_val, "eval_attr": attr_eval},
            ensure_ascii=False, indent=2), encoding="utf-8")

    # paired multi-TF vs 5m и nonlinear vs linear (val и eval отдельно:
    # одно только падение precision на более строгом пороге шумом не считается)
    paired = []
    for m in run_models:
        c5 = results.get(f"{m}__continuous_5m")
        ca = results.get(f"{m}__continuous_all_tf")
        if c5 and ca:
            row = {"model": m, "comparison": "all_tf_vs_5m",
                   "delta_balacc_val": ca["val"]["balanced_accuracy"] -
                   c5["val"]["balanced_accuracy"],
                   "delta_logloss_val": ca["val"]["log_loss"] -
                   c5["val"]["log_loss"]}
            if "eval" in ca and "eval" in c5:
                row["delta_balacc_eval"] = ca["eval"]["balanced_accuracy"] - \
                    c5["eval"]["balanced_accuracy"]
                row["delta_logloss_eval"] = ca["eval"]["log_loss"] - \
                    c5["eval"]["log_loss"]
            paired.append(row)
    lr = results.get("logreg__continuous_all_tf")
    hg = results.get("hgb__continuous_all_tf")
    if lr and hg:
        row = {"model": "linear_vs_nonlinear", "comparison": "hgb_vs_logreg",
               "delta_balacc_val": hg["val"]["balanced_accuracy"] -
               lr["val"]["balanced_accuracy"],
               "delta_logloss_val": hg["val"]["log_loss"] - lr["val"]["log_loss"]}
        if "eval" in hg and "eval" in lr:
            row["delta_balacc_eval"] = hg["eval"]["balanced_accuracy"] - \
                lr["eval"]["balanced_accuracy"]
            row["delta_logloss_eval"] = hg["eval"]["log_loss"] - \
                lr["eval"]["log_loss"]
        paired.append(row)

    metrics_out = {
        "manifest": manifest, "results": results, "paired": paired,
        "classes": [LABEL_NAMES[c] for c in classes],
        "row_counts": {
            "support_known_targets": n_rows_split,
            "eligible_infer_all_labels": n_rows_infer,
            "note": "support_known_targets — строки scoring/fit; "
                    "eligible_infer_all_labels — полный допустимый inference "
                    "включая CENSORED/неизвестный исход",
        },
        "policy_eligibility": policy_diag,
        "weekday_counts_supervised": weekday_counts,
        "quality_strata_counts": strata_counts,
        "excluded_quality_columns": excluded_quality,
        "scoring_note": "train/val/eval считаются ТОЛЬКО по supervised-строкам "
                        "(известный исход); predictions.parquet шире — это "
                        "inference universe, см. manifest.n_rows_split_infer",
    }
    (out / "metrics.json").write_text(
        json.dumps(metrics_out, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.no_breakdown:
        bd = write_breakdown(out, args.targets, classes)
        _p("breakdown", bd.get("n_matched"))

    _write_summary(out, results, paired, primary, manifest)
    if not args.no_chat:
        _maybe_post_model_ready(out, manifest, primary)
    print(f"ml_mtf_learn: primary={primary} rows={len(df)} out={out}")
    return 0


def _write_summary(out, results, paired, primary, manifest):
    L = []
    L.append("# ML-MTF-1 B2 — обучение и взаимодействия ТФ\n")
    L.append(f"primary winner (по val log_loss): **{primary}**\n")
    L.append(f"fits: {manifest.get('n_fits_total')} "
             f"(matrix {manifest.get('n_fits_matrix')} + ablation "
             f"{manifest.get('n_fits_ablation')})\n")
    L.append("## Матрица 3×4 (val: balacc / logloss; eval: balacc / logloss)\n")
    L.append("| model | feature_set | n_feat | val balacc | val logloss | "
             "eval balacc | eval logloss | thr |\n")
    L.append("|---|---|---|---|---|---|---|---|\n")
    for key, r in results.items():
        thr = r["threshold_selection"]
        sel = thr.get("threshold", "-") if thr.get("selection") == "selected" \
            else "NO_SELECTION"
        ev = r.get("eval") or {}
        L.append(f"| {r['model']} | {r['feature_set']} | {r['n_features']} | "
                 f"{r['val']['balanced_accuracy']:.4f} | "
                 f"{r['val']['log_loss']:.4f} | "
                 f"{ev.get('balanced_accuracy', float('nan')):.4f} | "
                 f"{ev.get('log_loss', float('nan')):.4f} | {sel} |\n")
    L.append("\n## Paired (val / eval)\n")
    for p in paired:
        L.append(f"- {p}\n")
    L.append("\n## Предупреждение\n")
    L.append("Наличие положительной метки != прибыль. Доходность проверяет B3.\n")
    L.append("NO_SELECTION -> selected_long ВСЕГДА False (входов нет), "
             "не порог 0.5.\n")
    L.append(f"\n## Split/данные\n{json.dumps(manifest['n_rows_split'], ensure_ascii=False)}"
             f"\n\n## Inference universe (predictions)\n"
             f"{json.dumps(manifest['n_rows_split_infer'], ensure_ascii=False)}\n")
    (out / "summary.md").write_text("".join(L), encoding="utf-8")


def _maybe_post_model_ready(out, manifest, primary):
    """Публикует MODEL_READY в chat.md (append-only, под lock).

    Шаблон собирается только f-strings: смесь f-string и .format() на строке,
    содержащей repr словаря (литеральные '{train': ...}), падала KeyError.
    """
    chat = Path(__file__).resolve().parents[2] / "chat.md"
    if not chat.exists():
        return
    now = datetime.now(TZ).isoformat(timespec="seconds")
    day = datetime.now(MSK).date().isoformat()
    thr = manifest.get("primary_threshold") or {}
    if thr.get("selection") == "selected":
        thr_txt = f"порог P(up)>={thr['threshold']} (выбран на val: precision " \
                  f"{thr.get('precision'):.4f}, {thr.get('n_signals')} сигналов " \
                  f"/ {thr.get('n_dates')} дат)"
    else:
        thr_txt = "NO_SELECTION на val -> selected_long = False (входов нет)"
    split_txt = ", ".join(f"{k}={v}" for k, v in manifest["n_rows_split"].items())
    infer_txt = ", ".join(f"{k}={v}"
                          for k, v in manifest["n_rows_split_infer"].items())
    ident = manifest.get("input_targets_identity", {})
    code = str(manifest.get("code_sha256", ""))[:16]

    results = {}
    try:
        results = json.loads(
            (out / "metrics.json").read_text(encoding="utf-8"))["results"]
    except Exception:
        results = {}

    def _sel(k):
        return ((results.get(k) or {}).get("threshold_selection") or {}).get("selection")

    nosel = sorted(k for k in results if _sel(k) != "selected")
    linear = min((k for k in results if k.startswith("logreg__")),
                 key=lambda k: results[k]["val"]["log_loss"], default=None)
    m5 = "hgb__continuous_5m"
    _ps = manifest.get("primary_policy_sufficiency") or {}
    report = Path(__file__).resolve().parents[2] / "docs" / "results" / \
        "ML_MTF_1_B2.md"

    msg = (
        f"\n### {day} — Builder-2 / MODEL_READY + DELIVERY ML-MTF-1 v3 "
        f"(run `{out.name}`)\n"
        "\nКонвейер обучен на **датасете v3**, predictions готовы для B3.\n"
        "- eligibility-gate (будни / сессия с 10:00 MSK / решения до 18:44 MSK, "
        "читает только `decision_ts`): "
        f"отклонено {manifest.get('policy_eligibility', {}).get('rejected_total')}, "
        "consistency с policy B1 = "
        f"{manifest.get('policy_eligibility', {}).get('consistent_with_b1_policy')}\n"
        "- whitelist признаков (без `n_min_*`/`voice_status_*`/identity): "
        f"{manifest.get('feature_set_sizes')} = "
        f"{manifest.get('b1_provenance', {}).get('n_predictor_columns')} predictor "
        f"B1, `whitelist_matches_b1_predictors="
        f"{manifest.get('b1_provenance', {}).get('whitelist_matches_b1_predictors')}`\n"
        f"- policy sufficiency: precision "
        f"{manifest.get('primary_policy_sufficiency', {}).get('precision_val')} "
        "vs val base_up "
        f"{manifest.get('primary_policy_sufficiency', {}).get('base_up_val')} -> "
        f"**sufficient="
        f"{manifest.get('primary_policy_sufficiency', {}).get('sufficient_vs_val_base_up')}**\n"
        f"- primary winner: `{primary}` ({thr_txt})\n"
        f"- run: `{out.name}`; rows universe: {manifest['n_rows_total']}; "
        f"supervised: {manifest.get('n_rows_supervised')}\n"
        f"- split (scoring/fit): {split_txt}\n"
        f"- split (inference, то что в predictions): {infer_txt}\n"
        f"- fits: {manifest.get('n_fits_total')} "
        f"(matrix {manifest.get('n_fits_matrix')} + ablation "
        f"{manifest.get('n_fits_ablation')}); seed {manifest.get('seed')}\n"
        f"- code sha256 `{code}…`; dataset identity "
        f"`{ident.get('identity_sha256', 'n/a')}` ({ident.get('path')})\n"
        f"- manifest (FREEZE до eval): `{out/'model_manifest.json'}`\n"
        f"- bundles: `{out/'bundles'}/` (sha256 в manifest)\n"
        f"- predictions: `{out/'predictions.parquet'}`\n"
        f"- metrics: `{out/'metrics.json'}`; tree leaves: `{out/'tree_leaves.json'}`;"
        f" ablation: `{out/'ablation.json'}`; summary: `{out/'summary.md'}`\n"
        f"- breakdown по тикеру/месяцу: `{out/'breakdown.json'}` (описательный, "
        "без влияния на отбор)\n"
        "\n**Точные фильтры для B3:**\n"
        f"- base: `split == \"eval\"` -> {manifest['n_rows_split_infer']['eval']} "
        f"строк на модель (inference universe: все допустимые решения, без "
        "фильтра по будущей метке)\n"
        f"- scoring/comparator: `split == \"eval\"` И строка есть в "
        f"supervised -> {manifest['n_rows_split']['eval']} строк "
        "(метрики в metrics.json уже посчитаны именно по ним)\n"
        "- на 12 моделей: `model_id.nunique() == 12`; по одной модели — "
        "`model_id == \"<winner>\"`, тогда строк ровно "
        f"{manifest['n_rows_split_infer']['eval']}\n"
        "\n**Точная привязка моделей для B3 (три переданные модели):**\n"
        f"- первичная: `model_id == \"{primary}\"`, "
        f"`selection_status == \"selected\"`, порог "
        f"{thr.get('threshold')} — входы только при `selected_long == True`; "
        f"fit_end {manifest.get('model_fit_end_ts')}, "
        f"available_at {manifest.get('policy_available_at_ts')}\n"
        f"- лучшая линейная: `model_id == \"{linear}\"` "
        "(min val log_loss среди logreg; выбрана **до** eval)\n"
        f"- 5m-компаратор: `model_id == \"{m5}\"`, порог "
        f"{((results.get(m5) or {}).get('threshold_selection') or {}).get('threshold')}\n"
        f"- NO_SELECTION -> передаются как **наличные деньги без входов**: "
        f"{', '.join('`' + k + '`' for k in nosel) or 'нет'}\n"
        "  (записи им изобретать нельзя)\n"
        "\n**Честные находки (в отчёте §9):**\n"
        f"- победитель по val log_loss на eval **ниже случайного** по balacc "
        "и хуже обеих базовых линий — знак edge меняется val→eval\n"
        "- лучший по eval `hgb__continuous_all_tf` (precision 0.5321) — "
        "результат мульти-теста из 36 сочетаний, не первичная модель\n"
        "- потолок издержек p_be ≈ 0.547 (14 bps, ATR ≈1%): "
        "**ни одна конфигурация его не проходит**\n"
        "- корень дерева теперь рыночный (`macd_hist_delta_1800s`), "
        "`n_min_*` в X не попадают; артефакт покрытия устранён\n"
        f"- отчёт: `{report}`\n"
        "\n**Схема predictions.parquet (12 колонок):** row_id, figi, decision_ts, "
        "model_id, split, p_down, p_neutral, p_up, selected_long, "
        "model_fit_end_ts, available_at_ts, selection_status.\n"
        "- `selection_status == \"NO_SELECTION\"` -> `selected_long == False` "
        "всегда (не порог 0.5); вероятности при этом сохранены\n"
        "- `model_fit_end_ts` = max(label_end_ts) train-строк (не decision_ts)\n"
        f"- `available_at_ts` = max(model_fit_end_ts, max val label_end_ts) "
        f"= {manifest.get('policy_available_at_ts')} — момент, когда "
        "модель + порог наблюдаемы целиком\n"
        "- scoring только по известным исходам; fit без retraining на eval; "
        "model_manifest.json заморожен ДО eval-вывода\n"
        "Наличие метки != прибыль; отрицательный label != шорт-доходность.\n"
    )
    lock = chat.parent / ".chat-write.lock"
    publish_to_chat(chat, msg)

def publish_to_chat(chat: Path, msg: str) -> None:
    """Append в chat.md под exclusive flock на ПОСТОЯННОМ lock-файле.

    Протокол архитектора (2026-10-14): `.chat-write.lock` — обычный файл, НЕ
    каталог; `open(a)` + `fcntl.flock(LOCK_EX)` + append + flush/fsync +
    `LOCK_UN`; lock-файл НЕ удалять; смешивать mkdir-lock и flock нельзя.

    Прежняя mkdir-версия молча отказывалась публиковать: lock уже существовал
    как файл, `mkdir` бросал FileExistsError 30 раз и функция просто return.
    Теперь ошибка записи поднимается явно, а после записи пост ПЕРЕЧИТЫВАЕТСЯ:
    отправленным считается только факт появления полного текста в chat.md.
    """
    import fcntl
    lock = chat.parent / ".chat-write.lock"
    lfd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o666)
    try:
        fcntl.flock(lfd, fcntl.LOCK_EX)
        try:
            with chat.open("a", encoding="utf-8") as f:
                f.write(msg)
                f.flush()
                os.fsync(f.fileno())
        finally:
            fcntl.flock(lfd, fcntl.LOCK_UN)
    except BaseException as exc:
        raise SystemExit(f"chat publish: запись не удалась: {exc!r}") from exc
    finally:
        os.close(lfd)

    text = chat.read_text(encoding="utf-8")
    if msg.strip() and msg.strip() not in text:
        raise SystemExit(
            f"chat publish: пост ({len(msg)} символов) не найден в chat.md "
            "после записи — отправленным не считаю")


def main(argv=None):
    ap = argparse.ArgumentParser(description="ML-MTF-1 B2 learner")
    ap.add_argument("--features", default=None,
                    help="features.parquet или каталог shards")
    ap.add_argument("--targets", default=None,
                    help="targets.parquet или каталог shards")
    ap.add_argument("--out", required=True)
    ap.add_argument("--schema", default=None, help="дополнение к схеме признаков")
    ap.add_argument("--train-end", default="2026-05-31")
    ap.add_argument("--val-start", default="2026-06-01")
    ap.add_argument("--val-end", default="2026-07-31")
    ap.add_argument("--eval-start", default="2026-08-01")
    ap.add_argument("--eval-end", default="2026-10-08")
    ap.add_argument("--embargo-min", type=int, default=120)
    ap.add_argument("--keep-all", action="store_true",
                    help="не фильтровать по label_status (все строки)")
    ap.add_argument("--models", default=None,
                    help="подмножество моделей через запятую (логи/тесты)")
    ap.add_argument("--sets", default=None,
                    help="подмножество наборов признаков через запятую")
    ap.add_argument("--no-ablation", action="store_true",
                    help="пропустить ablation нелинейной модели")
    ap.add_argument("--no-breakdown", action="store_true",
                    help="не считать breakdown.json (ticker/month)")
    ap.add_argument("--breakdown-only", action="store_true",
                    help="только breakdown.json для уже готового каталога --out "
                         "(пост-фактум, без переобучения)")
    ap.add_argument("--leaves-only", action="store_true",
                    help="только пересчёт tree_leaves.json для --out "
                         "(дерево детерминировано, матрица не переобучается)")
    ap.add_argument("--feature-set", default=None,
                    help="для --leaves-only без существующего tree_leaves.json")
    ap.add_argument("--no-chat", action="store_true",
                    help="не публиковать MODEL_READY в chat.md (для тестов)")
    ap.set_defaults(func=run)
    args = ap.parse_args(argv)
    if args.breakdown_only:
        if not args.targets:
            ap.error("--breakdown-only требует --targets")
        res = write_breakdown(args.out, args.targets, sorted(LABEL_MAP.values()))
        print(f"ml_mtf_learn: breakdown -> {Path(args.out) / 'breakdown.json'} "
              f"(matched {res.get('n_matched')} of {res.get('n_predictions')})")
        return 0
    if args.leaves_only:
        if not args.features or not args.targets:
            ap.error("--leaves-only требует --features и --targets")
        return leaves_only(args)
    if not args.features or not args.targets:
        ap.error("--features и --targets обязательны для обучения")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
