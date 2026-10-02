"""Этап 2 — живые реплеи в Analytics, теги пресета и админка тестов.

Приёмка (docs/ANALYTICS_TAB.md «Этап 2 — заказ владельца»):
  * список/карточка/срезы/сделки реплея читаются на лету из sandbox_trades;
  * теги настроек идут из сайдкара пресета, без него — has_sidecar=false (деградация);
  * DELETE /bot/tests/{name} чистит сделки + bot_test_runs + сайдкар, соседи не трогаются;
  * перезапуск = НОВОЕ имя и тот же пресет в ModeRequest (старый тест цел);
  * GET /bot/presets отдаёт «ветки» для модалки запуска.

Postgres не нужен: sqlite (aiosqlite) + подменённый SessionLocal + подменённый bot_set_mode
(иначе рестарт тронул бы боевую машину).
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app import database as dbmod
from app.api.routes import analysis_replays, bot
from app.database import Base, get_db
from app.models.sandbox_trade import SandboxTrade
from app.services import preset_tags
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

NAME_A = "mtf-rsi-v1 20260921-1000"
NAME_B = "bare-test"
T0 = datetime(2026, 9, 21, 7, 0, tzinfo=UTC)

PRESET = {
    "preset": {"id": "mtf-rsi-v1", "name": "MTF RSI v1", "notes": "базовая ветка"},
    "harness": {
        "timeframe": "5min",
        "period": ["2026-09-21", "2026-09-25"],
        "universe": ["SBER", "GAZP", "LKOH"],
        "robots": [{"robot": "pullback_ema", "params": {"quorum": 3, "len": 21}}],
        "exits": ["signal_exit", "target", "stop_loss"],
        "costs": {"commission": 0.0005, "slippage_bps": 2},
    },
    "runtime": {
        "money": {"initial_cash": 100000, "qty_per_trade": 10, "pos_pct": 0.2,
                  "max_positions": 5, "max_daily_loss": 5000},
        "sessions": ["morning", "day"],
        "overnight": False,
        "entry": {"quorum": 3, "confirm_flip": 1, "cooldown_bars": 5,
                  "gates": ["confirm_flip", "rank_enabled"]},
        "exits": {"sl_mode": "atr", "initial_sl_atr": 4.0, "trail_activation_comm_mult": 1.5},
        "regimes": ["TREND_UP", "TREND_DOWN"],
        "bias": {"enabled": True, "tf": "30min", "period": 5},
    },
    "targets": {"replay": {"engine": "ensemble_v4", "interval": "5min", "pace": "fast"}},
}

PAYLOAD = {
    "mode": "test", "test_name": NAME_A,
    "replay_start": "2026-09-21T07:00:00+00:00", "replay_end": "2026-09-25T16:00:00+00:00",
    "replay_pace": "fast", "test_engine": "ensemble_v4", "test_interval": "5min",
    "test_params": {"quorum": 3}, "replay_log_persist": True, "preset": PRESET,
}


def _meta(regime: str, reason: str) -> str:
    return json.dumps({"entry_regime": regime, "entry": {"reason": reason}})


_NEXT_ID = [0]


def _trade(session: Session, *, name: str, ticker: str, net: float | None, entry: datetime,
           exit_reason: str = "signal_exit", entry_reason: str = "quorum",
           regime: str = "TREND_UP", side: str = "LONG") -> None:
    opened = net is None
    _NEXT_ID[0] += 1   # sqlite не умеет autoincrement у BigInteger PK — id задаём сами
    session.add(SandboxTrade(
        id=_NEXT_ID[0],
        figi=f"FIGI{ticker}", ticker=ticker, side=side, qty=10,
        entry_time=entry, entry_price=100.0, stop_loss=99.0, take_profit=105.0,
        exit_time=None if opened else entry + timedelta(minutes=25),
        exit_price=None if opened else 100.0 + (net or 0.0),
        commission=1.0, net_pnl=net,
        exit_reason=None if opened else exit_reason,
        entry_reason=entry_reason, trailing_active=False, leverage=1.0,
        meta=_meta(regime, entry_reason), mode="paper", test_name=name,
    ))


@pytest.fixture()
def env(tmp_path, monkeypatch):
    sidecar_dir = tmp_path / "presets"
    monkeypatch.setattr(preset_tags, "SIDECAR_DIR", sidecar_dir)

    sync = create_engine(f"sqlite:///{tmp_path}/replay.db")
    Base.metadata.create_all(sync, tables=[SandboxTrade.__table__])
    with sync.begin() as conn:
        conn.execute(text(
            "CREATE TABLE bot_test_runs (name TEXT PRIMARY KEY, replay_start TIMESTAMP, "
            "replay_end TIMESTAMP, updated_at TIMESTAMP)"))
        conn.execute(
            text("INSERT INTO bot_test_runs (name, replay_start, replay_end, updated_at) "
                 "VALUES (:n, :s, :e, :u)"),
            {"n": NAME_A, "s": T0.isoformat(),
             "e": (T0 + timedelta(days=4)).isoformat(),
             "u": (T0 + timedelta(hours=1)).isoformat()})
    with Session(sync) as s:
        _trade(s, name=NAME_A, ticker="SBER", net=10.0, entry=T0)
        _trade(s, name=NAME_A, ticker="GAZP", net=5.0, entry=T0 + timedelta(hours=4),
               exit_reason="target", regime="TREND_DOWN", side="SHORT")
        _trade(s, name=NAME_A, ticker="SBER", net=-4.0, entry=T0 + timedelta(days=1),
               exit_reason="stop_loss", entry_reason="bias")
        _trade(s, name=NAME_A, ticker="GAZP", net=None, entry=T0 + timedelta(days=2))
        _trade(s, name=NAME_B, ticker="LKOH", net=-2.0, entry=T0, regime="HIGH_VOLATILITY")
        s.commit()
    sync.dispose()

    preset_tags.save_sidecar(NAME_A, PRESET, PAYLOAD)

    presets_dir = tmp_path / "cfg_presets"
    presets_dir.mkdir()
    (presets_dir / "mtf-rsi-v1.json").write_text(
        json.dumps(PRESET, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bot, "PRESETS_DIR", presets_dir)

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/replay.db")
    maker = async_sessionmaker(async_engine, expire_on_commit=False)
    monkeypatch.setattr(dbmod, "SessionLocal", maker)

    mode_calls: list = []

    async def _fake_set_mode(req):
        mode_calls.append(req)
        return {"mode": req.mode, "test_name": req.test_name, "restarted": True,
                "replay_pace": req.replay_pace, "replay_log_persist": req.replay_log_persist}

    monkeypatch.setattr(bot, "bot_set_mode", _fake_set_mode)

    app = FastAPI()
    app.include_router(analysis_replays.router)
    app.include_router(bot.router)

    async def _override():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, mode_calls=mode_calls, dbfile=tmp_path / "replay.db",
                              sidecar_dir=sidecar_dir, presets_dir=presets_dir)
    asyncio.run(async_engine.dispose())


def _get(env, url: str, **kw) -> dict:
    res = env.client.get(url, **kw)
    assert res.status_code == 200, res.text
    return res.json()


# ------------------------------------------------------------------ список

def test_replay_list_counts_and_sidecar(env):
    data = _get(env, "/api/v1/analysis/replays")
    assert data["count"] == 2
    by_name = {r["id"]: r for r in data["runs"]}

    a = by_name[NAME_A]
    assert a["kind"] == "replay"
    assert (a["trades"], a["wins"], a["losses"]) == (3, 2, 1)
    assert (a["gw"], a["gl"], a["net"]) == (15.0, 4.0, 11.0)
    assert a["pf"] == 3.75 and a["winrate"] == 66.67
    assert a["open"] == 1
    assert a["preset_id"] == "mtf-rsi-v1" and a["has_sidecar"] is True
    assert a["created_at"].startswith("2026-09-21T08:00")   # updated_at окна из bot_test_runs

    b = by_name[NAME_B]
    assert (b["trades"], b["wins"], b["net"]) == (1, 0, -2.0)
    assert b["has_sidecar"] is False and b["preset_id"] is None


def test_replay_list_without_window_falls_back_to_trades(env):
    engine = create_engine(f"sqlite:///{env.dbfile}")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM bot_test_runs WHERE name = :n"), {"n": NAME_A})
    engine.dispose()
    a = {r["id"]: r for r in _get(env, "/api/v1/analysis/replays")["runs"]}[NAME_A]
    assert a["created_at"].startswith("2026-09-21T07:00")   # первая сделка


# ------------------------------------------------------------------ карточка

def test_replay_detail_summary_and_tags(env):
    d = _get(env, f"/api/v1/analysis/replays/{NAME_A}")
    assert d["run"]["kind"] == "replay"
    assert d["run"]["period"][0].startswith("2026-09-21T07:00")

    s = d["summary"]
    assert (s["trades"], s["wins"], s["losses"], s["net"]) == (3, 2, 1, 11.0)
    assert s["wr"] == 66.67 and s["pf"] == 3.75
    assert s["open_positions"] == 1 and s["tickers"] == 2

    tags = d["config_tags"]
    assert tags["has_sidecar"] is True and tags["preset_id"] == "mtf-rsi-v1"
    groups = {g["group"]: g["items"] for g in tags["groups"]}
    assert set(groups) >= {"Пресет", "Роботы", "Сессии", "Выходы", "Деньги", "Вход", "Издержки"}
    assert any(i["k"] == "капитал" and i["v"] == "100000" for i in groups["Деньги"])
    assert any(i["k"] == "режим SL" for i in groups["Выходы"])

    rows = {r["ticker"]: r for r in d["rows"]}
    assert rows["SBER"]["trades"] == 2 and rows["GAZP"]["trades"] == 1
    assert d["strategies"][0]["trades"] == 3


def test_replay_detail_without_sidecar_degrades(env):
    d = _get(env, f"/api/v1/analysis/replays/{NAME_B}")
    assert d["config_tags"]["has_sidecar"] is False
    assert d["config_tags"]["groups"] == []
    assert d["summary"]["trades"] == 1


def test_replay_detail_404(env):
    res = env.client.get("/api/v1/analysis/replays/no-such-test")
    assert res.status_code == 404


# ------------------------------------------------------------------- срезы

def test_replay_slices_all_dims(env):
    d = _get(env, f"/api/v1/analysis/replays/{NAME_A}/slices")
    dims = {x["dim"]: x for x in d["dims"]}
    assert set(dims) == set(analysis_replays.REPLAY_DIMS)

    tick = dims["ticker"]["table"][0]
    assert {c["bucket"]: c["trades"] for c in tick["cells"]} == {"SBER": 2, "GAZP": 1}
    assert tick["total"]["trades"] == 3

    ex = {c["bucket"]: c["trades"] for c in dims["exit"]["table"][0]["cells"]}
    assert ex == {"signal_exit": 1, "target": 1, "stop_loss": 1}

    ent = {c["bucket"] for c in dims["entry"]["table"][0]["cells"]}
    assert ent == {"quorum", "bias"}

    reg = {c["bucket"]: c["trades"] for c in dims["regime"]["table"][0]["cells"]}
    assert reg == {"TREND_UP": 2, "TREND_DOWN": 1}

    side = {c["bucket"]: c["trades"] for c in dims["side"]["table"][0]["cells"]}
    assert side == {"LONG": 2, "SHORT": 1}

    # сессии/час/день недели считают те же 3 сделки (ярлыки зависят от таймзоны хоста)
    for dim in ("session", "hour", "weekday"):
        cells = dims[dim]["table"][0]["cells"]
        assert sum(c["trades"] for c in cells) == 3


def test_replay_slices_one_dim_and_bad_dim(env):
    d = _get(env, f"/api/v1/analysis/replays/{NAME_A}/slices", params={"dim": "exit"})
    assert [x["dim"] for x in d["dims"]] == ["exit"]
    res = env.client.get(f"/api/v1/analysis/replays/{NAME_A}/slices", params={"dim": "magic"})
    assert res.status_code == 400


# ------------------------------------------------------------------ сделки

def test_replay_trades_summary_and_filters(env):
    d = _get(env, f"/api/v1/analysis/replays/{NAME_A}/trades")
    assert d["total"] == 3
    s = d["summary"]
    assert (s["trades"], s["wins"], s["gw"], s["gl"], s["net"]) == (3, 2, 15.0, 4.0, 11.0)
    assert s["bars_avg"] == 5.0                       # 25 мин / 5min (TF из сайдкара)
    assert s["sl_min"] == 99.0 and s["tp_max"] == 105.0

    it = {i["ticker"]: i for i in d["items"]}
    assert it["SBER"]["regime_adx"] == "TREND_UP"      # из meta.entry_regime
    assert it["GAZP"]["exit"] == "target"
    assert it["SBER"]["entry_reason"] in ("quorum", "bias")

    loss = _get(env, f"/api/v1/analysis/replays/{NAME_A}/trades",
                params={"outcome": "loss"})
    assert loss["total"] == 1 and loss["items"][0]["ticker"] == "SBER"

    short = _get(env, f"/api/v1/analysis/replays/{NAME_A}/trades", params={"side": "SHORT"})
    assert short["total"] == 1 and short["items"][0]["side"] == "SHORT"

    by_ticker = _get(env, f"/api/v1/analysis/replays/{NAME_A}/trades",
                     params={"ticker": "gazp"})
    assert by_ticker["total"] == 1


def test_replay_trades_without_sidecar_has_no_bars(env):
    d = _get(env, f"/api/v1/analysis/replays/{NAME_B}/trades")
    assert d["summary"]["bars_avg"] is None


# -------------------------------------------------------------- админка бота

def test_bot_presets_endpoint(env):
    d = _get(env, "/api/v1/bot/presets")
    assert [p["id"] for p in d["presets"]] == ["mtf-rsi-v1"]
    assert d["presets"][0]["preset"]["preset"]["name"] == "MTF RSI v1"


def test_bot_tests_list_is_sql_aggregate(env):
    d = _get(env, "/api/v1/bot/tests")
    by_name = {t["name"]: t for t in d["tests"]}
    assert set(by_name) == {NAME_A, NAME_B}

    a = by_name[NAME_A]
    assert (a["trades"], a["wins"], a["losses"]) == (3, 2, 1)
    assert (a["gross_win"], a["gross_loss"], a["net"]) == (15.0, 4.0, 11.0)
    assert a["pf"] == 3.75 and a["winrate"] == 66.7
    assert a["positions_open"] == 1
    assert a["replay_start"].startswith("2026-09-21T07:00")   # окно, а не первая сделка
    assert by_name[NAME_B]["trades"] == 1


def test_bot_test_trades_endpoint(env):
    d = _get(env, f"/api/v1/bot/tests/{NAME_A}")
    assert d["test_name"] == NAME_A
    assert len(d["trades"]) == 4                       # 3 закрытых + 1 открытая
    closed = [t for t in d["trades"] if t["ts"]]
    assert sorted(round(t["net_pnl"], 2) for t in closed) == [-4.0, 5.0, 10.0]
    assert any(t["exit_reason"] == "stop_loss" for t in d["trades"])


def test_bot_test_tags_endpoint(env):
    d = _get(env, f"/api/v1/bot/tests/{NAME_A}/tags")
    assert d["test_name"] == NAME_A
    assert d["has_sidecar"] is True and d["preset_id"] == "mtf-rsi-v1"
    groups = {g["group"]: g["items"] for g in d["groups"]}
    assert groups["Пресет"][0] == {"k": "id", "v": "mtf-rsi-v1"}
    assert any(i["k"] == "pullback_ema" for i in groups["Роботы"])
    assert {"k": "движок", "v": "ensemble_v4"} in groups["Роботы"]
    assert {"k": "вход", "v": "утро/день"} in groups["Сессии"]
    assert {"k": "комиссия", "v": "0.0005"} in groups["Издержки"]

    miss = _get(env, "/api/v1/bot/tests/no-such-test/tags")
    assert miss["has_sidecar"] is False and miss["groups"] == []


def test_delete_one_test_cleans_trades_window_and_sidecar(env):
    assert (env.sidecar_dir / f"{NAME_A}.json").is_file()
    res = env.client.delete(f"/api/v1/bot/tests/{NAME_A}")
    assert res.status_code == 200, res.text
    detail = res.json()["details"][0]
    assert detail["name"] == NAME_A
    assert detail["trades"] == 4 and detail["windows"] == 1 and detail["sidecar"] is True

    assert not (env.sidecar_dir / f"{NAME_A}.json").exists()
    names = {t["name"] for t in _get(env, "/api/v1/bot/tests")["tests"]}
    assert names == {NAME_B}                           # сосед уцелел
    assert _get(env, f"/api/v1/bot/tests/{NAME_B}")["test_name"] == NAME_B
    res = env.client.get(f"/api/v1/analysis/replays/{NAME_A}")
    assert res.status_code == 404


def test_delete_tests_batch(env):
    res = env.client.post("/api/v1/bot/tests/delete",
                          json={"names": [NAME_A, NAME_B, "ghost"]})
    assert res.status_code == 200, res.text
    d = res.json()
    assert d["tests"] == 3 and d["deleted"] == 5       # 4 + 1 + 0 сделок
    assert _get(env, "/api/v1/bot/tests")["tests"] == []
    assert _get(env, "/api/v1/analysis/replays")["count"] == 0
    assert list(env.sidecar_dir.glob("*.json")) == []


def test_restart_creates_new_name_and_keeps_old(env):
    res = env.client.post(f"/api/v1/bot/tests/{NAME_A}/restart")
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["old_name"] == NAME_A
    new = out["test_name"]
    assert new != NAME_A
    assert re.fullmatch(r"mtf-rsi-v1 \d{8}-\d{4}", new), new
    assert len(new) <= preset_tags.NAME_LIMIT

    assert len(env.mode_calls) == 1
    req = env.mode_calls[0]
    assert req.mode == "test" and req.test_name == new
    assert req.replay_start == PAYLOAD["replay_start"]   # окно из сайдкара
    assert req.preset["preset"]["id"] == "mtf-rsi-v1"

    names = {t["name"] for t in _get(env, "/api/v1/bot/tests")["tests"]}
    assert NAME_A in names                              # старый прогон цел
    assert (env.sidecar_dir / f"{NAME_A}.json").is_file()


def test_restart_without_window_or_sidecar_is_400(env):
    res = env.client.post(f"/api/v1/bot/tests/{NAME_B}/restart")
    assert res.status_code == 400


def test_mode_writes_sidecar_only_with_preset(env):
    req = bot.ModeRequest(mode="test", test_name="unit side",
                          replay_start=PAYLOAD["replay_start"], test_interval="5min",
                          preset=PRESET)
    assert bot._save_test_sidecar("unit side", req, "fast") is True
    assert preset_tags.load_sidecar("unit side")["payload"]["test_interval"] == "5min"
    preset_tags.remove_sidecar("unit side")

    bare = bot.ModeRequest(mode="test", test_name="unit bare",
                           replay_start=PAYLOAD["replay_start"])
    assert bot._save_test_sidecar("unit bare", bare, "fast") is False
    assert preset_tags.load_sidecar("unit bare") is None
