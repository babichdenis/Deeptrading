"""Builder-2 / PARALLEL-DAY-1: focused checks для research_day_b2.

Герметичные синтетические проверки; одна проверка читает frozen dataset manifest
только как источник схемы (skip, если датасета нет).
"""
from __future__ import annotations

import json
import pickle
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from scripts import research_day_b2 as rdb

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc
BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
DATASET = REPO / rdb.REL_DATASET
MONDAY = datetime(2026, 3, 2, tzinfo=MSK)  # понедельник


def _sess(h: int, m: int, days: int = 0) -> datetime:
    return (MONDAY + timedelta(days=days, hours=h, minutes=m)).astimezone(UTC)


def _store(rows):
    from research.daypack_20261014.evaluate import MinuteStore
    return MinuteStore({"A": {t: (o, c) for t, o, c in rows}})


def _pred(ts: datetime, **kw) -> dict:
    d = {"row_id": "r1", "figi": "A", "ticker": "A", "decision_ts": ts.isoformat(),
         "available_at_ts": ts.isoformat(), "model_id": "fixture", "split": "fixture",
         "selected_long": True}
    d.update(kw)
    return d


# ------------------------------------------------------------------ stage A semantics


def test_entry_at_decision_open_and_net_identity():
    from research.daypack_20261014 import frozen_portfolio as engine
    from research.daypack_20261014.costs import CostParams

    t = _sess(10, 0)
    store = _store([(t, 100.0, 100.5),
                    (t + timedelta(minutes=30), 101.0, 101.5),
                    (t + timedelta(minutes=60), 102.0, 102.5),
                    (t + timedelta(minutes=120), 103.0, 103.5)])
    prev = engine.EXIT_HORIZON_MIN
    try:
        engine.EXIT_HORIZON_MIN = 30
        out = engine.execute_signal(_pred(t), store, CostParams(5.0, 2.0))
    finally:
        engine.EXIT_HORIZON_MIN = prev
    assert out.status == "FILLED"
    assert out.entry_ts == t
    assert out.exit_ts == t + timedelta(minutes=30)
    assert out.entry_base == 100.0 and out.exit_base == 101.0
    entry_fill = 100.0 * (1 + 2e-4)
    exit_fill = 101.0 * (1 - 2e-4)
    expected = ((exit_fill - entry_fill)
                - entry_fill * 5e-4 - exit_fill * 5e-4) / 100.0
    assert out.gross_per_unit == pytest.approx(0.01, abs=1e-12)
    assert out.net_per_unit == pytest.approx(expected, abs=1e-12)
    assert out.net_per_unit == pytest.approx(out.gross_per_unit - out.costs_per_unit,
                                             abs=1e-12)


def test_exit_clamped_to_cutoff_1844():
    from research.daypack_20261014 import frozen_portfolio as engine
    from research.daypack_20261014.costs import CostParams

    t = _sess(18, 20)
    cutoff = t.astimezone(MSK).replace(hour=18, minute=44, second=0,
                                       microsecond=0).astimezone(UTC)
    store = _store([(t, 50.0, 50.0), (cutoff, 51.0, 51.0),
                    (t + timedelta(minutes=30), 99.0, 99.0)])
    prev = engine.EXIT_HORIZON_MIN
    try:
        engine.EXIT_HORIZON_MIN = 30
        out = engine.execute_signal(_pred(t), store, CostParams(5.0, 2.0))
    finally:
        engine.EXIT_HORIZON_MIN = prev
    assert out.status == "FILLED" and out.exit_ts == cutoff
    assert out.exit_base == 51.0


def test_missing_entry_is_skip_and_missing_exit_is_incomplete():
    from research.daypack_20261014 import frozen_portfolio as engine
    from research.daypack_20261014.costs import CostParams

    t = _sess(11, 0)
    prev = engine.EXIT_HORIZON_MIN
    try:
        engine.EXIT_HORIZON_MIN = 60
        skip = engine.execute_signal(_pred(t), _store([]), CostParams(5.0, 2.0))
        incomplete = engine.execute_signal(_pred(t), _store([(t, 10.0, 10.0)]),
                                           CostParams(5.0, 2.0))
    finally:
        engine.EXIT_HORIZON_MIN = prev
    assert skip.status == "SKIP" and skip.reason == "missing_entry_minute"
    assert skip.net_per_unit is None
    assert incomplete.status == "INCOMPLETE"
    assert incomplete.reason == "exit_minute_missing"
    assert incomplete.entry_ts == t and incomplete.exit_ts is None
    assert incomplete.net_per_unit is None


