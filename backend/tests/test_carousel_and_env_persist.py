"""Регрессии:
- карусель не греет БД count(*) без нужды (15 млн строк = 9-11 с каждые 15 с);
- состояние прогона (движок/TF/окно) переживает рестарт: раньше TEST_ENGINE жил
  только в os.environ, и после рестарта контур молча падал в ensemble_v4 —
  реплей на порядок медленнее. Теперь источник один: data/run_state.json.
"""
import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

_ENV_KEYS = ("TEST_ENGINE", "TEST_INTERVAL", "TEST_PARAMS", "TEST_PRESET", "TEST_PRESET_MODE")


class _FakeDB:
    """Плоская сессия: считает, сколько раз выполнены запросы к candles."""

    def __init__(self, eligible, counts=None):
        self._eligible = eligible
        self._counts = counts or []
        self.candle_queries = 0
        self.counted_figis = []

    async def execute(self, stmt, params=None):
        sql = str(getattr(stmt, "text", stmt))
        if "FROM universe" in sql:
            return SimpleNamespace(all=lambda: list(self._eligible))
        if "count(*) FROM candles" in sql:
            self.candle_queries += 1
            self.counted_figis = list((params or {}).get("fs") or [])
            return SimpleNamespace(all=lambda: list(self._counts))
        raise AssertionError(f"неожиданный запрос: {sql}")


@pytest.fixture
def runtime_stub(monkeypatch):
    """Подменяем app.bot.runtime.runtime на объект с заданным universe."""
    mod = importlib.import_module("app.bot.runtime")

    def _set(active_figis, running=True):
        monkeypatch.setattr(
            mod, "runtime",
            SimpleNamespace(universe=[{"figi": f} for f in active_figis], running=running),
            raising=False)
    return _set


@pytest.fixture(autouse=True)
def _reset_counts_cache():
    scr = importlib.import_module("app.api.routes.screener")
    scr._counts_cache.update(ts=0.0, keys=frozenset(), data={})
    yield


# --- карусель -------------------------------------------------------------

@pytest.mark.asyncio
async def test_carousel_skips_count_when_all_active(runtime_stub):
    """Все тикеры активны (реплей) — count(*) по миллионам строк не выполняется."""
    scr = importlib.import_module("app.api.routes.screener")
    db = _FakeDB([("F1", "AAA", 1), ("F2", "BBB", 1), ("F3", "CCC", 1)])
    runtime_stub({"F1", "F2", "F3"})

    out = await scr._carousel_status(db)

    assert db.candle_queries == 0, "count(*) выполнен, хотя pending пуст"
    assert out["pending"] == []
    assert out["insufficient"] == 0
    assert out["eligible_count"] == 3
    assert out["active_count"] == 3
    assert out["bot_running"] is True


@pytest.mark.asyncio
async def test_carousel_counts_pending_once_then_caches(runtime_stub):
    """Pending считаются (и только они), повторный вызов берёт кэш."""
    scr = importlib.import_module("app.api.routes.screener")
    db = _FakeDB([("F1", "AAA", 1), ("F2", "BBB", 1)], counts=[("F2", 120)])
    runtime_stub({"F1"})

    first = await scr._carousel_status(db)
    second = await scr._carousel_status(db)

    assert db.candle_queries == 1, "счётчик должен посчитаться один раз (кэш)"
    assert db.counted_figis == ["F2"], "считать нужно только pending-тикеры"
    assert first["pending"] == [
        {"ticker": "BBB", "candle_count": 120, "need_download": False}]
    assert first["insufficient"] == 0
    assert second["pending"] == first["pending"]


@pytest.mark.asyncio
async def test_carousel_marks_ticker_without_candles(runtime_stub):
    """Тикер без свечей помечается need_download — счётчики не потерялись."""
    scr = importlib.import_module("app.api.routes.screener")
    db = _FakeDB([("F1", "AAA", 1)], counts=[])
    runtime_stub(set())

    out = await scr._carousel_status(db)

    assert out["pending"] == [
        {"ticker": "AAA", "candle_count": 0, "need_download": True}]
    assert out["insufficient"] == 1


