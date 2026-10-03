"""Тесты C.5: hysteresis метки, sticky-структура, parity для каждого префикса."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.engine.models import Candle
from app.services.regime_v2.classifier import classify_row
from app.services.regime_v2.hysteresis import (
    HysteresisParams,
    LabelHysteresis,
    RegimeV2State,
)
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

TS = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)


def mrow(**kw):
    base = {"atr_pct": 0.5, "atr_percentile": 50.0, "vol_percentile": 50.0,
            "drift_atr": 0.0, "slope_atr": 0.0, "di_spread": 0.0,
            "er": 0.1, "consistency": 0.5, "range_atr": 1.0, "rv_atr": 1.0}
    base.update(kw)
    return base


def test_label_hysteresis_suppresses_single_blip():
    h = LabelHysteresis(HysteresisParams(min_tenure=3, confirm_bars=2))
    seq = ["RANGE"] * 5 + ["TREND_UP"] + ["RANGE"] * 2
    out = [h.update(c) for c in seq]
    assert out == ["RANGE"] * 8


def test_label_hysteresis_switches_after_confirmation():
    h = LabelHysteresis(HysteresisParams(min_tenure=3, confirm_bars=2))
    seq = ["RANGE"] * 4 + ["TREND_UP", "TREND_UP"] + ["RANGE"]
    out = [h.update(c) for c in seq]
    assert out[3] == "RANGE"
    assert out[4] == "RANGE"
    assert out[5] == "TREND_UP"
    assert out[6] == "TREND_UP"


def test_label_hysteresis_min_tenure_blocks_fast_switch():
    h = LabelHysteresis(HysteresisParams(min_tenure=4, confirm_bars=1))
    out = [h.update("RANGE")]
    out += [h.update("TREND_UP") for _ in range(4)]
    assert out[:4] == ["RANGE"] * 4
    assert out[4] == "TREND_UP"


def test_sticky_structure_keeps_trending():
    row = mrow(er=0.3, slope_atr=0.2, drift_atr=0.5, di_spread=5.0)
    stateless = classify_row(TS, 3600, row)
    sticky = classify_row(TS, 3600, row, prev_structure="TRENDING")
    assert stateless.structure == "TRANSITION"
    assert sticky.structure == "TRENDING"


def _bars(n=90):
    out = []
    price = 100.0
    for i in range(n):
        if 30 <= i < 55:
            price *= 1.0009
        elif 60 <= i < 70:
            price *= 0.998
        else:
            price *= 1.0 + (0.0004 if i % 3 else -0.0003)
        out.append(Candle(ts=TS + timedelta(minutes=5 * i), open=price,
                          high=price * 1.001, low=price * 0.999, close=price, volume=1000.0))
    return out


def test_prefix_parity_every_prefix_and_forming_clone():
    bars = _bars()
    ms = compute_measurements(bars, RegimeV2Params(window=12))
    full = RegimeV2State()
    rows = []
    for m in ms:
        obs, raw, label = full.update(m["ts"], 300, m)
        rows.append((obs.structure, raw, label))
    for k in range(1, len(ms) + 1):
        prefix_state = RegimeV2State()
        last = None
        for m in ms[:k]:
            last = prefix_state.update(m["ts"], 300, m)
        assert (last[0].structure, last[1], last[2]) == rows[k - 1], k
    n_before = full.n
    snap_rows = list(full.rows)
    full.evaluate(ms[-1]["ts"], 300, ms[-1])
    assert full.n == n_before
    assert full.rows == snap_rows