def test_session_and_weekday_gating():
    from research.daypack_20261014 import frozen_portfolio as engine
    from research.daypack_20261014.costs import CostParams

    prev = engine.EXIT_HORIZON_MIN
    try:
        engine.EXIT_HORIZON_MIN = 30
        before = engine.execute_signal(_pred(_sess(9, 30)), _store([]),
                                       CostParams(5.0, 2.0))
        after = engine.execute_signal(_pred(_sess(19, 0)), _store([]),
                                      CostParams(5.0, 2.0))
        weekend = engine.execute_signal(_pred(_sess(10, 0, days=5)), _store([]),
                                        CostParams(5.0, 2.0))
    finally:
        engine.EXIT_HORIZON_MIN = prev
    assert before.status == "SKIP" and before.reason.startswith("out_of_session")
    assert after.status == "SKIP" and after.reason.startswith("out_of_session")
    assert weekend.status == "SKIP" and weekend.reason == "out_of_session:weekend"
    assert rdb.session_reason(_sess(10, 0)) == "ok"
    assert rdb.session_reason(_sess(9, 0)) == "before_session"
    assert rdb.session_reason(_sess(18, 44)) == "after_cutoff"
    assert rdb.session_reason(_sess(10, 0, days=5)) == "weekend"


def test_lookahead_available_at_rejected():
    from research.daypack_20261014 import frozen_portfolio as engine
    from research.daypack_20261014.costs import CostParams

    t = _sess(10, 0)
    store = _store([(t, 100.0, 100.0), (t + timedelta(minutes=30), 101.0, 101.0)])
    pred = _pred(t, available_at_ts=(t + timedelta(minutes=1)).isoformat())
    prev = engine.EXIT_HORIZON_MIN
    try:
        engine.EXIT_HORIZON_MIN = 30
        out = engine.execute_signal(pred, store, CostParams(5.0, 2.0))
    finally:
        engine.EXIT_HORIZON_MIN = prev
    assert out.status == "SKIP" and out.reason.startswith("lookahead_unavailable")


# ------------------------------------------------------------------ splits / purge


def test_split_assignment_and_embargo():
    boundary = datetime(2026, 6, 1, 0, 0, tzinfo=MSK)
    assert rdb.assign_split(datetime(2026, 5, 31, 18, 43, tzinfo=MSK)) == "train"
    assert rdb.assign_split(datetime(2026, 6, 1, 10, 0, tzinfo=MSK)) == "val"
    assert rdb.assign_split(datetime(2026, 7, 31, 18, 43, tzinfo=MSK)) == "val"
    assert rdb.assign_split(datetime(2026, 8, 1, 10, 0, tzinfo=MSK)) == "eval"
    assert rdb.assign_split(datetime(2026, 10, 8, 18, 43, tzinfo=MSK)) == "eval"
    assert rdb.assign_split(datetime(2026, 10, 9, 12, 0, tzinfo=MSK)) is None
    assert rdb.embargo_ok("train", datetime(2026, 5, 31, 18, 43, tzinfo=MSK), 120)
    assert not rdb.embargo_ok("train", boundary - timedelta(minutes=119), 120)
    assert rdb.embargo_ok("eval", datetime(2026, 10, 8, 18, 43, tzinfo=MSK), 120)
    assert not rdb.embargo_ok("val", datetime(2026, 7, 31, 22, 45, tzinfo=MSK), 120)


def test_unknown_targets_are_nan_and_excluded_from_fit():
    status = np.array(["FILLED", "INCOMPLETE", "SKIP", "FILLED"])
    n52 = np.array([0.001, np.nan, np.nan, -0.002])
    y = np.where(status == "FILLED", n52, np.nan)
    mask = np.isfinite(y)
    assert mask.tolist() == [True, False, False, True]
    assert np.isnan(y[1]) and np.isnan(y[2])
    assert float(np.mean(y[mask])) == pytest.approx(-0.0005)


def test_transform_fit_on_train_only():
    from sklearn.impute import SimpleImputer

    rng = np.random.RandomState(0)
    train = rng.rand(50, 5).astype(np.float32)
    train[0, 0] = np.nan
    val = rng.rand(20, 5).astype(np.float32) + 5.0
    imp = SimpleImputer(strategy="median").fit(train)
    assert np.allclose(imp.statistics_, np.nanmedian(train, axis=0))
    assert not np.allclose(np.nanmedian(val, axis=0), imp.statistics_, atol=1e-6)


