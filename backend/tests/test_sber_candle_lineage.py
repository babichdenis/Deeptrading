"""A3: тесты инструмента audit_sber_candle_lineage.

Только офлайн: нет БД, нет брокера, нет сети. Покрывают функции аудит-скрипта
(фингерпринт, структурный гейт, jump, кадено, анализ снимка, хэши секций,
SQL без секретов, состав колонок модели). Это тесты ИНСТРУМЕНТА, а не
доказательство происхождения бара: происхождение 1.0-строки NOT_VERIFIED,
дефект-генератор в коде не воспроизведён (см. report ARCH_20261008_B1_A3.md).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import audit_sber_candle_lineage as ascl

T0 = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _ts_list(minutes_step: int, n: int):
    return [{'ts': (T0 + timedelta(minutes=minutes_step * i)).isoformat()} for i in range(n)]


def test_placeholder_fingerprint_positives():
    assert ascl.is_flat_one_volzero(1.0, 1.0, 1.0, 1.0, 0)
    assert ascl.is_flat_one_volzero("1", "1.0", 1, "1", "0")


def test_placeholder_fingerprint_negatives():
    assert not ascl.is_flat_one_volzero(280.0, 280.0, 280.0, 280.0, 0)
    assert not ascl.is_flat_one_volzero(1.0, 1.0, 1.0, 1.0, 5)
    assert not ascl.is_flat_one_volzero(None, 1.0, 1.0, 1.0, 0)
    assert not ascl.is_flat_one_volzero("x", 1.0, 1.0, 1.0, 0)


def test_structural_ok_passes_placeholder():
    assert ascl.structural_ok(1.0, 1.0, 1.0, 1.0)


def test_jump_gate_rejects_280_to_1():
    j = ascl.jump_ratio(280.0, 1.0)
    assert j is not None and j > 0.40


def test_jump_gate_accepts_normal_tick():
    j = ascl.jump_ratio(280.0, 280.22)
    assert j is not None and j < 0.001


def test_cadence_5min_grid():
    c = ascl.cadence_hist(_ts_list(5, 10))
    assert c == {5: 9}


def test_cadence_continuous_1min():
    c = ascl.cadence_hist(_ts_list(1, 10))
    assert c == {1: 9}


def test_cadence_matches_observed_window():
    rows = [{'ts': (T0 + timedelta(minutes=m)).isoformat()}
            for m in (31, 36, 41, 46, 47, 51, 56, 61, 66)]
    assert ascl.cadence_hist(rows) == {1: 1, 4: 1, 5: 6}


def test_snapshot_analysis_flags_anomaly(tmp_path):
    rows = [
        {"ts": "2026-09-08 12:41:00+00:00", "open": "280.12000000", "high": "280.12000000",
         "low": "280.11000000", "close": "280.11000000", "volume": 5325},
        {"ts": ascl.ANOMALY["ts"], "open": "1.00000000", "high": "1.00000000",
         "low": "1.00000000", "close": "1.00000000", "volume": 0},
        {"ts": "2026-09-08 12:51:00+00:00", "open": "280.21000000", "high": "280.22000000",
         "low": "280.09000000", "close": "280.22000000", "volume": 20468},
    ]
    p = tmp_path / "snapshot_candles.json"
    p.write_text(json.dumps(rows))
    a = ascl.snapshot_analysis(p)
    assert a["total"] == 3
    assert len(a["placeholder_like"]) == 1
    assert a["placeholder_like"][0]["ts"] == ascl.ANOMALY["ts"]


def test_model_has_no_provenance_column():
    from app.models.candle import Candle
    cols = {c.name for c in Candle.__table__.columns}
    assert "source" not in cols
    assert "created_at" not in cols
    assert cols == {"id", "figi", "interval", "ts", "open", "high", "low", "close", "volume"}


def test_section_hash_deterministic_and_stable():
    a = {"window": [{"ts": "2026-09-08 12:47:00+00:00", "open": "1.0"}]}
    assert ascl.section_sha256(a) == ascl.section_sha256(a)
    assert ascl.section_sha256(a) == ascl.section_sha256(
        {"window": [{"ts": "2026-09-08 12:47:00+00:00", "open": "1.0"}]})
    assert len(ascl.section_sha256(a)) == 64


def test_sql_queries_no_secrets():
    joined = "\n".join(ascl.SQL_QUERIES.values()).lower()
    assert "password" not in joined
    assert "postgresql://" not in joined
    assert ascl.SQL_QUERIES["window_1m"].count("$") == 3


def test_sql_queries_cover_all_probes():
    assert set(ascl.SQL_QUERIES) == {
        "window_1m", "window_interval5", "flat_one_census",
        "interval_counts_day", "single_5m_row_day", "integrity_sber"}
def test_fact_table_depends_on_input():
    f1 = ascl.Findings()
    ft1 = ascl.fact_table(f1)
    joined = json.dumps(ft1, default=str, ensure_ascii=False)
    assert "9 баров" not in joined
    assert "подтверждён" not in joined
    unknowns = [x for x in ft1 if x["kind"] == "unknown"]
    assert unknowns, "пустой вход должен давать unknown-факты, а не наблюдения"
    assert not any(x["kind"] == "observed" and x["source"].startswith("probe_db")
                   and x["source"] != "probe_db.window_rows cadence" and x["value"] not in (None, {})
                   for x in ft1)
    f2 = ascl.Findings(
        window_rows=[{"ts": "2026-09-08T12:45:00Z"}, {"ts": "2026-09-08T12:50:00Z"}],
        interval_counts_day={1: 9},
    )
    ft2 = ascl.fact_table(f2)
    c1 = next(x for x in ft1 if x["source"] == "probe_db.window_rows cadence")
    c2 = next(x for x in ft2 if x["source"] == "probe_db.window_rows cadence")
    assert c1["kind"] == "unknown" and c2["kind"] == "observed"
    assert c2["value"] == {5: 1}
    k1 = next(x for x in ft1 if x["source"] == "probe_db.interval_counts_day")
    k2 = next(x for x in ft2 if x["source"] == "probe_db.interval_counts_day")
    assert k1["kind"] == "unknown" and k2["kind"] == "observed" and k2["value"] == {1: 9}


def test_infer_depends_on_input():
    i0 = ascl.infer(None)
    assert "9 баров" not in json.dumps(i0, default=str, ensure_ascii=False)
    assert i0["selection_evidence"]["interval_counts_day"].startswith("unknown")
    assert i0["broken_cadence"].startswith("unknown")
    ft = ascl.fact_table(ascl.Findings(
        window_rows=[{"ts": "2026-09-08T12:45:00Z"}, {"ts": "2026-09-08T12:50:00Z"}],
        interval_counts_day={1: 9},
    ))
    i1 = ascl.infer(ft)
    assert i1["selection_evidence"]["interval_counts_day"] == {1: 9}
    assert i1["broken_cadence"].startswith("observed cadence")
    assert i0["selection_evidence"] != i1["selection_evidence"]


def test_composition_fact_table_to_infer_empty():
    inf = ascl.infer(ascl.fact_table(ascl.Findings()))
    assert inf["broken_cadence"].startswith("unknown")
    assert "согласуется с внешней" not in inf["broken_cadence"]


def test_composition_single_timestamp_not_sparsity():
    ft = ascl.fact_table(ascl.Findings(
        window_rows=[{"ts": "2026-09-08T12:47:00Z"}]))
    inf = ascl.infer(ft)
    assert "insufficient" in inf["broken_cadence"]
    assert "согласуется с внешней" not in inf["broken_cadence"]


def test_composition_continuous_1m_no_sparsity_claim():
    rows = _ts_list(1, 12)
    inf = ascl.infer(ascl.fact_table(ascl.Findings(window_rows=rows)))
    assert "continuous 1m cadence" in inf["broken_cadence"]
    assert "разреженность не наблюдается" in inf["broken_cadence"]
    assert "согласуется с внешней" not in inf["broken_cadence"]


def test_composition_sparse_derived_only_from_intervals():
    rows = [{'ts': (T0 + timedelta(minutes=m)).isoformat()}
            for m in (0, 5, 10, 15, 47)]
    inf = ascl.infer(ascl.fact_table(ascl.Findings(window_rows=rows)))
    assert inf["broken_cadence"].startswith("observed cadence")
    assert "из рассчитанных интервалов" in inf["broken_cadence"]
    assert "{1: 1" not in inf["broken_cadence"]


def test_queried_empty_is_known_empty_not_unknown():
    f = ascl.Findings(queried=True)
    ft = ascl.fact_table(f)
    probe = [x for x in ft if x["source"].startswith("probe_db")]
    assert probe and all(x["kind"] == "observed" for x in probe)
    assert all("known empty" in x["fact"] for x in probe)
    inf = ascl.infer(ft)
    assert "известная пустота" in inf["broken_cadence"] or "known empty" in inf["broken_cadence"]


def test_census_count_not_fake_rows():
    f = ascl.Findings(queried=True, flat_one_census_count=1)
    ft = ascl.fact_table(f)
    census = next(x for x in ft if x["source"] == "probe_db.flat_one_census")
    assert census["kind"] == "observed"
    assert census["value"]["count"] == 1
    assert census["value"]["rows"] in (None, [])


def test_sql_windowed():
    assert "ts>=$2" in ascl.SQL_CENSUS
    assert "ts>=$2" in ascl.SQL_COUNTS
    assert "ts>=$2" in ascl.SQL_SINGLE_5M_DAY
    assert "day>=$2" in ascl.SQL_INTEGRITY and "day<$3" in ascl.SQL_INTEGRITY


def test_probe_db_fetch_args_windowed():
    import asyncio

    calls = []

    class StubConn:
        async def fetch(self, sql, *args):
            calls.append((sql, args))
            return []

    f = asyncio.run(ascl.probe_db(StubConn(), "FIGI"))
    assert isinstance(f, ascl.Findings)
    assert len(calls) == 6
    w_args = (ascl.W0, ascl.W1)
    for sql, args in calls:
        assert args[0] == "FIGI"
        if sql == ascl.SQL_INTEGRITY:
            assert args[1:] == (ascl.DAY0, ascl.DAY1), "integrity должен получать суточный scope"
        else:
            assert args[1:] == w_args, f"candle-запрос должен получать (W0, W1): {args}"


def test_offline_transfer_from_facts_src(tmp_path):
    import hashlib
    import subprocess

    src_db_probe = {
        "window": [
            {"ts": "2026-09-08 12:45:00+00:00", "open": "280.1", "high": "280.2",
             "low": "280.0", "close": "280.1", "volume": 100},
            {"ts": "2026-09-08 12:47:00+00:00", "open": "1.0", "high": "1.0",
             "low": "1.0", "close": "1.0", "volume": 0},
        ],
        "day_interval5_in_window": 7,
        "flat_one_census": 1,
        "interval_counts_day": {"1": 9, "5": 8},
        "single_5m_row_day": [{"ts": "2026-09-08 12:45:00+00:00", "volume": 0}],
        "integrity_sber": [{"day": "2026-09-08", "n": 9}],
        "cadence_minutes_window": {"2": 1},
    }
    src_blob = {
        "subject": "fixture subject",
        "meta": {"figi": "FIGI", "window_utc": ["a", "b"]},
        "queries": ascl.SQL_QUERIES,
        "findings": {"snapshot": {"total": 2}, "db_probe": src_db_probe},
        "facts": [{"fact": "old fact", "kind": "observed", "value": 1}],
        "section_hashes": {"window_1m": "stale"},
        "inference": {"placeholder_origin_of_1_0_row": {"status": "NOT_VERIFIED"}},
    }
    src = tmp_path / "facts_src.json"
    src.write_text(json.dumps(src_blob, ensure_ascii=False, default=str))
    src_sha_before = hashlib.sha256(src.read_bytes()).hexdigest()

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    proc = subprocess.run(
        [sys.executable, str(Path(ascl.__file__)),
         "--facts-src", str(src), "--save", str(out_dir)],
        capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    outdir = Path(proc.stdout.strip().split("saved: ")[1])
    man = json.loads((outdir / "manifest.json").read_text())
    fac = json.loads((outdir / "facts.json").read_text())

    assert man["mode"] == "offline_transfer"
    assert man["sha256_script"] == hashlib.sha256(Path(ascl.__file__).read_bytes()).hexdigest()
    assert man["facts_src_sha256"] == src_sha_before
    assert man["sha256_facts"] == hashlib.sha256((outdir / "facts.json").read_bytes()).hexdigest()
    assert man.get("supersedes") == src.parent.name, "новый manifest описывает supersession источника"
    assert hashlib.sha256(src.read_bytes()).hexdigest() == src_sha_before, "источник не должен изменяться"

    assert fac["findings"]["db_probe"] == src_db_probe, "db_probe переносится дословно"
    assert fac["meta"]["figi"] == "FIGI", "исторический scope meta сохраняется"
    assert fac["meta"]["transfer"]["facts_src_sha256"] == src_sha_before

    def nsh(o):
        def n(x):
            if isinstance(x, dict):
                return {str(k) if isinstance(k, (int, float)) else k: n(v) for k, v in x.items()}
            if isinstance(x, list):
                return [n(v) for v in x]
            return x
        return hashlib.sha256(json.dumps(n(o), sort_keys=True, default=str,
                                         ensure_ascii=False).encode()).hexdigest()

    for key, section in [
        ("snapshot", fac["findings"]["snapshot"]),
        ("window_1m", fac["findings"]["db_probe"]["window"]),
        ("interval_counts_day", fac["findings"]["db_probe"]["interval_counts_day"]),
        ("single_5m_row_day", fac["findings"]["db_probe"]["single_5m_row_day"]),
        ("integrity_sber", fac["findings"]["db_probe"]["integrity_sber"]),
        ("cadence_minutes_window", fac["findings"]["db_probe"]["cadence_minutes_window"]),
        ("day_interval5_in_window", fac["findings"]["db_probe"]["day_interval5_in_window"]),
        ("flat_one_census", fac["findings"]["db_probe"]["flat_one_census"]),
    ]:
        assert fac["section_hashes"][key] == nsh(section), f"section_hash не сходится: {key}"

    assert fac["facts_src_verbatim"]["facts"] == src_blob["facts"], "исходные facts сохранены verbatim"
    assert any(x["kind"] == "observed" and x["source"] == "probe_db.flat_one_census"
               for x in fac["facts"]), "facts пересчитаны из перенесённого db_probe"
    assert "9 баров" not in json.dumps(fac, default=str, ensure_ascii=False)
