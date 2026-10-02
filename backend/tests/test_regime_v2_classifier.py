"""Тесты Regime v2 C.3: RegimeObservation, rule-based классификатор, legacy-адаптер."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.services.regime_v2.classifier import (
    RegimeV2ClassifierParams,
    classify_row,
    derive_legacy,
)

TS = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def m(**kw):
    base = {
        "atr_pct": 0.5, "atr_percentile": 50.0, "vol_percentile": 50.0,
        "drift_atr": 0.0, "slope_atr": 0.0, "di_spread": 0.0,
        "er": 0.1, "consistency": 0.5, "range_atr": 1.0, "rv_atr": 1.0,
    }
    base.update(kw)
    return base


def test_h1_uptrend_is_trend_up():
    row = m(drift_atr=1.2, slope_atr=0.8, di_spread=15.0, er=0.6, vol_percentile=50.0)
    obs = classify_row(TS, 3600, row)
    assert obs.direction == "UP"
    assert obs.direction_strength > 0
    assert obs.structure == "TRENDING"
    assert obs.confidence > 0.5
    assert derive_legacy(obs) == "TREND_UP"


def test_h1_downtrend_is_trend_down():
    row = m(drift_atr=-1.0, slope_atr=-0.7, di_spread=-12.0, er=0.55)
    obs = classify_row(TS, 3600, row)
    assert obs.direction == "DOWN"
    assert derive_legacy(obs) == "TREND_DOWN"


def test_5m_direction_flat_even_on_uptrend():
    row = m(drift_atr=1.2, slope_atr=0.8, di_spread=15.0, er=0.6)
    obs = classify_row(TS, 300, row)
    assert obs.direction == "FLAT"
    assert obs.direction_strength == 0.0
    assert obs.structure == "TRANSITION"
    assert derive_legacy(obs) == "RANGE"


def test_direction_disagreement_returns_flat():
    row = m(drift_atr=0.6, slope_atr=-0.3, di_spread=0.0, er=0.1)
    obs = classify_row(TS, 3600, row)
    assert obs.direction == "FLAT"
    assert "direction_disagreement" in obs.reason_codes


def test_extreme_volatility_overrides_trend_in_legacy():
    row = m(drift_atr=1.2, slope_atr=0.8, di_spread=15.0, er=0.6, vol_percentile=95.0)
    obs = classify_row(TS, 3600, row)
    assert obs.volatility == "EXTREME"
    assert obs.structure == "TRENDING"
    assert derive_legacy(obs) == "HIGH_VOLATILITY"


def test_range_and_transition():
    quiet = classify_row(TS, 300, m(er=0.05, slope_atr=0.05))
    assert quiet.structure == "RANGE"
    assert derive_legacy(quiet) == "RANGE"
    mid = classify_row(TS, 300, m(er=0.3, slope_atr=0.2))
    assert mid.structure == "TRANSITION"
    assert "structure_transition" in mid.reason_codes
    assert derive_legacy(mid) == "RANGE"
    h1_mid = classify_row(TS, 3600, m(drift_atr=1.0, slope_atr=0.5, di_spread=10.0, er=0.2))
    assert h1_mid.direction == "UP"
    assert h1_mid.structure == "TRANSITION"
    assert derive_legacy(h1_mid) == "NEUTRAL"


def test_warmup_and_low_confidence():
    obs = classify_row(TS, 3600, m(atr_pct=None, vol_percentile=None))
    assert obs.confidence == 0.0
    assert obs.reason_codes == ("warmup",)
    assert derive_legacy(obs) == "NEUTRAL"
    low = replace(classify_row(TS, 3600, m()), confidence=0.1)
    assert derive_legacy(low) == "NEUTRAL"


def test_confidence_is_unambiguity_not_probability():
    row = m(drift_atr=1.0, slope_atr=0.9, di_spread=20.0, er=0.7, vol_percentile=50.0)
    obs = classify_row(TS, 3600, row)
    assert 0.0 <= obs.confidence <= 1.0
    d = obs.to_dict()
    assert d["direction"] == "UP"
    assert d["reason_codes"] == []
    assert d["version"] == "v2.0"
    assert isinstance(d["ts"], str)


def test_custom_params_flat_direction_tfs():
    row = m(drift_atr=1.2, slope_atr=0.8, di_spread=15.0, er=0.6)
    obs = classify_row(TS, 300, row, params=RegimeV2ClassifierParams(direction_tfs=(300, 3600)))
    assert obs.direction == "UP"
    assert derive_legacy(obs) == "TREND_UP"