def test_model_pickle_roundtrip_is_deterministic(tmp_path):
    rng = np.random.RandomState(7)
    X = rng.rand(400, 6)
    y = rng.randn(400) * 1e-3
    est = rdb._estimator("ridge").fit(X, y)
    est2 = rdb._estimator("hgb").fit(X, y)
    p = tmp_path / "m.pkl"
    with p.open("wb") as fh:
        pickle.dump({"ridge": est, "hgb": est2}, fh, protocol=4)
    with p.open("rb") as fh:
        loaded = pickle.load(fh)
    assert np.array_equal(est.predict(X), loaded["ridge"].predict(X))
    assert np.array_equal(est2.predict(X), loaded["hgb"].predict(X))


@pytest.mark.skipif(not (DATASET / "manifest.json").exists(),
                    reason="frozen dataset недоступен")
def test_quality_columns_are_metadata_not_predictors():
    m = json.loads((DATASET / "manifest.json").read_text(encoding="utf-8"))
    predictors = m["predictor_columns"]
    meta = set(m["metadata_columns"])
    assert len(predictors) == 58
    assert not (meta & set(predictors))
    assert len([c for c in predictors if c.endswith("_5m")]) == 7
    assert all(not c.startswith("n_min_") for c in predictors)


# ------------------------------------------------------------------ selection criterion


