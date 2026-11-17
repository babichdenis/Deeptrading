"""ARCH452: форма runtime._regimes не должна ронять список позиций.

Реальный дефект: `GET /api/v1/sandbox/positions` отвечал
`count 0, positions [], error="AttributeError: 'str' object has no attribute 'get'"`
при открытой позиции NLMK, потому что маршрут считал запись режима словарём.
"""
import pytest

from app.api.routes.sandbox import normalize_position_regime


def test_string_state_is_wrapped_not_crashed():
    # Основной путь runtime._process_candle: state — имя режима строкой.
    state, vol = normalize_position_regime({"state": "TREND_UP", "vol": 1.42})
    assert state == {"state": "TREND_UP"}
    assert state.get("state") == "TREND_UP"
    assert vol == 1.42


def test_dict_state_is_passed_through():
    rg = {
        "state": {
            "state": "TREND_DOWN",
            "reason": "adx>25",
            "features": {"atr_pct": 0.031, "adx": 28.4},
        },
        "vol": 0.9,
    }
    state, vol = normalize_position_regime(rg)
    assert state["state"] == "TREND_DOWN"
    assert state["reason"] == "adx>25"
    assert state["features"]["atr_pct"] == 0.031
    assert vol == 0.9


def test_missing_and_none_state_give_empty_dict():
    assert normalize_position_regime({}) == ({}, None)
    assert normalize_position_regime({"state": None}) == ({}, None)
    assert normalize_position_regime({"state": ""}) == ({}, None)


def test_non_dict_regime_record_does_not_crash():
    # Запись режима может оказаться не словарём — раньше это тоже давало AttributeError.
    for bad in ("TREND_UP", 42, ["TREND_UP"], None):
        state, vol = normalize_position_regime(bad)
        assert state == {}
        assert vol is None


def test_nested_state_dict_is_flattened_to_name():
    state, vol = normalize_position_regime(
        {"state": {"state": {"name": "RANGE"}, "reason": {"code": 1}}, "vol": "n/a"}
    )
    assert isinstance(state["state"], str)
    assert isinstance(state["reason"], str)
    assert vol is None


def test_regime_fields_are_always_serialisable_strings():
    """Поля regime/regime_reason из маршрута обязаны быть строками."""
    for rg in (
        {"state": "TREND_UP"},
        {"state": {"state": "TREND_UP", "reason": "ok"}},
        {"state": {"state": None, "reason": None}},
        {},
        "TREND_UP",
    ):
        state, _ = normalize_position_regime(rg)
        value = state.get("state") or ""
        reason = state.get("reason") or ""
        assert isinstance(value, str)
        assert isinstance(reason, str)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q", "-p", "no:warnings"]))
