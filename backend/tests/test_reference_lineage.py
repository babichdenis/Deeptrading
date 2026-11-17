"""Версионирование эталона REF-001: архив прежней версии и происхождение.

Эталон — точка отсчёта регрессий. Его нельзя молча перезаписать: прежняя версия
обязана остаться на диске, а новая — ссылаться на неё и нести хеши входов.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reference_run as rr  # noqa: E402


def test_archive_keeps_previous_version(tmp_path: Path):
    ref = tmp_path / "REF-001.json"
    ref.write_text(json.dumps({"id": "REF-001", "expected": {"bars": 1}}), encoding="utf-8")

    rel = rr._archive(ref, stamp="20260101T000000Z")

    assert rel is not None
    saved = tmp_path / rel
    assert saved.exists(), "прежняя версия должна остаться в archive/"
    assert json.loads(saved.read_text(encoding="utf-8"))["expected"]["bars"] == 1
    assert ref.exists(), "сам эталон при архивировании не удаляется"


def test_archive_of_missing_file_is_noop(tmp_path: Path):
    assert rr._archive(tmp_path / "REF-001.json", stamp="20260101T000000Z") is None


def test_archive_names_do_not_collide(tmp_path: Path):
    ref = tmp_path / "REF-001.json"
    ref.write_text("{}", encoding="utf-8")
    first = rr._archive(ref, stamp="20260101T000000Z")
    second = rr._archive(ref, stamp="20260102T000000Z")
    assert first != second
    assert len(list((tmp_path / "archive").glob("REF-001-*.json"))) == 2


def test_strategy_hash_tracks_inputs():
    base = {"strategy": {"engine": "rsi_trade_hub", "params": {}},
            "exit": {"policy": "signal_only"},
            "execution": {"qty": 1, "commission": 0.0005, "slippage_bps": 2.0}}
    other = json.loads(json.dumps(base))
    other["execution"]["commission"] = 0.001

    assert rr._strategy_hash(base) == rr._strategy_hash(base)
    assert rr._strategy_hash(base) != rr._strategy_hash(other), (
        "смена комиссии обязана менять хеш входов"
    )


@pytest.mark.parametrize("payload", [
    {"strategy": {"engine": "rsi_trade_hub", "params": {"period": 14}},
     "exit": {"policy": "signal_only"}, "execution": {"qty": 1}},
    {"strategy": {"engine": "rsi_trade_hub", "params": {}},
     "exit": {"policy": "atr"}, "execution": {"qty": 1}},
])
def test_strategy_hash_varies(payload):
    base = {"strategy": {"engine": "rsi_trade_hub", "params": {}},
            "exit": {"policy": "signal_only"},
            "execution": {"qty": 1, "commission": 0.0005, "slippage_bps": 2.0}}
    assert rr._strategy_hash(base) != rr._strategy_hash(payload)
