"""PARALLEL-DAY-1 / Worker-1 (Builder-1) — объяснимые сочетания 2–3 ТФ-факторов.

Ветка `day_b1_20261014`. Вопрос: существуют ли повторяющиеся комбинации признаков
на 2–3 таймфреймах, чья связь с разметкой переносится в более поздние сделки.

Входы (frozen, только чтение): dataset `ml_mtf_1_v3`; snapshot минут
`csv/<ticker>__m1.csv`; reference `backend/research/daypack_20261014`.

Выход: atoms.csv, rules_all.csv, rules_frozen.json, outcomes/, summary.csv,
ticker_month.csv, sensitivity.csv, bootstrap.csv, paired_coverage.csv,
RESULT.json, RESULT.md, identity.json, stages.json.

Проверки: нет lookahead (availability <= decision_ts), NULL не срабатывает,
join строго по row_id (нет сдвига метки), лимиты support/dates не понижаются,
immutable output, тождество net == gross - costs_total.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from research.daypack_20261014 import frozen_portfolio as fp
from research.daypack_20261014.costs import CostParams
from research.daypack_20261014.evaluate import summarize

BRANCH = "day_b1_20261014"
DATASET_VERSION = "ml_mtf_1_v3"

TRAIN_START = "2025-10-01"
TRAIN_END = "2026-05-31"
VAL_START = "2026-06-01"
VAL_END = "2026-07-31"
EVAL_START = "2026-08-01"
EVAL_END = "2026-10-08"
EMBARGO_MIN = 120
HORIZON_MIN = 120

COST_PRIMARY_ID = "c5_s2"
COST_PRIMARY = CostParams(5.0, 2.0)
COST_EXPENSIVE = CostParams(10.0, 5.0)
COST_SCENARIOS = ((COST_PRIMARY_ID, COST_PRIMARY), ("c10_s5", COST_EXPENSIVE))
SPLITS = ("val", "eval")

QUANTILES = (0.2, 0.5, 0.8)
MAX_ATOMS = 20
ATOM_MIN_SUPPORT = 1000
ATOM_MIN_DATES = 30
MAX_ATOMS_PER_SOURCE = 2
MAX_RULES_SINGLE = 20
MAX_RULES_PAIR = 190
MAX_RULES_TRIPLE = 1140

SEL_MIN_KNOWN_EVENTS = 200
SEL_MIN_KNOWN_DATES = 20
SEL_MAX_UNKNOWN_FRACTION = 0.05

BOOTSTRAP_DRAWS = 500
BOOTSTRAP_BLOCK_SESSIONS = 5
BOOTSTRAP_SEED = 42

TF_SUFFIXES = ("_1m", "_5m", "_600s", "_1800s", "_3600s")

STATUS_MISSING = 0
STATUS_FILLED = 1
STATUS_INCOMPLETE = 2
STATUS_SKIP = 3
STATUS_CODE = {"FILLED": STATUS_FILLED, "INCOMPLETE": STATUS_INCOMPLETE, "SKIP": STATUS_SKIP}


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def tf_of(column: str):
    for suffix in TF_SUFFIXES:
        if column.endswith(suffix):
            return suffix
    return None


def load_schema(dataset: Path) -> dict:
    return json.loads((dataset / "feature_schema.json").read_text())


def load_dataset_manifest(dataset: Path) -> dict:
    return json.loads((dataset / "manifest.json").read_text())


def predictor_columns(schema: dict) -> list:
    columns = list(schema["predictor_columns"])
    roles = schema.get("column_roles") or {}
    if roles:
        columns = [c for c in columns if roles.get(c, "predictor") == "predictor"]
    return columns


def atom_columns(schema: dict) -> tuple:
    preds = predictor_columns(schema)
    continuous = [c for c in preds if tf_of(c) is not None]
    votes = [c for c in preds if c.startswith("vote_")]
    return continuous, votes


@dataclass(frozen=True)
class Config:
    dataset: Path
    snapshot: Path
    out: Path
    tickers: tuple = ()
    max_atoms: int = MAX_ATOMS
    atom_min_support: int = ATOM_MIN_SUPPORT
    atom_min_dates: int = ATOM_MIN_DATES
    max_atoms_per_source: int = MAX_ATOMS_PER_SOURCE
    horizon_minutes: int = HORIZON_MIN
    scope: str = "full"

    def to_json(self) -> dict:
        return {
            "branch": BRANCH, "dataset_version": DATASET_VERSION, "scope": self.scope,
            "tickers": list(self.tickers),
            "splits": {"train": [TRAIN_START, TRAIN_END], "val": [VAL_START, VAL_END],
                       "eval": [EVAL_START, EVAL_END]},
            "embargo_min": EMBARGO_MIN, "horizon_minutes": self.horizon_minutes,
            "quantiles": list(QUANTILES), "max_atoms": self.max_atoms,
            "atom_min_support": self.atom_min_support, "atom_min_dates": self.atom_min_dates,
            "max_atoms_per_source": self.max_atoms_per_source,
            "rule_caps": [MAX_RULES_SINGLE, MAX_RULES_PAIR, MAX_RULES_TRIPLE],
            "selection": {"min_known_events": SEL_MIN_KNOWN_EVENTS,
                          "min_known_dates": SEL_MIN_KNOWN_DATES,
                          "max_unknown_fraction": SEL_MAX_UNKNOWN_FRACTION},
            "bootstrap": {"draws": BOOTSTRAP_DRAWS, "block_sessions": BOOTSTRAP_BLOCK_SESSIONS,
                          "seed": BOOTSTRAP_SEED},
        }


def build_identity(cfg: Config, manifest: dict, schema: dict) -> dict:
    tickers = list(cfg.tickers) if cfg.tickers else sorted(manifest.get("per_ticker", {}).keys())
    continuous, votes = atom_columns(schema)
    return {
        "branch": BRANCH,
        "config": cfg.to_json(),
        "code_sha256": sha256_file(Path(__file__)),
        "dataset": str(cfg.dataset),
        "dataset_params_hash": manifest.get("params_hash"),
        "dataset_code_sha256": manifest.get("code_sha256"),
        "dataset_version": manifest.get("dataset_version"),
        "eligibility_policy_hash": manifest.get("eligibility_policy_hash"),
        "label_cfg_hash": manifest.get("label_cfg_hash"),
        "snapshot": str(cfg.snapshot),
        "tickers": tickers,
        "n_continuous_features": len(continuous),
        "n_vote_features": len(votes),
    }


def identity_hash(identity: dict) -> str:
    return sha256_bytes(canonical_json(identity).encode("utf-8"))


class StageStore:
    def __init__(self, out: Path, config_hash: str, resume: bool):
        self.out = out
        self.config_hash = config_hash
        self.resume = resume
        self.marker = out / "stages.json"
        self.stages = json.loads(self.marker.read_text()) if (resume and self.marker.exists()) else {}

    def done(self, name: str) -> bool:
        return self.resume and self.stages.get(name, {}).get("config_hash") == self.config_hash

    def record(self, name: str, **extra) -> None:
        self.stages[name] = {"config_hash": self.config_hash,
                             "at": datetime.now(timezone.utc).isoformat(), **extra}
        self.marker.write_text(json.dumps(self.stages, indent=1, default=str))


def read_ticker_table(dataset: Path, ticker: str, columns: list) -> pd.DataFrame:
    features = pd.read_parquet(dataset / "features" / f"features_{ticker}.parquet",
                              columns=columns)
    targets = pd.read_parquet(dataset / "targets" / f"targets_{ticker}.parquet",
                             columns=["row_id", "figi", "ticker", "decision_ts", "label",
                                      "label_status", "label_end_ts"])
    merged = features.merge(targets, on="row_id", how="inner", suffixes=("", "_t"))
    for column in ("figi_t", "ticker_t", "decision_ts_t"):
        if column in merged.columns:
            merged = merged.drop(columns=[column])
    merged["decision_ts"] = pd.to_datetime(merged["decision_ts"], utc=True)
    merged["label_end_ts"] = pd.to_datetime(merged["label_end_ts"], utc=True)
    merged["label_status"] = merged["label_status"].astype("string")
    return merged


def build_decision_table(cfg: Config, schema: dict, tickers: list) -> pd.DataFrame:
    preds = predictor_columns(schema)
    columns = ["row_id", "figi", "ticker", "decision_ts"] + preds
    frames = [read_ticker_table(cfg.dataset, ticker, columns) for ticker in tickers]
    frames = [frame for frame in frames if not frame.empty]
    table = pd.concat(frames, ignore_index=True)
    for column in preds:
        table[column] = pd.to_numeric(table[column], errors="coerce").astype("float32")
    table["label"] = pd.to_numeric(table["label"], errors="coerce")
    table["known"] = (table["label_status"] == "MATURE") & table["label"].notna()
    return table.sort_values(["decision_ts", "figi"], kind="stable").reset_index(drop=True)


def msk_dates(series: pd.Series) -> np.ndarray:
    return np.array([value.date() for value in series.dt.tz_convert("Europe/Moscow")])


def split_masks(table: pd.DataFrame, dates: np.ndarray) -> dict:
    boundary_val = pd.Timestamp(VAL_START, tz="UTC")
    boundary_eval = pd.Timestamp(EVAL_START, tz="UTC")
    embargo = pd.Timedelta(minutes=EMBARGO_MIN)
    decision_ts = table["decision_ts"]
    label_end = table["label_end_ts"]
    train = ((dates >= pd.Timestamp(TRAIN_START).date()) & (dates <= pd.Timestamp(TRAIN_END).date())
             & (label_end < boundary_val).to_numpy()
             & (decision_ts < boundary_val - embargo).to_numpy())
    val = ((dates >= pd.Timestamp(VAL_START).date()) & (dates <= pd.Timestamp(VAL_END).date())
           & (label_end < boundary_eval).to_numpy()
           & (decision_ts < boundary_eval - embargo).to_numpy())
    ev = ((dates >= pd.Timestamp(EVAL_START).date()) & (dates <= pd.Timestamp(EVAL_END).date()))
    return {"train": np.asarray(train), "val": np.asarray(val), "eval": np.asarray(ev)}


@dataclass(frozen=True)
class Atom:
    atom_id: str
    column: str
    kind: str
    threshold: float
    tf: object

    def condition_text(self) -> str:
        if self.kind == "le":
            return f"{self.column} <= {self.threshold:.6g}"
        if self.kind == "gt":
            return f"{self.column} > {self.threshold:.6g}"
        return f"{self.column} == {self.threshold:+.0f}"


def build_atoms(table: pd.DataFrame, train_mask: np.ndarray, continuous: list, votes: list) -> list:
    atoms = []
    for column in continuous:
        values = table[column].to_numpy(dtype="float64")[train_mask]
        values = values[np.isfinite(values)]
        if values.size == 0:
            continue
        for q in QUANTILES:
            threshold = float(np.quantile(values, q))
            for kind in ("le", "gt"):
                atoms.append(Atom(f"{column}|{kind}|q{int(round(q * 100))}", column, kind,
                                  threshold, tf_of(column)))
    for column in votes:
        for kind, level in (("eq_p1", 1.0), ("eq_m1", -1.0)):
            atoms.append(Atom(f"{column}|{kind}", column, kind, level, None))
    return atoms


def atom_flags(table: pd.DataFrame, atoms: list) -> tuple:
    size = len(table)
    true = np.zeros((len(atoms), size), dtype=bool)
    unknown = np.zeros((len(atoms), size), dtype=bool)
    for index, atom in enumerate(atoms):
        values = table[atom.column].to_numpy(dtype="float64")
        missing = ~np.isfinite(values)
        unknown[index] = missing
        if atom.kind == "le":
            condition = values <= atom.threshold
        elif atom.kind == "gt":
            condition = values > atom.threshold
        else:
            condition = values == atom.threshold
        true[index] = condition & ~missing
    return true, unknown


def select_atoms(atoms: list, true: np.ndarray, masks: dict, known: np.ndarray, up: np.ndarray,
                 dates: np.ndarray, cfg: Config) -> tuple:
    train_mask = masks["train"]
    train_known = train_mask & known
    base_up = float(up[train_known].mean()) if train_known.any() else float("nan")
    rows = []
    for index, atom in enumerate(atoms):
        selected = true[index] & train_mask
        selected_known = selected & known
        support = int(selected_known.sum())
        atom_up = float(up[selected_known].mean()) if support else float("nan")
        rows.append({
            "atom_id": atom.atom_id, "column": atom.column, "kind": atom.kind,
            "threshold": atom.threshold, "tf": atom.tf, "condition": atom.condition_text(),
            "train_support": support,
            "train_dates": int(np.unique(dates[selected_known]).size) if support else 0,
            "train_p_up": atom_up, "train_base_up": base_up,
            "train_uplift": (atom_up - base_up) if support else float("nan"),
            "train_coverage": float(selected.mean()) if train_mask.any() else float("nan"),
        })
    stats = pd.DataFrame(rows)
    eligible = stats[(stats["train_support"] >= cfg.atom_min_support)
                     & (stats["train_dates"] >= cfg.atom_min_dates)]
    eligible = eligible.sort_values(["train_uplift", "atom_id"], ascending=[False, True],
                                    kind="stable")
    chosen = []
    per_source = {}
    for _, row in eligible.iterrows():
        source = row["column"]
        if per_source.get(source, 0) >= cfg.max_atoms_per_source:
            continue
        chosen.append(row["atom_id"])
        per_source[source] = per_source.get(source, 0) + 1
        if len(chosen) >= cfg.max_atoms:
            break
    return stats, chosen, base_up


def rule_valid(atom_ids: tuple, atoms_by_id: dict) -> bool:
    if len(atom_ids) < 2:
        return True
    columns = [atoms_by_id[a].column for a in atom_ids]
    if len(set(columns)) != len(columns):
        return False
    tfs = {atoms_by_id[a].tf for a in atom_ids if atoms_by_id[a].tf is not None}
    return len(tfs) >= 2


def generate_rules(chosen: list, atoms_by_id: dict) -> list:
    ordered = sorted(chosen)
    rules = [(a,) for a in ordered]
    rules.extend(sorted(combinations(ordered, 2))[:MAX_RULES_PAIR])
    if len(ordered) >= 3:
        rules.extend(sorted(combinations(ordered, 3))[:MAX_RULES_TRIPLE])
    in_cap = rules[:MAX_RULES_SINGLE + MAX_RULES_PAIR + MAX_RULES_TRIPLE]
    return [rule for rule in in_cap if rule_valid(rule, atoms_by_id)]


def rule_id_of(atom_ids: tuple) -> str:
    return "+".join(sorted(atom_ids))


def rule_flags(atom_ids: tuple, atoms_by_id: dict, index_of: dict, true: np.ndarray,
               unknown: np.ndarray) -> tuple:
    indices = [index_of[a] for a in atom_ids]
    combined_true = np.ones(true.shape[1], dtype=bool)
    combined_unknown = np.zeros(true.shape[1], dtype=bool)
    for index in indices:
        combined_true &= true[index]
        combined_unknown |= unknown[index]
    return combined_true & ~combined_unknown, combined_unknown


def rule_stats(atom_ids: tuple, atoms_by_id: dict, index_of: dict, true: np.ndarray,
               unknown: np.ndarray, masks: dict, known: np.ndarray, up: np.ndarray,
               dates: np.ndarray, events: dict, base_up: float) -> dict:
    selected_true, selected_unknown = rule_flags(atom_ids, atoms_by_id, index_of, true, unknown)
    atom_ids_sorted = sorted(atom_ids)
    record = {
        "rule_id": rule_id_of(atom_ids), "n_atoms": len(atom_ids),
        "atoms": ";".join(atom_ids_sorted),
        "condition": " AND ".join(atoms_by_id[a].condition_text() for a in atom_ids_sorted),
        "tfs": "+".join(sorted({atoms_by_id[a].tf for a in atom_ids_sorted
                                if atoms_by_id[a].tf})),
    }
    for split_name in ("train", "val", "eval"):
        mask = masks[split_name]
        selected = selected_true & mask
        active = ~selected_unknown & mask
        selected_known = selected & known
        support = int(selected_known.sum())
        p_up = float(up[selected_known].mean()) if support else float("nan")
        record[f"{split_name}_coverage_true"] = float(selected.mean()) if mask.any() else float("nan")
        record[f"{split_name}_coverage_defined"] = float(active.mean()) if mask.any() else float("nan")
        record[f"{split_name}_support"] = support
        record[f"{split_name}_p_up"] = p_up
        record[f"{split_name}_dates"] = int(np.unique(dates[selected_known]).size) if support else 0
    record["train_base_up"] = base_up
    record["train_lift"] = record["train_p_up"] - base_up if record["train_support"] else float("nan")
    val_known = masks["val"] & known
    val_base = float(up[val_known].mean()) if val_known.any() else float("nan")
    record["val_base_up"] = val_base
    record["val_lift"] = record["val_p_up"] - val_base if record["val_support"] else float("nan")
    record["val_event"] = event_metrics(selected_true, masks["val"], events[("val", COST_PRIMARY_ID)], dates)
    record["eval_event"] = event_metrics(selected_true, masks["eval"], events[("eval", COST_PRIMARY_ID)], dates)
    return record


def event_metrics(selected_true: np.ndarray, mask: np.ndarray, event: dict, dates: np.ndarray) -> dict:
    selected = selected_true & mask
    n_selected = int(selected.sum())
    if n_selected == 0:
        return {"n_selected": 0, "n_known": 0, "n_unknown": 0, "n_missing_entry": 0,
                "unknown_fraction": float("nan"), "mean_net": float("nan"),
                "known_dates": 0, "sum_net": float("nan")}
    status = event["status"]
    acct = event["accounted"]
    known_sel = selected & acct & (status == STATUS_FILLED)
    unknown_sel = selected & acct & (status == STATUS_INCOMPLETE)
    missing_sel = selected & acct & (status == STATUS_SKIP)
    denominator = int((known_sel.sum() + unknown_sel.sum() + missing_sel.sum()))
    net = event["net"]
    return {
        "n_selected": n_selected,
        "n_known": int(known_sel.sum()),
        "n_unknown": int(unknown_sel.sum()),
        "n_missing_entry": int(missing_sel.sum()),
        "unknown_fraction": (float(unknown_sel.sum()) / denominator) if denominator else float("nan"),
        "mean_net": float(np.nanmean(net[known_sel])) if known_sel.any() else float("nan"),
        "known_dates": int(np.unique(dates[known_sel]).size) if known_sel.any() else 0,
        "sum_net": float(np.nansum(net[known_sel])) if known_sel.any() else float("nan"),
    }


def feasible(metrics: dict) -> bool:
    if not np.isfinite(metrics.get("mean_net", float("nan"))):
        return False
    return (metrics["n_known"] >= SEL_MIN_KNOWN_EVENTS
            and metrics["known_dates"] >= SEL_MIN_KNOWN_DATES
            and metrics["unknown_fraction"] <= SEL_MAX_UNKNOWN_FRACTION)


def choose_primary(records: list, n_atoms) -> list:
    if n_atoms is None:
        candidates = [r for r in records if feasible(r["val_event"])]
    else:
        candidates = [r for r in records if r["n_atoms"] == n_atoms and feasible(r["val_event"])]
    candidates.sort(key=lambda r: (-r["val_event"]["mean_net"], r["rule_id"]))
    return candidates


def build_split_events(cfg: Config, table: pd.DataFrame, masks: dict, split_name: str,
                       tickers: list, costs: CostParams) -> tuple:
    mask = masks[split_name]
    subset = table[mask].reset_index(drop=True)
    previous = fp.EXIT_HORIZON_MIN
    records = []
    try:
        fp.EXIT_HORIZON_MIN = cfg.horizon_minutes
        for ticker in tickers:
            positions = np.flatnonzero(subset["ticker"].to_numpy() == ticker)
            if positions.size == 0:
                continue
            csv_path = cfg.snapshot / "csv" / f"{ticker}__m1.csv"
            if not csv_path.exists():
                continue
            figi = str(subset["figi"].to_numpy()[positions[0]])
            store = fp.MinuteStore()
            store.add_csv(csv_path, figi)
            for position in positions:
                row = subset.iloc[int(position)]
                decision_ts = row["decision_ts"].to_pydatetime()
                prediction = {
                    "row_id": row["row_id"], "figi": figi, "ticker": ticker,
                    "model_id": f"b1_{split_name}", "split": split_name,
                    "decision_ts": decision_ts, "available_at_ts": decision_ts,
                    "selected_long": True,
                }
                outcome = fp.execute_signal(prediction, store, costs)
                records.append({
                    "row_id": outcome.row_id, "figi": outcome.figi, "ticker": outcome.ticker,
                    "model_id": outcome.model_id, "split": split_name,
                    "decision_ts": outcome.decision_ts, "status": outcome.status,
                    "reason": outcome.reason, "entry_ts": outcome.entry_ts,
                    "exit_ts": outcome.exit_ts, "entry_base": outcome.entry_base,
                    "exit_base": outcome.exit_base,
                    "gross_per_unit": outcome.gross_per_unit,
                    "net_per_unit": outcome.net_per_unit,
                    "costs_per_unit": outcome.costs_per_unit,
                    "path_metrics_known": bool(outcome.path_metrics_known),
                })
    finally:
        fp.EXIT_HORIZON_MIN = previous
    return pd.DataFrame(records), subset


def align_events(table: pd.DataFrame, masks: dict, split_name: str, frame: pd.DataFrame,
                 subset: pd.DataFrame) -> dict:
    size = len(table)
    status = np.full(size, STATUS_MISSING, dtype=np.int8)
    net = np.full(size, np.nan, dtype=np.float64)
    accounted = np.zeros(size, dtype=bool)
    if len(frame):
        position_of = {row_id: index for index, row_id in
                       enumerate(table["row_id"].to_numpy())}
        for record in frame.itertuples(index=False):
            position = position_of.get(record.row_id)
            if position is None:
                continue
            status[position] = STATUS_CODE.get(record.status, STATUS_MISSING)
            accounted[position] = True
            if record.net_per_unit is not None and not pd.isna(record.net_per_unit):
                net[position] = float(record.net_per_unit)
    return {"status": status, "net": net, "accounted": accounted, "frame": frame,
            "subset": subset}


def outcome_objects(frame: pd.DataFrame):
    if frame is None or frame.empty:
        return []
    objects = []
    for record in frame.to_dict("records"):
        entry_ts = record["entry_ts"]
        exit_ts = record["exit_ts"]
        if entry_ts is None or pd.isna(entry_ts):
            entry_ts = None
        elif not isinstance(entry_ts, datetime):
            entry_ts = pd.Timestamp(entry_ts).to_pydatetime()
        if exit_ts is None or pd.isna(exit_ts):
            exit_ts = None
        elif not isinstance(exit_ts, datetime):
            exit_ts = pd.Timestamp(exit_ts).to_pydatetime()
        objects.append(fp.SignalOutcome(
            row_id=record["row_id"], figi=record["figi"], ticker=record["ticker"],
            decision_ts=pd.Timestamp(record["decision_ts"]).to_pydatetime(),
            model_id=record["model_id"], split=record["split"], selected=True,
            status=record["status"], reason=record["reason"], entry_ts=entry_ts,
            exit_ts=exit_ts, entry_base=record["entry_base"], exit_base=record["exit_base"],
            gross_per_unit=record["gross_per_unit"], net_per_unit=record["net_per_unit"],
            costs_per_unit=record["costs_per_unit"],
            path_metrics_known=bool(record["path_metrics_known"]),
        ))
    return objects


def arm_selection(arm, table: pd.DataFrame, masks: dict, atoms_by_id, index_of, true, unknown,
                  split_name: str) -> np.ndarray:
    mask = masks[split_name]
    if arm == "ALL":
        return mask.copy()
    if arm is None:
        return np.zeros(len(table), dtype=bool)
    selected_true, _ = rule_flags(tuple(sorted(arm)), atoms_by_id, index_of, true, unknown)
    return selected_true & mask


def run_arms(cfg: Config, table: pd.DataFrame, masks: dict, arms: dict, atoms_by_id, index_of,
             true, unknown, events: dict) -> tuple:
    rows = []
    portfolios = {}
    for split_name in SPLITS:
        for cost_id, costs in COST_SCENARIOS:
            frame = events[(split_name, cost_id)]["frame"]
            objects = outcome_objects(frame)
            row_ids = frame["row_id"].to_numpy() if len(frame) else np.array([], dtype=object)
            id_index = {row_id: position for position, row_id in enumerate(row_ids)}
            for arm_name, arm in arms.items():
                selection = arm_selection(arm, table, masks, atoms_by_id, index_of, true, unknown,
                                          split_name)
                chosen_ids = set(table.loc[selection, "row_id"].tolist())
                chosen = [objects[id_index[row_id]] for row_id in chosen_ids
                          if row_id in id_index]
                result = summarize(chosen, costs, horizon_minutes=cfg.horizon_minutes)
                rows.append(arm_row(arm_name, split_name, cost_id, arm, result["metrics"]))
                portfolios[(arm_name, split_name, cost_id)] = result["portfolio"]
    return rows, portfolios


def arm_row(arm_name: str, split_name: str, cost_id: str, arm, metrics: dict) -> dict:
    diagnostic = metrics.get("closed_cashflow_diagnostic", {})
    return {
        "arm": arm_name, "split": split_name, "cost": cost_id,
        "rule": ";".join(arm) if isinstance(arm, list) else str(arm),
        "n_signal_outcomes": metrics.get("n_signal_outcomes"),
        "n_entry": (metrics.get("n_executed", 0) or 0) + (metrics.get("n_accepted_open", 0) or 0),
        "closed": metrics.get("n_executed"), "open": metrics.get("n_accepted_open"),
        "incomplete": metrics.get("n_incomplete_outcomes"), "skip": metrics.get("n_skip"),
        "no_slot": metrics.get("n_no_slot"), "no_cash": metrics.get("n_no_cash"),
        "ignore_repeat": metrics.get("n_ignore_repeat"),
        "gross": metrics.get("gross_total"), "fees": metrics.get("fees_total"),
        "slip": metrics.get("slippage_total"), "costs": metrics.get("costs_total"),
        "net": metrics.get("net_total"), "net_mean_per_trade": metrics.get("net_mean_per_trade"),
        "win_rate": metrics.get("win_rate"),
        "known_prefix_final": metrics.get("last_known_equity"),
        "known_prefix_last_ts": metrics.get("last_known_ts"),
        "mdd_known_prefix": metrics.get("max_drawdown_known"),
        "final_equity": metrics.get("final_equity"),
        "unknown_tail": metrics.get("unknown_tail"),
        "identity_ok": metrics.get("costs_identity_net_eq_gross_minus_costs"),
        "closed_cashflow_diagnostic": diagnostic.get("closed_net"),
        "closed_cashflow_initial_plus": diagnostic.get("initial_plus_closed_net"),
        "closed_cashflow_is_full_equity": diagnostic.get("is_full_equity"),
    }


BREAKDOWN_COLUMNS = ("arm", "kind", "cost", "ticker", "month", "closed", "net", "gross", "fees", "slip")
EXECUTED_COLUMNS = ("row_id", "figi", "ticker", "model_id", "entry_ts", "exit_ts", "qty",
                    "entry_fill", "exit_fill", "slip_entry", "slip_exit", "commission_entry",
                    "commission_exit", "gross", "net", "costs_total", "entry_cost",
                    "exit_proceeds", "day", "month")
ACCEPTED_OPEN_COLUMNS = ("row_id", "figi", "ticker", "model_id", "entry_ts", "entry_base",
                         "qty", "entry_debit", "planned_exit_ts")


def executed_frame(portfolio) -> pd.DataFrame:
    rows = []
    for trade in getattr(portfolio, "executed", []):
        rows.append({
            "row_id": trade.row_id, "figi": trade.figi, "ticker": trade.ticker,
            "model_id": trade.model_id, "entry_ts": trade.entry_ts, "exit_ts": trade.exit_ts,
            "qty": trade.qty, "entry_fill": trade.entry_fill, "exit_fill": trade.exit_fill,
            "slip_entry": trade.slip_entry, "slip_exit": trade.slip_exit,
            "commission_entry": trade.commission_entry, "commission_exit": trade.commission_exit,
            "gross": trade.gross, "net": trade.net, "costs_total": trade.costs_total,
            "entry_cost": trade.entry_cost, "exit_proceeds": trade.exit_proceeds,
        })
    frame = pd.DataFrame(rows, columns=list(EXECUTED_COLUMNS))
    if not frame.empty:
        frame["day"] = [ts.astimezone(fp.MSK).date() for ts in frame["exit_ts"]]
        frame["month"] = [ts.astimezone(fp.MSK).strftime("%Y-%m") for ts in frame["exit_ts"]]
    return frame


def breakdown_rows(frame: pd.DataFrame, label: str, cost_id: str) -> list:
    if frame.empty:
        return []
    rows = []
    for kind, key in (("ticker", "ticker"), ("month", "month")):
        for value, group in frame.groupby(key):
            rows.append({"arm": label, "kind": kind, "cost": cost_id,
                         "ticker": value if kind == "ticker" else None,
                         "month": value if kind == "month" else None,
                         "closed": int(len(group)), "net": float(group["net"].sum()),
                         "gross": float(group["gross"].sum()),
                         "fees": float((group["commission_entry"] + group["commission_exit"]).sum()),
                         "slip": float((group["slip_entry"] + group["slip_exit"]).sum())})
    return rows


def robustness(portfolios: dict) -> dict:
    def frame_of(arm_name: str, cost_id: str) -> pd.DataFrame:
        portfolio = portfolios.get((arm_name, "eval", cost_id))
        return executed_frame(portfolio) if portfolio is not None else pd.DataFrame()

    primary_frame = frame_of("primary", COST_PRIMARY_ID)
    baseline_frame = frame_of("always_long", COST_PRIMARY_ID)
    secondary_id = COST_SCENARIOS[1][0]
    ticker_rows = (breakdown_rows(primary_frame, "primary", COST_PRIMARY_ID)
                   + breakdown_rows(frame_of("primary", secondary_id), "primary", secondary_id)
                   + breakdown_rows(baseline_frame, "always_long", COST_PRIMARY_ID))
    ticker_month = pd.DataFrame(ticker_rows, columns=list(BREAKDOWN_COLUMNS))

    sensitivity = pd.DataFrame(sensitivity_rows(primary_frame))
    paired = pd.DataFrame(paired_rows(primary_frame, baseline_frame))
    bootstrap = pd.DataFrame(bootstrap_rows(primary_frame, baseline_frame))
    return {"ticker_month": ticker_month, "sensitivity": sensitivity,
            "bootstrap": bootstrap, "paired_coverage": paired}


def sensitivity_rows(frame: pd.DataFrame) -> list:
    if frame.empty:
        return [{"variant": "primary", "closed": 0, "net": 0.0}]
    rows = [{"variant": "primary_full", "closed": int(len(frame)), "net": float(frame["net"].sum())}]
    by_ticker = frame.groupby("ticker")["net"].sum().sort_values(ascending=False)
    if len(by_ticker):
        best_ticker = by_ticker.index[0]
        subset = frame[frame["ticker"] != best_ticker]
        rows.append({"variant": f"without_best_ticker:{best_ticker}", "closed": int(len(subset)),
                     "net": float(subset["net"].sum())})
    by_day = frame.groupby("day")["net"].sum().sort_values(ascending=False)
    if len(by_day):
        best_day = by_day.index[0]
        subset = frame[frame["day"] != best_day]
        rows.append({"variant": f"without_best_day:{best_day}", "closed": int(len(subset)),
                     "net": float(subset["net"].sum())})
    return rows


def paired_rows(primary_frame: pd.DataFrame, baseline_frame: pd.DataFrame) -> list:
    primary_days = primary_frame.groupby("day")["net"].sum() if not primary_frame.empty else pd.Series(dtype="float64")
    baseline_days = baseline_frame.groupby("day")["net"].sum() if not baseline_frame.empty else pd.Series(dtype="float64")
    all_days = sorted(set(primary_days.index) | set(baseline_days.index))
    rows = []
    for day in all_days:
        rows.append({"day": str(day),
                     "primary_known": bool(day in primary_days.index),
                     "always_long_known": bool(day in baseline_days.index),
                     "primary_net": float(primary_days.get(day, np.nan)),
                     "always_long_net": float(baseline_days.get(day, np.nan))})
    return rows


def bootstrap_rows(primary_frame: pd.DataFrame, baseline_frame: pd.DataFrame) -> list:
    if primary_frame.empty or baseline_frame.empty:
        return [{"statistic": "daily_net_difference", "draws": 0, "mean": float("nan"),
                 "low": float("nan"), "high": float("nan")}]
    paired = (primary_frame.groupby("day")["net"].sum()
              - baseline_frame.groupby("day")["net"].sum()).dropna().sort_index()
    if paired.empty:
        return [{"statistic": "daily_net_difference", "draws": 0, "mean": float("nan"),
                 "low": float("nan"), "high": float("nan")}]
    values = paired.to_numpy(dtype="float64")
    n = len(values)
    block = min(BOOTSTRAP_BLOCK_SESSIONS, n)
    rng = random.Random(BOOTSTRAP_SEED)
    means = []
    for _ in range(BOOTSTRAP_DRAWS):
        collected = []
        while len(collected) < n:
            start = rng.randrange(n)
            collected.extend(values[(start + offset) % n] for offset in range(block))
        means.append(float(np.mean(collected[:n])))
    return [{"statistic": "daily_net_difference", "draws": BOOTSTRAP_DRAWS,
             "n_days": n, "mean": float(np.mean(means)),
             "low": float(np.quantile(means, 0.025)), "high": float(np.quantile(means, 0.975)),
             "observed": float(values.mean())}]


def run_self_checks(cfg: Config, identity: dict, config_hash: str, frozen: dict,
                    summary: pd.DataFrame, table: pd.DataFrame, masks: dict, chosen: list,
                    stats: pd.DataFrame) -> list:
    checks = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    boundary_val = pd.Timestamp(VAL_START, tz="UTC")
    boundary_eval = pd.Timestamp(EVAL_START, tz="UTC")
    embargo = pd.Timedelta(minutes=EMBARGO_MIN)
    train = table[masks["train"]]
    val = table[masks["val"]]
    add("embargo_train_val",
        bool(((train["label_end_ts"] < boundary_val)
              & (train["decision_ts"] < boundary_val - embargo)).all()),
        "train label_end и decision_ts до границы val минус embargo")
    add("embargo_val_eval",
        bool(((val["label_end_ts"] < boundary_eval)
              & (val["decision_ts"] < boundary_eval - embargo)).all()),
        "val label_end и decision_ts до границы eval минус embargo")
    add("row_id_unique", bool(table["row_id"].is_unique), "row_id уникален в decision table")
    add("split_disjoint",
        bool(not (masks["train"] & masks["val"]).any() and not (masks["val"] & masks["eval"]).any()),
        "train/val/eval попарно не пересекаются")

    picked = stats[stats["atom_id"].isin(chosen)]
    add("atom_support_limits",
        bool(len(picked) == 0 or ((picked["train_support"] >= ATOM_MIN_SUPPORT).all()
                                  and (picked["train_dates"] >= ATOM_MIN_DATES).all())),
        "у выбранных атомов support>=1000 и dates>=30")
    add("atom_source_cap",
        bool(len(picked) == 0 or picked.groupby("column").size().max() <= MAX_ATOMS_PER_SOURCE),
        "не более 2 атомов на исходный признак")
    add("atom_count_cap", bool(len(chosen) <= MAX_ATOMS), "не более 20 выбранных атомов")

    closed = summary[summary["closed"].fillna(0) > 0]
    add("net_identity", bool(len(closed) == 0 or closed["identity_ok"].all()),
        "net == gross - costs_total в summary по всем arms")
    add("identity_hash_stable", bool(identity_hash(identity) == config_hash),
        "config_hash воспроизводим из identity")

    criterion = frozen["criterion"]
    if frozen["primary"] is not None:
        event = frozen["primary"]["val_event"]
        applied = (event["n_known"] >= criterion["min_known_events"]
                   and event["known_dates"] >= criterion["min_known_dates"]
                   and event["unknown_fraction"] <= criterion["max_unknown_fraction"])
    else:
        applied = bool(frozen["no_selection"])
    add("selection_criterion_applied", bool(applied),
        "primary удовлетворяет п.9 либо NO_SELECTION корректен")

    return checks


def baseline_metrics(summary: pd.DataFrame) -> dict:
    result = {}
    for arm in ("cash", "always_long", "best_single_atom"):
        rows = summary[(summary["arm"] == arm) & (summary["split"] == "eval")
                       & (summary["cost"] == COST_PRIMARY_ID)]
        if rows.empty:
            continue
        row = rows.iloc[0]

        def number(value):
            return None if pd.isna(value) else float(value)

        result[arm] = {"net": number(row["net"]), "closed": int(row["closed"]) if pd.notna(row["closed"]) else 0,
                       "open": int(row["open"]) if pd.notna(row["open"]) else 0,
                       "n_entry": int(row["n_entry"]) if pd.notna(row["n_entry"]) else 0}
    return result


def assemble_result(cfg: Config, identity: dict, frozen: dict, summary: pd.DataFrame,
                    portfolios: dict, table: pd.DataFrame, masks: dict, extras: dict,
                    checks: list = None) -> dict:
    primary_name = frozen["primary"]["rule_id"] if frozen["primary"] else "NO_SELECTION"
    checks = checks or []
    return {
        "branch": BRANCH, "run": str(cfg.out), "config": cfg.to_json(),
        "input_hashes": identity,
        "primary": primary_name,
        "status": "NO_SELECTION_CASH" if frozen["no_selection"] else "SELECTED",
        "selection_criterion": frozen["criterion"],
        "attempt_count": frozen["attempts"],
        "universe": {"tickers": identity["tickers"],
                     "eligible_total": int(len(table)),
                     "eligible_known": int(table["known"].sum()),
                     "eligible_unknown": int(len(table) - int(table["known"].sum()))},
        "splits": {name: int(masks[name].sum()) for name in ("train", "val", "eval")},
        "summary": summary.to_dict("records"),
        "ticker_month": extras["ticker_month"].to_dict("records"),
        "sensitivity": extras["sensitivity"].to_dict("records"),
        "bootstrap": extras["bootstrap"].to_dict("records"),
        "paired_coverage_days": int(len(extras["paired_coverage"])),
        "atoms_chosen": frozen["atoms_chosen"],
        "baselines": baseline_metrics(summary),
        "checks": checks,
        "failed_checks": [item["check"] for item in checks if not item["ok"]],
        "remaining_limitations": ["corporate_actions_unverified", "snapshot_coverage_uneven",
                                  "eval_period_is_part_of_development_history",
                                  "development_unvalidated"],
        "reproduce_command": (f".venv/bin/python scripts/research_day_b1.py "
                              f"--dataset {cfg.dataset} --snapshot {cfg.snapshot} "
                              f"--out {cfg.out} --resume"),
    }


def markdown_table(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty:
        return "(empty)"
    columns = [str(column) for column in frame.columns]
    lines = ["| " + " | ".join(columns) + " |",
             "| " + " | ".join("---" for _ in columns) + " |"]
    for record in frame.itertuples(index=False, name=None):
        cells = ["" if value is None else str(value).replace("|", "\\|") for value in record]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def render_markdown(result: dict, frozen: dict, summary: pd.DataFrame) -> str:
    lines = [f"# PARALLEL-DAY-1 / B1 — {BRANCH}", "",
             f"- primary: `{result['primary']}` ({result['status']})",
             f"- criterion: `{result['selection_criterion']}`",
             f"- attempts: `{result['attempt_count']}`",
             f"- atoms: {', '.join(result['atoms_chosen']) or '(none)'}", "",
             "## Summary", "",
             markdown_table(summary), "",
             "## Robustness", "",
             markdown_table(pd.DataFrame(result["sensitivity"])), "",
             "## Bootstrap", "",
             markdown_table(pd.DataFrame(result["bootstrap"])), "",
             "## Limitations", ""]
    lines.extend(f"- {item}" for item in result["remaining_limitations"])
    lines.extend(["", f"## Ticker / Month (primary, {COST_PRIMARY_ID})", ""])
    breakdown = pd.DataFrame(result["ticker_month"])
    primary_rows = breakdown[(breakdown["arm"] == "primary") & (breakdown["cost"] == COST_PRIMARY_ID)]
    tickers = primary_rows[primary_rows["kind"] == "ticker"].drop(columns=["arm", "kind", "cost", "month"])
    months = primary_rows[primary_rows["kind"] == "month"][["month", "closed", "net", "gross", "fees", "slip"]]
    lines.extend([markdown_table(tickers.sort_values("net")), "", markdown_table(months), "",
                  f"## Ticker / Month (primary, {COST_SCENARIOS[1][0]})", ""])
    secondary_rows = breakdown[(breakdown["arm"] == "primary") & (breakdown["cost"] == COST_SCENARIOS[1][0])]
    tickers_secondary = secondary_rows[secondary_rows["kind"] == "ticker"].drop(columns=["arm", "kind", "cost", "month"])
    months_secondary = secondary_rows[secondary_rows["kind"] == "month"][["month", "closed", "net", "gross", "fees", "slip"]]
    lines.extend([markdown_table(tickers_secondary.sort_values("net")), "", markdown_table(months_secondary)])
    return "\n".join(lines) + "\n"


def execute(cfg: Config, resume: bool, force: bool) -> int:
    schema = load_schema(cfg.dataset)
    manifest = load_dataset_manifest(cfg.dataset)
    identity = build_identity(cfg, manifest, schema)
    config_hash = identity_hash(identity)
    cfg.out.mkdir(parents=True, exist_ok=True)
    identity_path = cfg.out / "identity.json"
    if identity_path.exists() and not force:
        previous = json.loads(identity_path.read_text())
        if previous.get("config_hash") != config_hash and not resume:
            raise SystemExit(f"{cfg.out}: identity hash mismatch; use --resume/--force/new --out")
    stages = StageStore(cfg.out, config_hash, resume)
    tickers = identity["tickers"]
    continuous, votes = atom_columns(schema)

    table_path = cfg.out / "core" / "decisions.parquet"
    if stages.done("core") and table_path.exists():
        table = pd.read_parquet(table_path)
    else:
        table = build_decision_table(cfg, schema, tickers)
        table_path.parent.mkdir(parents=True, exist_ok=True)
        table.to_parquet(table_path, index=False)
        stages.record("core", rows=int(len(table)))

    dates = msk_dates(table["decision_ts"])
    masks = split_masks(table, dates)
    known = table["known"].to_numpy()
    up = (table["label"].to_numpy() == 1.0)

    atoms = build_atoms(table, masks["train"], continuous, votes)
    true, unknown = atom_flags(table, atoms)
    atoms_stats, chosen, base_up = select_atoms(atoms, true, masks, known, up, dates, cfg)
    atoms_stats.to_csv(cfg.out / "atoms.csv", index=False)

    events = {}
    for cost_id, costs in COST_SCENARIOS:
        for split_name in SPLITS:
            path = cfg.out / "outcomes" / f"outcomes_{split_name}_{cost_id}.parquet"
            key = f"outcomes_{split_name}_{cost_id}"
            if stages.done(key) and path.exists():
                frame = pd.read_parquet(path)
                subset = table[masks[split_name]].reset_index(drop=True)
            else:
                frame, subset = build_split_events(cfg, table, masks, split_name, tickers, costs)
                path.parent.mkdir(parents=True, exist_ok=True)
                frame.to_parquet(path, index=False)
                stages.record(key, rows=int(len(frame)))
            events[(split_name, cost_id)] = align_events(table, masks, split_name, frame, subset)

    atoms_by_id = {atom.atom_id: atom for atom in atoms}
    index_of = {atom.atom_id: index for index, atom in enumerate(atoms)}

    rules = generate_rules(chosen, atoms_by_id)
    records = [rule_stats(rule, atoms_by_id, index_of, true, unknown, masks, known, up, dates,
                          events, base_up) for rule in rules]
    rules_all = pd.DataFrame(records)
    rules_all.to_csv(cfg.out / "rules_all.csv", index=False)

    singles = choose_primary(records, 1)
    pairs = choose_primary(records, 2)
    triples = choose_primary(records, 3)
    ordered = choose_primary(records, None)
    primary = ordered[0] if ordered else None
    frozen = {
        "primary": primary, "top3": ordered[1:4],
        "best_single_atom_baseline": singles[0] if singles else None,
        "no_selection": primary is None,
        "criterion": {"metric": "val_mean_event_net_c5_s2",
                      "min_known_events": SEL_MIN_KNOWN_EVENTS,
                      "min_known_dates": SEL_MIN_KNOWN_DATES,
                      "max_unknown_fraction": SEL_MAX_UNKNOWN_FRACTION},
        "attempts": {"singles_feasible": len(singles), "pairs_feasible": len(pairs),
                     "triples_feasible": len(triples), "rules_generated": len(rules)},
        "atoms_chosen": list(chosen),
        "input_hashes": identity,
    }
    (cfg.out / "rules_frozen.json").write_text(json.dumps(frozen, indent=1, default=str))

    arms = build_arms(primary, ordered[1:4], singles[0] if singles else None)
    rows, portfolios = run_arms(cfg, table, masks, arms, atoms_by_id, index_of, true, unknown,
                                events)
    summary = pd.DataFrame(rows)
    summary.to_csv(cfg.out / "summary.csv", index=False)

    extras = robustness(portfolios)
    for name, frame in extras.items():
        frame.to_csv(cfg.out / f"{name}.csv", index=False)

    for (arm_name, split_name, cost_id), portfolio in portfolios.items():
        if split_name != "eval" or cost_id != COST_PRIMARY_ID:
            continue
        write_ledgers(cfg.out / "ledgers", arm_name, portfolio)

    result = assemble_result(cfg, identity, frozen, summary, portfolios, table, masks, extras,
                             checks=run_self_checks(cfg, identity, config_hash, frozen, summary,
                                                    table, masks, chosen, atoms_stats))
    (cfg.out / "RESULT.json").write_text(json.dumps(result, indent=1, default=str))
    (cfg.out / "RESULT.md").write_text(render_markdown(result, frozen, summary))
    identity_path.write_text(json.dumps(
        {"config_hash": config_hash, "identity": identity,
         "generated_at": datetime.now(timezone.utc).isoformat()}, indent=1, default=str))
    print(json.dumps({"primary": result["primary"], "status": result["status"],
                      "rows": int(len(table)), "atoms": len(chosen), "rules": len(rules),
                      "out": str(cfg.out)}, ensure_ascii=False))
    return 0


def write_ledgers(root: Path, arm_name: str, portfolio) -> None:
    root.mkdir(parents=True, exist_ok=True)
    executed = executed_frame(portfolio)
    executed.to_csv(root / f"executed_{arm_name}.csv", index=False)
    open_rows = []
    for item in getattr(portfolio, "accepted_open", []):
        open_rows.append({"row_id": item.row_id, "figi": item.figi, "ticker": item.ticker,
                          "model_id": item.model_id, "entry_ts": item.entry_ts,
                          "entry_base": item.entry_base, "qty": item.qty,
                          "entry_debit": item.entry_debit,
                          "planned_exit_ts": item.planned_exit_ts})
    pd.DataFrame(open_rows, columns=list(ACCEPTED_OPEN_COLUMNS)).to_csv(
        root / f"accepted_open_{arm_name}.csv", index=False)


def build_arms(primary, top3: list, best_single) -> dict:
    arms = {"primary": primary["atoms"].split(";") if primary else None}
    for index, record in enumerate(top3, start=1):
        arms[f"top{index}"] = record["atoms"].split(";")
    arms["best_single_atom"] = best_single["atoms"].split(";") if best_single else None
    arms["always_long"] = "ALL"
    arms["cash"] = None
    return arms


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PARALLEL-DAY-1 Worker-1 / B1 rules branch")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--tickers", nargs="*", default=[])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-atoms", type=int, default=MAX_ATOMS)
    parser.add_argument("--atom-min-support", type=int, default=ATOM_MIN_SUPPORT)
    parser.add_argument("--atom-min-dates", type=int, default=ATOM_MIN_DATES)
    parser.add_argument("--scope", default="full")
    args = parser.parse_args(argv)
    cfg = Config(dataset=Path(args.dataset), snapshot=Path(args.snapshot), out=Path(args.out),
                 tickers=tuple(args.tickers), max_atoms=args.max_atoms,
                 atom_min_support=args.atom_min_support, atom_min_dates=args.atom_min_dates,
                 scope=args.scope)
    return execute(cfg, resume=args.resume, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
