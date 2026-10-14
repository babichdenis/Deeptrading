#!/usr/bin/env python3
"""Builder-2 / PARALLEL-DAY-1, ветка day_b2_20261014.

ML, обучаемый на экономическом результате:
  A) offline target из frozen минут (long от raw next-open = decision_ts, time-exit
     30/60/120 мин с clamp на 18:44 MSK, signed net return на cost 5/2);
  B) fixed matrix 12 fits (3 горизонта x {ridge,hgb} x {cont5,all_tf});
  C) 36 policy attempts и один общий selection criterion на val;
  D) freeze ДО eval, один eval, портфель через frozen daypack;
  E) residuals/robustness;
  F) summary.csv + RESULT.md + RESULT.json.

Общий common reference (backend/research/daypack_20261014) не модифицируется:
сюда он подключается read-only через PYTHONPATH=backend.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import random
import statistics
import sys
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from research.daypack_20261014 import execution_core as rx
from research.daypack_20261014 import frozen_portfolio as engine
from research.daypack_20261014.costs import CostParams
from research.daypack_20261014.evaluate import MinuteStore, summarize

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

BRANCH = "day_b2_20261014"
HORIZONS = (30, 60, 120)
THRESHOLDS = (0.0, 0.0005, 0.001)
MODELS = ("ridge", "hgb")
FEATURE_SETS = ("cont5", "all_tf")
COST_PRIMARY = ("c5_s2", 5.0, 2.0)
COST_SENS = ("c10_s5", 10.0, 5.0)
COST_SCENARIOS = (COST_PRIMARY, COST_SENS)
SEED = 42

SESSION_START_MOD = 600
SESSION_END_MOD = 1124
EMBARGO_MIN = 120
TRAIN_END = date(2026, 5, 31)
VAL_END = date(2026, 7, 31)
EVAL_END = date(2026, 10, 8)
SPLIT_BOUNDARY = {
    "train": datetime(2026, 6, 1, 0, 0, tzinfo=MSK),
    "val": datetime(2026, 8, 1, 0, 0, tzinfo=MSK),
}
MIN_KNOWN_EVENTS = 200
MIN_KNOWN_DATES = 20
MAX_UNKNOWN_FRAC = 0.05
N_BOOTSTRAP = 500
BOOTSTRAP_BLOCK = 5
BOOTSTRAP_SEED = 42

REL_DATASET = "backend/reports/architect_20261012/ml_mtf_1/b1/20261013T221949Z_v3_full"
REL_SNAPSHOT = "backend/reports/architect_20261011/research_core_v2/b1/20261011T1947Z/snapshot"
REL_DAYPACK = "backend/research/daypack_20261014"
REL_OUT_ROOT = "backend/reports/architect_20261014/parallel_day/b2"
REL_BRANCH_DIR = "backend/research/branches/day_b2_20261014"


# ------------------------------------------------------------------ helpers


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(raw) -> datetime:
    s = str(raw).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def dump_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    tmp.replace(path)


class Run:
    def __init__(self, out: Path):
        self.out = out
        out.mkdir(parents=True, exist_ok=True)
        self.log_path = out / "run.log"

    def log(self, msg: str) -> None:
        line = f"[{now_utc_iso()}] {msg}"
        print(line, flush=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def fit_key(model: str, fset: str, h: int) -> str:
    return f"{model}__{fset}__h{h}"


def session_reason(ts: datetime) -> str:
    loc = ts.astimezone(MSK)
    if loc.weekday() >= 5:
        return "weekend"
    mod = loc.hour * 60 + loc.minute
    if mod < SESSION_START_MOD:
        return "before_session"
    if mod >= SESSION_END_MOD:
        return "after_cutoff"
    return "ok"


def assign_split(ts: datetime) -> str | None:
    d = ts.astimezone(MSK).date()
    if d <= TRAIN_END:
        return "train"
    if d <= VAL_END:
        return "val"
    if d <= EVAL_END:
        return "eval"
    return None


def embargo_ok(split: str, entry_ts: datetime, h: int) -> bool:
    boundary = SPLIT_BOUNDARY.get(split)
    if boundary is None:
        return True
    return entry_ts + timedelta(minutes=max(h, EMBARGO_MIN)) < boundary


# ------------------------------------------------------------------ input manifest


def build_input_manifest(repo_root: Path, run: Run) -> dict:
    dataset = repo_root / REL_DATASET
    snapshot = repo_root / REL_SNAPSHOT
    daypack = repo_root / REL_DAYPACK
    ds_manifest = dataset / "manifest.json"
    sn_manifest = snapshot / "manifest.json"
    dp_manifest = daypack / "manifest.json"
    m = json.loads(ds_manifest.read_text(encoding="utf-8"))
    s = json.loads(sn_manifest.read_text(encoding="utf-8"))
    manifest = {
        "branch": BRANCH,
        "generated_utc": now_utc_iso(),
        "dataset": {
            "path": REL_DATASET,
            "manifest_sha256": sha256_file(ds_manifest),
            "dataset_version": m.get("dataset_version"),
            "feature_schema_version": m.get("feature_schema_version"),
            "label_cfg_hash": m.get("label_cfg_hash"),
            "eligibility_policy": m.get("eligibility_policy"),
            "eligibility_policy_hash": m.get("eligibility_policy_hash"),
            "code_sha256": m.get("code_sha256"),
            "params_hash": m.get("params_hash"),
            "feature_rows": m.get("feature_rows"),
            "target_rows": m.get("target_rows"),
            "n_tickers_ok": m.get("n_tickers_ok"),
            "n_predictors": len(m.get("predictor_columns") or []),
            "predictor_columns_sha256": hashlib.sha256(
                "\n".join(m.get("predictor_columns") or []).encode()).hexdigest(),
            "metadata_columns": m.get("metadata_columns"),
            "start": m.get("start"),
            "end": m.get("end"),
        },
        "snapshot": {
            "path": REL_SNAPSHOT,
            "manifest_sha256": sha256_file(sn_manifest),
            "tool": s.get("tool"),
            "generated_utc": s.get("generated_utc"),
            "params": s.get("params"),
            "universe_sha256": s.get("universe_sha256"),
            "n_universe_elite": s.get("n_universe_elite"),
            "code_sha256": s.get("code_sha256"),
            "corporate_actions_status": s.get("corporate_actions_status"),
            "calendar_status": s.get("calendar_status"),
            "db_tx_read_only": (s.get("db_ident") or {}).get("tx_read_only"),
        },
        "daypack": {
            "path": REL_DAYPACK,
            "manifest_sha256": sha256_file(dp_manifest),
            "files": json.loads(dp_manifest.read_text(encoding="utf-8")).get("files"),
            "read_only": True,
            "deviation": "none",
        },
        "code": {
            "script": str(Path(__file__).resolve().relative_to(repo_root)
                          if str(Path(__file__).resolve()).startswith(str(repo_root))
                          else Path(__file__).resolve()),
            "script_sha256": sha256_file(Path(__file__).resolve()),
            "python": sys.version.split()[0],
        },
        "universe": {
            "n_universe": s.get("n_universe_elite"),
            "dataset_tickers_ok": m.get("n_tickers_ok"),
            "missing_tickers": m.get("missing_tickers"),
        },
        "policy": {
            "tz": "Europe/Moscow",
            "weekdays": [0, 1, 2, 3, 4],
            "session_start_msK": "10:00",
            "decision_strictly_before_msK": "18:44",
            "decision_grid": "5m closes (targets = 5m ряды)",
            "source": "dataset eligibility_policy (bypass label maturity)",
        },
    }
    run.log("input manifest ready: dataset=%s snapshot=%s" % (
        manifest["dataset"]["manifest_sha256"][:12],
        manifest["snapshot"]["manifest_sha256"][:12]))
    return manifest


def build_config(input_manifest: dict) -> dict:
    return {
        "branch": BRANCH,
        "frozen_utc": now_utc_iso(),
        "frozen_stage": "before train/val and before eval",
        "seed": SEED,
        "horizons_minutes": list(HORIZONS),
        "thresholds": list(THRESHOLDS),
        "models": {
            "ridge": {"kind": "Pipeline", "steps": ["median_imputer", "standard_scaler",
                                                    "ridge"],
                      "params": {"alpha": 1.0}},
            "hgb": {"kind": "Pipeline", "steps": ["median_imputer",
                                                  "hist_gradient_boosting_regressor"],
                    "params": {"max_iter": 150, "max_leaf_nodes": 15,
                               "l2_regularization": 1.0, "min_samples_leaf": 200,
                               "early_stopping": False, "random_state": SEED,
                               "learning_rate": 0.1}},
        },
        "feature_sets": {
            "cont5": {"n": 7, "rule": "predictor_columns ending with _5m"},
            "all_tf": {"n": input_manifest["dataset"]["n_predictors"],
                       "rule": "dataset predictor_columns, без quality/metadata"},
        },
        "quality_columns_excluded": input_manifest["dataset"]["metadata_columns"],
        "splits": {
            "train": "<= 2026-05-31 MSK",
            "val": "2026-06-01 .. 2026-07-31 MSK",
            "eval": "2026-08-01 .. 2026-10-08 MSK",
            "embargo_minutes": EMBARGO_MIN,
            "label_end_rule": "entry_ts + max(h, 120) < next section start",
            "embargo_applied": "union of label_end rule and 120m embargo",
        },
        "economics": {
            "side": "LONG_ONLY",
            "entry": "open 1m бара со start-ts = decision_ts (raw next-open)",
            "exit": "min(entry + horizon, 18:44 MSK), только точная минута",
            "no_entry": "SKIP",
            "no_exit": "INCOMPLETE (endpoint unknown, не 0)",
            "primary_costs": {"id": COST_PRIMARY[0], "commission_bps": COST_PRIMARY[1],
                              "slippage_bps": COST_PRIMARY[2]},
            "sensitivity_costs": {"id": COST_SENS[0], "commission_bps": COST_SENS[1],
                                  "slippage_bps": COST_SENS[2]},
            "slots": 5,
            "slot_weight": 0.2,
            "qty_rule": "0.2 * initial_capital / raw_entry",
            "leverage": False,
            "reinvestment": False,
            "tie_break": "exits before entries, then FIGI order",
        },
        "selection": {
            "criterion": ("mean known event net on val among selected at cost 5/2 "
                          "(screening metric with overlap, НЕ portfolio PnL)"),
            "min_known_events": MIN_KNOWN_EVENTS,
            "min_known_dates": MIN_KNOWN_DATES,
            "max_unknown_endpoint_fraction": MAX_UNKNOWN_FRAC,
            "unknown_denominator": "all selected intended entries",
            "on_failure": "NO_SELECTION / cash (пороги и support не снижаются)",
            "freeze": "primary + top3 + best_ridge + best_cont5 до eval; "
                      "смена победителя по eval запрещена",
        },
        "baselines": ["cash", "always_long"],
        "cost_sensitivity": ["c5_s2", "c10_s5"],
        "robustness": {
            "paired_daily": "common calendar axis + explicit coverage",
            "bootstrap": {"n": N_BOOTSTRAP, "block_session_days": BOOTSTRAP_BLOCK,
                          "seed": BOOTSTRAP_SEED,
                          "note": "descriptive interval, не множественная коррекция"},
            "sensitivity": ["without_best_ticker", "without_best_day"],
        },
        "resources": {"cpu_threads": 1, "max_ram_gb": 1.5,
                      "checkpoint": "per ticker / per fit / per config",
                      "resume_hash_checked": True},
        "input_manifest_sha256_target": "см. input_manifest.json",
        "attempt_accounting": "все policy attempts сохраняются (12 fits x 3 thresholds)",
    }


# ------------------------------------------------------------------ stage A


def _target_columns(h: int) -> dict:
    return {
        f"st{h}": pa.string(),
        f"rs{h}": pa.string(),
        f"n52{h}": pa.float64(),
        f"n105{h}": pa.float64(),
        f"g{h}": pa.float64(),
        f"xt{h}": pa.string(),
        f"pk{h}": pa.bool_(),
    }


def stage_a_targets(repo_root: Path, cfg: dict, run: Run, resume: bool) -> dict:
    """Offline target: те же правила, что у frozen reference (execute_signal)."""
    out_dir = run.out / "targets_a"
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if (
        resume and index_path.exists()) else {}

    dataset = repo_root / REL_DATASET
    snapshot = repo_root / REL_SNAPSHOT
    target_files = sorted((dataset / "targets").glob("targets_*.parquet"))
    if not target_files:
        raise SystemExit("stage A: не найдены targets/*.parquet")

    c_primary = CostParams(COST_PRIMARY[1], COST_PRIMARY[2])
    c_sens = CostParams(COST_SENS[1], COST_SENS[2])
    c_primary.validate()
    c_sens.validate()

    summary = {"tickers": [], "rows": 0, "status_counts": {}, "reason_counts": {},
               "grid_ok": True, "session_rejected": 0, "minute_missing_files": []}
    prev_horizon = engine.EXIT_HORIZON_MIN
    try:
        for tf in target_files:
            ticker = tf.stem.split("_", 1)[1]
            dest = out_dir / f"{ticker}.parquet"
            if resume and ticker in index and index[ticker].get("empty"):
                continue
            if resume and dest.exists() and ticker in index:
                got = sha256_file(dest)
                if index[ticker].get("sha256") == got:
                    summary["tickers"].append(ticker)
                    summary["rows"] += index[ticker]["rows"]
                    continue

            tgt = pq.read_table(tf, columns=["row_id", "figi", "ticker",
                                             "decision_ts"]).to_pandas()
            if tgt.empty:
                run.log(f"stage A {ticker}: пустой targets (нет данных universe) — пропуск")
                index[ticker] = {"rows": 0, "sha256": None, "figi": None,
                                 "m1": False, "empty": True}
                dump_json(index_path, index)
                continue
            figi = str(tgt["figi"].iloc[0])
            dts = [parse_ts(v) for v in tgt["decision_ts"].tolist()]
            rids = tgt["row_id"].astype(str).tolist()

            keep = []
            for i, ts in enumerate(dts):
                loc = ts.astimezone(MSK)
                if loc.minute % 5 != 0:
                    summary["grid_ok"] = False
                if session_reason(ts) == "ok":
                    keep.append(i)
                else:
                    summary["session_rejected"] += 1
            if not keep:
                raise SystemExit(f"stage A: {ticker} не имеет session-eligible решений")

            m1 = snapshot / "csv" / f"{ticker}__m1.csv"
            store = MinuteStore()
            if m1.exists():
                store.add_csv(m1, figi)
            else:
                summary["minute_missing_files"].append(str(m1))

            n = len(keep)
            cols = {
                "row_id": [rids[i] for i in keep],
                "figi": [figi] * n,
                "ticker": [ticker] * n,
                "decision_ts": [dts[i].isoformat() for i in keep],
            }
            for h in HORIZONS:
                engine.EXIT_HORIZON_MIN = h
                st, rs, n52, n105, gg, xt, pk = [], [], [], [], [], [], []
                for i in keep:
                    ts = dts[i]
                    pred = {"row_id": rids[i], "figi": figi, "ticker": ticker,
                            "decision_ts": ts.isoformat(),
                            "available_at_ts": ts.isoformat(),
                            "model_id": "day_b2_offline_target", "split": "target",
                            "selected_long": True}
                    o = engine.execute_signal(pred, store, c_primary)
                    st.append(o.status)
                    rs.append(o.reason)
                    if o.status == "FILLED":
                        pred2 = dict(pred)
                        o2 = engine.execute_signal(pred2, store, c_sens)
                        n105.append(o2.net_per_unit)
                        gg.append(o.gross_per_unit)
                        xt.append(o.exit_ts.isoformat() if o.exit_ts else "")
                        pk.append(bool(o.path_metrics_known))
                        summary["status_counts"][f"{h}:FILLED"] = (
                            summary["status_counts"].get(f"{h}:FILLED", 0) + 1)
                    else:
                        n105.append(float("nan"))
                        gg.append(float("nan"))
                        xt.append("")
                        pk.append(False)
                        key = f"{h}:{o.status}"
                        summary["status_counts"][key] = (
                            summary["status_counts"].get(key, 0) + 1)
                        summary["reason_counts"][o.reason] = (
                            summary["reason_counts"].get(o.reason, 0) + 1)
                    n52.append(o.net_per_unit if o.net_per_unit is not None
                               else float("nan"))
                cols.update({f"st{h}": st, f"rs{h}": rs, f"n52{h}": n52,
                             f"n105{h}": n105, f"g{h}": gg, f"xt{h}": xt,
                             f"pk{h}": pk})

            schema = pa.schema([("row_id", pa.string()), ("figi", pa.string()),
                                ("ticker", pa.string()),
                                ("decision_ts", pa.string())] +
                               [(k, v) for h in HORIZONS
                                for k, v in _target_columns(h).items()])
            table = pa.table(cols, schema=schema)
            pq.write_table(table, dest, compression="zstd")
            index[ticker] = {"rows": n, "sha256": sha256_file(dest),
                             "figi": figi, "m1": m1.exists()}
            dump_json(index_path, index)
            summary["tickers"].append(ticker)
            summary["rows"] += n
            run.log(f"stage A {ticker}: rows={n} m1={m1.exists()}")
    finally:
        engine.EXIT_HORIZON_MIN = prev_horizon

    dump_json(run.out / "stage_a_summary.json", summary)
    run.log(f"stage A done: rows={summary['rows']} tickers={len(summary['tickers'])} "
            f"grid_ok={summary['grid_ok']} rejected={summary['session_rejected']}")
    if not summary["grid_ok"]:
        raise SystemExit("stage A: decision_ts вне 5m сетки")
    return summary


# ------------------------------------------------------------------ stage B


def _estimator(model: str):
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import StandardScaler

    if model == "ridge":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("model", Ridge(alpha=1.0)),
        ])
    if model == "hgb":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("model", HistGradientBoostingRegressor(
                max_iter=150, max_leaf_nodes=15, l2_regularization=1.0,
                min_samples_leaf=200, early_stopping=False, random_state=SEED)),
        ])
    raise SystemExit(f"unknown model {model}")


def _read_features(dataset: Path, ticker: str, predictors: list[str], row_ids: list[str]):
    import pandas as pd

    path = dataset / "features" / f"features_{ticker}.parquet"
    cols = ["row_id"] + [c for c in predictors]
    feat = pq.read_table(path, columns=cols).to_pandas()
    if feat["row_id"].duplicated().any():
        raise SystemExit(f"duplicate row_id in features {ticker}")
    feat = feat.set_index("row_id")
    sub = feat.reindex(row_ids)
    if sub[predictors[0]].isna().all() and not feat[predictors[0]].isna().all():
        raise SystemExit(f"join failed for {ticker}")
    return sub[predictors].to_numpy(dtype=np.float32)


def stage_b_fit(repo_root: Path, cfg: dict, input_manifest: dict, run: Run,
                resume: bool) -> dict:
    """Design-matrix build + 12 fits + predictions на val/eval."""
    dataset = repo_root / REL_DATASET
    ds_manifest = json.loads((dataset / "manifest.json").read_text(encoding="utf-8"))
    predictors = list(ds_manifest["predictor_columns"])
    cont5 = [c for c in predictors if c.endswith("_5m")]
    if len(cont5) != 7:
        raise SystemExit(f"cont5 должен состоять из 7 колонок, найдено {len(cont5)}")
    meta = set(ds_manifest["metadata_columns"])
    if meta & set(predictors):
        raise SystemExit("quality/metadata колонка попала в predictor_columns")
    idx5 = [predictors.index(c) for c in cont5]

    out_dir = run.out
    design_dir = out_dir / "design"
    design_dir.mkdir(parents=True, exist_ok=True)
    fit_dir = out_dir / "fits"
    fit_dir.mkdir(parents=True, exist_ok=True)
    pred_dir = out_dir / "predictions"
    pred_dir.mkdir(parents=True, exist_ok=True)

    a_index = json.loads((out_dir / "targets_a" / "_index.json").read_text(encoding="utf-8"))
    tickers = sorted(t for t in a_index if not a_index[t].get("empty")
                     and a_index[t]["rows"] > 0)
    if not tickers:
        raise SystemExit("stage B: нет stage A результатов")

    splits_wanted = ("train", "val", "eval")
    blocks = {s: [] for s in splits_wanted}
    acc_y = {s: {h: [] for h in HORIZONS} for s in splits_wanted}
    acc_meta = {s: [] for s in splits_wanted}
    acc_status = {s: {h: [] for h in HORIZONS} for s in splits_wanted}
    acc_n52 = {s: {h: [] for h in HORIZONS} for s in splits_wanted}
    dropped = {"embargo": 0, "no_split": 0, "not_known": {f"{s}:{h}": 0
                                                          for s in splits_wanted
                                                          for h in HORIZONS}}

    for ticker in tickers:
        tgt = pq.read_table(out_dir / "targets_a" / f"{ticker}.parquet").to_pandas()
        rids = tgt["row_id"].astype(str).tolist()
        dts = [parse_ts(v) for v in tgt["decision_ts"].tolist()]
        X = _read_features(dataset, ticker, predictors, rids)
        local = {s: [] for s in splits_wanted}
        for i, ts in enumerate(dts):
            split = assign_split(ts)
            if split is None:
                dropped["no_split"] += 1
                continue
            if not embargo_ok(split, ts, max(HORIZONS)):
                dropped["embargo"] += 1
                continue
            local[split].append(i)
            acc_meta[split].append((rids[i], tgt["figi"].iloc[i], ticker,
                                    ts.isoformat()))
            for h in HORIZONS:
                st = str(tgt[f"st{h}"].iloc[i])
                acc_status[split][h].append(st)
                acc_n52[split][h].append(float(tgt[f"n52{h}"].iloc[i]))
                if st == "FILLED" and embargo_ok(split, ts, h):
                    acc_y[split][h].append(float(tgt[f"n52{h}"].iloc[i]))
                else:
                    acc_y[split][h].append(float("nan"))
                    if split == "train":
                        dropped["not_known"][f"{split}:{h}"] += 1
        for s in splits_wanted:
            if local[s]:
                blocks[s].append(X[local[s]])
        run.log(f"stage B design {ticker}: " + ", ".join(
            f"{s}={len(local[s])}" for s in splits_wanted))

    design = {}
    for s in splits_wanted:
        if not blocks[s]:
            raise SystemExit(f"stage B: пустой сплит {s}")
        X = np.vstack(blocks[s]).astype(np.float32)
        del blocks[s]
        meta_arr = np.empty((len(acc_meta[s]), 4), dtype=object)
        for i, row in enumerate(acc_meta[s]):
            meta_arr[i, 0], meta_arr[i, 1] = row[0], row[1]
            meta_arr[i, 2], meta_arr[i, 3] = row[2], row[3]
        design[s] = {"X": X, "meta": meta_arr,
                     "status": {h: np.array(acc_status[s][h]) for h in HORIZONS},
                     "n52": {h: np.array(acc_n52[s][h], dtype=np.float64)
                             for h in HORIZONS},
                     "y": {h: np.array(acc_y[s][h], dtype=np.float64)
                           for h in HORIZONS}}
        run.log(f"stage B {s}: rows={len(meta_arr)} X={X.shape}")
    del blocks, acc_status, acc_n52, acc_y

    order = np.argsort(design["train"]["meta"][:, 0].astype(str))
    train_order_key = hashlib.sha256(
        "\n".join(design["train"]["meta"][order, 0].astype(str)).encode()).hexdigest()

    fit_records = []
    preds_val = {k: np.full(len(design["val"]["meta"]), np.nan, dtype=np.float64)
                 for k in [fit_key(m, f, h) for h in HORIZONS for f in FEATURE_SETS
                           for m in MODELS]}
    preds_eval = {k: np.full(len(design["eval"]["meta"]), np.nan, dtype=np.float64)
                  for k in preds_val}

    for h in HORIZONS:
        for fset in FEATURE_SETS:
            cols_idx = idx5 if fset == "cont5" else list(range(len(predictors)))
            for model in MODELS:
                key = fit_key(model, fset, h)
                pkl_path = fit_dir / f"{key}.pkl"
                y = design["train"]["y"][h]
                mask = np.isfinite(y)
                if not mask.any():
                    raise SystemExit(f"stage B: {key} не имеет известных train меток")
                Xtr = design["train"]["X"][order][mask[order]][:, cols_idx]
                ytr = y[order][mask[order]]
                meta_train = design["train"]["meta"][order][mask[order]]
                for row in meta_train:
                    ts = parse_ts(row[3])
                    if session_reason(ts) != "ok":
                        raise SystemExit(f"stage B: недопустимая метка в fit: {row}")
                started = time.time()
                est = _estimator(model)
                est.fit(Xtr, ytr)
                with pkl_path.open("wb") as fh:
                    pickle.dump({"key": key, "model": model, "feature_set": fset,
                                 "horizon": h, "columns": [predictors[i] for i in cols_idx],
                                 "estimator": est, "fit_rows": int(mask.sum()),
                                 "fit_order_sha256": train_order_key,
                                 "code_sha256": input_manifest["code"]["script_sha256"],
                                 "seed": SEED}, fh, protocol=4)
                pv = est.predict(design["val"]["X"][:, cols_idx])
                pe = est.predict(design["eval"]["X"][:, cols_idx])
                preds_val[key] = pv
                preds_eval[key] = pe
                rec = {"key": key, "model": model, "feature_set": fset, "horizon": h,
                       "n_fit": int(mask.sum()), "fit_seconds": round(time.time() - started, 2),
                       "pickle_sha256": sha256_file(pkl_path),
                       "feature_columns": [predictors[i] for i in cols_idx],
                       "n_features": len(cols_idx)}
                fit_records.append(rec)
                dump_json(fit_dir / "_index.json",
                          {"fits": fit_records,
                           "code_sha256": input_manifest["code"]["script_sha256"]})
                run.log(f"fit {key}: n={rec['n_fit']} {rec['fit_seconds']}s")

    design_meta = {s: [{"row_id": str(r[0]), "figi": str(r[1]), "ticker": str(r[2]),
                        "decision_ts": str(r[3])} for r in design[s]["meta"]]
                   for s in splits_wanted}

    def _write_pred(split: str, preds: dict) -> None:
        table_cols = {k: [m[k] for m in design_meta[split]] for k in
                      ("row_id", "figi", "ticker", "decision_ts")}
        for h in HORIZONS:
            table_cols[f"st{h}"] = design[split]["status"][h].tolist()
            table_cols[f"n52{h}"] = design[split]["n52"][h].tolist()
        for k, v in preds.items():
            table_cols[f"p__{k}"] = v.tolist()
        pq.write_table(pa.table(table_cols), pred_dir / f"{split}.parquet",
                       compression="zstd")

    _write_pred("val", preds_val)
    _write_pred("eval", preds_eval)

    dump_json(out_dir / "stage_b_summary.json", {
        "predictors": len(predictors), "cont5": cont5,
        "splits": {s: len(design_meta[s]) for s in splits_wanted},
        "known_fit_rows": {f"{h}": int(np.isfinite(design["train"]["y"][h]).sum())
                           for h in HORIZONS},
        "dropped": dropped, "fits": fit_records,
        "train_order_sha256": train_order_key,
        "embargo_rule": cfg["splits"]["label_end_rule"],
    })
    run.log(f"stage B done: {len(fit_records)} fits")
    return {"fits": fit_records, "predictors": predictors, "cont5": cont5,
            "design_meta": design_meta, "dropped": dropped,
            "splits": {s: len(design_meta[s]) for s in splits_wanted}}


# ------------------------------------------------------------------ stage C


def _screen(mask_selected, status, n52, decision_ts_iso, threshold_value):
    n_selected = int(mask_selected.sum())
    if n_selected == 0:
        return {"n_selected": 0, "n_known": 0, "n_unknown": 0, "n_skip": 0,
                "unknown_frac": 1.0, "n_dates": 0, "mean_net_known": None,
                "threshold_value": threshold_value, "eligible": False,
                "fail_reason": "no_selected"}
    st = status[mask_selected]
    nets = n52[mask_selected]
    ts = np.array(decision_ts_iso, dtype=object)[mask_selected]
    known = st == "FILLED"
    unknown = st == "INCOMPLETE"
    skipped = st == "SKIP"
    n_known = int(known.sum())
    n_unknown = int(unknown.sum())
    n_skip = int(skipped.sum())
    unknown_frac = n_unknown / n_selected
    dates = {parse_ts(t).astimezone(MSK).date().isoformat()
             for t, k in zip(ts, known) if k}
    mean_net = float(np.mean(nets[known])) if n_known else None
    fail = None
    if n_known < MIN_KNOWN_EVENTS:
        fail = f"known_events<{MIN_KNOWN_EVENTS}"
    elif len(dates) < MIN_KNOWN_DATES:
        fail = f"known_dates<{MIN_KNOWN_DATES}"
    elif unknown_frac > MAX_UNKNOWN_FRAC:
        fail = f"unknown_frac>{MAX_UNKNOWN_FRAC}"
    return {"n_selected": n_selected, "n_known": n_known, "n_unknown": n_unknown,
            "n_skip": n_skip, "unknown_frac": round(unknown_frac, 6),
            "n_dates": len(dates), "mean_net_known": mean_net,
            "threshold_value": threshold_value, "eligible": fail is None,
            "fail_reason": fail}


def stage_c_selection(run: Run, fit_meta: dict, resume: bool) -> dict:
    import pandas as pd

    val = pq.read_table(run.out / "predictions" / "val.parquet").to_pandas()
    decision_iso = val["decision_ts"].tolist()
    attempts = []
    for h in HORIZONS:
        status = val[f"st{h}"].to_numpy()
        n52 = val[f"n52{h}"].to_numpy(dtype=np.float64)
        for fset in FEATURE_SETS:
            for model in MODELS:
                key = fit_key(model, fset, h)
                col = val[f"p__{key}"].to_numpy(dtype=np.float64)
                for thr in THRESHOLDS:
                    mask = col > thr
                    rec = _screen(mask, status, n52, decision_iso, thr)
                    tag = f"{thr:.4f}".replace(".", "p")
                    rec.update({"fit": key, "attempt": f"{key}__t{tag}",
                                "model": model, "feature_set": fset,
                                "horizon": h, "threshold": thr, "split": "val"})
                    attempts.append(rec)
    attempts.sort(key=lambda r: (-int(r["eligible"]),
                                 -(r["mean_net_known"] if r["mean_net_known"] is not None
                                   else -1e18), r["attempt"], r["threshold"]))
    eligible = [a for a in attempts if a["eligible"]]

    def _best(pred_fn):
        for a in eligible:
            if pred_fn(a):
                return a
        return None

    primary = eligible[0] if eligible else None
    top3 = eligible[:3]
    best_ridge = _best(lambda a: a["model"] == "ridge")
    best_cont5 = _best(lambda a: a["feature_set"] == "cont5")

    freeze = {
        "branch": BRANCH,
        "frozen_utc": now_utc_iso(),
        "stage": "after train+val selection, before eval",
        "immutable_after_write": True,
        "selection_criterion": {
            "metric": "mean known event net on val among selected, cost 5/2",
            "min_known_events": MIN_KNOWN_EVENTS,
            "min_known_dates": MIN_KNOWN_DATES,
            "max_unknown_endpoint_fraction": MAX_UNKNOWN_FRAC,
            "note": "screening metric с overlap, НЕ portfolio PnL; "
                    "расхождение с ledger фиксируется заранее",
        },
        "n_attempts": len(attempts),
        "n_eligible": len(eligible),
        "status": "PRIMARY" if primary else "NO_SELECTION",
        "primary": primary,
        "top3_exploratory": top3,
        "best_ridge": best_ridge,
        "best_cont5_comparator": best_cont5,
        "attempt_table": attempts,
        "never_reselected_after_eval": True,
    }
    if primary is None:
        freeze["primary_policy"] = "cash"
    freeze_path = run.out / "freeze.json"
    dump_json(freeze_path, freeze)
    freeze_sha = sha256_file(freeze_path)
    run.log(f"stage C: attempts={len(attempts)} eligible={len(eligible)} "
            f"primary={primary['attempt'] if primary else 'NO_SELECTION'} "
            f"freeze_sha={freeze_sha[:12]}")
    return {"freeze": freeze, "freeze_sha256": freeze_sha, "val_rows": len(val),
            "attempts": attempts, "primary": primary, "top3": top3,
            "best_ridge": best_ridge, "best_cont5": best_cont5}


# ------------------------------------------------------------------ stage D


def _config_list(sel: dict) -> list[dict]:
    seen = {}
    order = []
    for label, rec in (("primary", sel["primary"]),
                       ("top3_2", sel["top3"][1] if len(sel["top3"]) > 1 else None),
                       ("top3_3", sel["top3"][2] if len(sel["top3"]) > 2 else None),
                       ("best_ridge", sel["best_ridge"]),
                       ("best_cont5", sel["best_cont5"])):
        if rec is None:
            continue
        key = rec["attempt"]
        if key in seen:
            seen[key].append(label)
            continue
        seen[key] = [label]
        order.append({"labels": [label], "attempt": rec})
    for k, labels in seen.items():
        for item in order:
            if item["attempt"]["attempt"] == k:
                item["labels"] = labels
                break
    return order


def evaluate_config(preds: list[dict], horizon: int, costs: tuple,
                    figi_map: dict, m1_paths: dict, run: Run) -> dict:
    c = CostParams(costs[1], costs[2])
    c.validate()
    by_ticker: dict[str, list[dict]] = {}
    for p in preds:
        by_ticker.setdefault(p["ticker"], []).append(p)
    prev = engine.EXIT_HORIZON_MIN
    outcomes = []
    try:
        engine.EXIT_HORIZON_MIN = horizon
        for ticker in sorted(by_ticker):
            store = MinuteStore()
            m1 = m1_paths.get(ticker)
            if m1 is not None and m1.exists():
                store.add_csv(m1, figi_map[ticker])
            for p in sorted(by_ticker[ticker], key=lambda x: x["row_id"]):
                outcomes.append(engine.execute_signal(p, store, c))
            del store
    finally:
        engine.EXIT_HORIZON_MIN = prev
    return summarize(outcomes, c, horizon_minutes=horizon)


def _mk_pred(meta: dict, selected: bool, horizon: int, model_id: str,
             split: str) -> dict:
    return {"row_id": meta["row_id"], "figi": meta["figi"],
            "ticker": meta["ticker"], "decision_ts": meta["decision_ts"],
            "available_at_ts": meta["decision_ts"], "model_id": model_id,
            "split": split, "selected_long": selected, "horizon": horizon}


def _portfolio_facts(res: dict) -> dict:
    pf = res["portfolio"]
    m = res["metrics"]
    executed = pf.executed
    fees = sum(t.commission_entry + t.commission_exit for t in executed)
    slip = sum(t.qty * (t.slip_entry - t.slip_exit) for t in executed)
    gross = sum(t.gross for t in executed)
    net = sum(t.net for t in executed)
    return {
        "metrics": m,
        "n_signal": pf.n_signal, "n_executed": pf.n_executed,
        "n_accepted_open": pf.n_accepted_open, "n_skip": pf.n_skip,
        "n_ignore_repeat": pf.n_ignore_repeat, "n_no_slot": pf.n_no_slot,
        "n_no_cash": pf.n_no_cash, "n_incomplete": pf.n_incomplete_outcomes,
        "gross_total": gross, "fees_total": fees, "slippage_total": slip,
        "net_total": net,
        "identity_ok": abs((gross - fees - slip) - net) < 1e-9,
        "final_equity": pf.final_equity,
        "last_known_equity": pf.last_known_equity,
        "last_known_ts": pf.last_known_ts.isoformat() if pf.last_known_ts else None,
        "max_drawdown_known": pf.max_drawdown_known,
        "free_cash_end": pf.free_cash_end,
        "closed_cashflow_diagnostic": m.get("closed_cashflow_diagnostic"),
        "day_realized": {d.isoformat(): v for d, v in pf.day_realized.items()},
        "day_unknown": sorted(d.isoformat() for d in pf.day_unknown),
        "equity_known": [[d.isoformat(), v] for d, v in pf.equity_known],
        "trades": [{"ticker": t.ticker, "figi": t.figi, "row_id": t.row_id,
                    "entry_ts": t.entry_ts.isoformat(),
                    "exit_ts": t.exit_ts.isoformat(), "net": t.net,
                    "gross": t.gross, "costs_total": t.costs_total,
                    "fees": t.commission_entry + t.commission_exit,
                    "slip": t.qty * (t.slip_entry - t.slip_exit)}
                   for t in executed],
        "accepted_open": [{"row_id": o.row_id, "ticker": o.ticker,
                           "entry_ts": o.entry_ts.isoformat(),
                           "entry_debit": o.entry_debit,
                           "planned_exit_ts": (o.planned_exit_ts.isoformat()
                                               if o.planned_exit_ts else None)}
                          for o in pf.accepted_open],
    }


def stage_d_eval(repo_root: Path, run: Run, sel: dict, fit_meta: dict,
                 freeze_sha: str, resume: bool) -> dict:
    snapshot = repo_root / REL_SNAPSHOT
    a_index = json.loads((run.out / "targets_a" / "_index.json").read_text(encoding="utf-8"))
    figi_map = {t: a_index[t]["figi"] for t in a_index if not a_index[t].get("empty")}
    m1_paths = {t: snapshot / "csv" / f"{t}__m1.csv" for t in figi_map}
    (run.out / "eval").mkdir(parents=True, exist_ok=True)
    dump_json(run.out / "eval_run.json", {
        "started_utc": now_utc_iso(), "freeze_sha256": freeze_sha,
        "note": "freeze.json не модифицируется после этого момента"})

    ev = pq.read_table(run.out / "predictions" / "eval.parquet").to_pandas()
    metas = [{"row_id": str(r.row_id), "figi": str(r.figi), "ticker": str(r.ticker),
              "decision_ts": str(r.decision_ts)} for r in ev.itertuples()]
    for m in metas:
        ts = parse_ts(m["decision_ts"])
        if ts.second or ts.microsecond:
            raise SystemExit("eval decision вне минутной сетки")
        if session_reason(ts) != "ok":
            raise SystemExit(f"eval decision вне сессии: {m}")

    baseline_horizon = sel["primary"]["horizon"] if sel["primary"] else max(HORIZONS)
    configs = []
    for item in _config_list(sel):
        a = item["attempt"]
        col = ev[f"p__{a['fit']}"].to_numpy(dtype=np.float64)
        selected = col > a["threshold"]
        configs.append({
            "name": "+".join(item["labels"]),
            "attempt": a, "model_id": a["attempt"], "horizon": a["horizon"],
            "selected_mask": selected,
        })
    configs.append({"name": "always_long", "attempt": None,
                    "model_id": "baseline_always_long", "horizon": baseline_horizon,
                    "selected_mask": np.ones(len(metas), dtype=bool)})
    configs.append({"name": "cash", "attempt": None, "model_id": "baseline_cash",
                    "horizon": baseline_horizon,
                    "selected_mask": np.zeros(len(metas), dtype=bool)})

    ledger_dir = run.out / "ledgers"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for cfg_rec in configs:
        h = cfg_rec["horizon"]
        model_id = cfg_rec["model_id"]
        for costs in COST_SCENARIOS:
            tag = f"{cfg_rec['name']}__{costs[0]}"
            res_path = run.out / "eval" / f"{tag}.json"
            if resume and res_path.exists():
                results.append(json.loads(res_path.read_text(encoding="utf-8")))
                run.log(f"resume eval {tag}")
                continue
            mask = cfg_rec["selected_mask"]
            preds = [_mk_pred(metas[i], True, h, model_id, "eval")
                     for i in np.nonzero(mask)[0]]
            started = time.time()
            res = evaluate_config(preds, h, costs, figi_map, m1_paths, run)
            facts = _portfolio_facts(res)
            pf = res["portfolio"]
            engine.save_executed_ledger(pf, ledger_dir / f"{tag}__executed.csv")
            engine.save_accepted_open_ledger(pf, ledger_dir / f"{tag}__open.csv")
            rec = {"config": cfg_rec["name"], "labels": cfg_rec["labels"]
                   if "labels" in cfg_rec else [cfg_rec["name"]],
                   "attempt": cfg_rec["attempt"], "model_id": model_id,
                   "horizon": h, "cost_id": costs[0],
                   "commission_bps": costs[1], "slippage_bps": costs[2],
                   "n_eligible_eval": len(metas),
                   "n_selected": int(mask.sum()),
                   "freeze_sha256": freeze_sha,
                   "seconds": round(time.time() - started, 2),
                   "facts": facts}
            dump_json(res_path, rec)
            del res, pf
            results.append(rec)
            run.log(f"eval {tag}: selected={rec['n_selected']} "
                    f"closed={facts['n_executed']} net={facts['net_total']:.6f} "
                    f"final={facts['final_equity']}")
    dump_json(run.out / "stage_e_meta.json",
              {"n_eligible_eval": len(metas),
               "baseline_horizon": baseline_horizon,
               "configs": [c["name"] for c in configs]})
    return {"results": results, "n_eligible_eval": len(metas),
            "baseline_horizon": baseline_horizon}


# ------------------------------------------------------------------ stage E


def _daily_axis(results: list[dict], config_name: str, cost_id: str) -> dict:
    for r in results:
        if r["config"] == config_name and r["cost_id"] == cost_id:
            return {d: v for d, v in r["facts"]["day_realized"].items()}
    return {}


def bootstrap_block_ci(diff: dict, n: int = N_BOOTSTRAP,
                       block: int = BOOTSTRAP_BLOCK, seed: int = BOOTSTRAP_SEED) -> dict:
    days = sorted(diff)
    vals = [diff[d] for d in days]
    if not vals:
        return {"n_days": 0, "mean": None, "median": None, "ci_low": None,
                "ci_high": None, "n_blocks": 0, "block_days": block,
                "seed": seed, "n_bootstrap": n}
    nb = max(1, len(vals) // block)
    blocks = [vals[i * block:(i + 1) * block] for i in range(nb)]
    blocks = [b for b in blocks if b]
    rng = random.Random(seed)
    means = []
    for _ in range(n):
        sample = []
        for _ in range(len(blocks)):
            sample.extend(blocks[rng.randrange(len(blocks))])
        if not sample:
            continue
        means.append(statistics.mean(sample))
    means.sort()
    lo = means[int(0.025 * (len(means) - 1))] if means else None
    hi = means[int(0.975 * (len(means) - 1))] if means else None
    return {"n_days": len(vals), "n_blocks": len(blocks),
            "mean": statistics.mean(vals), "median": statistics.median(vals),
            "ci_low": lo, "ci_high": hi, "block_days": block, "seed": seed,
            "n_bootstrap": n}


def stage_e(run: Run, sel: dict, eval_out: dict, val_meta: dict) -> dict:
    results = eval_out["results"]
    primary_name = sel["primary"]["attempt"] if sel["primary"] else None
    pcfg = None
    if primary_name:
        for r in results:
            if (r["cost_id"] == COST_SCENARIOS[0][0]
                    and r["config"].split("+")[0] == "primary"):
                pcfg = r["config"]
                break
    by_cost = {}
    for cost in COST_SCENARIOS:
        cid = cost[0]
        axis = {}
        for r in results:
            if r["cost_id"] != cid:
                continue
            for d, v in r["facts"]["day_realized"].items():
                axis.setdefault(d, set()).add(r["config"])
        names = sorted({r["config"] for r in results if r["cost_id"] == cid})
        paired = {}
        if pcfg:
            p_day = _daily_axis(results, pcfg, cid)
            for base in ("cash", "always_long"):
                b_day = _daily_axis(results, base, cid) if base != "cash" else {}
                if base == "cash":
                    common = sorted(p_day)
                else:
                    common = sorted(set(p_day) & set(b_day))
                full_axis = sorted(axis)
                coverage = (len(common) / len(full_axis)) if full_axis else 0.0
                diff = {d: p_day.get(d, 0.0) - b_day.get(d, 0.0) for d in common}
                paired[base] = {
                    "n_axis_days": len(full_axis), "n_common_known_days": len(common),
                    "coverage": round(coverage, 6),
                    "total_diff": sum(diff.values()),
                    "mean_daily_diff": (statistics.mean(diff.values())
                                        if diff else None),
                    "bootstrap": bootstrap_block_ci(diff),
                    "missing_days": sorted(set(full_axis) - set(common)),
                }
        by_cost[cid] = {"paired": paired, "configs": names}

    ticker_month = {}
    sensitivity = {}
    if pcfg:
        for cost in COST_SCENARIOS:
            cid = cost[0]
            prim = next(r for r in results
                        if r["config"] == pcfg and r["cost_id"] == cid)
            trades = prim["facts"]["trades"]
            by_t: dict[str, float] = {}
            by_m: dict[str, float] = {}
            for t in trades:
                by_t[t["ticker"]] = by_t.get(t["ticker"], 0.0) + t["net"]
                mkey = t["exit_ts"][:7]
                by_m[mkey] = by_m.get(mkey, 0.0) + t["net"]
            total = prim["facts"]["net_total"]
            best_t = max(by_t, key=by_t.get) if by_t else None
            day_net = prim["facts"]["day_realized"]
            best_d = max(day_net, key=day_net.get) if day_net else None
            ticker_month[cid] = {
                "by_ticker": dict(sorted(by_t.items(), key=lambda kv: -kv[1])),
                "by_month": dict(sorted(by_m.items())),
                "n_trades": len(trades),
                "concentration_top1_share": (abs(by_t[best_t]) / abs(total)
                                             if best_t and total else None),
                "best_ticker": best_t, "best_day": best_d,
            }
            sensitivity[cid] = {
                "total_net": total,
                "net_without_best_ticker": (total - by_t[best_t]) if best_t else total,
                "net_without_best_day": (total - day_net[best_d]) if best_d else total,
                "n_trades_without_best_ticker": (len(trades) - sum(
                    1 for t in trades if t["ticker"] == best_t)) if best_t else len(trades),
                "note": "без перенастройки: тот же frozen комплект, тот же ledger",
            }

    residuals = {}
    for split in ("val", "eval"):
        tbl = pq.read_table(run.out / "predictions" / f"{split}.parquet").to_pandas()
        block = {}
        for h in HORIZONS:
            known = tbl[f"st{h}"].to_numpy() == "FILLED"
            y = tbl[f"n52{h}"].to_numpy(dtype=np.float64)
            for key in sorted(c for c in tbl.columns if c.startswith("p__")):
                if not key.endswith(f"h{h}"):
                    continue
                p = tbl[key].to_numpy(dtype=np.float64)
                m = known & np.isfinite(p) & np.isfinite(y)
                if m.sum() < 10:
                    continue
                err = p[m] - y[m]
                dec = pd_qcut(p[m], y[m], 10)
                block[f"{key[3:]}"] = {
                    "n_known": int(m.sum()), "mae": float(np.mean(np.abs(err))),
                    "bias": float(np.mean(err)),
                    "rmse": float(np.sqrt(np.mean(err ** 2))),
                    "pearson": float(np.corrcoef(p[m], y[m])[0, 1]),
                    "mean_pred": float(np.mean(p[m])), "mean_realized": float(np.mean(y[m])),
                    "deciles": dec,
                }
        residuals[split] = block

    unknown = {}
    for split in ("val", "eval"):
        tbl = pq.read_table(run.out / "predictions" / f"{split}.parquet").to_pandas()
        u = {}
        for h in HORIZONS:
            st = tbl[f"st{h}"]
            u[str(h)] = {"FILLED": int((st == "FILLED").sum()),
                         "INCOMPLETE": int((st == "INCOMPLETE").sum()),
                         "SKIP": int((st == "SKIP").sum())}
            by_t = tbl.loc[st == "INCOMPLETE", "ticker"].value_counts().head(8)
            u[str(h)]["top_incomplete_tickers"] = {str(k): int(v)
                                                   for k, v in by_t.items()}
        unknown[split] = u

    return {"paired_and_coverage": by_cost, "ticker_month": ticker_month,
            "sensitivity": sensitivity, "residuals": residuals,
            "unknown_location": unknown}


def pd_qcut(pred: np.ndarray, realized: np.ndarray, k: int) -> list[dict]:
    order = np.argsort(pred)
    n = len(order)
    out = []
    for i in range(k):
        sl = order[i * n // k:(i + 1) * n // k]
        if len(sl) == 0:
            continue
        out.append({"bin": i + 1, "n": int(len(sl)),
                    "pred_lo": float(pred[sl].min()), "pred_hi": float(pred[sl].max()),
                    "pred_mean": float(pred[sl].mean()),
                    "realized_mean": float(realized[sl].mean()),
                    "hit_rate": float(np.mean(realized[sl] > 0))})
    return out


# ------------------------------------------------------------------ stage F


def stage_f(run: Run, repo_root: Path, input_manifest: dict, cfg: dict, sel: dict,
            eval_out: dict, stage_e_out: dict, stage_b_out: dict,
            stage_a_out: dict, freeze_sha: str) -> dict:
    import pandas as pd

    rows = []
    for r in eval_out["results"]:
        f = r["facts"]
        a = r["attempt"] or {}
        rows.append({
            "config": r["config"], "model_id": r["model_id"],
            "feature_set": a.get("feature_set"), "horizon": r["horizon"],
            "threshold": a.get("threshold"), "cost_id": r["cost_id"],
            "commission_bps": r["commission_bps"], "slippage_bps": r["slippage_bps"],
            "split": "eval", "n_eligible": r["n_eligible_eval"],
            "n_selected": r["n_selected"], "n_signal": f["n_signal"],
            "n_closed": f["n_executed"], "n_open": f["n_accepted_open"],
            "n_skip": f["n_skip"], "n_ignore": f["n_ignore_repeat"],
            "n_no_slot": f["n_no_slot"], "n_no_cash": f["n_no_cash"],
            "n_incomplete": f["n_incomplete"],
            "gross_total": f["gross_total"], "fees_total": f["fees_total"],
            "slippage_total": f["slippage_total"], "net_total": f["net_total"],
            "final_equity": f["final_equity"],
            "last_known_equity": f["last_known_equity"],
            "last_known_ts": f["last_known_ts"],
            "max_drawdown_known": f["max_drawdown_known"],
            "identity_ok": f["identity_ok"],
            "mean_net_known_val_screen": (a.get("mean_net_known")
                                          if a.get("split") == "val" else None),
        })
    for a in sel["attempts"]:
        rows.append({
            "config": f"attempt:{a['attempt']}", "model_id": a["attempt"],
            "feature_set": a["feature_set"], "horizon": a["horizon"],
            "threshold": a["threshold"], "cost_id": "c5_s2_screening",
            "commission_bps": 5.0, "slippage_bps": 2.0, "split": "val",
            "n_eligible": eval_out["n_eligible_eval"], "n_selected": a["n_selected"],
            "n_known": a["n_known"], "n_unknown": a["n_unknown"],
            "n_skip": a["n_skip"], "unknown_frac": a["unknown_frac"],
            "n_dates": a["n_dates"], "mean_net_known_val": a["mean_net_known"],
            "eligible": a["eligible"], "fail_reason": a["fail_reason"],
        })
    df = pd.DataFrame(rows)
    summary_path = run.out / "summary.csv"
    df.to_csv(summary_path, index=False)

    primary = sel["primary"]
    pcfg = None
    if primary:
        for r in eval_out["results"]:
            if r["cost_id"] == COST_SCENARIOS[0][0] and r["config"].split("+")[0] == "primary":
                pcfg = r["config"]
                break
    p_res = [r for r in eval_out["results"] if pcfg and r["config"] == pcfg]

    limits = [
        "Период — development, не fresh holdout; выбор по val, один eval после freeze.",
        "Minute-store покрытие неполное: часть тикеров начинается в 2026-01..2026-08, "
        "m1 заканчивается около 2026-10-04 (SBER 2026-10-08) -> INCOMPLETE/unknown "
        "на хвосте eval; unknown ≠ 0 и не входит в mean known net.",
        "Корпоративные действия и календарь snapshot'а: UNVERIFIED / NOT_VERIFIED.",
        "Screening metric (mean known event net) — с overlap и без учёта 5 слотов; "
        "портфельный ledger может заметно расходиться с ним.",
        "Fractional quantities (research, не live lots); short не рассматривается.",
        "При NO_SELECTION/cash пороги и support не снижаются.",
        "500 bootstrap-выборок блоками по 5 сессионных дней — описательный интервал, "
        "не коррекция множественного поиска.",
        "Цена входа = открытие 1m-бара decision_ts (raw next-open = закрытие 5m).",
        "Непрерывность промежуточных минут (path_metrics) не влияет на endpoint PnL "
        "без стопов; path metrics помечены отдельно.",
        "Модельный reference daypack не изменялся (read-only), deviation = none.",
    ]

    result = {
        "branch": BRANCH,
        "run_id": run.out.name,
        "run_path": str(run.out.relative_to(repo_root)),
        "generated_utc": now_utc_iso(),
        "config": {"path": str((run.out / "config_frozen.json").relative_to(repo_root)),
                   "branch_config": str((repo_root / REL_BRANCH_DIR / "config.json")
                                        .relative_to(repo_root)),
                   "seed": SEED},
        "inputhashes": {
            "dataset_manifest_sha256": input_manifest["dataset"]["manifest_sha256"],
            "snapshot_manifest_sha256": input_manifest["snapshot"]["manifest_sha256"],
            "daypack_manifest_sha256": input_manifest["daypack"]["manifest_sha256"],
            "script_sha256": input_manifest["code"]["script_sha256"],
            "script_sha256_stage_f": sha256_file(Path(__file__).resolve()),
            "freeze_sha256": freeze_sha,
            "predictor_columns_sha256": input_manifest["dataset"]["predictor_columns_sha256"],
        },
        "primary": {"status": sel["freeze"]["status"],
                    "config": pcfg, "policy": "cash" if primary is None else "long"},
        "selection": {
            "criterion": sel["freeze"]["selection_criterion"],
            "n_attempts": sel["freeze"]["n_attempts"],
            "n_eligible": sel["freeze"]["n_eligible"],
            "top3_exploratory": sel["freeze"]["top3_exploratory"],
            "best_ridge": sel["freeze"]["best_ridge"],
            "best_cont5_comparator": sel["freeze"]["best_cont5_comparator"],
            "never_reselected_after_eval": True,
        },
        "universe": input_manifest["universe"],
        "counts": {
            "stage_a_rows": stage_a_out["rows"],
            "stage_a_status": stage_a_out["status_counts"],
            "stage_a_session_rejected": stage_a_out["session_rejected"],
            "splits": stage_b_out["splits"],
            "dropped": stage_b_out["dropped"],
            "n_eligible_eval": eval_out["n_eligible_eval"],
            "baseline_horizon": eval_out["baseline_horizon"],
            "known_by_split_residuals": {
                s: {k: v["n_known"] for k, v in stage_e_out["residuals"][s].items()}
                for s in stage_e_out["residuals"]},
            "unknown_by_split": stage_e_out["unknown_location"],
        },
        "per_cost": [
            {"config": r["config"], "cost_id": r["cost_id"], "horizon": r["horizon"],
             "n_selected": r["n_selected"], "signals": r["n_selected"],
             "entry": r["n_selected"] - r["facts"]["n_skip"],
             "closed": r["facts"]["n_executed"],
             "open": r["facts"]["n_accepted_open"],
             "gross": r["facts"]["gross_total"], "fees": r["facts"]["fees_total"],
             "slip": r["facts"]["slippage_total"], "net": r["facts"]["net_total"],
             "final_equity": r["facts"]["final_equity"],
             "last_known_equity": r["facts"]["last_known_equity"],
             "last_known_ts": r["facts"]["last_known_ts"],
             "max_drawdown_known": r["facts"]["max_drawdown_known"],
             "genuine_known_prefix_last_known_ts": r["facts"]["last_known_ts"],
             "genuine_final_equity": r["facts"]["final_equity"],
             "closed_cashflow_diagnostic": r["facts"]["closed_cashflow_diagnostic"],
             "identity_ok": r["facts"]["identity_ok"]}
            for r in eval_out["results"]],
        "baselines_and_paired": stage_e_out["paired_and_coverage"],
        "ticker_month": stage_e_out["ticker_month"],
        "sensitivity": stage_e_out["sensitivity"],
        "residuals": stage_e_out["residuals"],
        "failed_checks": [],
        "remaining_limitations": limits,
        "reproduce": (
            "cd backend && OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 "
            ".venv/bin/python scripts/research_day_b2.py "
            f"--repo {repo_root} --out {run.out.relative_to(repo_root)} --resume"),
        "summary_csv": str(summary_path.relative_to(repo_root)),
    }
    dump_json(run.out / "RESULT.json", result)

    md = []
    md.append(f"# PARALLEL-DAY-1 · Builder-2 · ветка `{BRANCH}`\n")
    md.append(f"Run: `{run.out.name}` · {result['generated_utc']}\n")
    md.append(f"\n## Статус primary\n\n`{sel['freeze']['status']}`\n")
    if primary:
        md.append(f"\n- config: `{primary['attempt']}` (threshold {primary['threshold']})\n"
                  f"- horizon: {primary['horizon']} мин, cost 5/2\n"
                  f"- val screening: mean known event net **{primary['mean_net_known']:.6f}** "
                  f"при n_known={primary['n_known']}, dates={primary['n_dates']}, "
                  f"unknown_frac={primary['unknown_frac']}\n")
    else:
        md.append("\nДопустимых конфигураций нет -> NO_SELECTION / cash "
                  "(пороги и support не снижались).\n")
    md.append(f"\nВсего policy attempts: **{sel['freeze']['n_attempts']}** "
              f"(12 fits x 3 порога), допустимых: {sel['freeze']['n_eligible']}.\n")
    md.append("\n## Критерий отбора (общий, п.9)\n\n")
    sc = sel["freeze"]["selection_criterion"]
    md.append(f"- метрика: {sc['metric']}\n- min known events: {sc['min_known_events']}\n"
              f"- min known dates: {sc['min_known_dates']}\n"
              f"- max unknown endpoint fraction: {sc['max_unknown_endpoint_fraction']}\n"
              f"- {sc['note']}\n")
    md.append("\n## Сплиты и embargo\n\n")
    md.append(f"- train/val/eval: {stage_b_out['splits']}\n"
              f"- dropped: {stage_b_out['dropped']}\n")
    md.append("\n## Eval ledger (cost 5/2 и 10/5)\n\n")
    md.append("| config | cost | horizon | selected | closed | open | gross | fees | slip | net | final | last_known |\n")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for r in eval_out["results"]:
        f = r["facts"]
        md.append(f"| {r['config']} | {r['cost_id']} | {r['horizon']} | "
                  f"{r['n_selected']} | {f['n_executed']} | {f['n_accepted_open']} | "
                  f"{f['gross_total']:.6f} | {f['fees_total']:.6f} | "
                  f"{f['slippage_total']:.6f} | {f['net_total']:.6f} | "
                  f"{f['final_equity']} | {f['last_known_equity']} |\n")
    md.append("\n## Paired daily vs cash / always_long\n\n")
    for cid, blk in stage_e_out["paired_and_coverage"].items():
        md.append(f"### cost {cid}\n\n")
        for base, p in blk["paired"].items():
            b = p["bootstrap"]
            md.append(f"- vs **{base}**: common known days {p['n_common_known_days']}/"
                      f"{p['n_axis_days']} (coverage {p['coverage']:.3f}), "
                      f"total diff {p['total_diff']:.6f}, mean daily {p['mean_daily_diff']}, "
                      f"bootstrap95 [{b['ci_low']}, {b['ci_high']}] "
                      f"(n={b['n_bootstrap']}, block={b['block_days']}d, seed={b['seed']})\n")
    md.append("\n## Sensitivity (без лучшего тикера / дня)\n\n")
    for cid, s in stage_e_out["sensitivity"].items():
        md.append(f"- {cid}: total {s['total_net']:.6f} -> without best ticker "
                  f"{s['net_without_best_ticker']:.6f}, without best day "
                  f"{s['net_without_best_day']:.6f}\n")
    md.append("\n## Воспроизводимость\n\n```\n" + result["reproduce"] + "\n```\n")
    md.append("\n## Ограничения\n\n")
    for i, l in enumerate(limits, 1):
        md.append(f"{i}. {l}\n")
    (run.out / "RESULT.md").write_text("".join(md), encoding="utf-8")
    run.log(f"stage F: summary.csv, RESULT.md, RESULT.json записаны "
            f"({summary_path.stat().st_size} B)")
    return result


# ------------------------------------------------------------------ stage G


def _slot_spearman(pred, fit: str, h: int, min_slot: int = 5) -> dict:
    from scipy.stats import spearmanr

    p_col = f"p__{fit}"
    y_col = f"n52{h}"
    st_col = f"st{h}"
    d = pred[["decision_ts", p_col, y_col, st_col]].copy()
    d[y_col] = d[y_col].astype(np.float64)
    d = d[(d[st_col] == "FILLED") & np.isfinite(d[y_col])]
    ics = []
    for _, g in d.groupby("decision_ts"):
        if len(g) < min_slot or g[p_col].nunique() < 2 or g[y_col].nunique() < 2:
            continue
        rho = spearmanr(g[p_col].to_numpy(dtype=np.float64),
                        g[y_col].to_numpy(dtype=np.float64)).statistic
        if np.isfinite(rho):
            ics.append(float(rho))
    if not ics:
        return {"n_slots": 0, "mean_ic": None, "sd": None, "t_stat": None,
                "frac_positive": None}
    arr = np.asarray(ics, dtype=np.float64)
    sd = float(arr.std(ddof=1)) if len(arr) > 1 else 0.0
    return {"n_slots": int(len(arr)), "mean_ic": float(arr.mean()),
            "sd": sd or None,
            "t_stat": float(arr.mean() / (sd / math.sqrt(len(arr))))
            if sd else None,
            "frac_positive": float((arr > 0).mean())}


def _m1_close_arrays(path: Path):
    import pandas as pd

    df = pd.read_csv(path, usecols=["ts", "close"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    ts = df["ts"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    ts = ts.astype("datetime64[ns]", copy=False)
    return ts, df["close"].to_numpy(dtype=np.float64)


def _ret30(ts_np, close_np, decision_iso: str, window_min: int = 91,
           lag_min: int = 30):
    dt = parse_ts(decision_iso).astimezone(UTC).replace(tzinfo=None)
    d64 = np.datetime64(dt, "ns")
    lo = d64 - np.timedelta64(window_min, "m")
    i_hi = int(np.searchsorted(ts_np, d64, side="left"))
    i_lo = int(np.searchsorted(ts_np, lo, side="right"))
    if i_hi <= i_lo:
        return None
    t1 = ts_np[i_hi - 1]
    j = int(np.searchsorted(ts_np, t1 - np.timedelta64(lag_min, "m"),
                            side="right")) - 1
    if j < i_lo:
        return None
    c1 = float(close_np[i_hi - 1])
    c0 = float(close_np[j])
    if not math.isfinite(c1) or not math.isfinite(c0) or c0 <= 0.0:
        return None
    return c1 / c0 - 1.0


def _attach_ret30(df, csv_dir: Path):
    df = df.reset_index(drop=True)
    ret = np.full(len(df), np.nan, dtype=np.float64)
    for tkr, idxs in df.groupby("ticker").groups.items():
        path = csv_dir / f"{tkr}__m1.csv"
        if not path.exists():
            continue
        ts_np, close_np = _m1_close_arrays(path)
        for i in idxs:
            val = _ret30(ts_np, close_np, str(df.at[i, "decision_ts"]))
            if val is not None:
                ret[i] = val
    out = df.copy()
    out["ret30"] = ret
    return out


def _event_stats(d, st_col: str, y_col: str) -> dict:
    import pandas as pd

    known = d[(d[st_col] == "FILLED") & np.isfinite(d[y_col].astype(np.float64))]
    base = {"n_rows": int(len(d)), "n_known": int(len(known))}
    if known.empty:
        base.update({"mean_net_known": None, "frac_positive": None,
                     "sum_net_known": None, "n_dates": 0, "best_ticker": None,
                     "mean_without_best_ticker": None,
                     "sum_without_best_day": None, "by_month": {}})
        return base
    k = known.copy()
    ts_naive = pd.to_datetime(k["decision_ts"], utc=True).dt.tz_localize(None)
    k["day"] = ts_naive.dt.date
    k["month"] = ts_naive.dt.to_period("M")
    sums = k.groupby("ticker")[y_col].sum().sort_values(ascending=False)
    day_sums = k.groupby("day")[y_col].sum().sort_values(ascending=False)
    best = str(sums.index[0])
    n_best = int((k["ticker"] == best).sum())
    total = float(sums.sum())
    base.update({
        "mean_net_known": float(k[y_col].mean()),
        "frac_positive": float((k[y_col] > 0).mean()),
        "sum_net_known": total,
        "n_dates": int(k["day"].nunique()),
        "best_ticker": best,
        "mean_without_best_ticker":
            float((total - sums.iloc[0]) / max(len(k) - n_best, 1)),
        "sum_without_best_day": float(total - day_sums.iloc[0]),
        "by_month": {str(m): {"n": int(len(g)), "sum": float(g[y_col].sum())}
                     for m, g in k.groupby("month")},
    })
    return base


def _dip_chase(d, st_col: str, y_col: str) -> dict:
    from scipy.stats import spearmanr

    known = d[np.isfinite(d["ret30"]) & (d[st_col] == "FILLED")
              & np.isfinite(d[y_col].astype(np.float64))]
    out = {"n_rows": int(len(d)), "n_with_ret30_known": int(len(known))}
    for name, mask in (("dip", known["ret30"] < 0.0),
                       ("chase", known["ret30"] >= 0.0)):
        s = known[mask]
        out[name] = {"n": int(len(s)),
                     "mean_net": float(s[y_col].mean()) if len(s) else None,
                     "frac_positive": float((s[y_col] > 0).mean())
                     if len(s) else None,
                     "sum_net": float(s[y_col].sum()) if len(s) else 0.0}
    if len(known) >= 3:
        rho, pv = spearmanr(known["ret30"].to_numpy(dtype=np.float64),
                            known[y_col].to_numpy(dtype=np.float64))
        out["spearman_ret30_net"] = {"n": int(len(known)), "rho": float(rho),
                                     "p_value": float(pv)}
    else:
        out["spearman_ret30_net"] = {"n": int(len(known)), "rho": None,
                                     "p_value": None}
    dip_mean = out["dip"]["mean_net"]
    chase_mean = out["chase"]["mean_net"]
    out["dip_mean_gt_chase_mean"] = bool(
        dip_mean is not None and chase_mean is not None
        and dip_mean > chase_mean)
    return out


def _ledger_ids(path: Path) -> set:
    import pandas as pd

    if not path.exists():
        return set()
    df = pd.read_csv(path, usecols=["row_id"])
    return set(df["row_id"].astype(str))


def _selected_frame(ev, primary: dict, executed_ids: set, open_ids: set):
    col = ev[f"p__{primary['fit']}"].to_numpy(dtype=np.float64)
    mask = col > float(primary["threshold"])
    sel = ev.loc[mask].reset_index(drop=True)
    rid = sel["row_id"].astype(str)
    sel["position"] = np.where(rid.isin(executed_ids), "executed",
                               np.where(rid.isin(open_ids), "open", "blocked"))
    return sel


def _accounting_checks(sel_n: int, facts: dict) -> dict:
    counters = {k: int(facts[k]) for k in
                ("n_signal", "n_executed", "n_accepted_open",
                 "n_ignore_repeat", "n_no_slot", "n_no_cash", "n_skip")}
    acc_sum = sum(counters[k] for k in
                  ("n_executed", "n_accepted_open", "n_ignore_repeat",
                   "n_no_slot", "n_no_cash", "n_skip"))
    return {"n_selected_recomputed": int(sel_n), "counters": counters,
            "selected_matches_n_signal":
                bool(int(sel_n) == counters["n_signal"]),
            "accounting_sums_to_signal": bool(acc_sum == counters["n_signal"])}


def _executed_ledger_dip_chase(led, sel_with_ret30):
    import pandas as pd

    m = led.merge(sel_with_ret30[["row_id", "ret30"]], on="row_id", how="left")
    out = {}
    for name, mask in (("dip", m["ret30"] < 0.0), ("chase", m["ret30"] >= 0.0)):
        s = m[mask]
        out[name] = {"n": int(len(s)),
                     "mean_gross": float(s["gross"].mean()) if len(s) else None,
                     "mean_net": float(s["net"].mean()) if len(s) else None}
    return out


def _is_primary_eval(rec: dict) -> bool:
    for lab in (rec.get("labels") or []):
        if "primary" in str(lab).split("+"):
            return True
    return False


def _prereg_verdict(result: dict) -> dict:
    ic_val = result["cs_ic"].get("val", {}).get("h60") or {}
    ic_eval = result["cs_ic"].get("eval", {}).get("h60") or {}
    h1 = bool(ic_val.get("mean_ic") is not None
              and ic_val.get("t_stat") is not None
              and ic_val["mean_ic"] >= 0.05 and ic_val["t_stat"] >= 2.0
              and (ic_eval.get("mean_ic") or 0.0) > 0.0)
    led = result.get("h2_executed_ledger") or {}
    dip = led.get("dip", {})
    chase = led.get("chase", {})
    h2 = bool((dip.get("n") or 0) >= 30
              and dip.get("mean_gross") is not None
              and dip["mean_gross"] > 0.0
              and chase.get("mean_gross") is not None
              and chase["mean_gross"] <= 0.0)
    blocked = (result.get("repeat_accounting") or {}).get("blocked") or {}
    h3 = bool((blocked.get("n_known") or 0) >= 200
              and (blocked.get("n_dates") or 0) >= 20
              and (blocked.get("mean_net_known") or 0.0) > 0.0)
    return {"h1_cs_ic": h1, "h2_no_chase": h2, "h3_repeat_blocked": h3}


def _fmt(value, digits: int = 6) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _dc_row(label: str, blk: dict) -> str:
    dip = blk.get("dip", {})
    chase = blk.get("chase", {})
    rho = blk.get("spearman_ret30_net", {})
    return (f"| {label} | {dip.get('n')} | {_fmt(dip.get('mean_net'))} | "
            f"{chase.get('n')} | {_fmt(chase.get('mean_net'))} | "
            f"{_fmt(rho.get('rho'), 4)} | {blk.get('n_with_ret30_known')} |\n")


def _write_diagnostics_md(run: Run, result: dict) -> None:
    p = result.get("primary") or {}
    md = ["# PARALLEL-DAY-1 · Builder-2 · stage G diagnostics\n\n",
          f"Run: `{run.out.name}` · {result['generated_utc']} · "
          f"script_sha256_stage_g {result['script_sha256_stage_g'][:12]}\n\n",
          "Диагностика по prereg diag-1: event-level метрики (screening-style), "
          "НЕ portfolio PnL; eval exploratory (окно сожжено в-development). "
          "Отбор правил по eval запрещён.\n\n",
          f"Primary: `{p.get('attempt')}` · fit {p.get('fit')} · "
          f"threshold {p.get('threshold')} · horizon {p.get('horizon')}\n\n"]
    md.append("## H1 · CS-IC (Spearman в слоте decision_ts, ≥5 тикеров, FILLED)\n\n")
    md.append("| split | h | slots | mean IC | t | frac>0 |\n")
    md.append("|---|---|---|---|---|---|\n")
    for split in ("val", "eval"):
        for hh in HORIZONS:
            r = result["cs_ic"].get(split, {}).get(f"h{hh}") or {}
            md.append(f"| {split} | {hh} | {r.get('n_slots')} | "
                      f"{_fmt(r.get('mean_ic'))} | {_fmt(r.get('t_stat'))} | "
                      f"{_fmt(r.get('frac_positive'), 4)} |\n")
    md.append(f"\nPrereg-вердикт H1 (val h60 mean_IC ≥ 0.05, t ≥ 2, eval IC > 0): "
              f"**{'PASS' if result['prereg']['h1_cs_ic'] else 'FAIL'}**\n\n")
    md.append("## H2 · dip vs chase (ret30: close[−30м к t1] к t1, окно 91м)\n\n")
    md.append("| группа | dip n | dip mean net | chase n | chase mean net | "
              "Spearman | n known |\n|---|---|---|---|---|---|---|\n")
    for split in ("val", "eval"):
        for group in ("all", "executed", "blocked"):
            blk = result["dip_chase"].get(split, {}).get(group)
            if blk:
                md.append(_dc_row(f"{split}·{group}", blk))
    cov_v = result["dip_chase"].get("val", {}).get("ret30_coverage")
    cov_e = result["dip_chase"].get("eval", {}).get("ret30_coverage")
    md.append(f"\ncoverage ret30: val {_fmt(cov_v, 4)} / eval {_fmt(cov_e, 4)}\n")
    led = result.get("h2_executed_ledger")
    if led:
        md.append("executed ledger (гросс/нетто по ценам close-to-close, "
                  "ledger units):\n\n")
        md.append("| sign | n | mean gross | mean net |\n|---|---|---|---|\n")
        for name in ("dip", "chase"):
            r = led[name]
            md.append(f"| {name} | {r['n']} | {_fmt(r['mean_gross'])} | "
                      f"{_fmt(r['mean_net'])} |\n")
    md.append(f"\nPrereg-вердикт H2 (executed dip n ≥ 30, gross_dip > 0, "
              f"gross_chase ≤ 0): "
              f"**{'PASS' if result['prereg']['h2_no_chase'] else 'FAIL'}**\n\n")
    md.append("## H3 · repeat-блокировка (selected vs executed, primary c5_s2)\n\n")
    acc = result.get("repeat_accounting")
    if acc and acc.get("counters"):
        c = acc["counters"]
        md.append(f"accounting: n_signal {c['n_signal']} = executed "
                  f"{c['n_executed']} + open {c['n_accepted_open']} + "
                  f"ignore_repeat {c['n_ignore_repeat']} + no_slot "
                  f"{c['n_no_slot']} + no_cash {c['n_no_cash']} + skip "
                  f"{c['n_skip']} · reselected {acc['n_selected_recomputed']} · "
                  f"positions {acc['positions']}\n\n")
        md.append("| группа | n known | mean net | dates | frac>0 | "
                  "mean w/o best ticker | sum w/o best day |\n"
                  "|---|---|---|---|---|---|---|\n")
        for name in ("executed", "blocked"):
            s = acc[name]
            md.append(f"| {name} | {s['n_known']} | "
                      f"{_fmt(s['mean_net_known'])} | {s['n_dates']} | "
                      f"{_fmt(s['frac_positive'], 4)} | "
                      f"{_fmt(s['mean_without_best_ticker'])} | "
                      f"{_fmt(s['sum_without_best_day'])} |\n")
        if acc.get("blocked", {}).get("by_month"):
            months = ", ".join(
                f"{m}: n={v['n']} sum={v['sum']:.3f}"
                for m, v in acc["blocked"]["by_month"].items())
            md.append(f"\nblocked по месяцам: {months}\n")
    md.append(f"\nPrereg-вердикт H3 (blocked mean_net > 0, n_known ≥ 200, "
              f"dates ≥ 20): "
              f"**{'PASS' if result['prereg']['h3_repeat_blocked'] else 'FAIL'}**\n\n")
    md.append("## Mechanical checks\n\n")
    if result["checks_failed"]:
        md.append(f"FAILED: {', '.join(result['checks_failed'])}\n")
    else:
        md.append("все механические проверки пройдены\n")
    (run.out / "DIAGNOSTICS.md").write_text("".join(md), encoding="utf-8")


def stage_g_diagnostics(repo_root: Path, run: Run, sel: dict, eval_out: dict,
                        resume: bool) -> dict:
    """Диагностика prereg diag-1: CS-IC, repeat-блокировка, dip/chase."""
    import pandas as pd

    out_path = run.out / "stage_g_summary.json"
    if resume and out_path.exists():
        res = json.loads(out_path.read_text(encoding="utf-8"))
        run.log("resume stage G diagnostics")
        return res
    primary = sel.get("primary")
    result = {"stage": "g", "primary": None, "cs_ic": {},
              "repeat_accounting": None, "dip_chase": {},
              "h2_executed_ledger": None, "prereg": {},
              "checks_failed": [], "generated_utc": now_utc_iso(),
              "script_sha256_stage_g": sha256_file(Path(__file__).resolve())}
    if primary is None:
        result["checks_failed"].append("primary_absent")
        dump_json(out_path, result)
        _write_diagnostics_md(run, result)
        return result
    fit = primary["fit"]
    thr = float(primary["threshold"])
    h = int(primary["horizon"])
    result["primary"] = {"attempt": primary["attempt"], "fit": fit,
                         "threshold": thr, "horizon": h,
                         "model": primary["model"],
                         "feature_set": primary["feature_set"]}
    csv_dir = repo_root / REL_SNAPSHOT / "csv"
    val = pq.read_table(run.out / "predictions" / "val.parquet").to_pandas()
    ev = pq.read_table(run.out / "predictions" / "eval.parquet").to_pandas()

    for split, d in (("val", val), ("eval", ev)):
        for hh in HORIZONS:
            fkey = fit_key(primary["model"], primary["feature_set"], hh)
            result["cs_ic"].setdefault(split, {})[f"h{hh}"] = \
                _slot_spearman(d, fkey, hh)

    prim_eval = next((r for r in eval_out.get("results", [])
                      if r.get("cost_id") == COST_PRIMARY[0]
                      and _is_primary_eval(r)), None)
    if prim_eval is None:
        result["checks_failed"].append("primary_eval_absent")
        result["repeat_accounting"] = {"available": False}
        run.log("stage G: primary eval record absent")
    else:
        facts = prim_eval["facts"]
        tag = prim_eval["config"]
        exec_path = run.out / "ledgers" / f"{tag}__{COST_PRIMARY[0]}__executed.csv"
        open_path = run.out / "ledgers" / f"{tag}__{COST_PRIMARY[0]}__open.csv"
        sel_ev = _selected_frame(ev, primary, _ledger_ids(exec_path),
                                 _ledger_ids(open_path))
        acc = _accounting_checks(len(sel_ev), facts)
        acc["positions"] = {str(k): int(v) for k, v in
                            sel_ev["position"].value_counts().items()}
        acc["executed"] = _event_stats(sel_ev[sel_ev["position"] == "executed"],
                                       f"st{h}", f"n52{h}")
        acc["blocked"] = _event_stats(sel_ev[sel_ev["position"] == "blocked"],
                                      f"st{h}", f"n52{h}")
        acc["blocked_mean_gt_executed"] = bool(
            acc["blocked"]["mean_net_known"] is not None
            and acc["executed"]["mean_net_known"] is not None
            and acc["blocked"]["mean_net_known"]
            > acc["executed"]["mean_net_known"])
        result["repeat_accounting"] = acc
        if not acc["selected_matches_n_signal"]:
            result["checks_failed"].append("selected_matches_n_signal")
        if not acc["accounting_sums_to_signal"]:
            result["checks_failed"].append("accounting_sums_to_signal")
        sel_ev = _attach_ret30(sel_ev, csv_dir)
        result["dip_chase"]["eval"] = {
            "all": _dip_chase(sel_ev, f"st{h}", f"n52{h}"),
            "executed": _dip_chase(sel_ev[sel_ev["position"] == "executed"],
                                   f"st{h}", f"n52{h}"),
            "blocked": _dip_chase(sel_ev[sel_ev["position"] == "blocked"],
                                  f"st{h}", f"n52{h}"),
            "ret30_coverage": float(np.isfinite(sel_ev["ret30"]).mean()),
        }
        if exec_path.exists():
            led = pd.read_csv(exec_path)
            result["h2_executed_ledger"] = _executed_ledger_dip_chase(led, sel_ev)
        if result["dip_chase"]["eval"]["ret30_coverage"] < 0.9:
            result["checks_failed"].append("ret30_coverage_eval")

    col_v = val[f"p__{fit}"].to_numpy(dtype=np.float64)
    sel_val = _attach_ret30(val.loc[col_v > thr], csv_dir)
    result["dip_chase"]["val"] = {
        "all": _dip_chase(sel_val, f"st{h}", f"n52{h}"),
        "ret30_coverage": float(np.isfinite(sel_val["ret30"]).mean()),
    }
    if result["dip_chase"]["val"]["ret30_coverage"] < 0.9:
        result["checks_failed"].append("ret30_coverage_val")
    for split in ("val", "eval"):
        ic = result["cs_ic"].get(split, {}).get(f"h{h}") or {}
        if not ic.get("n_slots"):
            result["checks_failed"].append(f"cs_ic_empty_{split}")
    result["prereg"] = _prereg_verdict(result)
    dump_json(out_path, result)
    _write_diagnostics_md(run, result)
    run.log(f"stage G: checks_failed={result['checks_failed']} "
            f"prereg={result['prereg']}")
    return result


# ------------------------------------------------------------------ main


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Builder-2 PARALLEL-DAY-1")
    ap.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--out", default=None)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--stage", default="all",
                    choices=["all", "a", "b", "c", "d", "e", "f", "g"])
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args(argv)

    repo_root = Path(args.repo).resolve()
    if args.out:
        out = (repo_root / args.out).resolve() if not str(args.out).startswith("/") \
            else Path(args.out)
    else:
        rid = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"_{BRANCH}"
        out = repo_root / REL_OUT_ROOT / rid
    run = Run(out)
    run.log(f"start stage={args.stage} out={out} resume={args.resume}")

    input_manifest_path = out / "input_manifest.json"
    if input_manifest_path.exists() and args.resume:
        input_manifest = json.loads(input_manifest_path.read_text(encoding="utf-8"))
    else:
        input_manifest = build_input_manifest(repo_root, run)
        dump_json(input_manifest_path, input_manifest)

    cfg_path = out / "config_frozen.json"
    branch_cfg = repo_root / REL_BRANCH_DIR / "config.json"
    if cfg_path.exists() and args.resume:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    else:
        cfg = build_config(input_manifest)
        cfg["input_manifest_sha256"] = sha256_file(input_manifest_path)
        cfg["config_written_before_train_and_eval"] = True
        dump_json(cfg_path, cfg)
        if not branch_cfg.exists():
            dump_json(branch_cfg, cfg)
        run.log(f"config frozen: {sha256_file(cfg_path)[:12]}")

    stage_a_out = {}
    stage_b_out = {}
    sel = {}
    eval_out = {}
    stage_e_out = {}

    if args.stage in ("all", "a"):
        stage_a_out = stage_a_targets(repo_root, cfg, run, args.resume)
    if args.stage in ("all", "b"):
        stage_b_out = stage_b_fit(repo_root, cfg, input_manifest, run, args.resume)
    if args.stage in ("all", "c"):
        fit_meta = stage_b_out or json.loads(
            (out / "stage_b_summary.json").read_text(encoding="utf-8"))
        sel = stage_c_selection(run, fit_meta, args.resume)
    if args.stage in ("all", "d"):
        if not sel:
            sel = _load_selection(out)
        eval_out = stage_d_eval(repo_root, run, sel, stage_b_out,
                                sel["freeze_sha256"], args.resume)
        freeze_sha_now = sha256_file(out / "freeze.json")
        if freeze_sha_now != sel["freeze_sha256"]:
            raise SystemExit("freeze.json изменён после заморозки — нарушение протокола")
    if args.stage in ("all", "e"):
        if not sel:
            sel = _load_selection(out)
        if not eval_out:
            eval_out = _load_eval(out)
        stage_e_out = stage_e(run, sel, eval_out, None)
        dump_json(out / "stage_e_summary.json", {
            k: v for k, v in stage_e_out.items() if k != "residuals"})
    if args.stage in ("all", "f"):
        if not sel:
            sel = _load_selection(out)
        if not eval_out:
            eval_out = _load_eval(out)
        if not stage_e_out:
            stage_e_out = json.loads((out / "stage_e_summary.json").read_text(
                encoding="utf-8"))
            stage_e_out["residuals"] = stage_e(run, sel, eval_out, None)["residuals"]
        if not stage_b_out:
            stage_b_out = json.loads((out / "stage_b_summary.json").read_text(
                encoding="utf-8"))
        if not stage_a_out:
            stage_a_out = json.loads((out / "stage_a_summary.json").read_text(
                encoding="utf-8"))
        stage_f(run, repo_root, input_manifest, cfg, sel, eval_out, stage_e_out,
                stage_b_out, stage_a_out, sel["freeze_sha256"])
    if args.stage in ("all", "g"):
        if not sel:
            sel = _load_selection(out)
        if not eval_out:
            eval_out = _load_eval(out)
        stage_g_diagnostics(repo_root, run, sel, eval_out, args.resume)
    run.log("DONE")
    return 0


def _load_selection(out: Path) -> dict:
    freeze = json.loads((out / "freeze.json").read_text(encoding="utf-8"))
    attempts = freeze["attempt_table"]
    primary = freeze.get("primary")
    top3 = freeze.get("top3_exploratory") or []
    return {"freeze": freeze, "freeze_sha256": sha256_file(out / "freeze.json"),
            "attempts": attempts, "primary": primary, "top3": top3,
            "best_ridge": freeze.get("best_ridge"),
            "best_cont5": freeze.get("best_cont5_comparator"),
            "val_rows": None}


def _load_eval(out: Path) -> dict:
    results = []
    for p in sorted((out / "eval").glob("*.json")):
        results.append(json.loads(p.read_text(encoding="utf-8")))
    meta = json.loads((out / "stage_e_meta.json").read_text(encoding="utf-8")) \
        if (out / "stage_e_meta.json").exists() else {}
    return {"results": results,
            "n_eligible_eval": meta.get("n_eligible_eval"),
            "baseline_horizon": meta.get("baseline_horizon")}


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
