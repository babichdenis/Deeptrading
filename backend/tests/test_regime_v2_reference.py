"""Reference-label тест Regime v2 (C.6): эталонные окна real-данных, frozen fixture.

Фикстуру генерирует scripts/regime_v2_freeze_reference.py (read-only БД);
тест герметично реплеит бары и требует побитового совпадения вывода пайплайна
(measurements → classifier → hysteresis mild 2/1) с замороженным эталоном.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from app.engine.models import Candle
from app.services.regime_v2.hysteresis import RegimeV2State
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

FIXTURE = Path(__file__).parent / "fixtures" / "regime_v2_reference.json"
LABEL_FIELDS = ("structure", "direction", "volatility", "raw", "hyst")


def _load():
    if not FIXTURE.exists():
        pytest.skip(f"reference fixture отсутствует: {FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_reference_cases_present():
    data = _load()
    assert {"range_to_trend", "blip_in_trend", "trend_flip", "hv_to_trend"} <= set(data["cases"])


def test_reference_replay_exact():
    data = _load()
    window = data["meta"]["window"]
    for name, case in data["cases"].items():
        bars = [Candle(ts=datetime.fromisoformat(b["ts"]), open=b["open"], high=b["high"],
                       low=b["low"], close=b["close"], volume=b["volume"])
                for b in case["bars"]]
        ms = compute_measurements(bars, RegimeV2Params(window=window))
        state = RegimeV2State()
        got = []
        for m in ms:
            obs, raw, hyst = state.update(m["ts"], case["tf"], m)
            got.append({"ts": m["ts"].isoformat(), "structure": obs.structure,
                        "direction": obs.direction, "volatility": obs.volatility,
                        "raw": raw, "hyst": hyst, "confidence": obs.confidence})
        assert len(got) == len(case["expected"]), name
        for g, w in zip(got, case["expected"], strict=True):
            assert g["ts"] == w["ts"], name
            for f in LABEL_FIELDS:
                assert g[f] == w[f], (name, g["ts"], f, g[f], w[f])
            assert abs(g["confidence"] - w["confidence"]) < 1e-12, (name, g["ts"])
