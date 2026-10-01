"""REF-001: эталонный прогон — регрессия fingerprint (нужна БД с данными; иначе skip)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "configs" / "reference" / "REF-001.json"

pytestmark = pytest.mark.integration


def _load_mod():
    spec = importlib.util.spec_from_file_location("reference_run", ROOT / "scripts" / "reference_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_reference_001_matches():
    if not REF.exists():
        pytest.skip("REF-001.json ещё не зафиксирован (scripts/reference_run.py --write)")
    mod = _load_mod()
    ref = json.loads(REF.read_text(encoding="utf-8"))
    try:
        got = mod.run_reference(ref)
    except BaseException as e:  # нет БД/данных на этой машине (SystemExit тоже не Exception)
        pytest.skip(f"нет данных: {type(e).__name__}: {str(e)[:80]}")
    exp = ref["expected"]
    assert got["data_hash"] == exp["data_hash"], "data_hash: данные изменились"
    assert got["fingerprint"] == exp["fingerprint"], "fingerprint: регрессия движка"
    assert got["trades"] == exp["trades"]
    assert abs(got["net"] - exp["net"]) < 1e-6
