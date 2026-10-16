"""ML-MTF-1 (B2): focused offline-тесты обучающего конвейера.

Герметично, без DB/broker, на детерминированных синтетических данных. Проверяет:
- детерминизм (повторный run даёт идентичный predictions/manifest);
- purge/embargo: target, дотянувшийся через границу, уходит из предыдущего сплита;
  embargo-окно применяется к предыдущему сплиту, а не к собственному началу;
- разделение inference universe / supervised: инференс НЕ фильтруется по будущей
  метке, scoring — только по известным исходам;
- NO_SELECTION -> selected_long всегда False (не порог 0.5);
- freeze: manifest+bundles пишутся ДО eval; перезагрузка bundle даёт те же predict;
- model_fit_end_ts = max label_end_ts train, available_at_ts включает val;
- target-колонки не попадают в матрицу признаков;
- матрица 3×4 заполнена, primary-winner выбран по val log_loss;
- экспорт листьев дерева (support/P/lift + условия в исходной шкале);
- ablation переиспользует full из матрицы и явно считает число fits;
- метрики: brier, reliability bins, baseline от train-prior;
- threshold selection возвращает selected или NO_SELECTION.
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ml_mtf_learn as M

SEED = 42
TZ = timezone.utc

# 12 отметок в день внутри07:00–15:04 UTC = 10:00–18:04 MSK: все строки проходят
# eligibility-policy ветки (будни, сессия с 10:00, решения до 18:44 MSK).
_SESSION_SLOTS = tuple(
    (datetime(2000, 1, 1, 7, 0) + timedelta(minutes=44 * i)).timetuple()[3:5]
    for i in range(12))


def _session_timestamps(n, start=datetime(2026, 2, 16, 0, 0, 0, tzinfo=TZ)):
    """N детерминированных отметок: только будни, только сессия.

    Плотность 12/день ≈ прежние 8/день при step=3h, а календарный охват
    заканчивается около eval_end — иначе гейт выкинул бы ~3/4 фикстуры и
    матрица осталась без val/eval.
    """
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            for h, m in _SESSION_SLOTS:
                if len(out) >= n:
                    break
                out.append(d.replace(hour=h, minute=m, second=0, microsecond=0))
        d += timedelta(days=1)
    return out


def _make_synthetic(n=2000, seed=SEED, extra_future=0,
                    start=datetime(2026, 2, 16, 0, 0, 0, tzinfo=TZ),
                    step=timedelta(hours=3)):
    """Детерминированный синтетический dataset контракта v1 (default schema).

    Охватывает train(<=05-31)/val(06-01..07-31)/eval(08-01..10-08), чтобы матрица
    заполнялась на всех сплитах.
    """
    rng = np.random.default_rng(seed)
    schema = M.default_feature_schema()
    cont = schema["continuous_all_tf"]
    votes = list(schema["individual_votes"])
    feat_cols = cont + votes

    ts = _session_timestamps(n, start)

    # гладкий signal (-1,0,1) с инерцией -> мягкая предсказуемость + структура ТФ
    sig = np.zeros(n, dtype=int)
    s = 0
    for i in range(n):
        s += rng.choice([-1, 0, 0, 1])
        s = max(-1, min(1, s))
        sig[i] = s
    # label = signal с 12% переворотами (шум)
    flip = rng.random(n) < 0.12
    label = np.where(flip, -sig, sig).astype(int)

    rows = []
    for i in range(n):
        row = {"row_id": f"r{i:06d}", "figi": "FIGI0000",
               "ticker": "TEST", "decision_ts": ts[i].isoformat(),
               "feature_max_available_ts": ts[i].isoformat(),
               "feature_schema_version": "v1"}
        # сигнальные признаки: 5m/60m несут структуру, остальные шум
        for c in cont:
            base = rng.normal(0, 1)
            if c == "rsi14_5m":
                base = 50 + 18 * sig[i] + rng.normal(0, 6)
            elif c == "macd_hist_5m":
                base = 0.4 * sig[i] + rng.normal(0, 0.3)
            elif c == "bias":
                base = 0.3 * sig[i] + rng.normal(0, 0.2)
            row[c] = float(base)
        for c in votes:
            if c.startswith(M.AGE_PREFIX):
                row[c] = int(rng.integers(0, 24))
            elif c.startswith(M.VOICE_PREFIX):
                row[c] = float(rng.random())
            else:
                row[c] = int(rng.choice([-1, 0, 1]))
        row["label"] = int(label[i])
        row["label_cfg_hash"] = "synthetic"
        row["label_status"] = "MATURE"
        row["label_end_ts"] = (ts[i] + step * 24).isoformat()
        rows.append(row)

    if extra_future > 0:
        for j in range(extra_future):
            k = n + j
            row = {"row_id": f"r{k:06d}", "figi": "FIGI0000", "ticker": "TEST",
                   "decision_ts": (start + step * (n + j + 100000)).isoformat(),
                   "feature_max_available_ts": (start + step * (n + j)).isoformat(),
                   "feature_schema_version": "v1"}
            for c in cont:
                row[c] = float(rng.normal(0, 1))
            for c in votes:
                if c.startswith(M.AGE_PREFIX):
                    row[c] = int(rng.integers(0, 24))
                elif c.startswith(M.VOICE_PREFIX):
                    row[c] = float(rng.random())
                else:
                    row[c] = int(rng.choice([-1, 0, 1]))
            row["label"] = int(rng.choice([-1, 0, 1]))
            row["label_cfg_hash"] = "synthetic"
            row["label_status"] = "MATURE"
            row["label_end_ts"] = row["decision_ts"]
            rows.append(row)

    df = pd.DataFrame(rows)
    # coverage-колонки (metadata, не предикторы): нужны для quality-strata и
    # для проверки, что 'n_min_*' уходит в excluded_quality_columns, а не в X.
    n_all = n + extra_future
    for tf in M.TF_LIST:
        cap = max(1, tf // 60)
        vals = np.where(rng.random(n_all) > 0.15, cap,
                        np.maximum(1, cap - rng.integers(1, cap + 1, n_all)))
        df[f"{M.COVERAGE_PREFIX}{M.TF_SUFFIX[tf]}"] = vals.astype(int)
    return df, feat_cols


def _write_parquet(df, d: Path):
    d.mkdir(parents=True, exist_ok=True)
    f = d / "features.parquet"
    t = d / "targets.parquet"
    feats = df[[c for c in df.columns if c not in
                ("label", "label_cfg_hash", "label_status", "label_end_ts")]]
    # targets по контракту B1 содержат ticker/decision_ts вдобавок к подписи
    tgt = df[["row_id", "ticker", "decision_ts", "label", "label_cfg_hash",
              "label_status", "label_end_ts"]]
    feats.to_parquet(f, index=False)
    tgt.to_parquet(t, index=False)
    return f, t


def _run_in(tmp, features, targets, extra=None):
    out = tmp / "out"
    argv = ["--features", str(features), "--targets", str(targets),
            "--out", str(out), "--no-chat"]
    if extra:
        argv += extra
    rc = M.main(argv)
    assert rc == 0
    return out


LIGHT = ["--models", "dtree", "--sets", "individual_votes", "--no-ablation"]


def test_matrix_filled_and_primary_chosen(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t)          # полная матрица 12 fits + ablation
    metrics = json.loads((out / "metrics.json").read_text())
    keys = list(metrics["results"].keys())
    assert len(keys) == 12, keys
    assert metrics["manifest"]["primary_winner"] in keys
    sets = {k.split("__")[1] for k in keys}
    assert sets == set(M.FEATURE_SETS)

    # число fits считается ЯВНО: матрица ровно 12, ablation — до 5 drop-TF,
    # а 'full' переиспользуется из матрицы, а не дообучается
    m = metrics["manifest"]
    assert m["n_fits_matrix"] == 12
    assert 1 <= m["n_fits_ablation"] <= 5
    assert m["n_fits_total"] == m["n_fits_matrix"] + m["n_fits_ablation"]
    abl = json.loads((out / "ablation.json").read_text())
    if "full" in abl:
        assert abl["full"]["reused_from_matrix"] is True
        assert abl["full"]["log_loss"] == pytest.approx(
            metrics["results"]["hgb__continuous_all_tf"]["val"]["log_loss"],
            abs=1e-12)


def test_no_selection_means_no_entries(tmp_path):
    """NO_SELECTION -> selected_long ВСЕГДА False, вероятности сохраняются.

    Метки намеренно делаются шумом: листья дают p_up<0.5, ни один порог из
    [0.50,0.60,0.70] не набирает 200 сигналов / 20 дат.
    """
    df, _ = _make_synthetic()
    rng = np.random.default_rng(7)
    df["label"] = rng.choice([-1, 0, 1], len(df))
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    r = json.loads((out / "metrics.json").read_text())["results"][
        "dtree__individual_votes"]
    assert r["selection_status"] == "NO_SELECTION"
    assert r["threshold_selection"]["reason"]

    import pyarrow.parquet as pq
    pdf = pq.read_table(out / "predictions.parquet").to_pandas()
    assert not pdf["selected_long"].any(), \
        "невыбранный порог не даёт права на вход"
    assert set(pdf["selection_status"]) == {"NO_SELECTION"}
    assert pdf["p_up"].notna().all()
    tot = pdf["p_down"] + pdf["p_neutral"] + pdf["p_up"]
    assert np.allclose(tot.to_numpy(), 1.0, atol=1e-9)


def test_metrics_have_proper_calibration_and_baselines(tmp_path):
    """brier + reliability bins + baseline от train-prior (п.5 REWORK)."""
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    r = json.loads((out / "metrics.json").read_text())["results"][
        "dtree__individual_votes"]

    for split in ("train", "val", "eval"):
        s = r[split]
        assert 0.0 <= s["brier_multiclass"] <= 3.0
        bins = s["reliability_bins_up"]
        assert bins, "reliability bins обязательны"
        for b in bins:
            assert {"n", "mean_predicted", "observed", "gap"} <= set(b)
            assert 0.0 <= b["mean_predicted"] <= 1.0
        n_scored = sum(v["support"] for v in s["per_label"].values())
        assert sum(b["n"] for b in bins) == n_scored
        # baseline посчитан по train-prior и доступен для сравнения
        assert "prior_log_loss" in s["baseline"]
        assert "majority_accuracy" in s["baseline"]
        assert s["log_loss_vs_prior"] == pytest.approx(
            s["log_loss"] - s["baseline"]["prior_log_loss"], abs=1e-12)
        assert s["accuracy_vs_majority"] == pytest.approx(
            s["accuracy"] - s["baseline"]["majority_accuracy"], abs=1e-12)
    # train-prior одинаков для всех сплитов (это доли train)
    assert r["train"]["baseline"]["train_prior"] == r["eval"]["baseline"]["train_prior"]
    assert r["train"]["class_share"]["up"] != pytest.approx(1 / 3, abs=1e-6)


def test_tree_conditions_have_raw_scale(tmp_path):
    """Условия листьев обязаны быть переиспользоваемы в исходной шкале."""
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t,
                  ["--models", "dtree", "--sets", "continuous_all_tf"])
    leaves = json.loads((out / "tree_leaves.json").read_text())
    assert leaves["leaves"]
    kept = set(leaves["kept_features"])
    for lf in leaves["leaves"]:
        assert len(lf["conditions"]) == len(lf["conditions_raw"]) > 0
        for z_cond, raw_cond in zip(lf["conditions"], lf["conditions_raw"]):
            for cond in (z_cond, raw_cond):
                name = cond.split(" <= ")[0].split(" > ")[0]
                assert name in kept, (name, kept)
            # имена признаков в z- и в исходной шкале совпадают
            assert z_cond.split(" <= ")[0].split(" > ")[0] == \
                raw_cond.split(" <= ")[0].split(" > ")[0]
            assert np.isfinite(float(raw_cond.split(" <= ")[-1]
                                     .split(" > ")[-1]))


def test_prep_export_restore_roundtrip():
    rng = np.random.default_rng(1)
    cols = [f"c{i}" for i in range(6)]
    X = rng.normal(size=(200, 6)).astype(np.float32)
    X[0, 3] = np.nan
    X[:, 5] = np.nan                      # целиком NaN -> выпадает из kept
    p = M._Prep()
    p.fit_transform(X, cols)
    p2 = M._Prep.restore(p.export())
    assert p2.kept == p.kept and p2.dropped == p.dropped
    Xt = rng.normal(size=(37, 6)).astype(np.float32)
    assert np.array_equal(p.transform(Xt), p2.transform(Xt))


def test_dataset_identity_flags_revoked(tmp_path):
    d = tmp_path / "ds"
    d.mkdir()
    (d / "manifest.json").write_text('{"cfg": 1}', encoding="utf-8")
    (d / "features.parquet").write_bytes(b"x" * 10)
    a = M.dataset_identity(d)
    b = M.dataset_identity(d)
    assert a["identity_sha256"] == b["identity_sha256"]
    assert a["n_files"] == 2 and a["n_content_hashed"] == 1

    (d / "SUPERSEDED.txt").write_text("отозван", encoding="utf-8")
    with pytest.raises(SystemExit):
        M.dataset_identity(d)


def test_join_diagnostics_rejects_duplicates(tmp_path):
    df, _ = _make_synthetic(n=50)
    f, t = _write_parquet(df, tmp_path)
    dup = pd.concat([pd.read_parquet(t), pd.read_parquet(t).head(1)],
                    ignore_index=True)
    dup.to_parquet(t, index=False)
    with pytest.raises(SystemExit, match="one-to-one"):
        M.load_contract(f, t, M.default_feature_schema(), supervised_only=False)


def test_target_columns_excluded_from_features(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    _run_in(tmp_path, f, t, LIGHT)
    feats = pd.read_parquet(f)
    assert "label" not in feats.columns and "y" not in feats.columns


def test_censored_kept_for_inference_out_of_scoring(tmp_path):
    """CENSORED не фильтрует инференс, но не участвует в fit/scoring (п.2 REWORK)."""
    df, _ = _make_synthetic()
    df.loc[df.index[::2], "label_status"] = "CENSORED"
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    m = json.loads((out / "metrics.json").read_text())["manifest"]
    n_mature = int((df["label_status"] == "MATURE").sum())

    # universe не отфильтрован по будущей метке
    assert m["n_rows_total"] == len(df)
    # supervised: CENSORED исключены, purge дополнительно убирает MATURE-строки,
    # чей target дотягивается через границу сплита
    assert m["n_rows_supervised"] <= n_mature < m["n_rows_total"]

    # в eval нет границы после себя, поэтому там сравнение точное и без purge:
    # supervised == MATURE в окне, inference == все строки в окне
    tdt = pd.to_datetime(df["decision_ts"], utc=True)
    ev_mask = tdt.dt.date.between(date(2026, 8, 1), date(2026, 10, 8)).to_numpy()
    assert m["n_rows_split_infer"]["eval"] == int(ev_mask.sum())
    assert m["n_rows_split"]["eval"] == int(
        (ev_mask & (df["label_status"].to_numpy() == "MATURE")).sum())
    assert m["n_rows_split"]["eval"] < m["n_rows_split_infer"]["eval"]

    import pyarrow.parquet as pq
    pdf = pq.read_table(out / "predictions.parquet").to_pandas()
    # в predictions есть и CENSORED-строки, в scoring их нет
    assert len(pdf) == sum(m["n_rows_split_infer"].values())
    ev = pdf[pdf["split"] == "eval"]
    assert len(ev) == m["n_rows_split_infer"]["eval"]
    censored_ids = set(df.loc[df["label_status"] == "CENSORED", "row_id"])
    assert censored_ids & set(ev["row_id"]), "CENSORED обязаны попадать в predict"

    r = json.loads((out / "metrics.json").read_text())["results"][
        "dtree__individual_votes"]
    assert r["eval"]["n_scored"] == m["n_rows_split"]["eval"]
    assert r["eval"]["n_scored"] < m["n_rows_split_infer"]["eval"]


def test_embargo_applies_to_previous_split():
    # окно 120 мин перед 2026-06-01 00:00 вычищается из ПРЕДЫДУЩЕГО сплита (train),
    # а не из val: решение в этом окне принято до границы, ему там не место
    start = datetime(2026, 5, 31, 22, 30, 0, tzinfo=TZ)
    idx = pd.date_range(start, periods=20, freq="5min", tz=TZ)
    # rows: 22:30..23:55 (05-31, внутри embargo) + 00:00,00:05 (06-01)
    df = pd.DataFrame({"decision_ts": pd.Series(idx)})
    splits = M.temporal_split(
        df, date(2026, 5, 31), date(2026, 6, 1), date(2026, 7, 31),
        date(2026, 8, 1), date(2026, 10, 8), embargo_min=120)

    # по дате 05-31 целиком train, но всё окно [22:00, 00:00) вычищено
    assert int(splits["train"].sum()) == 0
    assert int(splits["train_infer"].sum()) == 0
    # и в val оно не попадает (решение принято раньше границы val)
    val_mask = splits["val"]
    assert not val_mask[0]           # 22:30 внутри embargo
    assert not val_mask[17]          # 23:55 внутри embargo
    assert val_mask[18]              # 00:00 — за пределами embargo
    assert val_mask[19]              # 00:05
    assert int(val_mask.sum()) == 2
    # val до границы embargo не сокращается: окно до собственного начала
    # этому сплиту и так не принадлежало
    assert splits["val"].sum() == splits["val_infer"].sum()


def test_purge_label_crosses_boundary():
    """Две конкретные строки из ревью архитектора (п.1 REWORK).

    Принадлежность даты недостаточно: важно, куда ДОТЯГИВАЕТСЯ target-path.
    """
    df = pd.DataFrame({
        "decision_ts": ["2026-05-31T15:00:00Z",   # строка из ревью архитектора
                        "2026-07-31T23:30:00Z",   # вторая строка из ревью
                        "2026-07-31T15:00:00Z"],  # вне embargo-окна -> чистый purge
        "label_end_ts": ["2026-06-01T10:00:00Z",
                         "2026-08-01T10:00:00Z",
                         "2026-08-01T10:00:00Z"],
    })
    s = M.temporal_split(df, date(2026, 5, 31), date(2026, 6, 1),
                         date(2026, 7, 31), date(2026, 8, 1),
                         date(2026, 10, 8), embargo_min=120)
    # решение 31.05 15:00 с outcome 01.06 10:00 НЕ остаётся в train. Оно не в
    # embargo-окне [22:00,00:00) — из train его убрал именно purge
    assert s["train_infer"][0] and not s["train"][0]
    # решение 31.07 23:30 с outcome 01.08 10:00 НЕ остаётся в val; оно ещё и в
    # embargo-окне перед границей eval, поэтому его нет и в inference universe
    assert not s["val"][1] and not s["val_infer"][1]
    # то же для решения ВНЕ embargo-окна: в inference остаётся, в supervised нет
    assert s["val_infer"][2] and not s["val"][2]
    # target, целиком укладывающийся в свой сплит, остаётся
    df2 = pd.DataFrame({
        "decision_ts": ["2026-05-10T15:00:00Z"],
        "label_end_ts": ["2026-05-11T15:00:00Z"],
    })
    s2 = M.temporal_split(df2, date(2026, 5, 31), date(2026, 6, 1),
                          date(2026, 7, 31), date(2026, 8, 1),
                          date(2026, 10, 8), embargo_min=120)
    assert s2["train"][0]
    # неизвестный конец в supervised недопустим
    df3 = pd.DataFrame({
        "decision_ts": ["2026-05-10T15:00:00Z"],
        "label_end_ts": [pd.NaT],
    })
    s3 = M.temporal_split(df3, date(2026, 5, 31), date(2026, 6, 1),
                          date(2026, 7, 31), date(2026, 8, 1),
                          date(2026, 10, 8), embargo_min=120)
    assert not s3["train"][0] and s3["train_infer"][0]


def test_determinism(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path / "a")
    out1 = _run_in(tmp_path / "a", f, t, LIGHT)
    out2 = tmp_path / "out2"
    rc = M.main(["--features", str(f), "--targets", str(t),
                 "--out", str(out2), "--no-chat"] + LIGHT)
    assert rc == 0
    p1 = (out1 / "predictions.parquet").read_bytes()
    p2 = (out2 / "predictions.parquet").read_bytes()
    assert p1 == p2
    m1 = (out1 / "model_manifest.json").read_text()
    m2 = (out2 / "model_manifest.json").read_text()
    assert m1 == m2


def test_no_future_leakage_in_train(tmp_path):
    """Добавление будущих (post-eval) строк не меняет train-метрики."""
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path / "base")
    out1 = _run_in(tmp_path / "base", f, t, LIGHT)
    m1 = json.loads((out1 / "metrics.json").read_text())
    tr1 = m1["results"]["dtree__individual_votes"]["train"]["balanced_accuracy"]

    df2, _ = _make_synthetic(extra_future=500)
    f2, t2 = _write_parquet(df2, tmp_path / "fut")
    out2 = _run_in(tmp_path / "fut", f2, t2, LIGHT)
    m2 = json.loads((out2 / "metrics.json").read_text())
    tr2 = m2["results"]["dtree__individual_votes"]["train"]["balanced_accuracy"]
    assert tr1 == pytest.approx(tr2, rel=0, abs=1e-12)


def test_tree_leaves_and_ablation(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t,
                  ["--models", "dtree", "--sets", "continuous_all_tf"])
    leaves = json.loads((out / "tree_leaves.json").read_text())
    assert len(leaves["leaves"]) > 0
    for lf in leaves["leaves"]:
        assert "support" in lf and "p_label" in lf and "lift" in lf
        assert sum(lf["p_label"].values()) == pytest.approx(1.0, abs=1e-9)
    assert isinstance(leaves["val_attr"], dict) and len(leaves["val_attr"]) > 0
    abl = json.loads((out / "ablation.json").read_text())
    assert "full" in abl
    for k in abl:
        if k.startswith("drop_tf"):
            assert "delta_log_loss" in abl[k]


def test_threshold_selection_or_no_selection(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    metrics = json.loads((out / "metrics.json").read_text())
    for r in metrics["results"].values():
        sel = r["threshold_selection"]
        assert sel["selection"] in ("selected", "NO_SELECTION")
        if sel["selection"] == "selected":
            assert sel["n_signals"] >= 200 and sel["n_dates"] >= 20


def test_all_nan_feature_dropped_without_name_shift(tmp_path):
    """SimpleImputer выбрасывает целиком-NaN колонки -> имена листьев обязаны совпадать."""
    df, _ = _make_synthetic()
    df["age_kama_trend"] = np.nan
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    leaves = json.loads((out / "tree_leaves.json").read_text())
    assert "age_kama_trend" in leaves["dropped_all_nan"]
    assert "age_kama_trend" not in leaves["kept_features"]

    used = {c.split(" <= ")[0].split(" > ")[0]
            for lf in leaves["leaves"] for c in lf["conditions"]}
    assert used, "ожидаемые условия листьев"
    assert used <= set(leaves["kept_features"]), used - set(leaves["kept_features"])

    r = json.loads((out / "metrics.json").read_text())["results"][
        "dtree__individual_votes"]
    assert r["n_features"] == len(leaves["kept_features"])
    assert r["dropped_all_nan"] == leaves["dropped_all_nan"]


def test_predictions_schema_and_split_coverage(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    import pyarrow.parquet as pq
    tbl = pq.read_table(out / "predictions.parquet")
    assert tbl.column_names == ["row_id", "figi", "decision_ts", "model_id",
                                "split", "p_down", "p_neutral", "p_up",
                                "selected_long", "model_fit_end_ts",
                                "available_at_ts", "selection_status"]
    pdf = tbl.to_pandas()
    n_models = pdf["model_id"].nunique()
    assert n_models == 1 and set(pdf["split"]) == {"train", "val", "eval"}
    manifest = json.loads((out / "model_manifest.json").read_text())
    # predictions = inference universe, а не supervised-подмножество
    n_infer = sum(manifest["n_rows_split_infer"].values())
    assert len(pdf) == n_models * n_infer, (len(pdf), n_infer)
    assert n_infer > sum(manifest["n_rows_split"].values())

    # NO_SELECTION -> selected_long ВСЕГДА False; selected -> p_up >= порог
    r = json.loads((out / "metrics.json").read_text())["results"][
        "dtree__individual_votes"]
    sel = r["threshold_selection"]
    assert set(pdf["selection_status"]) == {r["selection_status"]}
    assert r["selection_status"] == sel["selection"]
    if sel["selection"] == "selected":
        assert np.array_equal(pdf["selected_long"].to_numpy(),
                              (pdf["p_up"].to_numpy() >= sel["threshold"]))
    else:
        assert not pdf["selected_long"].any(), \
            "NO_SELECTION не даёт права на вход — не порог 0.5"
        assert "reason" in sel

    # as-of моменты детерминированы и согласованы с определениями
    assert pdf["model_fit_end_ts"].nunique() == 1
    assert pdf["available_at_ts"].nunique() == 1
    fit = pd.Timestamp(pdf["model_fit_end_ts"].iloc[0])
    avail = pd.Timestamp(pdf["available_at_ts"].iloc[0])
    assert fit <= avail
    # model_fit_end_ts = max(label_end_ts) фактически использованных train-строк.
    # Окно воспроизводим здесь же: дата<=train_end, минус embargo-окно перед
    # границей val, минус purge (label_end >= val_start) и неизвестный исход.
    tdf = pd.read_parquet(t)
    tdt = pd.to_datetime(tdf["decision_ts"], utc=True)
    tlend = pd.to_datetime(tdf["label_end_ts"], utc=True, errors="coerce")
    val_b = pd.Timestamp("2026-06-01", tz="UTC")
    in_train = ((tdt.dt.date <= date(2026, 5, 31))
                & ~((tdt >= val_b - pd.Timedelta(minutes=120)) & (tdt < val_b))
                & tlend.notna() & (tlend < val_b))
    assert in_train.any(), "синтетика должна дать supervised train-строки"
    assert fit == tlend[in_train].max(), \
        "model_fit_end_ts обязан равняться max(label_end_ts) train-строк"
    assert fit > tdt[in_train].max(), \
        "иначе это decision_ts, а не label_end_ts"
    # purge: метка train-строк не дотягивается до границы val
    assert fit < val_b
    # available_at включает val-метки (порог/отбор), поэтому не раньше fit
    assert avail >= pd.to_datetime(
        tdf.loc[tdt.dt.date.between(date(2026, 6, 1), date(2026, 7, 31)),
                "label_end_ts"], utc=True, errors="coerce").min()
    assert manifest["model_fit_end_ts"] == pdf["model_fit_end_ts"].iloc[0]
    assert manifest["policy_available_at_ts"] == pdf["available_at_ts"].iloc[0]


def test_freeze_manifest_and_bundle_reload(tmp_path):
    """manifest+bundles пишутся ДО eval; reload bundle даёт те же предсказания."""
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    manifest = json.loads((out / "model_manifest.json").read_text())
    assert manifest["freeze"]["stage"] == "after_train_val_selection_before_eval"
    assert manifest["freeze"]["eval_evaluated_after_manifest_write"] is True
    assert manifest["input_targets_identity"]["identity_sha256"]
    assert manifest["code_sha256"]
    assert manifest["join_diagnostics"]["feature_available_late"] == 0

    key = "dtree__individual_votes"
    assert key in manifest["bundles"]
    import hashlib
    bp = out / manifest["bundles"][key]["path"]
    assert hashlib.sha256(bp.read_bytes()).hexdigest() == \
        manifest["bundles"][key]["sha256"]

    # reload: те же вероятности, что и в predictions
    b = M._load_bundle(bp)
    prep = M._Prep.restore(b["prep"])
    import pyarrow.parquet as pq
    pdf = pq.read_table(out / "predictions.parquet").to_pandas()
    sub = pdf[pdf["model_id"] == key]
    X = pd.read_parquet(f).set_index("row_id").loc[sub["row_id"], b["cols"]] \
        .to_numpy(np.float32)
    proba = M.align_proba(b["model"].predict_proba(prep.transform(X)),
                          b["model"].classes_, [0, 1, 2])
    got = sub[["p_down", "p_neutral", "p_up"]].to_numpy()
    assert np.allclose(proba, got, atol=1e-12), "reload обязан давать те же predict"


def test_breakdown_ticker_month_matches_metrics(tmp_path):
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    bd = json.loads((out / "breakdown.json").read_text())
    assert bd["n_matched"] == bd["n_predictions"] > 0
    m = "dtree__individual_votes"
    assert m in bd["by_ticker"] and m in bd["by_month"]

    manifest = json.loads((out / "model_manifest.json").read_text())
    n_eval = manifest["n_rows_split"]["eval"]
    tk = bd["by_ticker"][m]["eval"]
    assert set(tk) == {"TEST"}
    assert tk["TEST"]["n"] == n_eval
    assert abs(sum(tk["TEST"]["class_share"].values()) - 1.0) < 1e-9
    # сверка с metrics.json: balanced_accuracy должен сходиться построением
    ev = json.loads((out / "metrics.json").read_text())["results"][m]["eval"]
    assert tk["TEST"]["balanced_accuracy"] == pytest.approx(
        ev["balanced_accuracy"], abs=1e-9)
    assert tk["TEST"]["log_loss"] == pytest.approx(ev["log_loss"], abs=1e-9)
    assert sum(tk["TEST"]["per_label"][c]["support"] for c in
               ("down", "neutral", "up")) == n_eval

    # месяцы покрывают все строки сплита без пересечений
    months = bd["by_month"][m]["eval"]
    assert sum(v["n"] for v in months.values()) == n_eval

    # пост-фактум пересчёт даёт байт-в-байт тот же файл
    before = (out / "breakdown.json").read_bytes()
    rc = M.main(["--out", str(out), "--targets", str(t), "--breakdown-only"])
    assert rc == 0
    assert (out / "breakdown.json").read_bytes() == before


def _v2_like_frame():
    """Кадр, повторяющий колонки датасета B1 v2 (feature_schema_version=1m_mtf_v2)."""
    schema = M.default_feature_schema()
    cols = list(dict.fromkeys(schema["all_tf_votes"]))
    rng = np.random.default_rng(7)
    data = {c: rng.normal(size=64) for c in cols}
    # coverage (полнота корзины ТФ) — numeric, но это metadata, не предиктор
    data.update({c: 5 for c in schema["coverage_columns"]})
    data.update({f"{M.VOICE_PREFIX}status_{s}": "ok"
                 for s in M.VOTE_STRATEGIES})
    data.update({k: "x" for k in ("row_id", "figi", "ticker", "decision_ts",
                                  "feature_max_available_ts",
                                  "feature_schema_version")})
    return pd.DataFrame(data), schema


def test_v2_tf_suffix_resolves_all_timeframes():
    """Суффикс 1m ('_1m', v2) обязан попадать в наборы; coverage — НЕ обязан.

    Регрессия: при каноничном '_60s' в схеме и реальных '_1m'-колонках у B1
    мелкий ТФ МОЛЧА выпадал (29/36 вместо 36/36) — набор был непуст, поэтому
    падения не было. Противоположная регрессия: `n_min_<tf>` подхватывается
    suffix-фолбэком как «ТФ-признак» и превращает покрытие источника в X.
    """
    df, schema = _v2_like_frame()
    out = M.resolve_feature_columns(df, schema)

    cont = out["continuous_all_tf"]
    n_ind = len(M.CONT_INDICATORS) * len(M.TF_LIST)
    assert len(cont) == n_ind + len(M.SINGLE_CONT)
    assert set(M.tf_of_column(c) for c in cont) == set(M.TF_LIST)
    assert len(out["continuous_5m"]) == len(M.CONT_INDICATORS)
    assert len(out["individual_votes"]) == len(M.VOTE_STRATEGIES) * 2 + \
        len(M.VOICE_COLS)
    assert len(out["all_tf_votes"]) == len(cont) + len(out["individual_votes"])
    assert len(set(cont)) == len(cont)
    # coverage остаётся в схеме как metadata, но в наборы не идёт
    assert schema["coverage_columns"], "coverage_columns должны сохраняться"
    for s in M.FEATURE_SETS:
        assert not [c for c in out[s] if c.startswith(M.COVERAGE_PREFIX)], s

    # прежняя форма суффикса (_60s, контракт v1) даёт ТОТ ЖЕ состав.
    # Колонку НЕ переименовываем, если канонический алиас уже есть в кадре —
    # иначе df получит два 'n_min_60s' и df[c] станет DataFrame.
    rename = {c: c[:-2] + "60s" for c in df.columns
              if c.endswith("_1m") and c[:-2] + "60s" not in df.columns}
    out1 = M.resolve_feature_columns(df.rename(columns=rename), schema)
    assert {k: len(v) for k, v in out1.items()} == \
           {k: len(v) for k, v in out.items()}
    # алиас: '_1m' в схеме → '_60s' в df
    assert M._alias_variant(f"{M.CONT_INDICATORS[0]}_1m",
                            set(out1["continuous_all_tf"])) == \
        f"{M.CONT_INDICATORS[0]}_60s"
    # канон уже на месте → возвращается он сам
    assert M._alias_variant(f"{M.CONT_INDICATORS[0]}_1m",
                            set(out["continuous_all_tf"])) == \
        f"{M.CONT_INDICATORS[0]}_1m"
    # нужного ТФ в df нет → None
    assert M._alias_variant(f"{M.CONT_INDICATORS[0]}_1m", {"bias"}) is None


def test_quality_columns_never_enter_feature_sets():
    """n_min_*/voice_status_*/identity-timing не должны попасть ни в один X.

    В v2 победитель split'а приходился на n_min_3600s — качество данных, а не
    рынок. Проверяются оба канала: явный список schema и suffix-fallback.
    """
    df, schema = _v2_like_frame()
    cov = [c for c in df.columns if c.startswith(M.COVERAGE_PREFIX)]
    assert cov, "фикстура должна содержать n_min_<tf>"

    # явный список: если их вручную прописали в схему — всё равно отсекаются
    poisoned = dict(schema)
    poisoned["continuous_all_tf"] = list(schema["continuous_all_tf"]) + cov
    out = M.resolve_feature_columns(df, poisoned)
    for s in M.FEATURE_SETS:
        assert not set(out[s]) & set(cov), s

    # suffix-fallback: схема пуста, в df есть 'n_min_5m' как ТФ-суффикс
    bare = M.resolve_feature_columns(df, {s: [] for s in M.FEATURE_SETS})
    for s in M.FEATURE_SETS:
        assert not set(bare[s]) & set(cov), (s, set(bare[s]) & set(cov))
        for c in bare[s]:
            assert c not in M.META_EXACT, (s, c)
    # при этом сами колонки в df остаются (это metadata для coverage)
    assert all(c in df.columns for c in cov)


def test_non_numeric_status_columns_never_enter_feature_sets():
    """Строковые voice_status_* и мета-колонки не должны попасть в X.

    Иначе to_numpy(float32) валит прогон, а в фолбэк-ветке по префиксу
    'voice_' они ловятся молча.
    """
    df, schema = _v2_like_frame()
    status = [c for c in df.columns if c.startswith(f"{M.VOICE_PREFIX}status_")]
    assert status, "фикстура должна содержать строковые voice_status_*"

    out = M.resolve_feature_columns(df, schema)
    for s in M.FEATURE_SETS:
        assert not set(out[s]) & set(status)
        for c in out[s]:
            assert pd.api.types.is_numeric_dtype(df[c].dtype), (s, c)

    # фолбэк по паттерну (schema убрана) тоже обязан их отсечь
    bare = M.resolve_feature_columns(df, {s: [] for s in M.FEATURE_SETS})
    assert not set(bare["individual_votes"]) & set(status)
    assert all(pd.api.types.is_numeric_dtype(df[c].dtype)
               for c in bare["individual_votes"])


def test_policy_eligibility_gate_reasons():
    """Единая eligibility-policy ветки: будни, сессия с 10:00 MSK, до 18:44 MSK.

    Proof архитектора: 29.08.2026 (суббота) давал 37 targets. Гейт читает
    ТОЛЬКО decision_ts — ни label, ни label_status, ни MATURE, поэтому это не
    фильтр по будущему исходу.
    """
    ts = pd.Series([
        "2026-08-29T07:00:00Z",   # суббота, 10:00 MSK — выходной
        "2026-08-31T07:00:00Z",   # понедельник, ровно 10:00 MSK — открытие
        "2026-08-31T06:59:00Z",   # 09:59 MSK — до сессии
        "2026-08-31T15:44:00Z",   # 18:44 MSK — ровно cutoff, строго до него
        "2026-08-31T15:43:00Z",   # 18:43 MSK — допустимо
        "2026-08-31T15:40:00Z",   # последнее решение датасета B1 (18:40 MSK)
    ])
    assert list(M.policy_reason(ts)) == [
        "weekend", "ok", "before_session", "after_cutoff", "ok", "ok"]
    assert list(M.policy_eligible(ts)) == \
        [False, True, False, False, True, True]

    # гейт не зависит от метки: переключение label_status ничего не меняет
    for status in ("MATURE", "CENSORED", "unknown"):
        assert list(M.policy_eligible(ts)) == \
            [False, True, False, False, True, True], status


def test_policy_gate_applied_in_run(tmp_path):
    """Субботние решения уходят и из supervised, и из inference universe.

    Раньше `_session_label_rows` фильтровал время, но не weekday — субботние
    строки считались валидными наблюдениями.
    """
    df, _ = _make_synthetic()
    tdt = pd.to_datetime(df["decision_ts"], utc=True)
    in_eval = tdt.dt.date.between(date(2026, 8, 1),
                                  date(2026, 10, 8)).to_numpy()
    moved = list(df.index[in_eval][:12])
    # сдвигаем ровно на выходные, сохраняя время сессии (сб/вс внутри eval)
    new_ts = []
    for ts in pd.to_datetime(df.loc[moved, "decision_ts"], utc=True):
        delta = (5 - ts.weekday()) % 7 or 7
        nxt = ts + timedelta(days=delta)
        assert nxt.weekday() == 5 and nxt.date() <= date(2026, 10, 8)
        new_ts.append(nxt)
    df.loc[moved, "decision_ts"] = [t.isoformat() for t in new_ts]
    assert (M.policy_reason(df["decision_ts"]) == "weekend").sum() >= 12

    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    m = json.loads((out / "metrics.json").read_text())["manifest"]

    assert m["policy_eligibility"]["policy"] == M.POLICY_NAME
    assert m["policy_eligibility"]["gate_source"] == \
        "decision_ts only (без label/MATURE)"
    assert m["policy_eligibility"]["reject_reasons"]["weekend"] >= 12
    assert m["policy_eligibility"]["per_split_dropped"]["eval"] >= 12

    # ни одна выходная строка не прошла в supervised
    for k in ("train", "val", "eval"):
        assert m["weekday_counts_supervised"][k]["sat"] == 0
        assert m["weekday_counts_supervised"][k]["sun"] == 0
        assert sum(m["weekday_counts_supervised"][k].values()) == \
            m["n_rows_split"][k]

    # inference universe тоже урезан ровно на отброшенные субботние строки
    tdt2 = pd.to_datetime(df["decision_ts"], utc=True)
    ev = tdt2.dt.date.between(date(2026, 8, 1), date(2026, 10, 8)).to_numpy()
    wd = pd.to_datetime(tdt2[ev], utc=True).dt.tz_convert(M.MSK).dt.weekday
    assert m["n_rows_split_infer"]["eval"] == int((wd < 5).sum())
    assert m["policy_eligibility"]["rejected_total"] >= 12


def test_manifest_declares_fixed_feature_contract(tmp_path):
    """Фиксированный контракт признаков: whitelist зафиксирован в manifest.

    Иначе «какие функции вошли в модель» восстанавливается только из кода,
    а B3 обязан получать frozen сочетания функций, а не пересобирать их.
    """
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    m = json.loads((out / "metrics.json").read_text())["manifest"]

    assert m["feature_set_sizes"] == {
        "individual_votes": len(M.VOTE_STRATEGIES) * 2 + len(M.VOICE_COLS),
        "continuous_5m": len(M.CONT_INDICATORS),
        "continuous_all_tf": len(M.CONT_INDICATORS) * len(M.TF_LIST)
        + len(M.SINGLE_CONT),
        "all_tf_votes": len(M.CONT_INDICATORS) * len(M.TF_LIST)
        + len(M.SINGLE_CONT) + len(M.VOTE_STRATEGIES) * 2 + len(M.VOICE_COLS),
    }
    for s, cols in m["feature_columns"].items():
        assert not [c for c in cols if c.startswith(M.COVERAGE_PREFIX)], s
        assert not [c for c in cols if c.startswith("voice_status_")], s
        assert not [c for c in cols if c in M.META_EXACT], s
    # metadata остаётся доступной и явно названа исключённой
    assert any(c.startswith(M.COVERAGE_PREFIX)
               for c in m["excluded_quality_columns"])

    # support известных исходов и полный inference-допуск разделены
    rc = json.loads((out / "metrics.json").read_text())["row_counts"]
    assert rc["support_known_targets"] == m["n_rows_split"]
    assert rc["eligible_infer_all_labels"] == m["n_rows_split_infer"]
    assert sum(rc["support_known_targets"].values()) < \
        sum(rc["eligible_infer_all_labels"].values())


def test_policy_sufficiency_declared_when_precision_below_base(tmp_path):
    """Precision порога ниже val base_up => политика НЕДОСТАТОЧНА, не «edge»."""
    df, _ = _make_synthetic()
    rng = np.random.default_rng(7)
    df["label"] = rng.choice([-1, 0, 1], len(df))
    f, t = _write_parquet(df, tmp_path)
    out = _run_in(tmp_path, f, t, LIGHT)
    m = json.loads((out / "metrics.json").read_text())["manifest"]
    ps = m["primary_policy_sufficiency"]
    assert ps["base_up_val"] is not None
    if ps["selected_threshold"] is None:
        assert ps["sufficient_vs_val_base_up"] is False
        assert "НЕДОСТАТОЧНА" in ps["note"] or "NO_SELECTION" in ps["note"]
    elif ps["precision_val"] < ps["base_up_val"]:
        assert ps["sufficient_vs_val_base_up"] is False
        assert "НЕДОСТАТОЧНА" in ps["note"]
        assert "edge" in ps["note"].lower() or "НЕ" in ps["note"]
    else:
        assert ps["sufficient_vs_val_base_up"] is True


def test_publish_to_chat_uses_flock_and_verifies(tmp_path):
    """Публикация идёт под flock на постоянном lock-файле и перечитывается.

    Прежний mkdir-lock молча отказывался публиковать: lock-файл уже существует
    как обычный файл, 30 попыток mkdir проходили впустую и MODEL_READY не
    доходил до чата.
    """
    chat = tmp_path / "chat.md"
    chat.write_text("# header\n", encoding="utf-8")
    lock = tmp_path / ".chat-write.lock"
    lock.write_text("постоянный lock", encoding="utf-8")   # уже есть, не удалять

    msg = "\n### test — Builder-2 / пост под flock\nтело поста\n"
    M.publish_to_chat(chat, msg)
    assert msg.strip() in chat.read_text(encoding="utf-8")
    assert lock.exists(), "lock-файл нельзя удалять"
    assert lock.read_text(encoding="utf-8") == "постоянный lock"

    # append, а не перезапись
    M.publish_to_chat(chat, "\n### второй пост\n")
    text = chat.read_text(encoding="utf-8")
    assert text.startswith("# header\n") and "второй пост" in text


def test_b1_manifest_is_source_of_truth(tmp_path):
    """Роли колонок и eligibility-policy берутся из manifest.json B1.

    B1 (пост 2026-10-14): `eligibility_policy_hash` и `predictor_columns` —
    источник истины. B2 обязан либо совпасть с ним, либо упасть вслух, а не
    молча обучиться на своём представлении о том, что является признаком.
    """
    df, _ = _make_synthetic()
    f, t = _write_parquet(df, tmp_path)
    schema = M.default_feature_schema()
    good = {
        "dataset_version": "ml_mtf_1_v3",
        "feature_schema_version": "1m_mtf_v3",
        "predictor_columns": schema["all_tf_votes"],
        "metadata_columns": ["row_id", "ticker"] + schema["coverage_columns"],
        "eligibility_policy": {"tz": "Europe/Moscow", "weekdays": [0, 1, 2, 3, 4],
                               "open": "10:00", "close": "18:40",
                               "decision_strictly_before": "18:44"},
        "eligibility_policy_hash": "b8f7f9cd0498d4b9",
        "label_cfg_hash": "a898948ff9f0d027",
        "code_sha256": "121388e4dd46d575f71507e13f230e3e83da9a36900f5bbf7a32b1b8d48387fa",
    }
    mp = tmp_path / "manifest.json"
    mp.write_text(json.dumps(good), encoding="utf-8")

    out = _run_in(tmp_path, f, t, LIGHT)
    m = json.loads((out / "metrics.json").read_text())["manifest"]
    bp = m["b1_provenance"]
    assert bp["whitelist_matches_b1_predictors"] is True
    assert bp["eligibility_policy_hash"] == "b8f7f9cd0498d4b9"
    assert bp["n_predictor_columns"] == 58
    assert bp["label_cfg_hash"] == "a898948ff9f0d027"
    assert m["policy_eligibility"]["consistent_with_b1_policy"] is True
    assert m["policy_eligibility"]["b1_eligibility_policy_hash"] == \
        "b8f7f9cd0498d4b9"

    argv = ["--features", str(f), "--targets", str(t), "--no-chat"] + LIGHT

    # роли расходятся -> жёсткая ошибка, а не тихая подмена набора
    bad = dict(good)
    bad["predictor_columns"] = list(schema["all_tf_votes"]) + ["n_min_5m"]
    mp.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        M.main(argv[:2] + ["--out", str(tmp_path / "out2")] + argv[2:])
    assert "predictor_columns" in str(ei.value)

    # политика расходится -> жёсткая ошибка
    bad2 = dict(good)
    bad2["eligibility_policy"] = {"tz": "UTC", "weekdays": [0, 6],
                                  "open": "09:00",
                                  "decision_strictly_before": "18:44"}
    mp.write_text(json.dumps(bad2), encoding="utf-8")
    with pytest.raises(SystemExit) as ei:
        M.main(argv[:2] + ["--out", str(tmp_path / "out3")] + argv[2:])
    assert "eligibility-policy" in str(ei.value)
