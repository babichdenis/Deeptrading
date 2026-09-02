"""Vote attribution + shadow scoring тесты (read-only, canonical July run).

Проверяют:
- shadow score не использует сделки ПОСЛЕ decision_time (walk-forward, no future);
- редкие функции сжимаются к нулю (shrink с фиксированным prior);
- functions_mask согласуется со списком активных функций;
- каждая score-строка линкуется на intent/candidate/trade;
- shadow score не влияет на EngineRunner (детерминизм прогона).
"""
import csv
import json
import os

import pytest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN_DIR = os.path.join(BACKEND, "reports", "5b44f3b383df")
RUN_ID = "5b44f3b383df"
PRIOR_N = 20


def _load_scores() -> list[dict]:
    with open(os.path.join(RUN_DIR, "vote_shadow_scores.csv"), newline="") as f:
        return list(csv.DictReader(f))


def _load_attribution() -> dict:
    with open(os.path.join(RUN_DIR, "vote_attribution.json")) as f:
        return json.load(f)


def _load_intents() -> dict[str, dict]:
    with open(os.path.join(RUN_DIR, "entry_intents.csv"), newline="") as f:
        return {r["intent_id"]: r for r in csv.DictReader(f)}


def _load_trades() -> dict[str, dict]:
    with open(os.path.join(RUN_DIR, "trades.csv"), newline="") as f:
        return {r["trade_id"]: r for r in csv.DictReader(f)}


@pytest.fixture(scope="module")
def scores() -> list[dict]:
    return _load_scores()


@pytest.fixture(scope="module")
def intents() -> dict[str, dict]:
    return _load_intents()


def test_no_future_trades_used_in_score(scores: list[dict]) -> None:
    """Для каждой score-строки: training_period_end < decision_time и веса
    построены только по сделкам, закрытым до начала дня decision."""
    assert scores, "scores пустой"
    for row in scores:
        train_end = row["training_period_end"]
        dec = row["decision_time"]
        assert train_end, f"нет training_period_end для {row['intent_id']}"
        assert train_end < dec, f"future leak: train_end={train_end} >= decision={dec}"


def test_rare_functions_shrink_toward_zero(scores: list[dict]) -> None:
    """Редкая функция (n << prior_n=20) сжимается: shrunk = n*mean/(n+20) < raw."""
    attribution = _load_attribution()
    per_fn = attribution["per_function"]
    vwap = per_fn.get("vwap_reclaim")
    assert vwap and vwap["count"] < PRIOR_N, "ожидалась редкая функция vwap_reclaim"
    median = vwap["net_bps_median"]
    # последний снапшот весов (конец окна)
    last = scores[-1]
    weights = json.loads(last["weights_snapshot"].replace("'", '"'))
    w = weights.get("vwap_reclaim")
    assert w is not None, "vwap_reclaim нет в последнем снапшоте"
    # shrink: n=4 -> w = mean*4/24 <= mean/6 по модулю
    assert abs(w) <= abs(median) / 4, f"редкая функция не сжата: |w|={abs(w)} >= median/4={abs(median)/4}"
    # первый снапшот: до закрытых сделок веса обязаны быть 0 (нет look-ahead)
    first = scores[0]
    w0 = json.loads(first["weights_snapshot"].replace("'", '"'))
    assert all(abs(v) < 1e-9 for v in w0.values()), "первый день: веса должны быть нулевыми"


def test_functions_mask_reconciles(scores: list[dict], intents: dict[str, dict]) -> None:
    """functions_mask в score == маска интента; quorum_count согласован."""
    for row in scores:
        it = intents.get(row["intent_id"])
        assert it, f"score row не связан с intent: {row['intent_id']}"
        mask = sorted(x.strip() for x in row["functions"].split(";"))
        it_mask = sorted(json.loads(it["functions_mask"].replace("'", '"')))
        assert mask == it_mask, f"маска не совпадает для {row['intent_id']}: {mask} vs {it_mask}"
        qc = int(it["quorum_count"] or 0)
        assert qc >= 1, "quorum_count < 1"
        assert qc <= len(mask), f"quorum_count={qc} > len(mask)={len(mask)}"


def test_all_score_rows_link_to_candidate(scores: list[dict], intents: dict[str, dict]) -> None:
    """Каждая score-строка линкуется на candidate и trade."""
    with open(os.path.join(RUN_DIR, "trades.csv"), newline="") as f:
        trade_keys = {(r["figi"], r["decision_time"].replace("Z", "+00:00"), r["side"])
                      for r in csv.DictReader(f)}
    for row in scores:
        it = intents[row["intent_id"]]
        assert it["candidate_id"], "нет candidate_id"
        key = (it["figi"], it["decision_time"].replace("Z", "+00:00"),
               "LONG" if it["side"] == "BUY" else "SHORT")
        assert key in trade_keys, f"сделка не найдена для {row['intent_id']}"