def _ts_frame(n: int, per_day: int) -> np.ndarray:
    return np.array([
        (_sess(10, 0) + timedelta(days=i // per_day, minutes=5 * (i % per_day))
         ).isoformat() for i in range(n)], dtype=object)


def test_screen_gates_on_support_dates_and_unknown():
    n = 400
    status = np.array(["INCOMPLETE" if i % 40 == 0 else "FILLED" for i in range(n)],
                      dtype=object)
    ts = _ts_frame(n, 10)
    n52 = np.full(n, 0.0004)
    good = np.zeros(n, dtype=bool)
    good[:250] = True
    rec = rdb._screen(good, status, n52, ts, 0.0)
    assert rec["eligible"] is True
    assert rec["n_known"] == 243 and rec["n_unknown"] == 7
    assert rec["unknown_frac"] == pytest.approx(7 / 250)
    assert rec["n_dates"] >= 20

    low = rdb._screen(np.r_[np.ones(150, bool), np.zeros(n - 150, bool)],
                      status, n52, ts, 0.0)
    assert not low["eligible"] and low["fail_reason"] == "known_events<200"

    one_day = _ts_frame(n, n)
    few = rdb._screen(good, status, n52, one_day, 0.0)
    assert not few["eligible"] and few["fail_reason"] == "known_dates<20"

    heavy_unknown = np.array(["INCOMPLETE" if i % 3 == 0 else "FILLED"
                              for i in range(n)], dtype=object)
    rec2 = rdb._screen(np.ones(n, bool), heavy_unknown, n52, ts, 0.0)
    assert not rec2["eligible"] and rec2["fail_reason"] == "unknown_frac>0.05"

    empty = rdb._screen(np.zeros(n, dtype=bool), status, n52, ts, 0.0)
    assert empty["eligible"] is False and empty["fail_reason"] == "no_selected"


# ------------------------------------------------------------------ daypack parity


def _write_m1(path: Path, ticker: str, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        fh.write("ticker,figi,interval,ts,open,high,low,close,volume,source,partial\n")
        for t, o, c in rows:
            fh.write(f"{ticker},{ticker}F,1,{t.isoformat()},{o},{o},{c},{c},10,db,\n")


def test_per_ticker_evaluate_matches_reference_evaluate(tmp_path):
    from research.daypack_20261014.evaluate import MinuteStore, evaluate

    t = _sess(10, 0)
    rows = {"A": [(t, 100.0, 100.0), (t + timedelta(minutes=30), 101.0, 101.0)],
            "B": [(t, 200.0, 200.0), (t + timedelta(minutes=30), 199.0, 199.0)]}
    store = MinuteStore({k: {ts: (o, c) for ts, o, c in v} for k, v in rows.items()})
    preds = [_pred(t, figi="A", ticker="A", row_id="ra"),
             _pred(t, figi="B", ticker="B", row_id="rb")]
    ref = evaluate(preds, store, horizon_minutes=30, commission_bps=5, slippage_bps=2)

    figi_map, m1_paths = {}, {}
    for k, v in rows.items():
        p = tmp_path / f"{k}__m1.csv"
        _write_m1(p, k, v)
        m1_paths[k] = p
        figi_map[k] = f"{k}F"

    preds_matched = [dict(p, figi=figi_map[p["ticker"]]) for p in preds]

    class _Run:
        def log(self, *a, **k):
            pass

    mine = rdb.evaluate_config(preds_matched, 30, rdb.COST_PRIMARY, figi_map,
                               m1_paths, _Run())
    assert mine["metrics"]["net_total"] == pytest.approx(
        ref["metrics"]["net_total"], abs=1e-12)
    assert mine["portfolio"].n_executed == ref["portfolio"].n_executed == 2
    assert mine["metrics"]["costs_identity_net_eq_gross_minus_costs"]

    mismatched = rdb.evaluate_config(preds, 30, rdb.COST_PRIMARY, figi_map,
                                     m1_paths, _Run())
    assert mismatched["portfolio"].n_skip == 2
    assert mismatched["metrics"]["net_total"] == 0.0


def test_repeat_signal_same_figi_is_ignored():
    from research.daypack_20261014 import frozen_portfolio as engine
    from research.daypack_20261014.costs import CostParams

    t = _sess(10, 0)
    rows = [(t, 100.0, 100.0), (t + timedelta(minutes=30), 110.0, 110.0)]
    store = _store(rows)
    preds = [_pred(t, row_id=f"r{i}") for i in range(3)]
    prev = engine.EXIT_HORIZON_MIN
    try:
        engine.EXIT_HORIZON_MIN = 30
        outs = [engine.execute_signal(p, store, CostParams(5.0, 2.0)) for p in preds]
        res = engine.run_portfolio(outs, CostParams(5.0, 2.0))
    finally:
        engine.EXIT_HORIZON_MIN = prev
    assert res.n_executed == 1
    assert res.n_ignore_repeat == 2


def test_bootstrap_is_deterministic_with_fixed_seed():
    diff = {f"2026-08-{1 + i:02d}": (i % 7) * 0.001 for i in range(25)}
    a = rdb.bootstrap_block_ci(diff)
    b = rdb.bootstrap_block_ci(diff)
    assert a == b
    assert a["n_days"] == 25 and a["n_blocks"] == 5
    assert a["ci_low"] is not None and a["ci_low"] <= a["mean"] <= a["ci_high"]


def test_freeze_is_written_once_and_hash_is_stable(tmp_path):
    import pandas as pd

    class _Run:
        out = tmp_path
        def log(self, *a, **k):
            pass

    keys = [rdb.fit_key(m, f, h) for h in rdb.HORIZONS
            for f in rdb.FEATURE_SETS for m in rdb.MODELS]
    n = 300
    val = pd.DataFrame({"decision_ts": _ts_frame(n, 5)})
    for h in rdb.HORIZONS:
        val[f"st{h}"] = ["FILLED"] * (n - 10) + ["INCOMPLETE"] * 10
        val[f"n52{h}"] = np.linspace(1e-4, 2e-3, n)
    for k in keys:
        val[f"p__{k}"] = np.linspace(-1e-3, 3e-3, n)
    (tmp_path / "predictions").mkdir()
    val.to_parquet(tmp_path / "predictions" / "val.parquet", index=False)

    sel = rdb.stage_c_selection(_Run(), {}, False)
    assert sel["freeze"]["n_attempts"] == 36
    assert sel["freeze"]["immutable_after_write"] is True
    assert sel["freeze_sha256"] == rdb.sha256_file(tmp_path / "freeze.json")
    assert sel["freeze"]["status"] == "PRIMARY"
    assert len(sel["freeze"]["top3_exploratory"]) == 3
    assert sel["freeze"]["best_ridge"] is not None
    assert sel["freeze"]["best_cont5_comparator"] is not None
    assert sel["freeze"]["never_reselected_after_eval"] is True


def test_attempt_id_is_unique_per_threshold(tmp_path):
    import pandas as pd

    class _Run:
        out = tmp_path
        def log(self, *a, **k):
            pass

    keys = [rdb.fit_key(m, f, h) for h in rdb.HORIZONS
            for f in rdb.FEATURE_SETS for m in rdb.MODELS]
    n = 300
    val = pd.DataFrame({"decision_ts": _ts_frame(n, 5)})
    for h in rdb.HORIZONS:
        val[f"st{h}"] = ["FILLED"] * n
        val[f"n52{h}"] = np.linspace(1e-4, 2e-3, n)
    for i, k in enumerate(keys):
        val[f"p__{k}"] = np.linspace(-1e-3 + i * 1e-6, 3e-3, n)
    (tmp_path / "predictions").mkdir()
    val.to_parquet(tmp_path / "predictions" / "val.parquet", index=False)

    sel = rdb.stage_c_selection(_Run(), {}, False)
    table = sel["freeze"]["attempt_table"]
    ids = [a["attempt"] for a in table]
    assert len(ids) == len(set(ids)) == 36
    for a in table:
        assert a["fit"] in keys
        assert a["attempt"].startswith(a["fit"] + "__t")
    cfgs = rdb._config_list(sel)
    names = [i["attempt"]["attempt"] for i in cfgs]
    assert len(names) == len(set(names)) == len(cfgs)
    prim = sel["freeze"]["primary"]["attempt"]
    prim_labels = [i["labels"] for i in cfgs if i["attempt"]["attempt"] == prim][0]
    assert "primary" in prim_labels
    assert sum("primary" in i["labels"] for i in cfgs) == 1
    assert all(i["attempt"]["attempt"].rsplit("__t", 1)[0] in keys for i in cfgs)


# ------------------------------------------------------------------ stage G


def _stage_g_m1_csv(csv_path: Path, ticker: str, entries) -> None:
    lines = ["ticker,figi,interval,ts,open,high,low,close,volume"]
    for dts, is_dip in entries:
        for m in range(40, -1, -1):
            ts = dts - timedelta(minutes=m)
            if m >= 31:
                close = 100.0
            elif m == 0:
                close = 500.0
            else:
                close = 99.0 if is_dip else 101.0
            lines.append(f"{ticker},FIGI,1,{ts.isoformat()},{close},{close},"
                         f"{close},{close},1")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stage_g_fixture(tmp_path: Path):
    import pandas as pd

    repo = tmp_path / "repo"
    out = tmp_path / "run_out"
    csv_dir = repo / rdb.REL_SNAPSHOT / "csv"
    csv_dir.mkdir(parents=True)
    (out / "predictions").mkdir(parents=True)
    (out / "ledgers").mkdir(parents=True)

    tickers = [f"T{i}" for i in range(6)]
    dates = [(datetime(2026, 6, 1, tzinfo=UTC) + timedelta(days=d))
             for d in range(44)]
    decision = [d.replace(hour=7, minute=0).isoformat() for d in dates]
    rows = []
    for di, dts in enumerate(decision):
        for ti, tkr in enumerate(tickers):
            y = (ti + 0.5) * 1e-3 - 1e-3
            p = 5e-4 + ti * 1e-4 + (di % 3) * 1e-5
            if ti == 0 and di < 4:
                p = -1e-3
            rows.append({
                "row_id": f"{di}_{ti}", "ticker": tkr, "decision_ts": dts,
                "st30": "FILLED", "st60": "FILLED", "st120": "FILLED",
                "n5230": y, "n5260": y, "n52120": y,
                "p__ridge__cont5__h30": p, "p__ridge__cont5__h60": p,
                "p__ridge__cont5__h120": p})
    df = pd.DataFrame(rows)
    df.to_parquet(out / "predictions" / "val.parquet", index=False)
    df.to_parquet(out / "predictions" / "eval.parquet", index=False)

    entries = [(datetime.fromisoformat(dts), di % 2 == 0)
               for di, dts in enumerate(decision)]
    for tkr in tickers:
        _stage_g_m1_csv(csv_dir / f"{tkr}__m1.csv", tkr, entries)

    facts = {"n_signal": 260, "n_executed": 1, "n_accepted_open": 0,
             "n_ignore_repeat": 259, "n_no_slot": 0, "n_no_cash": 0,
             "n_skip": 0}
    tag = "primary+best_ridge+best_cont5"
    (out / "ledgers" / f"{tag}__c5_s2__executed.csv").write_text(
        "row_id,gross,net,fees,slip\n4_1,0.001,0.0005,0,0\n", encoding="utf-8")
    (out / "ledgers" / f"{tag}__c5_s2__open.csv").write_text(
        "row_id\n", encoding="utf-8")
    primary = {"attempt": "ridge__cont5__h60__t0p0000",
               "fit": "ridge__cont5__h60", "model": "ridge",
               "feature_set": "cont5", "horizon": 60, "threshold": 0.0}
    sel = {"primary": primary}
    eval_out = {"results": [{"cost_id": "c5_s2",
                             "labels": ["primary+best_ridge+best_cont5"],
                             "config": tag, "facts": facts}]}
    return repo, out, sel, eval_out


def test_ret30_window_edge_regression_and_entry_bar(tmp_path):
    csv_path = tmp_path / "A__m1.csv"
    decision = datetime(2026, 3, 2, 10, 20, tzinfo=UTC)
    lines = ["ticker,figi,interval,ts,open,high,low,close,volume"]
    for m in range(31, 0, -1):
        ts = decision - timedelta(minutes=m)
        close = 100.0 if m == 31 else 101.0
        lines.append(f"A,F,1,{ts.isoformat()},{close},{close},{close},"
                     f"{close},1")
    lines.append(f"A,F,1,{decision.isoformat()},500,500,500,500,1")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ts_np, close_np = rdb._m1_close_arrays(csv_path)
    got = rdb._ret30(ts_np, close_np, decision.isoformat())
    assert got == pytest.approx(101.0 / 100.0 - 1.0, abs=1e-12)
    edge = rdb._ret30(ts_np, close_np, decision.isoformat(), window_min=31)
    assert edge is None


def test_ret30_none_without_baseline(tmp_path):
    csv_path = tmp_path / "B__m1.csv"
    decision = datetime(2026, 3, 2, 10, 20, tzinfo=UTC)
    lines = ["ticker,figi,interval,ts,open,high,low,close,volume"]
    for m in range(5, 0, -1):
        ts = decision - timedelta(minutes=m)
        lines.append(f"B,F,1,{ts.isoformat()},101,101,101,101,1")
    csv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ts_np, close_np = rdb._m1_close_arrays(csv_path)
    assert rdb._ret30(ts_np, close_np, decision.isoformat()) is None


def test_cs_ic_ranks_within_slot_and_skips_degenerate():
    import pandas as pd

    pos_ts = "2026-06-01T10:00:00+00:00"
    neg_ts = "2026-06-02T10:00:00+00:00"
    deg_ts = "2026-06-03T10:00:00+00:00"
    inc_ts = "2026-06-04T10:00:00+00:00"
    small_ts = "2026-06-05T10:00:00+00:00"
    rows = []
    for i in range(6):
        rows.append({"decision_ts": pos_ts, "p__ridge__cont5__h60": 0.1 + i,
                     "n5260": 1.0 + i, "st60": "FILLED"})
        rows.append({"decision_ts": neg_ts, "p__ridge__cont5__h60": 0.1 + i,
                     "n5260": 10.0 - i, "st60": "FILLED"})
        rows.append({"decision_ts": deg_ts, "p__ridge__cont5__h60": 0.5,
                     "n5260": 1.0 + i, "st60": "FILLED"})
        rows.append({"decision_ts": inc_ts, "p__ridge__cont5__h60": 0.1 + i,
                     "n5260": 1.0 + i, "st60": "INCOMPLETE"})
    for i in range(3):
        rows.append({"decision_ts": small_ts, "p__ridge__cont5__h60": 0.1 + i,
                     "n5260": 1.0 + i, "st60": "FILLED"})

    r = rdb._slot_spearman(pd.DataFrame(rows), "ridge__cont5__h60", 60)
    assert r["n_slots"] == 2
    assert r["mean_ic"] == pytest.approx(0.0, abs=1e-6)
    assert r["frac_positive"] == pytest.approx(0.5)
    assert r["t_stat"] == pytest.approx(0.0, abs=1e-6)


def test_dip_chase_groups_spearman_and_sign():
    import pandas as pd

    rows = [
        {"ret30": -0.02, "st60": "FILLED", "n5260": 0.003},
        {"ret30": -0.01, "st60": "FILLED", "n5260": 0.002},
        {"ret30": 0.01, "st60": "FILLED", "n5260": -0.002},
        {"ret30": 0.02, "st60": "FILLED", "n5260": -0.003},
        {"ret30": 0.05, "st60": "SKIP", "n5260": 0.01},
        {"ret30": float("nan"), "st60": "FILLED", "n5260": 0.001},
    ]
    r = rdb._dip_chase(pd.DataFrame(rows), "st60", "n5260")
    assert r["n_rows"] == 6
    assert r["n_with_ret30_known"] == 4
    assert r["dip"]["n"] == 2 and r["chase"]["n"] == 2
    assert r["dip"]["mean_net"] == pytest.approx(0.0025)
    assert r["chase"]["mean_net"] == pytest.approx(-0.0025)
    assert r["dip"]["frac_positive"] == pytest.approx(1.0)
    assert r["dip_mean_gt_chase_mean"] is True
    assert r["spearman_ret30_net"]["n"] == 4
    assert r["spearman_ret30_net"]["rho"] == pytest.approx(-1.0)
    assert r["spearman_ret30_net"]["p_value"] == pytest.approx(0.0, abs=1e-6)


def test_selected_frame_and_accounting_exact():
    import pandas as pd

    df = pd.DataFrame({
        "row_id": ["r1", "r2", "r3", "r4", "r5", "r6"],
        "p__ridge__cont5__h60": [0.1, 0.05, -0.1, 0.2, 0.0, -0.03],
    })
    primary = {"fit": "ridge__cont5__h60", "threshold": 0.0, "horizon": 60}
    sel = rdb._selected_frame(df, primary, {"r1"}, set())
    assert list(sel["row_id"]) == ["r1", "r2", "r4"]
    assert sel["position"].tolist() == ["executed", "blocked", "blocked"]

    facts = {"n_signal": 3, "n_executed": 1, "n_accepted_open": 0,
             "n_ignore_repeat": 1, "n_no_slot": 0, "n_no_cash": 1,
             "n_skip": 0}
    acc = rdb._accounting_checks(len(sel), facts)
    assert acc["selected_matches_n_signal"] is True
    assert acc["accounting_sums_to_signal"] is True
    acc2 = rdb._accounting_checks(len(sel), dict(facts, n_signal=4))
    assert acc2["selected_matches_n_signal"] is False
    acc3 = rdb._accounting_checks(len(sel), dict(facts, n_ignore_repeat=0))
    assert acc3["accounting_sums_to_signal"] is False


def test_stage_g_end_to_end(tmp_path):
    repo, out, sel, eval_out = _stage_g_fixture(tmp_path)
    run = rdb.Run(out)

    res = rdb.stage_g_diagnostics(repo, run, sel, eval_out, False)
    assert res["checks_failed"] == []
    assert (out / "stage_g_summary.json").exists()
    assert (out / "DIAGNOSTICS.md").exists()

    acc = res["repeat_accounting"]
    assert acc["n_selected_recomputed"] == 260
    assert acc["selected_matches_n_signal"] is True
    assert acc["accounting_sums_to_signal"] is True
    assert acc["positions"] == {"blocked": 259, "executed": 1}
    assert acc["blocked"]["n_known"] == 259
    assert acc["blocked"]["n_dates"] == 44
    assert acc["blocked"]["mean_net_known"] > 0
    assert acc["blocked_mean_gt_executed"] is True

    ic = res["cs_ic"]["val"]["h60"]
    assert ic["n_slots"] == 44
    assert ic["mean_ic"] == pytest.approx(1.0, abs=1e-6)
    assert res["cs_ic"]["eval"]["h120"]["n_slots"] == 44

    dc_eval = res["dip_chase"]["eval"]
    assert dc_eval["ret30_coverage"] == pytest.approx(1.0)
    assert dc_eval["all"]["dip"]["n"] > 0
    assert dc_eval["all"]["chase"]["n"] > 0
    assert res["dip_chase"]["val"]["ret30_coverage"] == pytest.approx(1.0)
    assert res["h2_executed_ledger"]["dip"]["n"] == 1
    assert res["h2_executed_ledger"]["dip"]["mean_gross"] == pytest.approx(0.001)

    assert res["prereg"]["h3_repeat_blocked"] is True
    assert res["prereg"]["h1_cs_ic"] is False
    assert res["prereg"]["h2_no_chase"] is False

    res2 = rdb.stage_g_diagnostics(repo, run, sel, eval_out, True)
    on_disk = json.loads((out / "stage_g_summary.json").read_text())
    assert res2 == on_disk
    md = (out / "DIAGNOSTICS.md").read_text(encoding="utf-8")
    assert "H1 · CS-IC" in md and "H3 · repeat" in md