@pytest.mark.asyncio
async def test_carousel_recounts_when_universe_changed(runtime_stub):
    """Новый FIGI в вселенной → кэш инвалидируется, считаем заново."""
    scr = importlib.import_module("app.api.routes.screener")
    db = _FakeDB([("F1", "AAA", 1)], counts=[("F1", 10)])
    runtime_stub(set())
    await scr._carousel_status(db)
    db._counts = [("F1", 10), ("F2", 99)]
    db._eligible = [("F1", "AAA", 1), ("F2", "BBB", 1)]

    out = await scr._carousel_status(db)

    assert db.candle_queries == 2, "новый FIGI обязан пересчитаться"
    assert {p["ticker"]: p["candle_count"] for p in out["pending"]} == {
        "AAA": 10, "BBB": 99}


# --- состояние прогона ----------------------------------------------------

@pytest.fixture
def run_state(tmp_path, monkeypatch):
    rs = importlib.import_module("app.services.run_state")
    path = tmp_path / "run_state.json"
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(rs, "RUN_STATE_FILE", path)
    return rs, path


def test_run_state_roundtrip_keeps_engine(run_state):
    """Движок/TF/окно записываются и читаются обратно без потерь."""
    rs, path = run_state
    state = {
        "mode": "test", "test_name": "run 1",
        "replay_start": "2026-09-01T04:00:00Z", "replay_end": "2026-09-05T21:59:00Z",
        "replay_pace": "fast", "replay_log_persist": False,
        "test_engine": "rsi_trade_hub", "test_interval": "10min",
        "test_params": {"quorum": 2}, "preset": {"runtime": {"stop_pct": 10.0}},
        "preset_mode": "preset",
    }
    assert rs.save_run_state(state) is True

    back = rs.load_run_state()
    assert back["test_engine"] == "rsi_trade_hub"
    assert back["test_interval"] == "10min"
    assert back["test_params"] == {"quorum": 2}
    assert back["preset"] == {"runtime": {"stop_pct": 10.0}}
    assert back["preset_mode"] == "preset", "решение из модалки переживает рестарт"
    assert "updated_at" in json.loads(path.read_text(encoding="utf-8"))
    assert not path.with_suffix(".json.tmp").exists(), "остался временный файл"


def test_run_state_strips_unknown_keys(run_state):
    """Мусор в состояние не пишется (старые/чужие поля не накапливаются)."""
    rs, path = run_state
    rs.save_run_state({"mode": "test", "test_engine": "rsi_trade_hub", "junk": 1})

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "junk" not in raw
    assert "test_engine" in raw


def test_engine_survives_process_restart(run_state):
    """РЕГРЕССИЯ: движок не теряется после рестарта.

    Имитируем новый процесс: TEST_* в окружении пусто (как после рестарта),
    состояние лежит только в файле — автостарт обязан его восстановить.
    """
    rs, path = run_state
    rs.save_run_state({
        "mode": "test", "test_name": "run 1",
        "replay_start": "2026-09-01T04:00:00Z", "replay_end": "2026-09-05T21:59:00Z",
        "replay_pace": "fast", "test_engine": "rsi_trade_hub", "test_interval": "10min",
    })

    # «новый процесс»: env чистый
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    state, source = rs.resolve_boot_state(SimpleNamespace(bot_mode="sandbox"))

    assert source == "run_state.json"
    assert state["test_engine"] == "rsi_trade_hub"
    assert state["test_interval"] == "10min"
    assert state["replay_start"] == "2026-09-01T04:00:00Z"

    # автостарт пересобирает process-env кэш из файла
    rs.sync_env(state)
    assert os.environ["TEST_ENGINE"] == "rsi_trade_hub"
    assert os.environ["TEST_INTERVAL"] == "10min"


def test_preset_mode_syncs_to_env_and_clears(run_state):
    """Режим «пресет/UI» — тоже состояние прогона: едет через env и чистится."""
    rs, _ = run_state
    rs.sync_env({"preset_mode": "ui"})
    assert os.environ["TEST_PRESET_MODE"] == "ui"

    rs.sync_env({"preset_mode": ""})
    assert "TEST_PRESET_MODE" not in os.environ


def test_sync_env_clears_previous_engine(run_state):
    """Возврат к ensemble_v4 убирает ключи, а не оставляет прошлый движок."""
    rs, _ = run_state
    os.environ["TEST_ENGINE"] = "rsi_trade_hub"
    os.environ["TEST_INTERVAL"] = "10min"

    rs.sync_env({"mode": "test", "test_engine": "", "test_interval": ""})

    assert "TEST_ENGINE" not in os.environ
    assert "TEST_INTERVAL" not in os.environ