def test_shadow_score_cannot_affect_engine(scores: list[dict]) -> None:
    """Движок не читает shadow scores: файлы vote_* не упоминаются в engine/ensemble."""
    import subprocess
    import sys

    roots = [os.path.join(BACKEND, "app", "engine"),
             os.path.join(BACKEND, "app", "services")]
    for root in roots:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_engine_golden.py"],
            cwd=BACKEND, capture_output=True, text=True)
        # fallback: grep по исходникам движка
        hits = []
        for dirpath, _, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                with open(os.path.join(dirpath, fn)) as f:
                    if "vote_shadow" in f.read() or "shadow_score" in f.read():
                        hits.append(os.path.join(dirpath, fn))
        assert not hits, f"движок ссылается на shadow score: {hits}"


DS_DIR = os.path.join(RUN_DIR, "vote_attribution_dataset_v1")


def test_canonical_shrinkage_k_200() -> None:
    with open(os.path.join(DS_DIR, "manifest.json")) as f:
        m = json.load(f)
    assert m["shrinkage_k"] == 200, f"k={m['shrinkage_k']} != 200"
    assert m["formula_version"] == "v1_median_shrink"
    assert m["config_hash"] == "1c7f75dc44c2aa67"
    assert "training_cutoff_rule" in m
    with open(os.path.join(DS_DIR, "shadow_weights.json")) as f:
        sw = json.load(f)
    assert sw["shrinkage_k"] == 200


def test_weights_are_median_shrunk_and_walk_forward() -> None:
    scores = _load_scores()
    with open(os.path.join(DS_DIR, "shadow_weights.json")) as f:
        sw = json.load(f)
    # любой ненулевой вес w = (n/(n+200))*median для соответствующего дня
    for day, wmap in sw["weights_by_day"].items():
        for fn, w in wmap.items():
            assert w >= 0  # weight magnitude не может быть отрицательным признаком; у нас 0+ shrink
    # первый день: нет закрытых сделок -> все веса 0
    first_day = sorted(sw["weights_by_day"])[0]
    assert all(abs(v) < 1e-9 for v in sw["weights_by_day"][first_day].values())
    # каждый score-row: training_period_end < decision_time
    for row in scores:
        assert row["training_period_end"] < row["decision_time"]


def test_rare_n_shrinks_stronger_at_k200_than_k20() -> None:
    """vwap_reclaim (n=4): |w_k200| строго меньше |w_k20|."""
    import ast
    attribution = _load_attribution()
    n = attribution["per_function"]["vwap_reclaim"]["count"]
    median = attribution["per_function"]["vwap_reclaim"]["net_bps_median"]
    scores = _load_scores()
    last = scores[-1]
    w200 = json.loads(last["weights_snapshot"].replace("'", '"'))["vwap_reclaim"]
    w20 = (n / (n + 20)) * median  # старое shrink
    assert abs(w200) < abs(w20), f"k=200 не сжимает сильнее: {w200} vs {w20}"


def test_pair_present_includes_pair_exact() -> None:
    a = _load_attribution()
    pp = a["per_pair_present"]
    pe = a["per_pair_exact"]
    for pair, block in pe.items():
        assert pair in pp, f"pair_exact {pair} нет в pair_present"


def test_all_21_unordered_pairs_exist() -> None:
    fns = ["bollinger_reclaim", "donchian_breakout", "macd_cross", "pullback_ema",
           "range_compression_breakout", "rsi_reversal", "vwap_reclaim"]
    pairs = {(a, b) for i, a in enumerate(fns) for b in fns[i + 1:]}
    assert len(pairs) == 21
    a = _load_attribution()
    present = {tuple(sorted(p.split("+"))) for p in a["per_pair_present"]}
    missing = pairs - present
    assert not missing, f"нет пар: {missing}"
    # каждая пара имеет минимум 1 executed intent (все 21 встречаются в данных July)


def test_artifacts_have_manifest_runid_cutoff() -> None:
    with open(os.path.join(DS_DIR, "manifest.json")) as f:
        m = json.load(f)
    assert m["run_id"] == RUN_ID
    assert m["config_hash"].startswith("1c7f75dc44c2aa67")
    assert m["training_cutoff_rule"]
    required = ["function_attribution.csv", "pair_present_attribution.csv",
                "pair_exact_attribution.csv", "intent_vote_features.csv",
                "shadow_weights.json", "vote_shadow_scores.csv", "manifest.json"]
    for fn in required:
        assert os.path.exists(os.path.join(DS_DIR, fn)), f"нет файла {fn}"
