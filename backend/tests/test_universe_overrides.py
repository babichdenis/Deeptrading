"""Проверки загрузчика ручных переопределений юниверса.

Раньше путь к файлу был зашит в двух местах runtime, поэтому прогон на
замороженном наборе данных подхватывал состояние рабочей машины. Здесь
проверяется, что путь берётся из окружения, кэш обновляется при изменении
файла, а отсутствие или порча файла не роняют расчёт.
"""
import json

import pytest

from app.bot import runtime as rt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(rt._UNIVERSE_OVERRIDES_ENV, raising=False)
    rt._universe_overrides_cache["key"] = None
    rt._universe_overrides_cache["items"] = None
    yield
    rt._universe_overrides_cache["key"] = None
    rt._universe_overrides_cache["items"] = None


def test_default_path_used_when_env_is_empty():
    assert rt.universe_overrides_path() == rt._UNIVERSE_OVERRIDES_DEFAULT


def test_env_overrides_default_path(tmp_path, monkeypatch):
    target = tmp_path / "mine.json"
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(target))
    assert rt.universe_overrides_path() == target


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, "   ")
    assert rt.universe_overrides_path() == rt._UNIVERSE_OVERRIDES_DEFAULT


def test_missing_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(tmp_path / "absent.json"))
    assert rt.load_universe_overrides() is None


def test_broken_json_returns_none_without_raising(tmp_path, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text("[{broken", encoding="utf-8")
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(bad))
    assert rt.load_universe_overrides() is None


def test_entries_without_figi_are_dropped(tmp_path, monkeypatch):
    path = tmp_path / "u.json"
    path.write_text(json.dumps([
        {"figi": "AAA", "side": "short"},
        {"side": "long"},
        {"figi": "BBB", "side": "long"},
    ]), encoding="utf-8")
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(path))
    items = rt.load_universe_overrides()
    assert [i["figi"] for i in items] == ["AAA", "BBB"]


def test_cache_is_refreshed_when_file_changes(tmp_path, monkeypatch):
    path = tmp_path / "u.json"
    path.write_text(json.dumps([{"figi": "AAA"}]), encoding="utf-8")
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(path))
    assert [i["figi"] for i in rt.load_universe_overrides()] == ["AAA"]

    # другой размер и содержимое -> кэш обязан обновиться, а не отдать старое
    path.write_text(json.dumps([{"figi": "BBB"}, {"figi": "CCC"}]), encoding="utf-8")
    assert [i["figi"] for i in rt.load_universe_overrides()] == ["BBB", "CCC"]


def test_non_list_json_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "u.json"
    path.write_text(json.dumps({"figi": "AAA"}), encoding="utf-8")
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(path))
    assert rt.load_universe_overrides() is None


def test_skip_side_matches_configured_direction(tmp_path, monkeypatch):
    """Ручная блокировка стороны: short запрещает BUY, long запрещает SELL."""
    path = tmp_path / "u.json"
    path.write_text(json.dumps([
        {"figi": "SHORTY", "side": "short"},
        {"figi": "LONGY", "side": "long"},
    ]), encoding="utf-8")
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(path))

    def skip_for(figi):
        for c in rt.load_universe_overrides() or []:
            if c.get("figi") == figi:
                if c.get("side") == "short":
                    return "BUY"
                if c.get("side") == "long":
                    return "SELL"
        return ""

    assert skip_for("SHORTY") == "BUY"
    assert skip_for("LONGY") == "SELL"
    assert skip_for("OTHER") == ""


@pytest.mark.parametrize("selection", [None, "   "])
def test_legacy_file_does_not_activate_without_explicit_selection(tmp_path, monkeypatch, selection):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([{"figi": "STALE", "side": "short"}]), encoding="utf-8")
    monkeypatch.setattr(rt, "_UNIVERSE_OVERRIDES_DEFAULT", path)
    if selection is not None:
        monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, selection)
    assert rt.load_universe_overrides() is None


def test_explicit_legacy_selection_can_be_disabled_without_cached_leak(tmp_path, monkeypatch):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps([{"figi": "CHOSEN", "side": "long"}]), encoding="utf-8")
    monkeypatch.setattr(rt, "_UNIVERSE_OVERRIDES_DEFAULT", path)
    monkeypatch.setenv(rt._UNIVERSE_OVERRIDES_ENV, str(path))
    assert rt.load_universe_overrides()[0]["figi"] == "CHOSEN"
    monkeypatch.delenv(rt._UNIVERSE_OVERRIDES_ENV)
    assert rt.load_universe_overrides() is None
    assert rt._universe_overrides_cache["items"] is None
