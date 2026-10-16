"""Focused-проверки ветки PARALLEL-DAY-1 / Worker-1 (Builder-1) `day_b1_20261014`.

Закрывают ровно то, что критично для доверия к ветке:
- нет сдвига метки: join features↔targets строго по row_id;
- будущая/итоговая мутация target не меняет X (признаки не зависят от исхода);
- NULL → UNKNOWN и НЕ срабатывает (не «RSI 0»);
- правило из нескольких атомов: разные исходные признаки и ≥2 ТФ;
- обрезка правил и лимит 2 атома на исходный признак;
- границы `event_metrics` (unknown не 0) и `feasible`;
- отсутствие lookahead: `available_at_ts == decision_ts`, embargo сплитов;
- immutable output при смене identity;
- тождество `net == gross - costs_total` в ledger и summary.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(BACKEND / "scripts") not in sys.path:
    sys.path.insert(0, str(BACKEND / "scripts"))

import research_day_b1 as b1
from research.branches.day_b1_20261014 import fixture as fx


def write_fixture(root: Path, tickers=("AAA",), train=1, val=1, ev=1) -> Path:
    sessions = {
        "train": fx.weekday_sessions(date(2026, 3, 2), train),
        "val": fx.weekday_sessions(date(2026, 6, 1), val),
        "eval": fx.weekday_sessions(date(2026, 8, 3), ev),
    }
    fx.build_fixture(root, tickers=tuple(tickers), sessions=sessions)
    return root


@pytest.fixture(scope="module")
def full_run(tmp_path_factory):
    root = tmp_path_factory.mktemp("b1_full")
    dataset = write_fixture(root / "dataset", tickers=("AAA",), train=40, val=26, ev=26)
    out = root / "run"
    cfg = b1.Config(dataset=dataset, snapshot=dataset, out=out)
    assert b1.execute(cfg, resume=False, force=True) == 0
    return {"dataset": dataset, "out": out, "cfg": cfg}


def test_contract_fields_present(full_run):
    result = json.loads((full_run["out"] / "RESULT.json").read_text())
    required = ["branch", "run", "config", "input_hashes", "primary", "status",
                "selection_criterion", "attempt_count", "universe", "summary", "splits",
                "atoms_chosen", "baselines", "checks", "failed_checks",
                "remaining_limitations", "reproduce_command", "ticker_month",
                "sensitivity", "paired_coverage_days", "bootstrap"]
    for key in required:
        assert key in result, key
    assert result["branch"] == "day_b1_20261014"
    assert result["status"] in ("SELECTED", "NO_SELECTION_CASH")
    assert result["failed_checks"] == []
    assert set(result["baselines"]) >= {"cash", "always_long"}
    for name in ("summary.csv", "RESULT.md", "RESULT.json", "atoms.csv", "rules_all.csv",
                 "rules_frozen.json", "identity.json", "stages.json", "ticker_month.csv",
                 "sensitivity.csv", "bootstrap.csv", "paired_coverage.csv"):
        assert (full_run["out"] / name).exists(), name
    assert (full_run["out"] / "ledgers").is_dir()


def test_selection_respects_limits(full_run):
    stats = pd.read_csv(full_run["out"] / "atoms.csv")
    frozen = json.loads((full_run["out"] / "rules_frozen.json").read_text())
    chosen = frozen["atoms_chosen"]
    assert len(chosen) <= b1.MAX_ATOMS
    picked = stats[stats["atom_id"].isin(chosen)]
    assert set(picked["atom_id"]) == set(chosen)
    assert (picked["train_support"] >= b1.ATOM_MIN_SUPPORT).all()
    assert (picked["train_dates"] >= b1.ATOM_MIN_DATES).all()
    assert picked.groupby("column").size().max() <= b1.MAX_ATOMS_PER_SOURCE
    assert frozen["attempts"]["rules_generated"] <= (b1.MAX_RULES_SINGLE + b1.MAX_RULES_PAIR
                                                      + b1.MAX_RULES_TRIPLE)


def test_rules_all_multiframe_and_distinct_columns(full_run):
    rules = pd.read_csv(full_run["out"] / "rules_all.csv")
    multi = rules[rules["n_atoms"] >= 2]
    assert not multi.empty
    for _, row in multi.iterrows():
        ids = row["atoms"].split(";")
        columns = [atom.split("|")[0] for atom in ids]
        assert len(set(columns)) == len(columns)
        tfs = {b1.tf_of(atom.split("|")[0]) for atom in ids}
        tfs = {tf for tf in tfs if tf is not None}
        assert len(tfs) >= 2


def test_primary_rule_is_valid(full_run):
    frozen = json.loads((full_run["out"] / "rules_frozen.json").read_text())
    if frozen["primary"] is None:
        pytest.skip("NO_SELECTION: primary rule отсутствует по критерию")
    ids = tuple(frozen["primary"]["atoms"].split(";"))
    assert ids
    if len(ids) >= 2:
        columns = [atom.split("|")[0] for atom in ids]
        assert len(set(columns)) == len(columns)
        tfs = {b1.tf_of(atom.split("|")[0]) for atom in ids if b1.tf_of(atom.split("|")[0])}
        assert len(tfs) >= 2


def test_summary_and_ledger_identity(full_run):
    summary = pd.read_csv(full_run["out"] / "summary.csv")
    for _, row in summary.iterrows():
        if row["closed"] and row["closed"] > 0:
            assert row["identity_ok"] is True or row["identity_ok"] == True  # noqa: E712
            assert abs(row["net"] - (row["gross"] - row["costs"])) < 1e-6
    for path in (full_run["out"] / "ledgers").glob("executed_*.csv"):
        frame = pd.read_csv(path)
        if frame.empty:
            continue
        residual = (frame["gross"] - frame["costs_total"] - frame["net"]).abs()
        assert residual.max() < 1e-6


def test_no_lookahead_split_masks(full_run):
    schema = b1.load_schema(full_run["dataset"])
    table = b1.build_decision_table(full_run["cfg"], schema, ["AAA"])
    dates = b1.msk_dates(table["decision_ts"])
    masks = b1.split_masks(table, dates)
    embargo = pd.Timedelta(minutes=b1.EMBARGO_MIN)
    val_boundary = pd.Timestamp(b1.VAL_START, tz="UTC")
    eval_boundary = pd.Timestamp(b1.EVAL_START, tz="UTC")
    train = table[masks["train"]]
    assert (train["label_end_ts"] < val_boundary).all()
    assert (train["decision_ts"] < val_boundary - embargo).all()
    val = table[masks["val"]]
    assert (val["label_end_ts"] < eval_boundary).all()
    assert (val["decision_ts"] < eval_boundary - embargo).all()
    assert not (masks["train"] & masks["val"]).any()


def test_target_mutation_does_not_change_features(tmp_path):
    dataset = write_fixture(tmp_path / "ds", tickers=("AAA",))
    schema = b1.load_schema(dataset)
    cfg = b1.Config(dataset=dataset, snapshot=dataset, out=tmp_path / "out")
    before = b1.build_decision_table(cfg, schema, ["AAA"])

    target_path = dataset / "targets" / "targets_AAA.parquet"
    targets = pd.read_parquet(target_path)
    targets["label"] = -pd.to_numeric(targets["label"], errors="coerce")
    targets.to_parquet(target_path, index=False)
    after = b1.build_decision_table(cfg, schema, ["AAA"])

    predictors = b1.predictor_columns(schema)
    pd.testing.assert_frame_equal(before[predictors].reset_index(drop=True),
                                  after[predictors].reset_index(drop=True))
    before_labels = before.set_index("row_id")["label"]
    after_labels = after.set_index("row_id")["label"]
    common = before_labels.notna() & after_labels.notna()
    assert common.any()
    assert (after_labels[common] == -before_labels[common]).all()


def test_label_join_by_row_id(tmp_path):
    dataset = write_fixture(tmp_path / "ds", tickers=("AAA",))
    schema = b1.load_schema(dataset)
    table = b1.read_ticker_table(dataset, "AAA",
                                 ["row_id", "figi", "ticker", "decision_ts"] + b1.predictor_columns(schema))
    targets = pd.read_parquet(dataset / "targets" / "targets_AAA.parquet")
    joined = table.merge(targets[["row_id", "label", "decision_ts"]], on="row_id",
                         suffixes=("", "_t"))
    assert (joined["decision_ts"] == joined["decision_ts_t"]).all()
    assert set(table["row_id"]).issubset(set(targets["row_id"]))


def test_null_is_unknown_not_true():
    table = pd.DataFrame({"x": [1.0, np.nan, 3.0]})
    for kind, threshold, expected_true in (("gt", 2.0, [False, False, True]),
                                           ("le", 2.0, [True, False, False])):
        atom = b1.Atom(f"x|{kind}", "x", kind, threshold, "_1m")
        true, unknown = b1.atom_flags(table, [atom])
        assert unknown[0].tolist() == [False, True, False]
        assert true[0].tolist() == expected_true
        assert not (true[0] & unknown[0]).any()
    vote = pd.DataFrame({"vote_s": [1.0, np.nan, -1.0]})
    atom = b1.Atom("vote_s|eq_p1", "vote_s", "eq_p1", 1.0, None)
    true, unknown = b1.atom_flags(vote, [atom])
    assert unknown[0].tolist() == [False, True, False]
    assert true[0].tolist() == [True, False, False]


def test_rule_valid_requires_distinct_columns_and_two_tfs():
    atoms_by_id = {
        "x_1m|gt|q80": b1.Atom("x_1m|gt|q80", "x_1m", "gt", 1.0, "_1m"),
        "x_1m|le|q20": b1.Atom("x_1m|le|q20", "x_1m", "le", 0.0, "_1m"),
        "y_1m|gt|q80": b1.Atom("y_1m|gt|q80", "y_1m", "gt", 1.0, "_1m"),
        "z_5m|gt|q80": b1.Atom("z_5m|gt|q80", "z_5m", "gt", 1.0, "_5m"),
        "vote_s|eq_p1": b1.Atom("vote_s|eq_p1", "vote_s", "eq_p1", 1.0, None),
    }
    assert b1.rule_valid(("x_1m|gt|q80",), atoms_by_id) is True
    assert b1.rule_valid(("x_1m|gt|q80", "x_1m|le|q20"), atoms_by_id) is False
    assert b1.rule_valid(("x_1m|gt|q80", "y_1m|gt|q80"), atoms_by_id) is False
    assert b1.rule_valid(("x_1m|gt|q80", "z_5m|gt|q80"), atoms_by_id) is True
    assert b1.rule_valid(("x_1m|gt|q80", "vote_s|eq_p1"), atoms_by_id) is False
    assert b1.rule_valid(("vote_s|eq_p1", "y_1m|gt|q80"), atoms_by_id) is False


def test_generate_rules_within_caps():
    atoms_by_id = {}
    tfs = ("_1m", "_5m", "_600s", "_1800s", "_3600s")
    chosen = []
    for index in range(20):
        column = f"col{index}{tfs[index % 5]}"
        atom_id = f"{column}|gt|q80"
        atoms_by_id[atom_id] = b1.Atom(atom_id, column, "gt", 1.0, tfs[index % 5])
        chosen.append(atom_id)
    rules = b1.generate_rules(chosen, atoms_by_id)
    assert len(rules) <= b1.MAX_RULES_SINGLE + b1.MAX_RULES_PAIR + b1.MAX_RULES_TRIPLE
    assert all(b1.rule_valid(rule, atoms_by_id) for rule in rules)
    assert len({b1.rule_id_of(rule) for rule in rules}) == len(rules)


def test_feasible_boundaries():
    base = {"mean_net": 0.001, "n_known": 200, "known_dates": 20, "unknown_fraction": 0.05}
    assert b1.feasible(base) is True
    assert b1.feasible({**base, "n_known": 199}) is False
    assert b1.feasible({**base, "known_dates": 19}) is False
    assert b1.feasible({**base, "unknown_fraction": 0.050001}) is False
    assert b1.feasible({**base, "mean_net": float("nan")}) is False


def test_event_metrics_unknown_is_not_zero():
    selected = np.array([True, True, True, True, False])
    mask = np.array([True, True, True, True, True])
    dates = np.array([date(2026, 6, 1)] * 5, dtype=object)
    status = np.array([b1.STATUS_FILLED, b1.STATUS_INCOMPLETE, b1.STATUS_SKIP,
                       b1.STATUS_FILLED, b1.STATUS_FILLED], dtype=np.int8)
    net = np.array([0.01, np.nan, np.nan, -0.03, 0.5])
    accounted = np.array([True, True, True, True, True])
    event = {"status": status, "net": net, "accounted": accounted}
    metrics = b1.event_metrics(selected, mask, event, dates)
    assert metrics["n_selected"] == 4
    assert metrics["n_known"] == 2
    assert metrics["n_unknown"] == 1
    assert metrics["n_missing_entry"] == 1
    assert abs(metrics["mean_net"] - (-0.01)) < 1e-12
    assert abs(metrics["unknown_fraction"] - (1.0 / 4.0)) < 1e-12


def test_available_at_equals_decision(monkeypatch, tmp_path):
    dataset = write_fixture(tmp_path / "ds", tickers=("AAA",), val=2)
    schema = b1.load_schema(dataset)
    cfg = b1.Config(dataset=dataset, snapshot=dataset, out=tmp_path / "out")
    table = b1.build_decision_table(cfg, schema, ["AAA"])
    dates = b1.msk_dates(table["decision_ts"])
    masks = b1.split_masks(table, dates)

    captured = []
    real = b1.fp.execute_signal

    def spy(prediction, store, costs):
        captured.append(dict(prediction))
        return real(prediction, store, costs)

    monkeypatch.setattr(b1.fp, "execute_signal", spy)
    b1.build_split_events(cfg, table, masks, "val", ["AAA"], b1.COST_PRIMARY)
    assert captured
    assert all(item["available_at_ts"] == item["decision_ts"] for item in captured)
    assert all(item["selected_long"] is True for item in captured)


def test_immutable_output_on_identity_change(tmp_path):
    dataset = write_fixture(tmp_path / "ds", tickers=("AAA",))
    out = tmp_path / "out"
    cfg = b1.Config(dataset=dataset, snapshot=dataset, out=out)
    assert b1.execute(cfg, resume=False, force=True) == 0
    changed = b1.Config(dataset=dataset, snapshot=dataset, out=out, scope="probe")
    with pytest.raises(SystemExit):
        b1.execute(changed, resume=False, force=False)
    assert (out / "identity.json").exists()


def _outcome_row(row_id, status, entry_ts, entry_base, decision_ts="2026-08-03T07:05:00+00:00"):
    return {"row_id": row_id, "figi": "F", "ticker": "AAA", "model_id": "m",
            "split": "eval", "decision_ts": pd.Timestamp(decision_ts), "status": status,
            "reason": "r", "entry_ts": entry_ts, "exit_ts": entry_ts,
            "entry_base": entry_base, "exit_base": entry_base,
            "gross_per_unit": 0.0, "net_per_unit": 0.0, "costs_per_unit": 0.0,
            "path_metrics_known": False}


def test_skip_row_nat_is_not_an_entry():
    frame = pd.DataFrame([
        _outcome_row("skip", "SKIP", pd.NaT, float("nan")),
        _outcome_row("filled", "FILLED", pd.Timestamp("2026-08-03T07:05:00+00:00"), 100.0),
    ])
    objects = b1.outcome_objects(frame)
    assert objects[0].entry_ts is None and objects[0].exit_ts is None
    assert objects[1].entry_ts is not None and objects[1].entry_base == 100.0
