"""Тесты чистого снапшота макро-рядов: дедупликация, порядок, конфликты."""
from __future__ import annotations

import hashlib
import os

from scripts.rebuild_macro_snapshot import process


def _write(path: str, lines: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def _sha(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def test_drops_exact_duplicates_and_sorts(tmp_path) -> None:
    src = tmp_path / "brent_1m.csv"
    _write(str(src), [
        "2026-01-27T12:48:00Z;64.96;64.96;64.96;64.96;1;",
        "2026-01-27T12:30:00Z;64.65;64.63;64.63;64.65;5;",
        "2026-01-27T12:48:00Z;64.96;64.96;64.96;64.96;1;",
    ])
    out = tmp_path / "out.csv"
    info = process(str(src), str(out), [])
    assert info["rows_in"] == 3
    assert info["rows_out"] == 2
    assert info["exact_duplicates_dropped"] == 1
    assert info["conflicts_resolved"] == 0
    with open(out, encoding="utf-8") as handle:
        stamps = [line.split(";")[0] for line in handle if line.strip()]
    assert stamps == ["2026-01-27T12:30:00Z", "2026-01-27T12:48:00Z"]


def test_conflict_keeps_larger_volume_and_is_reported(tmp_path) -> None:
    src = tmp_path / "gold_5m.csv"
    _write(str(src), [
        "2026-02-02T10:00:00Z;4000.0;4001.0;4002.0;3999.0;3;",
        "2026-02-02T10:00:00Z;4000.0;4001.0;4002.0;3999.0;77;",
    ])
    out = tmp_path / "out.csv"
    rows: list[dict] = []
    info = process(str(src), str(out), rows)
    assert info["conflicts_resolved"] == 1
    assert info["exact_duplicates_dropped"] == 0
    assert len(rows) == 1
    assert rows[0]["rule"] == "larger_volume"
    assert rows[0]["ts"] == "2026-02-02T10:00:00Z"
    with open(out, encoding="utf-8") as handle:
        text = handle.read().strip()
    assert text.endswith(";77;")


def test_source_file_is_not_modified(tmp_path) -> None:
    src = tmp_path / "usdrub_1m.csv"
    _write(str(src), [
        "2026-03-03T08:00:00Z;80000.0;80010.0;80020.0;79990.0;10;",
        "2026-03-03T08:00:00Z;80000.0;80010.0;80020.0;79990.0;10;",
    ])
    before = _sha(str(src))
    process(str(src), str(tmp_path / "out.csv"), [])
    assert _sha(str(src)) == before


def test_handles_figi_prefixed_rows(tmp_path) -> None:
    src = tmp_path / "old_format.csv"
    _write(str(src), [
        "BBG004730N88;2026-04-04T09:00:00Z;100.0;101.0;102.0;99.0;5;",
        "BBG004730N88;2026-04-04T09:00:00Z;100.0;101.0;102.0;99.0;5;",
        "BBG004730N88;2026-04-04T09:05:00Z;101.0;102.0;103.0;100.0;6;",
    ])
    out = tmp_path / "out.csv"
    info = process(str(src), str(out), [])
    assert info["rows_out"] == 2
    assert info["exact_duplicates_dropped"] == 1
    assert os.path.exists(out)
