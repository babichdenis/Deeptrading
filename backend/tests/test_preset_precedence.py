"""Пресет ГЛАВНЫЙ (решение владельца), но расхождение с настройками UI не молчит:
apply_test_overrides кладёт значения пресета поверх всего, preset_ui_conflicts()
возвращает список полей, которые пресет перебил, — его показывает модалка в UI.
"""
import importlib
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

rt = importlib.import_module("app.bot.runtime")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("TEST_ENGINE", "TEST_INTERVAL", "TEST_PARAMS", "TEST_PRESET",
              "TEST_GATES", "TEST_VARIANT", "TEST_PRESET_FORCE", "TEST_PRESET_MODE"):
        monkeypatch.delenv(k, raising=False)


class _Cfg:
    """Минимальный cfg: apply_test_overrides только setattr-ит поля."""


def _mk_cfg(**kw):
    c = _Cfg()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _preset(runtime: dict) -> str:
    import json
    return json.dumps({"runtime": runtime}, ensure_ascii=False)


def test_preset_overrides_ui_locked_field(monkeypatch):
    """ГЛАВНОЕ: пресет перебивает сохранённое из UI, даже когда ui-lock активен."""
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))
    cfg = _mk_cfg(pos_pct=5)

    applied = rt.apply_test_overrides(cfg, locked={"pos_pct"})

    assert cfg.pos_pct == 25, "пресет обязан победить ui-lock"
    assert any(a.startswith("preset:pos_pct") for a in applied)


def test_preset_applied_after_other_overrides(monkeypatch):
    """Пресет кладётся последним: гейты/TEST_* его не затирают."""
    monkeypatch.setenv("TEST_ENGINE", "rsi_trade_hub")
    monkeypatch.setenv("TEST_INTERVAL", "10min")
    # confirm_fct → confirm_flip есть и в гейтах (TEST_GATES_OFF = 0), и в пресете.
    monkeypatch.setenv("TEST_GATES", "off")
    monkeypatch.setenv("TEST_PRESET", _preset({"entry": {"confirm_flip": 7}}))
    cfg = _mk_cfg(confirm_flip=99)

    rt.apply_test_overrides(cfg, locked=set())

    assert cfg.strategy_id == "rsi_trade_hub"
    assert cfg.interval_name == "10min"
    assert cfg.confirm_flip == 7, "значение пресета должно быть последним"


def test_non_preset_overrides_still_respect_ui_lock(monkeypatch):
    """Без пресета ui-lock работает как раньше (движок/TF не затирают UI молча)."""
    monkeypatch.setenv("TEST_ENGINE", "rsi_trade_hub")
    cfg = _mk_cfg(strategy_id="ensemble_v4")

    applied = rt.apply_test_overrides(cfg, locked={"strategy_id"})

    assert cfg.strategy_id == "ensemble_v4"
    assert "ui-lock:strategy_id" in applied


def test_no_preset_no_conflicts(monkeypatch):
    monkeypatch.setenv("TEST_ENGINE", "rsi_trade_hub")

    assert rt.preset_ui_conflicts({"strategy_id": "ensemble_v4"}) == []


def test_conflicts_list_ui_and_preset_values(monkeypatch):
    """В конфликте видно И старое значение UI, И новое пресета."""
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))

    got = rt.preset_ui_conflicts({"pos_pct": 5, "margin_leverage": 3})

    assert got == [{"field": "pos_pct", "ui": 5, "preset": 25}]


def test_no_conflict_when_values_equal(monkeypatch):
    """Совпадение значений — не конфликт (модалка не должна ложно ругаться)."""
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))

    assert rt.preset_ui_conflicts({"pos_pct": 25}) == []


def test_no_conflict_for_ui_fields_preset_does_not_touch(monkeypatch):
    """Пресет не упоминает поле — UI-значение остаётся, конфликта нет."""
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))

    assert rt.preset_ui_conflicts({"margin_leverage": 3, "sessions": ["morning"]}) == []


def test_conflicts_survive_broken_preset(monkeypatch):
    """Битый JSON пресета — пустой список, а не исключение на старте."""
    monkeypatch.setenv("TEST_PRESET", "{не json")

    assert rt.preset_ui_conflicts({"pos_pct": 5}) == []


def test_gates_in_preset_still_switch_off_others(monkeypatch):
    """Гейты пресета (список включённых) продолжают выключать остальные."""
    import json
    gates = json.loads(json.dumps(rt.TEST_GATES_ON))
    first_on = next(iter(gates))
    monkeypatch.setenv("TEST_PRESET", _preset({"entry": {"gates": [first_on]}}))
    cfg = _mk_cfg()

    rt.apply_test_overrides(cfg, locked=set())

    assert getattr(cfg, first_on) == gates[first_on]
    off_key = next(k for k in rt.TEST_GATES_OFF if k != first_on)
    assert getattr(cfg, off_key) == rt.TEST_GATES_OFF[off_key]


# --- Выбор режима в модалке: "preset" (по пресету) | "ui" (по ползункам) ---

def test_preset_mode_ui_skips_preset(monkeypatch):
    """РЕШЕНИЕ ВЛАДЕЛЬЦА: выбран режим «по UI» — пресет НЕ применяется вовсе.

    Это дефолт при истёкшем таймере: молчание не должно означать «переписать
    настройки пресетом» — иначе результат прогона не отличить от явного выбора.
    """
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))
    monkeypatch.setenv("TEST_PRESET_MODE", "ui")
    cfg = _mk_cfg(pos_pct=5)

    applied = rt.apply_test_overrides(cfg, locked=set())

    assert cfg.pos_pct == 5, "в режиме «по UI» пресет не должен трогать поля"
    assert any("SKIPPED" in a for a in applied), "в логе должно быть видно, что пресет пропущен"


def test_preset_mode_preset_applies(monkeypatch):
    """Явный выбор «по пресету» — применяется как раньше (перебивает UI)."""
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))
    monkeypatch.setenv("TEST_PRESET_MODE", "preset")
    cfg = _mk_cfg(pos_pct=5)

    rt.apply_test_overrides(cfg, locked={"pos_pct"})

    assert cfg.pos_pct == 25


def test_conflicts_reported_in_ui_mode(monkeypatch):
    """Даже в режиме «по UI» расхождения считаются — их показывает модалка."""
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))
    monkeypatch.setenv("TEST_PRESET_MODE", "ui")

    assert rt.preset_ui_conflicts({"pos_pct": 5}) == [
        {"field": "pos_pct", "ui": 5, "preset": 25}]


def test_mode_ui_keeps_non_preset_test_overrides(monkeypatch):
    """«По UI» отменяет только пресет — движок/TF прогона остаются."""
    monkeypatch.setenv("TEST_ENGINE", "rsi_trade_hub")
    monkeypatch.setenv("TEST_INTERVAL", "10min")
    monkeypatch.setenv("TEST_PRESET", _preset({"money": {"pos_pct": 25}}))
    monkeypatch.setenv("TEST_PRESET_MODE", "ui")
    cfg = _mk_cfg(strategy_id="ensemble_v4", interval_name="1min", pos_pct=5)

    rt.apply_test_overrides(cfg, locked=set())

    assert cfg.strategy_id == "rsi_trade_hub", "движок выбран в тест-модалке, не пресетом"
    assert cfg.interval_name == "10min"
    assert cfg.pos_pct == 5
