"""Тесты чистых функций калибровочной диагностики режима (без БД, без runtime)."""
from __future__ import annotations

from datetime import UTC, datetime

from app.services.regime import RegimeDetector
from app.services.regime_calibration import (
    bar_range_ratio,
    classify_mixed,
    forward_outcome,
    future_class,
    percentile,
    rankdata,
    session_of,
    spearman,
    summarize_mixed,
    transition_stats,
    trend_conditions,
)


def feats(**kw):
    base = {"atr_pct": 0.5, "atr_percentile": 50.0, "ema_slope": 0.1, "adx": 15.0,
            "volume_ratio": 1.0, "drift_pct": 0.8, "consistency": 0.8}
    base.update(kw)
    return base


def test_percentile_linear_interpolation():
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 0) == 1
    assert percentile([1, 2, 3, 4], 100) == 4
    assert percentile([5], 90) == 5


def test_session_of_msk_windows():
    assert session_of(datetime(2026, 7, 1, 4, 0, tzinfo=UTC)) == "morning"
    assert session_of(datetime(2026, 7, 1, 8, 0, tzinfo=UTC)) == "day"
    assert session_of(datetime(2026, 7, 1, 17, 0, tzinfo=UTC)) == "evening"
    assert session_of(datetime(2026, 7, 1, 15, 50, tzinfo=UTC)) == "clearing"
    assert session_of(datetime(2026, 7, 4, 8, 0, tzinfo=UTC)) == "weekend"


def test_transition_stats_runs():
    st = transition_stats(["RANGE", "RANGE", "TREND_UP", "RANGE"])
    assert st["transitions"] == 2
    assert st["runs"] == 3
    assert st["singleton_runs"] == 2
    assert abs(st["singleton_share"] - 2 / 3) < 1e-9
    assert st["by_state"]["RANGE"]["total_bars"] == 3


def test_bar_range_ratio():
    assert bar_range_ratio(101.0, 99.0, 100.0, 0.5) == 4.0
    assert bar_range_ratio(101.0, 99.0, 100.0, None) is None
    assert bar_range_ratio(101.0, 99.0, 100.0, 0.0) is None


def test_trend_conditions_pass_sides():
    det = RegimeDetector()
    t = trend_conditions(feats(adx=20.0), det)
    assert t["up_pass"] is True
    assert t["dn_pass"] is False
    t2 = trend_conditions(feats(drift_pct=-0.8, ema_slope=-0.1, consistency=0.2, adx=20.0), det)
    assert t2["dn_pass"] is True
    assert t2["up_pass"] is False


def test_classify_mixed_up_blocked_only_by_adx():
    d = classify_mixed(feats(adx=15.0), 1.0, RegimeDetector())
    assert d["primary"] == "up"
    assert d["blockers"] == ["adx"]
    assert d["code"] == "up_adx"
    assert d["failed_up"] == ["adx"]


def test_classify_mixed_up_blocked_by_cons_and_adx():
    d = classify_mixed(feats(adx=15.0, consistency=0.6), 1.0, RegimeDetector())
    assert d["code"] == "up_cons_adx"
    assert set(d["failed_up"]) == {"cons", "adx"}


def test_classify_mixed_conflict_drift_vs_slope():
    d = classify_mixed(feats(drift_pct=0.8, ema_slope=-0.1), 1.0, RegimeDetector())
    assert d["primary"] == "none"
    assert d["code"] == "conflict_drift_up_slope_dn"


def test_classify_mixed_down_side():
    d = classify_mixed(
        feats(drift_pct=-0.8, ema_slope=-0.1, consistency=0.2, adx=15.0), 1.0,
        RegimeDetector())
    assert d["primary"] == "down"
    assert d["failed_dn"] == ["adx"]


def test_classify_mixed_down_blocked_by_cons_and_adx():
    d = classify_mixed(
        feats(drift_pct=-0.8, ema_slope=-0.1, consistency=0.5, adx=15.0), 1.0,
        RegimeDetector())
    assert d["primary"] == "down"
    assert set(d["failed_dn"]) == {"cons", "adx"}


