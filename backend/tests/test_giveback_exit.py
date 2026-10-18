"""Trail-giveback (trail_activation_pct/trail_giveback_pct): чистая логика, валидация, изоляция."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.bot.runtime import PaperBotRuntime, _giveback_step
from app.engine.models import PositionState
from app.engine.strategies import ParamValidationError, build_strategy, validate_params


class TestGivebackStep:
    def test_disabled_never_triggers_but_tracks_peak(self):
        peak, hit = _giveback_step(50.0, None, 1000.0, 0.0, 0.5)
        assert peak == 50.0
        assert hit is False
        peak, hit = _giveback_step(10.0, 100.0, 1000.0, 2.0, 0.0)
        assert peak == 100.0
        assert hit is False

    def test_peak_seeds_from_first_upnl(self):
        peak, hit = _giveback_step(-30.0, None, 1000.0, 2.0, 0.5)
        assert peak == -30.0
        assert hit is False

    def test_no_activation_no_trigger(self):
        peak, _ = _giveback_step(19.0, 0.0, 1000.0, 2.0, 0.5)
        assert peak == 19.0
        _, hit = _giveback_step(5.0, peak, 1000.0, 2.0, 0.5)
        assert hit is False

    def test_activation_then_exact_giveback_boundary(self):
        peak, _ = _giveback_step(20.0, 0.0, 1000.0, 2.0, 0.5)
        assert peak == 20.0
        _, hit = _giveback_step(15.0, peak, 1000.0, 2.0, 0.5)
        assert hit is True

    def test_giveback_below_threshold_no_trigger(self):
        peak, _ = _giveback_step(30.0, 0.0, 1000.0, 2.0, 0.5)
        _, hit = _giveback_step(26.0, peak, 1000.0, 2.0, 0.5)
        assert hit is False

    def test_new_peak_cannot_self_trigger(self):
        peak, hit = _giveback_step(40.0, 0.0, 1000.0, 2.0, 0.5)
        assert peak == 40.0 and hit is False

    def test_peak_monotonic(self):
        peak, _ = _giveback_step(40.0, 0.0, 1000.0, 2.0, 0.5)
        peak, hit = _giveback_step(30.0, peak, 1000.0, 2.0, 0.5)
        assert peak == 40.0 and hit is True
        peak, _ = _giveback_step(10.0, peak, 1000.0, 2.0, 0.5)
        assert peak == 40.0


class TestParamsValidation:
    def test_defaults_off(self):
        v = validate_params("time_grid", {"schedule": "06:55:short"})
        assert v["trail_activation_pct"] == 0.0
        assert v["trail_giveback_pct"] == 0.0

    def test_accepts_trail_params(self):
        v = validate_params("time_grid", {
            "schedule": "06:55:short,18:40:long",
            "trail_activation_pct": 3.0,
            "trail_giveback_pct": 0.5,
        })
        assert v["trail_activation_pct"] == 3.0
        assert v["trail_giveback_pct"] == 0.5

    def test_out_of_range_rejected(self):
        with pytest.raises(ParamValidationError):
            validate_params("time_grid", {"trail_activation_pct": 101.0})
        with pytest.raises(ParamValidationError):
            validate_params("time_grid", {"trail_giveback_pct": -1.0})

    def test_build_strategy_accepts_trail_params(self):
        strat = build_strategy("time_grid", {
            "schedule": "06:55:short",
            "trail_activation_pct": 2.0,
            "trail_giveback_pct": 0.5,
        })
        assert strat.p.trail_activation_pct == 2.0
        assert strat.p.trail_giveback_pct == 0.5

    def test_strategy_defaults_off(self):
        strat = build_strategy("time_grid", {"schedule": "06:55:short"})
        assert strat.p.trail_activation_pct == 0.0
        assert strat.p.trail_giveback_pct == 0.0


def _rt_with_params(params: dict) -> PaperBotRuntime:
    rt = PaperBotRuntime()
    rt.config.params = dict(params)
    return rt


def _close(px: float) -> SimpleNamespace:
    return SimpleNamespace(close=px)


class TestGivebackExit:
    def test_disabled_by_default(self):
        rt = _rt_with_params({})
        rt._gb_peak["F"] = 0.0
        price, reason = rt._giveback_exit("F", _close(204.0), PositionState.LONG, 100.0, 10)
        assert price is None
        assert rt._gb_peak["F"] == 0.0

    def test_long_activation_and_trigger(self):
        rt = _rt_with_params({"trail_activation_pct": 2.0, "trail_giveback_pct": 0.5})
        rt._gb_peak["F"] = 0.0
        price, _ = rt._giveback_exit("F", _close(102.0), PositionState.LONG, 100.0, 10)
        assert price is None
        assert rt._gb_peak["F"] == 20.0
        price, reason = rt._giveback_exit("F", _close(101.5), PositionState.LONG, 100.0, 10)
        assert price == 101.5
        assert reason == "trail_exit"

    def test_short_activation_and_trigger(self):
        rt = _rt_with_params({"trail_activation_pct": 2.0, "trail_giveback_pct": 0.5})
        rt._gb_peak["F"] = 0.0
        price, _ = rt._giveback_exit("F", _close(98.0), PositionState.SHORT, 100.0, 10)
        assert price is None
        assert rt._gb_peak["F"] == 20.0
        price, reason = rt._giveback_exit("F", _close(98.5), PositionState.SHORT, 100.0, 10)
        assert price == 98.5
        assert reason == "trail_exit"

    def test_garbage_params_do_not_exit(self):
        rt = _rt_with_params({"trail_activation_pct": "abc", "trail_giveback_pct": 0.5})
        price, reason = rt._giveback_exit("F", _close(101.5), PositionState.LONG, 100.0, 10)
        assert price is None and reason == ""

    def test_state_lifecycle(self):
        rt = _rt_with_params({"trail_activation_pct": 2.0, "trail_giveback_pct": 0.5})
        rt._gb_peak["F"] = 12.0
        rt._clear_exit_state("F")
        assert "F" not in rt._gb_peak
        rt._gb_peak["F"] = 12.0
        rt._reset_run_state()
        assert rt._gb_peak == {}
