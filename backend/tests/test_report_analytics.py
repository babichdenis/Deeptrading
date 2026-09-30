"""Вкладка Analytics: срезы, парсеры отчётов, обогащение сделок, API /api/v1/analysis.

Приёмка (docs/ANALYTICS_TAB.md): ансамбль-прогон виден списком/строками/срезами/
сделками, результат достижим по API, таблицы experiments не тронуты.
Здесь — изолированные куски на синтетике, чтобы тесты не зависели от БД и reports/.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from app.api.routes import analysis_reports
from app.database import Base, get_db
from app.engine.models import Candle
from app.models.reports import ReportRow, ReportRun, ReportSlice, ReportTrade
from app.services.report_slices import (
    DIMS,
    adx_regime_bucket,
    bucket_order,
    compute_slices,
    er_bucket,
    hour_bucket,
    ordered_buckets,
    session_bucket,
    weekday_bucket,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session

REPORT_TABLES = [ReportRun.__table__, ReportRow.__table__, ReportTrade.__table__, ReportSlice.__table__]
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _load_importer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_reports.py"
    spec = importlib.util.spec_from_file_location("import_reports_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


imp = _load_importer()


# --------------------------------------------------------------------- срезы

def test_session_buckets_use_msk_labels():
    assert session_bucket("2026-09-01T07:30:00+00:00") == "утро 10-14"   # 10:30 МСК
    assert session_bucket("2026-09-01T11:00:00+00:00") == "день 14-19"    # 14:00 МСК
    assert session_bucket("2026-09-01T16:00:00+00:00") == "вечер 19-24"   # 19:00 МСК
    assert session_bucket("2026-09-01T20:59:00+00:00") == "вечер 19-24"   # 23:59 МСК
    assert session_bucket("2026-09-01T05:00:00+00:00") == "вне сессий"
    assert session_bucket("2026-09-01T21:30:00+00:00") == "вне сессий"
    assert session_bucket(None) == "вне сессий"


def test_regime_and_er_buckets():
    assert adx_regime_bucket(30.0) == "тренд"
    assert adx_regime_bucket(25.0) == "тренд"
    assert adx_regime_bucket(22.0) == "переход"
    assert adx_regime_bucket(19.9) == "диапазон"
    assert adx_regime_bucket(None) == "нет данных"
    assert er_bucket(0.10) == "<0.15"
    assert er_bucket(0.15) == "0.15-0.3"
    assert er_bucket(0.2999) == "0.15-0.3"
    assert er_bucket(0.30) == ">=0.3"
    assert er_bucket(None) == "нет данных"


def test_hour_and_weekday_are_msk():
    assert hour_bucket("2026-09-01T07:00:00+00:00") == "10"
    assert weekday_bucket("2026-09-01T07:00:00+00:00") == "вт"
    assert hour_bucket("2026-09-01T21:00:00+00:00") == "00"
    assert weekday_bucket("2026-09-01T21:00:00+00:00") == "ср"


def _sample_trades() -> list[dict]:
    return [
        {"strategy": "a", "ticker": "SBER", "net_pnl": 10.0,
         "entry_time": "2026-09-01T07:30:00+00:00", "regime_adx": "тренд", "er_in": 0.5},
        {"strategy": "a", "ticker": "SBER", "net_pnl": -4.0,
         "entry_time": "2026-09-01T11:30:00+00:00", "regime_adx": "диапазон", "er_in": 0.1},
        {"strategy": "b", "ticker": "LKOH", "net_pnl": -2.0,
         "entry_time": "2026-09-02T17:00:00+00:00", "regime_adx": "тренд", "er_in": 0.4},
        {"strategy": "b", "ticker": "LKOH", "net_pnl": 5.0,
         "entry_time": "2026-09-03T05:00:00+00:00", "regime_adx": "переход", "er_in": None},
    ]


def test_compute_slices_totals_are_invariant_per_dim():
    trades = _sample_trades()
    rows = compute_slices(trades)
    for dim in DIMS:
        subset = [r for r in rows if r["dim"] == dim]
        assert subset, f"dim {dim} пуст"
        assert sum(r["trades"] for r in subset) == len(trades)
        assert sum(r["gross_wins_n"] for r in subset) == sum(1 for t in trades if t["net_pnl"] > 0)
        assert sum(r["gross_losses_n"] for r in subset) == sum(1 for t in trades if t["net_pnl"] <= 0)
        assert round(sum(r["net"] for r in subset), 6) == round(sum(t["net_pnl"] for t in trades), 6)
        assert round(sum(r["gw"] for r in subset), 6) == round(
            sum(t["net_pnl"] for t in trades if t["net_pnl"] > 0), 6)


def test_compute_slices_groups_by_strategy():
    rows = compute_slices(_sample_trades())
    by_strat = {}
    for r in rows:
        if r["dim"] == "session" and r["strategy"] == "a":
            by_strat[r["bucket"]] = r["trades"]
    assert by_strat["утро 10-14"] == 1
    assert by_strat["день 14-19"] == 1


def test_ordered_buckets_follows_canonical_order():
    assert bucket_order("session")[0] == "утро 10-14"
    assert ordered_buckets("session", {"вне сессий", "утро 10-14"}) == [
        "утро 10-14", "день 14-19", "вечер 19-24", "вне сессий"]
    hours = ordered_buckets("hour", {"12", "02", "99?"})
    assert hours[:3] == ["00", "01", "02"] and hours[-1] == "99?" and len(hours) == 25
    assert ordered_buckets("regime_adx", {"диапазон", "тренд", "переход", "нет данных"}) == [
        "тренд", "переход", "диапазон", "нет данных"]


# ------------------------------------------------------------------- парсеры

def test_detect_kind(tmp_path):
    real = tmp_path / "bt_ose_real_1.json"
    real.write_text(json.dumps({"meta": {}, "raw": []}), encoding="utf-8")
    matrix = tmp_path / "ose_matrix_1.json"
    matrix.write_text(json.dumps({"period": [], "raw": []}), encoding="utf-8")
    wf = tmp_path / "WF_x.json"
    wf.write_text(json.dumps({"meta": {}, "phases": []}), encoding="utf-8")
    exp = tmp_path / "EXP-1.json"
    exp.write_text(json.dumps({"experiment_id": "E1"}), encoding="utf-8")
    junk = tmp_path / "notes.json"
    junk.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    assert imp.detect_kind(real, json.loads(real.read_text())) == "real"
    assert imp.detect_kind(matrix, json.loads(matrix.read_text())) == "matrix"
    assert imp.detect_kind(wf, json.loads(wf.read_text())) == "wf"
    assert imp.detect_kind(exp, json.loads(exp.read_text())) == "exp"
    assert imp.detect_kind(junk, json.loads(junk.read_text())) is None


def _real_payload() -> dict:
    return {
        "meta": {"created_utc": "2026-09-30T02:00:05+00:00", "interval": "10min",
                 "period": ["2026-09-01", "2026-09-24"], "tickers": ["SBER", "LKOH"]},
        "raw": [
            {"strategy": "bot", "exit": "x01", "ticker": "SBER", "trades": 2, "wins": 1,
             "gw": 10.0, "gl": 4.0, "net": 6.0, "max_dd_pct": 0.5, "commission": 1.0, "sec": 2.0,
             "trades_detail": [
                 {"side": "LONG", "entry_time": "2026-09-01T07:30:00+00:00", "entry_price": 100.0,
                  "exit_time": "2026-09-01T08:30:00+00:00", "exit_price": 101.0,
                  "exit_reason": "target", "bars_held": 6, "net_pnl": 10.0},
                 {"side": "SHORT", "entry_time": "2026-09-01T11:30:00+00:00", "entry_price": 100.0,
                  "exit_time": "2026-09-01T12:30:00+00:00", "exit_price": 100.5,
                  "exit_reason": "stop_loss", "bars_held": 6, "net_pnl": -4.0},
             ]},
            {"strategy": "bot", "exit": "x01", "ticker": "LKOH", "error": "boom", "sec": 1.0},
        ],
    }


def test_parse_real_keeps_rows_and_details(tmp_path):
    p = tmp_path / "bt_ose_real_1.json"
    data = _real_payload()
    parsed = imp.parse_real(p, data)
    assert parsed["kind"] == "real"
    assert len(parsed["rows"]) == 2, "error-строка тоже строка: показываем её с trades=0"
    err_row = next(r for r in parsed["rows"] if r["ticker"] == "LKOH")
    assert err_row["trades"] == 0 and err_row["raw"]["error"] == "boom"
    row = parsed["rows"][0]
    assert (row["trades"], row["wins"], row["gw"], row["gl"], row["net"]) == (2, 1, 10.0, 4.0, 6.0)
    assert row["pf"] == pytest.approx(2.5)
    assert len(parsed["trades"]) == 2
    assert parsed["trades"][0]["net_pnl"] == 10.0
    assert parsed["meta"]["interval"] == "10min"


def test_parse_wf_aggregates_oos_only(tmp_path):
    p = tmp_path / "WF_x.json"
    data = {
        "meta": {"name": "wf_demo", "created_utc": "2026-09-30T01:00:00+00:00"},
        "phases": [
            {"type": "IS", "by_label": {"bot": {"trades": 100, "wins": 50, "gross_win": 9.0,
                                                "gross_loss": 1.0, "net_pnl": 8.0}}},
            {"type": "OOS", "by_label": {"bot": {"trades": 10, "wins": 4, "gross_win": 5.0,
                                                 "gross_loss": 3.0, "net_pnl": 2.0,
                                                 "commission": 0.5, "max_drawdown_pct": 1.5}}},
            {"type": "OOS", "by_label": {"bot": {"trades": 6, "wins": 3, "gross_win": 4.0,
                                                 "gross_loss": 1.0, "net_pnl": 3.0,
                                                 "commission": 0.25, "max_drawdown_pct": 0.5}}},
        ],
    }
    parsed = imp.parse_wf(p, data)
    assert parsed["kind"] == "wf" and parsed["name"] == "wf_demo"
    assert len(parsed["rows"]) == 1
    row = parsed["rows"][0]
    assert (row["trades"], row["wins"], row["net"]) == (16, 7, 5.0)
    assert row["gw"] == pytest.approx(9.0) and row["gl"] == pytest.approx(4.0)
    assert row["commission"] == pytest.approx(0.75)
    assert row["max_dd_pct"] == pytest.approx(1.5)
    assert row["raw"]["oos_phases"] == 2
    assert parsed["trades"] == []


def test_parse_exp_rows_and_trades(tmp_path):
    p = tmp_path / "EXP-1.json"
    one = {"side": "LONG", "entry_time": "2026-09-01T07:30:00+00:00", "entry_price": 100.0,
           "exit_time": "2026-09-01T09:30:00+00:00", "exit_price": 102.0,
           "exit_reason": "target", "bars_held": 2, "net_pnl": 2.0}
    two = {**one, "net_pnl": -1.0, "exit_reason": "stop_loss"}
    data = {
        "experiment_id": "EXP-1", "name": "cfg:x", "status": "completed",
        "created_utc": "2026-09-30T02:00:00+00:00",
        "config": {"exits": ["x01"], "timeframe": "10min", "period": ["2026-09-01", "2026-09-24"]},
        "artifacts": {"per_ticker": {"SBER": {"trades": [one, two]}, "LKOH": {"trades": [one]}}},
    }
    parsed = imp.parse_exp(p, data)
    assert parsed["kind"] == "exp"
    assert {r["ticker"] for r in parsed["rows"]} == {"SBER", "LKOH"}
    sber = next(r for r in parsed["rows"] if r["ticker"] == "SBER")
    assert (sber["trades"], sber["wins"], sber["net"]) == (2, 1, 1.0)
    assert all(r["exit"] == "x01" for r in parsed["rows"])
    assert len(parsed["trades"]) == 3


# --------------------------------------------------------------- обогащение

def _trend_bars(n: int = 120, step: float = 0.5) -> tuple[list[Candle], list[float]]:
    closes = [100.0 + i * step for i in range(n)]
    bars = [Candle(ts=T0 + timedelta(minutes=10 * i), open=c, high=c, low=c, close=c, volume=10.0)
            for i, c in enumerate(closes)]
    return bars, closes


def test_enrich_from_series_fills_everything():
    bars, closes = _trend_bars()
    trades = [{
        "strategy": "bot", "ticker": "SBER", "side": "LONG", "exit": "x01",
        "entry_time": T0 + timedelta(minutes=600), "exit_time": T0 + timedelta(minutes=700),
        "entry_price": closes[60], "net_pnl": 5.0, "session": None,
    }]
    n_tickers, n_trades = imp.enrich_from_series({"SBER": trades}, {"SBER": bars})
    assert (n_tickers, n_trades) == (1, 1)

    t = trades[0]
    assert t["session"] == "утро 10-14"
    assert t["regime_adx"] == "тренд"          # линейный рост → ADX = 100
    assert t["er_in"] == pytest.approx(1.0)    # прямая → ER = 1
    # x01 = fixed 1%/2% → уровни считаются от цены входа
    assert t["sl_price"] == pytest.approx(closes[60] * 0.99)
    assert t["tp_price"] == pytest.approx(closes[60] * 1.02)
    # в окне вход→выход цена только растёт: MAE = 0, MFE > 0 (в единицах ATR)
    assert t["mae_atr"] == 0.0
    assert t["mfe_atr"] and t["mfe_atr"] > 0


def test_enrich_from_series_respects_signal_only_exit():
    bars, closes = _trend_bars()
    trades = [{
        "strategy": "bot", "ticker": "SBER", "side": "SHORT", "exit": "x07",
        "entry_time": T0 + timedelta(minutes=600), "exit_time": T0 + timedelta(minutes=700),
        "entry_price": closes[60], "net_pnl": -1.0,
    }]
    imp.enrich_from_series({"SBER": trades}, {"SBER": bars})
    assert trades[0]["sl_price"] is None
    assert trades[0]["tp_price"] is None
    assert trades[0]["regime_adx"] == "тренд"


def test_enrich_from_series_without_series_is_noop():
    trades = [{"ticker": "SBER", "entry_time": T0, "net_pnl": 1.0}]
    assert imp.enrich_from_series({"SBER": trades}, {}) == (0, 0)


# ---------------------------------------------------------------- импортёр

class _Args:
    def __init__(self, tmp_path: Path):
        self.reports_dir = str(tmp_path)
        self.no_enrich = True
        self.force = False
        self.dry_run = False


@pytest.fixture()
def sqlite_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/reports.db")
    Base.metadata.create_all(engine, tables=REPORT_TABLES)
    session = Session(engine)
    yield session
    session.close()
    engine.dispose()


def test_import_is_idempotent_by_content_hash(tmp_path, sqlite_session):
    payload = _real_payload()
    p = tmp_path / "bt_ose_real_1.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    args = _Args(tmp_path)

    first = imp.import_file(sqlite_session, p, None, args)
    assert first.startswith("NEW"), first
    assert sqlite_session.query(ReportRun).count() == 1
    n_rows = sqlite_session.query(ReportRow).count()
    n_trades = sqlite_session.query(ReportTrade).count()
    n_slices = sqlite_session.query(ReportSlice).count()
    assert n_rows == 2 and n_trades == 2 and n_slices > 0

    second = imp.import_file(sqlite_session, p, None, args)
    assert "уже импортирован" in second
    assert sqlite_session.query(ReportRun).count() == 1
    assert sqlite_session.query(ReportRow).count() == n_rows
    assert sqlite_session.query(ReportTrade).count() == n_trades
    assert sqlite_session.query(ReportSlice).count() == n_slices

    run = sqlite_session.query(ReportRun).one()
    assert run.content_hash == hashlib.sha256(p.read_bytes()).hexdigest()
    assert run.kind == "real"


def test_import_force_rewrites_same_hash(tmp_path, sqlite_session):
    p = tmp_path / "bt_ose_real_1.json"
    p.write_text(json.dumps(_real_payload()), encoding="utf-8")
    args = _Args(tmp_path)
    imp.import_file(sqlite_session, p, None, args)
    args.force = True
    msg = imp.import_file(sqlite_session, p, None, args)
    assert msg.startswith("NEW")
    assert sqlite_session.query(ReportRun).count() == 1
    assert sqlite_session.query(ReportTrade).count() == 2


def test_imported_slices_have_all_dims(tmp_path, sqlite_session):
    p = tmp_path / "bt_ose_real_1.json"
    p.write_text(json.dumps(_real_payload()), encoding="utf-8")
    imp.import_file(sqlite_session, p, None, _Args(tmp_path))
    dims = {s.dim for s in sqlite_session.query(ReportSlice).all()}
    assert dims == set(DIMS)


# --------------------------------------------------------------------- API

def _seed(session: Session) -> int:
    run = ReportRun(file_name="bt_ose_real_1.json", kind="real", name="bt_ose_real_1",
                    created_at=datetime(2026, 9, 30, 2, 0, tzinfo=UTC),
                    meta={"interval": "10min", "period": ["2026-09-01", "2026-09-24"]},
                    content_hash="h" * 64, mtime=datetime(2026, 9, 30, 2, 0, tzinfo=UTC))
    session.add(run)
    session.flush()
    session.add(ReportRow(run_id=run.id, strategy="bot", exit="x01", ticker="SBER",
                          trades=2, wins=1, gw=10.0, gl=4.0, net=6.0, pf=2.5, max_dd_pct=0.5,
                          commission=1.0, sec=2.0))
    session.add(ReportRow(run_id=run.id, strategy="bot", exit="x01", ticker="LKOH",
                          trades=1, wins=0, gw=0.0, gl=2.0, net=-2.0, pf=0.0, max_dd_pct=0.2,
                          commission=0.5, sec=1.0))
    trades = [
        {"strategy": "bot", "exit": "x01", "ticker": "SBER", "side": "LONG",
         "entry_time": datetime(2026, 9, 1, 7, 30, tzinfo=UTC),
         "exit_time": datetime(2026, 9, 1, 8, 30, tzinfo=UTC),
         "entry_price": 100.0, "exit_price": 101.0, "exit_reason": "target", "bars_held": 6,
         "net_pnl": 10.0, "sl_price": 99.0, "tp_price": 102.0, "mae_atr": 0.2, "mfe_atr": 1.4,
         "session": "утро 10-14", "regime_adx": "тренд", "er_in": 0.5},
        {"strategy": "bot", "exit": "x01", "ticker": "SBER", "side": "SHORT",
         "entry_time": datetime(2026, 9, 1, 11, 30, tzinfo=UTC),
         "exit_time": datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
         "entry_price": 100.0, "exit_price": 100.5, "exit_reason": "stop_loss", "bars_held": 6,
         "net_pnl": -4.0, "sl_price": 101.0, "tp_price": 98.0, "mae_atr": 1.1, "mfe_atr": 0.3,
         "session": "день 14-19", "regime_adx": "диапазон", "er_in": 0.1},
        {"strategy": "bot", "exit": "x01", "ticker": "LKOH", "side": "LONG",
         "entry_time": datetime(2026, 9, 2, 17, 0, tzinfo=UTC),
         "exit_time": datetime(2026, 9, 2, 18, 0, tzinfo=UTC),
         "entry_price": 6000.0, "exit_price": 6000.0, "exit_reason": "signal_exit", "bars_held": 6,
         "net_pnl": -2.0, "sl_price": 5940.0, "tp_price": 6120.0, "mae_atr": 0.9, "mfe_atr": 0.1,
         "session": "вечер 19-24", "regime_adx": "тренд", "er_in": 0.4},
    ]
    for t in trades:
        session.add(ReportTrade(run_id=run.id, **t))
    for s in compute_slices([{**t, "strategy": "bot"} for t in trades]):
        session.add(ReportSlice(run_id=run.id, **s))
    session.commit()
    return run.id


@pytest.fixture()
def api_client(tmp_path):
    dbfile = tmp_path / "api.db"
    sync_engine = create_engine(f"sqlite:///{dbfile}")
    Base.metadata.create_all(sync_engine, tables=REPORT_TABLES)
    since = datetime.now(UTC) - timedelta(days=7)
    with sync_engine.begin() as conn:
        conn.execute(text("CREATE TABLE instruments (figi TEXT, ticker TEXT)"))
        conn.execute(text(
            "CREATE TABLE candles (figi TEXT, interval INTEGER, ts TIMESTAMP, "
            "open DOUBLE PRECISION, close DOUBLE PRECISION)"))
        for i in range(6):
            conn.execute(text("INSERT INTO instruments (figi, ticker) VALUES ('F1', 'SBER')"))
            conn.execute(
                text("INSERT INTO candles (figi, interval, ts, open, close) "
                     "VALUES ('F1', 24, :ts, :o, :c)"),
                {"ts": since + timedelta(days=i), "o": 100.0 + i, "c": 100.0 + i * 1.5},
            )
    with Session(sync_engine) as seed_session:
        run_id = _seed(seed_session)
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{dbfile}")
    app = FastAPI()
    app.include_router(analysis_reports.router)

    async def _override():
        async with AsyncSession(async_engine, expire_on_commit=False) as session:
            yield session

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as client:
        yield client, run_id
    asyncio.run(async_engine.dispose())


def test_api_reports_list(api_client):
    client, run_id = api_client
    res = client.get("/api/v1/analysis/reports")
    assert res.status_code == 200
    data = res.json()
    assert data["count"] == 1
    run = data["runs"][0]
    assert run["id"] == run_id
    assert (run["trades"], run["wins"], run["gw"], run["gl"], run["net"]) == (3, 1, 10.0, 6.0, 4.0)
    assert run["kind"] == "real" and run["detail_trades"] == 3

    res = client.get("/api/v1/analysis/reports", params={"kind": "wf"})
    assert res.json()["count"] == 0


def test_api_report_detail_has_gross_in_pieces_and_rubles(api_client):
    client, run_id = api_client
    res = client.get(f"/api/v1/analysis/reports/{run_id}")
    assert res.status_code == 200
    data = res.json()
    summary = data["summary"]
    assert summary["trades"] == 3
    assert (summary["wins"], summary["losses"]) == (1, 2)
    assert summary["gw"] == pytest.approx(10.0)
    assert summary["gl"] == pytest.approx(6.0)
    assert summary["wr"] == pytest.approx(33.33)
    assert summary["expectancy"] == pytest.approx(4.0 / 3, abs=5e-4)
    assert len(data["strategies"]) == 1
    strat = data["strategies"][0]
    assert strat["tickers"] == 2 and strat["exits"] == ["x01"]
    assert len(data["rows"]) == 2
    assert set(data["dims"]) == set(DIMS)


def test_api_report_404(api_client):
    client, _ = api_client
    assert client.get("/api/v1/analysis/reports/9999").status_code == 404


def test_api_slices_by_session(api_client):
    client, run_id = api_client
    res = client.get(f"/api/v1/analysis/reports/{run_id}/slices", params={"dim": "session"})
    assert res.status_code == 200
    dims = res.json()["dims"]
    assert len(dims) == 1 and dims[0]["dim"] == "session"
    assert dims[0]["buckets"] == ["утро 10-14", "день 14-19", "вечер 19-24", "вне сессий"]
    cells = dims[0]["table"][0]["cells"]
    assert [c["trades"] for c in cells] == [1, 1, 1, 0]
    assert dims[0]["table"][0]["total"]["trades"] == 3


def test_api_slices_rejects_unknown_dim(api_client):
    client, run_id = api_client
    assert client.get(f"/api/v1/analysis/reports/{run_id}/slices",
                      params={"dim": "weather"}).status_code == 400


def test_api_trades_summary_has_sl_tp_bounds(api_client):
    client, run_id = api_client
    res = client.get(f"/api/v1/analysis/reports/{run_id}/trades")
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 3 and len(data["items"]) == 3
    summary = data["summary"]
    assert summary["sl_min"] == pytest.approx(99.0)
    assert summary["sl_max"] == pytest.approx(5940.0)
    assert summary["tp_min"] == pytest.approx(98.0)
    assert summary["tp_max"] == pytest.approx(6120.0)
    assert summary["mae_atr_max"] == pytest.approx(1.1)
    assert summary["mfe_atr_max"] == pytest.approx(1.4)
    assert summary["bars_avg"] == pytest.approx(6.0)
    assert summary["worst"] == pytest.approx(-4.0) and summary["best"] == pytest.approx(10.0)


def test_api_trades_filters(api_client):
    client, run_id = api_client
    base = f"/api/v1/analysis/reports/{run_id}/trades"
    assert client.get(base, params={"ticker": "SBER"}).json()["total"] == 2
    assert client.get(base, params={"outcome": "win"}).json()["total"] == 1
    assert client.get(base, params={"outcome": "loss"}).json()["total"] == 2
    assert client.get(base, params={"session": "вечер 19-24"}).json()["total"] == 1
    assert client.get(base, params={"regime_adx": "тренд"}).json()["total"] == 2
    assert client.get(base, params={"side": "short"}).json()["total"] == 1
    assert client.get(base, params={"exit": "x99"}).json()["total"] == 0


def test_api_market_leaders(api_client):
    client, _ = api_client
    res = client.get("/api/v1/analysis/market/leaders", params={"window": "week"})
    assert res.status_code == 200
    data = res.json()
    assert data["window"] == "week" and data["count"] >= 1
    assert data["leaders"][0]["ticker"] == "SBER"
    assert data["leaders"][0]["pct"] > 0


def test_import_does_not_touch_experiments_tables(tmp_path, sqlite_session):
    p = tmp_path / "bt_ose_real_1.json"
    p.write_text(json.dumps(_real_payload()), encoding="utf-8")
    imp.import_file(sqlite_session, p, None, _Args(tmp_path))
    written = {row[0] for row in sqlite_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert "experiments" not in written
    assert "experiment_trades" not in written
    assert "sandbox_trades" not in written
    assert {"report_runs", "report_rows", "report_trades", "report_slices"} <= written