def test_summarize_mixed_counts():
    det = RegimeDetector()
    diags = [
        classify_mixed(feats(adx=15.0), 1.0, det),
        classify_mixed(feats(adx=17.0), 1.0, det),
        classify_mixed(feats(drift_pct=0.8, ema_slope=-0.1), 1.0, det),
    ]
    s = summarize_mixed(diags)
    assert s["n"] == 3
    assert s["by_code"]["up_adx"] == 2
    assert s["by_code"]["conflict_drift_up_slope_dn"] == 1
    assert s["failed_up_conjuncts"]["adx"] == 3
    assert s["failed_up_conjuncts"]["slope"] == 1
    assert "range_drift_margin" in s["margins"]


def test_detector_defaults_unchanged():
    d = RegimeDetector()
    assert d.slope_threshold == 0.0002
    assert d.adx_threshold == 19.0
    assert d.atr_percentile_threshold == 78.0
    assert d.range_mult == 2.75
    assert d.drift_pct == 0.5
    assert d.drift_bars == 6
    assert d.cons_pct == 0.71
    assert d.cons_relax_pct == 0.66
    assert d.cons_bars == 6


def test_forward_outcome_exact_values():
    closes = [100.0, 101.0, 102.0, 101.0, 100.0]
    highs = [100.5, 101.5, 102.5, 101.5, 100.5]
    lows = [99.5, 100.5, 101.5, 100.5, 99.5]
    atr_pct = [1.0] * 5
    out = forward_outcome(closes, highs, lows, atr_pct, 0, 4)
    assert out is not None
    assert out["ret_pct"] == 0.0
    assert out["ret_atr"] == 0.0
    assert out["mfe_atr"] == 2.5
    assert out["mae_atr"] == -0.5
    assert out["er"] == 0.0
    assert out["cons"] == 0.5
    assert abs(out["rv_pct"] - 0.9901596794423744) < 1e-12
    assert abs(out["rv_ratio"] - 0.9901596794423744) < 1e-12
    assert out["range_atr"] == 3.0
    assert out["range_norm"] == 1.5
    assert out["dir_eff"] == 0.0


def test_forward_outcome_uptrend_and_bounds():
    closes = [100.0, 101.0, 102.0, 103.0]
    highs = [100.0, 101.0, 102.0, 103.0]
    lows = [100.0, 101.0, 102.0, 103.0]
    atr_pct = [2.0] * 4
    out = forward_outcome(closes, highs, lows, atr_pct, 0, 3)
    assert out is not None
    assert abs(out["ret_atr"] - 1.5) < 1e-9
    assert out["er"] == 1.0
    assert out["cons"] == 1.0
    assert out["mae_atr"] == 0.5
    assert forward_outcome(closes, highs, lows, atr_pct, 1, 3) is None
    assert forward_outcome(closes, highs, lows, [None] * 4, 0, 2) is None


def test_future_class_mapping():
    base = {"range_norm": 1.0, "dir_eff": 0.0, "er": 0.2}
    assert future_class({**base, "range_norm": 1.6}) == "FUT_HIGH_VOL"
    assert future_class({**base, "dir_eff": 0.8, "er": 0.6}) == "FUT_UP"
    assert future_class({**base, "dir_eff": -0.8, "er": 0.6}) == "FUT_DOWN"
    assert future_class({**base, "er": 0.2}) == "FUT_RANGE"
    assert future_class({**base, "er": 0.45}) == "FUT_TRANSITION"
    assert future_class(None) == "NO_DATA"


def test_rankdata_and_spearman():
    assert rankdata([30.0, 10.0, 20.0]) == [3.0, 1.0, 2.0]
    assert rankdata([1.0, 1.0, 2.0]) == [1.5, 1.5, 3.0]
    assert abs(spearman([1, 2, 3, 4], [2, 4, 6, 8]) - 1.0) < 1e-12
    assert abs(spearman([1, 2, 3, 4], [8, 6, 4, 2]) + 1.0) < 1e-12
    assert spearman([1, 2], [2, 4]) is None
