"""TEST_PRESET: whitelist-применение runtime-блока пресета («ветки») в apply_test_overrides."""
from __future__ import annotations

import os
from types import SimpleNamespace

from app.bot.runtime import _runtime_preset_overrides, apply_test_overrides


def test_runtime_preset_overrides_map():
    rt = {
        "sessions": ["morning", "day"],
        "overnight": True,
        "money": {"initial_cash": 55555, "qty_per_trade": 2, "pos_pct": 0.3, "max_positions": 3},
        "exits": {"sl_mode": "atr", "initial_sl_atr": 2.5, "trail_activation_comm_mult": None},
        "entry": {"cooldown_bars": 7, "confirm_flip": 1, "gates": ["confirm_flip", "rank_enabled"]},
        "regimes": ["TREND_UP", "TREND_DOWN"],
    }
    out = _runtime_preset_overrides(rt)
    assert out["sessions"] == ["morning", "day"]
    assert out["overnight"] is True
    assert out["initial_cash"] == 55555
    assert out["qty_per_trade"] == 2
    assert out["trail_activation_comm_mult"] is None  # None применяется = выключение
    assert out["reentry_cooldown_bars"] == 7
    assert out["confirm_flip"] == 1  # явное entry-поле перекрывает значение из гейтов
    assert out["trade_regimes"] == ["TREND_UP", "TREND_DOWN"]
    # гейты: всё выключено, включены только перечисленные
    assert out["imoex_guard"] is False
    assert out["rank_enabled"] is True


def test_null_trail_off_and_all_regimes_skipped():
    out = _runtime_preset_overrides({"regimes": "all",
                                     "exits": {"trail_activation_comm_mult": None}})
    assert "trade_regimes" not in out
    assert out["trail_activation_comm_mult"] is None


def test_apply_test_overrides_via_env():
    os.environ["TEST_PRESET"] = '{"runtime": {"money": {"initial_cash": 12345}}}'
    try:
        cfg = SimpleNamespace()
        applied = apply_test_overrides(cfg)
        assert cfg.initial_cash == 12345
        assert any("initial_cash" in a for a in applied)
        assert any(a == "preset=runtime" for a in applied)
    finally:
        os.environ.pop("TEST_PRESET", None)