def test_sync_env_serializes_json_without_spaces(run_state):
    """JSON оверрайдов без пробелов — иначе .env/парсер режет значение."""
    rs, _ = run_state
    rs.sync_env({"test_params": {"quorum": 2}, "preset": {"runtime": {"stop_pct": 10.0}}})

    assert os.environ["TEST_PARAMS"] == '{"quorum":2}'
    assert os.environ["TEST_PRESET"] == '{"runtime":{"stop_pct":10.0}}'


def test_resolve_boot_state_falls_back_to_env(run_state, monkeypatch):
    """Без файла — легаси-фолбэк из .env, и источник честно помечен."""
    rs, _ = run_state
    monkeypatch.setenv("BOT_TEST_NAME", "legacy run")
    monkeypatch.setenv("TEST_ENGINE", "ose_bollinger")

    state, source = rs.resolve_boot_state(SimpleNamespace(
        bot_mode="test", bot_test_start="2026-09-01T04:00:00Z", bot_test_pace="wall"))

    assert source == ".env (legacy, run_state.json нет)"
    assert state["test_name"] == "legacy run"
    assert state["test_engine"] == "ose_bollinger"
    assert state["replay_pace"] == "wall"
    assert state["replay_start"] == "2026-09-01T04:00:00Z"


def test_load_run_state_survives_broken_file(run_state):
    """Битый JSON не роняет автостарт — просто пустое состояние."""
    rs, path = run_state
    path.write_text("{не json", encoding="utf-8")

    assert rs.load_run_state() == {}


# --- тест-модалка: движки и таймфреймы -------------------------------------

@pytest.mark.asyncio
async def test_engines_endpoint_lists_registry_and_intervals():
    """UI должен получить полный реестр движков и допустимые TF (раньше их не было)."""
    bot = importlib.import_module("app.api.routes.bot")
    out = await bot.bot_engines()

    ids = [e["id"] for e in out["engines"]]
    assert "rsi_trade_hub" in ids, "быстрый движок из прогонов владельца должен быть в списке"
    assert "ose_bollinger" in ids
    assert out["default"]["id"] == "", "пустой id = ensemble_v4 (поведение по умолчанию)"
    tf_ids = [i["id"] for i in out["intervals"]]
    assert "10min" in tf_ids and "1min" in tf_ids
    assert all(i["label"] for i in out["intervals"])


# --- .env: миграция на run_state.json -------------------------------------

def test_env_drops_only_migrated_keys(tmp_path):
    """Из .env уходят ключи прогона, но ops-ручки (вариант файла) остаются."""
    bot = importlib.import_module("app.api.routes.bot")
    env = tmp_path / ".env"
    env.write_text(
        "BOT_MODE=test\n"
        "BOT_TEST_NAME=run 1\n"
        "BOT_TEST_START=2026-09-01T04:00:00Z\n"
        "BOT_TEST_END=2026-09-05T21:59:00Z\n"
        "BOT_TEST_PACE=fast\n"
        "BOT_TEST_LOG_PERSIST=0\n"
        "BOT_TEST_VARIANT=q2ref\n"
        "TEST_VARIANT=\n"
        "GITHUB_TOKEN=secret\n",
        encoding="utf-8")

    bot._write_env_mode("sandbox", env_path=str(env))

    text = env.read_text(encoding="utf-8")
    assert "BOT_MODE=sandbox" in text
    for migrated in ("BOT_TEST_NAME", "BOT_TEST_START", "BOT_TEST_END",
                     "BOT_TEST_PACE", "BOT_TEST_LOG_PERSIST"):
        assert migrated not in text, f"{migrated} должен жить в run_state.json"
    assert "BOT_TEST_VARIANT=q2ref" in text, "вариант — ops-ручка, её не трогаем"
    assert "GITHUB_TOKEN=secret" in text


def test_env_adds_mode_key_when_absent(tmp_path):
    """Если BOT_MODE в .env не было — он добавляется, а не теряется."""
    bot = importlib.import_module("app.api.routes.bot")
    env = tmp_path / ".env"
    env.write_text("UNIVERSE_MODE=\n", encoding="utf-8")

    bot._write_env_mode("test", env_path=str(env))

    assert "BOT_MODE=test" in env.read_text(encoding="utf-8")
