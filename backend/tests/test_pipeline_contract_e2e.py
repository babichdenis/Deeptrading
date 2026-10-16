"""Сквозной контракт пайплайна данных (B3): источник → запись → CandleHub → A7/A24.

Контур (реальные модули, без сети и рабочей БД):
    stub источника → upsert_candles (изолированный PostgreSQL) → seed_from_db
    → CandleHub (1m-ряд) → A7 (build_ticker_data/run_ticker) → A24
    (execute_candidate_signal) → сводка.

Правила:
- Внешняя сеть мокается (fetch_candles подменяется stub-ом), собственная логика
  приложения НЕ дублируется тестовым алгоритмом.
- PostgreSQL-тесты идут ТОЛЬКО в тестовую БД по PIPELINE_TEST_DATABASE_URL
  (fail-closed, см. fixtures/pipeline_contract/db.py). Без этой переменной
  контур не запускается; обязательный gate не должен становиться зелёным.
- Одиночный прогон контура: `python scripts/check_pipeline_contract.py`.
  Прямой запуск pytest активируется `PIPELINE_CONTRACT=1`.

Тесты, кодирующие КОНТРАКТ, который пока не выполняется кодом, помечены
комментарием `ДЕФЕКТ` и адресуются владельцу файла (B1 — работа с данными,
B2 — расчёт исполнения). Они падают до исправления и проходят после.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import pytest_asyncio

BACKEND = Path(__file__).resolve().parents[1]
_SCRIPTS = BACKEND / "scripts"
for _p in (str(BACKEND), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import day_candidate_execution as a24  # noqa: E402
import day_strategies_core as a7  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.engine.models import Candle as EngineCandle  # noqa: E402
from app.models.candle import Candle as DBCandle  # noqa: E402
from app.services import candle_integrity  # noqa: E402
from app.services import candle_hub_orchestrator as cho  # noqa: E402
from app.services import tinvest  # noqa: E402
from tests.fixtures.pipeline_contract import dataset as ds  # noqa: E402
from tests.fixtures.pipeline_contract import db as tdb  # noqa: E402

_COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=5.0)
_S1A = a7.StrategyConfig("S1-A", "S1", "12:00", "12:10", theta=0.004)
_ENTRY_HHMM = "12:10"


# --------------------------------------------------------------------- контур БД
def _contract_mode() -> bool:
    return os.environ.get("PIPELINE_CONTRACT") == "1"


@pytest_asyncio.fixture
async def db_session():
    """Сессия изолированной тестовой БД. Вне gate-режима — skip.

    Отсутствие/непригодность контура (нет PIPELINE_TEST_DATABASE_URL, имя без
    `test`) — ошибка, а не зелёный skip.
    """
    if not _contract_mode():
        pytest.skip("pipeline DB contour: запускать через scripts/check_pipeline_contract.py")
    target = tdb.resolve_test_target()
    await tdb.prepare_schema(target.url)
    engine, maker = tdb.make_sessionmaker(target.url)
    try:
        async with maker() as session:
            await tdb.truncate(session)
            yield session
            await session.rollback()
    finally:
        await engine.dispose()


# --------------------------------------------------------------------- помощники
def _two_days() -> tuple:
    days = ds.last_weekdays(2)
    return days[0], days[1]


def _happy_corpus() -> tuple:
    """warmup-день (ровно 100.0) + eval-день со скачком 100→101 в 12:00.

    S1-A long: |half_ret| = 1% > theta; вход 12:10; SL далеко (ATR мал);
    выход по времени 18:30 -> FILLED.
    """
    warm, ev = _two_days()
    rows = ds.rows_from_prices(ds.ANON_FIGI, warm, ds.day_prices(warm, base=100.0))
    rows += ds.rows_from_prices(
        ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0, overrides={"12:00": 101.0})
    )
    return warm, ev, rows


class Pipeline:
    """Сквозной путь поверх реальных модулей (без сети)."""

    def __init__(self, db):
        self.db = db
        self.figi = ds.ANON_FIGI
        self.orch = cho.CandleHubOrchestrator()

    async def write(self, rows) -> int:
        return await tinvest.upsert_candles(self.db, rows)

    async def load_from_db(self) -> int:
        n = await self.orch.seed_from_db(self.db, self.figi)
        self.orch.ensure_tfs(self.figi)
        return n

    def hub_minutes(self) -> list:
        series = self.orch.hub.series(self.figi, "1min")
        return [
            a7.MinuteBar(c.ts, float(c.open), float(c.high), float(c.low),
                         float(c.close), float(c.volume))
            for c in series.snapshot()
        ]

    async def count_db(self) -> int:
        return int((await self.db.execute(
            select(func.count()).select_from(DBCandle).where(DBCandle.figi == self.figi)
        )).scalar_one())


async def _load_pipeline(db, rows) -> "Pipeline":
    pipe = Pipeline(db)
    await pipe.write(rows)
    await pipe.load_from_db()
    return pipe


def _tdata(pipe: Pipeline, warm, ev) -> a7.TickerData:
    issues: list = []
    return a7.build_ticker_data(ds.ANON_TICKER, pipe.hub_minutes(), warm, issues, ev)


def _run_s1a(tdata: a7.TickerData, ev):
    return a7.run_ticker(tdata, [_S1A], ev, ev, _COSTS)


def _signal_from_trade(trade, ev, *, entry_hhmm=_ENTRY_HHMM) -> dict:
    entry_iso = a7.label_ts(ev, entry_hhmm).isoformat()
    return {
        "type": "signal",
        "candidate_id": "C1",
        "ticker": ds.ANON_TICKER,
        "day": ev.isoformat(),
        "spec_hash": a24.CANDIDATE_SPEC_HASH,
        "side": trade["side"],
        "atr": float(trade["atr"]),
        "decision_label": "12:00",
        "decision_close_time": entry_iso,
        "intended_entry_time": entry_iso,
        "observed_until": entry_iso,
    }


# ============================================================
# 1. Корректный пакет: источник → БД → CandleHub → A7 → FILLED
# ============================================================
async def test_happy_path_db_to_a7_filled(db_session):
    warm, ev, rows = _happy_corpus()
    pipe = await _load_pipeline(db_session, rows)

    assert await pipe.count_db() == len(rows)
    assert len(pipe.hub_minutes()) == len(rows)

    tdata = _tdata(pipe, warm, ev)
    trades, skips = _run_s1a(tdata, ev)

    assert skips == []
    assert len(trades) == 1
    t = trades[0]
    assert t["status"] == "FILLED"
    assert t["exit_reason"] == "time_exit_18_30"
    assert t["side"] == "long"
    assert abs(t["net"] - (t["gross"] - t["costs_total"])) < 1e-9  # тождество accounting
    assert t["net"] == t["net"]  # не NaN


async def test_happy_path_repeat_is_idempotent(db_session):
    warm, ev, rows = _happy_corpus()
    pipe = await _load_pipeline(db_session, rows)
    n1 = await pipe.count_db()
    t1 = _run_s1a(_tdata(pipe, warm, ev), ev)[0][0]

    await pipe.write(rows)  # повтор того же пакета
    await pipe.load_from_db()
    n2 = await pipe.count_db()
    t2 = _run_s1a(_tdata(pipe, warm, ev), ev)[0][0]

    assert n1 == n2 == len(rows)
    assert json.dumps(t1, sort_keys=True, default=str) == json.dumps(t2, sort_keys=True, default=str)


async def test_future_bars_do_not_change_accepted_signal(db_session):
    """Причинность: бары ПОСЛЕ входа не меняют принятое решение/вход."""
    warm, ev, rows = _happy_corpus()
    pipe_a = await _load_pipeline(db_session, rows)
    t_a = _run_s1a(_tdata(pipe_a, warm, ev), ev)[0][0]

    # второй вариант: после входа цена уходит вниз (будущее меняется)
    rows_b = ds.rows_from_prices(
        ds.ANON_FIGI, warm, ds.day_prices(warm, base=100.0)
    ) + ds.rows_from_prices(
        ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0, overrides={"12:00": 101.0, "13:00": 80.0})
    )
    await tdb.truncate(db_session)
    pipe_b = await _load_pipeline(db_session, rows_b)
    t_b = _run_s1a(_tdata(pipe_b, warm, ev), ev)[0][0]

    for field in ("side", "entry_ts", "entry_base", "decision_close", "atr", "signal_time"):
        assert t_a[field] == t_b[field], f"причинность нарушена: {field}"


# ============================================================
# 2. Дубли и граница записи
# ============================================================
async def test_conflicting_duplicate_rejected_explicitly(db_session):
    """ДЕФЕКТ (B1): конфликт дублей должен отклоняться явным контрактным отказом
    ДО записи, а не сырой ошибкой драйвера и не случайной last-write."""
    _warm, ev, rows = _happy_corpus()
    ts0 = a7.label_ts(ev, "12:00")
    good = next(r for r in rows if r["ts"] == ts0)
    conflict = list(rows) + [dict(good, close=float(good["close"]) + 5.0)]

    from asyncpg.exceptions import CardinalityViolationError

    raised: BaseException | None = None
    try:
        await tinvest.upsert_candles(db_session, conflict)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    await db_session.rollback()

    if raised is not None:
        assert not isinstance(raised, CardinalityViolationError), (
            "B1: конфликт дублей дал сырую ошибку драйвера (CardinalityViolation) "
            f"вместо явного контрактного отказа: {raised!r}"
        )
        return
    persisted = (await db_session.execute(
        select(DBCandle.id).where(DBCandle.figi == ds.ANON_FIGI,
                                   DBCandle.interval == 1, DBCandle.ts == ts0)
    )).all()
    assert persisted == [], "B1: конфликт дублей разрешён молча (last-write) — нет явного отказа"


@pytest.mark.parametrize("mutation", ["nan", "negative_volume", "bad_range", "negative_price"])
async def test_invalid_numeric_rejected_at_write_boundary(db_session, mutation):
    """ДЕФЕКТ (B1): запись OHLCV без собственной проверки — NaN/Inf, отрицательные
    цены/объём, high<low не должны попадать в БД."""
    _warm, ev, rows = _happy_corpus()
    ts0 = a7.label_ts(ev, "12:00")
    bad = dict(next(r for r in rows if r["ts"] == ts0))
    if mutation == "nan":
        bad["close"] = float("nan")
    elif mutation == "negative_volume":
        bad["volume"] = -1
    elif mutation == "bad_range":
        bad["low"] = bad["high"] + 10.0
    elif mutation == "negative_price":
        bad["open"] = -1.0

    result = None
    raised = False
    try:
        result = await tinvest.upsert_candles(db_session, [bad])
    except BaseException:  # noqa: BLE001
        raised = True
    await db_session.rollback()
    persisted = (await db_session.execute(
        select(DBCandle.id).where(DBCandle.figi == bad["figi"],
                                   DBCandle.interval == bad["interval"], DBCandle.ts == bad["ts"])
    )).all()
    assert not persisted, f"B1: невалидная свеча ({mutation}) записана в БД"
    assert raised or result == 0, (
        f"B1: невалидная свеча ({mutation}) не отклонена явно (тихий успех, saved={result!r})"
    )


async def test_invalidate_failure_is_not_swallowed(db_session):
    """ДЕФЕКТ (B1): ошибка invalidate подавляется (`except Exception: pass`) —
    успешный ответ при неаннулированном кэше целостности недопустим."""
    _warm, _ev, rows = _happy_corpus()

    async def _boom(*_a, **_k):
        raise RuntimeError("integrity cache down")

    original = candle_integrity.invalidate
    candle_integrity.invalidate = _boom
    try:
        succeeded = False
        try:
            await tinvest.upsert_candles(db_session, rows)
            succeeded = True
        except BaseException:  # noqa: BLE001
            succeeded = False
        await db_session.rollback()
    finally:
        candle_integrity.invalidate = original

    assert not succeeded, "B1: ошибка invalidate подавлена — запись вернула успех"


# ============================================================
# 3. Атомарность: нет преждевременного hub update при провале записи
# ============================================================
async def _seed_hub_with_gap(pipe: Pipeline, ev, drop_hhmm: str) -> int:
    rows = ds.rows_from_prices(
        ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0, overrides={"12:00": 101.0})
    )
    drop_ts = a7.label_ts(ev, drop_hhmm)
    kept = ds.without(rows, {drop_ts})
    candles = [
        EngineCandle(ts=r["ts"], open=float(r["open"]), high=float(r["high"]),
                     low=float(r["low"]), close=float(r["close"]), volume=float(r["volume"]))
        for r in kept
    ]
    pipe.orch.hub.seed_1m(pipe.figi, candles, source="db", fire=False)
    return len(candles)


async def test_write_error_does_not_publish_to_hub(db_session, monkeypatch):
    """ДЕФЕКТ (B1): fill_gaps публикует в hub ДО успешного сохранения. Провал
    upsert_candles не должен менять hub."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    before = await _seed_hub_with_gap(pipe, ev, "13:00")
    gap_row = next(
        r for r in ds.rows_from_prices(ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0,
                                      overrides={"12:00": 101.0}))
        if r["ts"] == a7.label_ts(ev, "13:00")
    )

    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row])

    async def _fail(_db, _rows):
        raise RuntimeError("db write failed")

    monkeypatch.setattr(cho, "upsert_candles", _fail)

    with pytest.raises(RuntimeError):
        await pipe.orch.fill_gaps(db_session, pipe.figi)

    after = len(pipe.orch.hub.series(pipe.figi, "1min"))
    assert after == before, "B1: hub опубликовал свечи до успешного сохранения в БД"


async def test_empty_source_is_not_error_and_no_fake_data(db_session, monkeypatch):
    """Пустой успешный ответ источника — не ошибка и не выдуманные данные."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    before = await _seed_hub_with_gap(pipe, ev, "13:00")

    calls: list = []

    def _empty(figi, interval, _a, _b):
        calls.append((figi, interval))
        return []

    monkeypatch.setattr(cho, "fetch_candles", _empty)
    filled = await pipe.orch.fill_gaps(db_session, pipe.figi)

    assert calls, "fill_gaps должен обращаться к источнику (правильный рабочий путь)"
    assert all(f == pipe.figi for f, _ in calls)
    assert filled == 0
    assert len(pipe.orch.hub.series(pipe.figi, "1min")) == before


# ============================================================
# 4. Пропуски пути: до входа / после входа / после завершённого выхода
# ============================================================
async def test_gap_before_entry_skips(db_session):
    warm, ev, rows = _happy_corpus()
    rows = ds.without(rows, {a7.label_ts(ev, _ENTRY_HHMM)})
    pipe = await _load_pipeline(db_session, rows)

    trades, skips = _run_s1a(_tdata(pipe, warm, ev), ev)
    assert trades == []
    assert [s["reason"] for s in skips] == ["entry_minute_missing"]


async def test_gap_after_entry_is_incomplete_net_unknown(db_session):
    warm, ev, rows = _happy_corpus()
    rows = ds.without(rows, {a7.label_ts(ev, "13:00")})
    pipe = await _load_pipeline(db_session, rows)

    trades, _ = _run_s1a(_tdata(pipe, warm, ev), ev)
    assert len(trades) == 1
    t = trades[0]
    assert t["status"] == "INCOMPLETE"
    assert t["incomplete_reason"] == "path_gap_after_entry"
    assert t["net"] == "" and t["exit_fill"] == ""  # net неизвестен, не 0


async def test_gap_after_completed_exit_stays_filled(db_session):
    """Стоп, достоверно исполненный ДО дыры, остаётся FILLED."""
    warm, ev = _two_days()
    rows = ds.rows_from_prices(ds.ANON_FIGI, warm, ds.day_prices(warm, base=100.0))
    rows += ds.rows_from_prices(
        ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0, overrides={"12:00": 101.0, "12:15": 90.0})
    )
    rows = ds.without(rows, {a7.label_ts(ev, "14:00")})  # дыра ПОСЛЕ исполнения стопа
    pipe = await _load_pipeline(db_session, rows)

    trades, _ = _run_s1a(_tdata(pipe, warm, ev), ev)
    assert len(trades) == 1
    t = trades[0]
    assert t["status"] == "FILLED"
    assert t["exit_reason"] == "sl_gap_open"


# ============================================================
# 5. A24: та же рабочая логика пути через вторую оболочку
# ============================================================
async def test_a24_filled_matches_a7_contract(db_session):
    warm, ev, rows = _happy_corpus()
    pipe = await _load_pipeline(db_session, rows)
    tdata = _tdata(pipe, warm, ev)

    a7_trade = _run_s1a(tdata, ev)[0][0]
    rec = a24.execute_candidate_signal(_signal_from_trade(a7_trade, ev), tdata, _COSTS)

    assert rec["status"] == "FILLED"
    assert rec["exit_reason"] == a7_trade["exit_reason"]
    assert abs(rec["net"] - a7_trade["net"]) < 1e-9


async def test_a24_missing_entry_skips(db_session):
    warm, ev, rows = _happy_corpus()
    a7_trade = _run_s1a(_tdata(await _load_pipeline(db_session, rows), warm, ev), ev)[0][0]

    rows2 = ds.without(rows, {a7.label_ts(ev, _ENTRY_HHMM)})
    await tdb.truncate(db_session)
    pipe2 = await _load_pipeline(db_session, rows2)
    rec = a24.execute_candidate_signal(_signal_from_trade(a7_trade, ev), _tdata(pipe2, warm, ev), _COSTS)

    assert rec["status"] == "SKIP"
    assert rec["incomplete_reason"] == "missing_entry"


async def test_a24_incomplete_unknown_net(db_session):
    warm, ev, rows = _happy_corpus()
    a7_trade = _run_s1a(_tdata(await _load_pipeline(db_session, rows), warm, ev), ev)[0][0]

    rows2 = ds.without(rows, {a7.label_ts(ev, "13:00")})
    await tdb.truncate(db_session)
    pipe2 = await _load_pipeline(db_session, rows2)
    rec = a24.execute_candidate_signal(_signal_from_trade(a7_trade, ev), _tdata(pipe2, warm, ev), _COSTS)

    assert rec["status"] == "INCOMPLETE"
    assert rec["incomplete_reason"] == "path_gap_after_entry"
    assert rec["net"] == "" and rec["exit_ts"] == ""


async def test_a24_uses_a7_walk_path(db_session, monkeypatch):
    """Правильный рабочий путь: A24 исполняет путь через A7 `_walk`, а не дублирует движок."""
    warm, ev, rows = _happy_corpus()
    pipe = await _load_pipeline(db_session, rows)
    tdata = _tdata(pipe, warm, ev)
    a7_trade = _run_s1a(tdata, ev)[0][0]

    calls: list = []
    original = a7._walk

    def _spy(*args, **kwargs):
        calls.append(args[2] if len(args) > 2 else None)
        return original(*args, **kwargs)

    monkeypatch.setattr(a7, "_walk", _spy)
    rec = a24.execute_candidate_signal(_signal_from_trade(a7_trade, ev), tdata, _COSTS)

    assert calls, "A24 обязан исполнять путь через A7 _walk (единое ядро), а не копию"
    assert rec["status"] == "FILLED"


# ============================================================
# 6. Повторяемость
# ============================================================
async def test_fingerprint_reproducible(db_session):
    warm, ev, rows = _happy_corpus()
    pipe = await _load_pipeline(db_session, rows)

    def _fp() -> str:
        t = _run_s1a(_tdata(pipe, warm, ev), ev)[0]
        return json.dumps(t, sort_keys=True, default=str)

    assert _fp() == _fp()


# ============================================================
# 8. R2: регрессии orchestration через реальный fill_gaps (B1)
# ============================================================
def _row_at(ev, hhmm: str) -> dict:
    rows = ds.rows_from_prices(
        ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0, overrides={"12:00": 101.0})
    )
    return dict(next(r for r in rows if r["ts"] == a7.label_ts(ev, hhmm)))


async def test_late_candle_recovered_after_empty_source(db_session, monkeypatch):
    """R2/B1-1: бессрочный пустой кэш не должен блокировать поздно пришедшую свечу.
    На повторе ТОГО ЖЕ экземпляра оркестратора свеча обязана докачаться."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    await _seed_hub_with_gap(pipe, ev, "12:01")
    gap_row = _row_at(ev, "12:01")

    calls = {"n": 0}

    def _src(figi, interval, _a, _b):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [gap_row]

    monkeypatch.setattr(cho, "fetch_candles", _src)

    r1 = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    assert r1.committed == 0 and r1.remaining_gaps

    r2 = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)

    assert calls["n"] >= 2, "B1: повторный fill_gaps не обратился к источнику (бессрочный пустой кэш)"
    assert r2.committed == 1, "B1: поздно пришедшая свеча не восстановлена на том же экземпляре"
    hub_ts = {c.ts for c in pipe.orch.hub.series(pipe.figi, "1min").snapshot()}
    assert a7.label_ts(ev, "12:01") in hub_ts


@pytest.mark.parametrize("kind", ["other_figi", "wrong_interval", "outside_window"])
async def test_foreign_source_row_rejected_by_real_fill_gaps(db_session, monkeypatch, kind):
    """R2/B1-2: реальный fill_gaps обязан отклонять строки, не принадлежащие запросу
    (чужой figi / не тот interval / вне окна) — без записи и без публикации в hub X."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    before = await _seed_hub_with_gap(pipe, ev, "12:01")
    ts_in = a7.label_ts(ev, "12:01")

    if kind == "other_figi":
        row = dict(_row_at(ev, "12:01"), figi="OTHERFI0000000001")
        bad_key = ("OTHERFI0000000001", 1, ts_in)
        foreign_ts = ts_in
    elif kind == "wrong_interval":
        row = dict(_row_at(ev, "12:01"), interval=5)
        bad_key = (pipe.figi, 5, ts_in)
        foreign_ts = ts_in
    else:
        ts_out = a7.label_ts(ev, "19:00")
        row = dict(_row_at(ev, "12:01"), ts=ts_out)
        bad_key = (pipe.figi, 1, ts_out)
        foreign_ts = ts_out

    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [row])
    await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)

    figi, ival, ts = bad_key
    persisted = (await db_session.execute(
        select(DBCandle.id).where(DBCandle.figi == figi,
                                   DBCandle.interval == ival, DBCandle.ts == ts)
    )).all()
    assert persisted == [], f"B1: чужая строка ({kind}) записана в БД"

    hub_ts = {c.ts for c in pipe.orch.hub.series(pipe.figi, "1min").snapshot()}
    assert foreign_ts not in hub_ts, f"B1: чужая строка ({kind}) опубликована в hub X"
    assert len(pipe.orch.hub.series(pipe.figi, "1min")) == before, f"B1: hub X изменился ({kind})"


async def test_failed_upsert_does_not_persist_when_session_reused(db_session, monkeypatch):
    """R2/B1-3: при провале ДО commit (ошибка invalidate после INSERT) неуспешный
    пакет не должен сохраниться при переиспользовании сессии. Тест НЕ откатывает
    транзакцию за код — имитируется вызывающий, который ловит ошибку и коммитит."""
    _warm, _ev, rows = _happy_corpus()

    async def _boom(*_a, **_k):
        raise RuntimeError("integrity cache down after insert")

    monkeypatch.setattr(candle_integrity, "invalidate", _boom)

    raised = None
    try:
        await tinvest.upsert_candles_detailed(db_session, rows)
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "B1: ошибка invalidate не поднялась"

    try:
        await db_session.commit()
    except BaseException:  # noqa: BLE001
        await db_session.rollback()

    cnt = int((await db_session.execute(
        select(func.count()).select_from(DBCandle).where(DBCandle.figi == ds.ANON_FIGI)
    )).scalar_one())
    assert cnt == 0, (
        "B1: неуспешный пакет сохранился при переиспользовании сессии "
        "(upsert_candles_detailed не владеет rollback)"
    )


async def test_hub_recovered_from_db_when_source_unavailable(db_session, monkeypatch):
    """R2/B1-4: публикация в hub упала, данные уже committed в БД. На повторе при
    недоступном источнике hub восстанавливается ИЗ БД, а не ждёт нового ответа API."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    await _seed_hub_with_gap(pipe, ev, "12:01")
    gap_ts = a7.label_ts(ev, "12:01")
    gap_row = _row_at(ev, "12:01")

    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row])

    fail = {"publish": True}
    original_seed = pipe.orch.hub.seed_1m

    def _seed(*a, **k):
        if fail["publish"]:
            raise RuntimeError("hub publish failed")
        return original_seed(*a, **k)

    monkeypatch.setattr(pipe.orch.hub, "seed_1m", _seed)

    r1 = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    assert r1.committed == 1, "B1: committed-строка должна быть записана до публикации"
    assert r1.hub_publish_errors, "B1: провал публикации должен быть зафиксирован"
    stored = (await db_session.execute(
        select(DBCandle.id).where(DBCandle.figi == pipe.figi,
                                   DBCandle.interval == 1, DBCandle.ts == gap_ts)
    )).all()
    assert stored, "committed-данные обязаны лежать в БД"

    fail["publish"] = False

    def _down(*a, **k):
        raise RuntimeError("source down")

    monkeypatch.setattr(cho, "fetch_candles", _down)

    await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)

    hub_ts = {c.ts for c in pipe.orch.hub.series(pipe.figi, "1min").snapshot()}
    assert gap_ts in hub_ts, (
        "B1: hub не восстановлен из БД после провала публикации "
        "(зависит от нового ответа источника)"
    )


async def test_committed_counts_accepted_unique_rows(db_session, monkeypatch):
    """R2/B1-4: committed = реально принятые УНИКАЛЬНЫЕ строки, а не len(raw rows)."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    await _seed_hub_with_gap(pipe, ev, "12:01")
    gap_row = _row_at(ev, "12:01")

    monkeypatch.setattr(cho, "fetch_candles", lambda *a, **k: [gap_row, dict(gap_row)])
    res = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)

    assert res.committed == 1, (
        f"B1: committed={res.committed} учитывает дубли (len raw), а не принятые уникальные"
    )


# ============================================================
# 9. R3: pending-recovery по FIGI (B1)
# ============================================================
_OTHER_FIGI = "ANONPIPELINEFIGI0002"


def _seed_figi(pipe: Pipeline, figi: str, ev, *, drop_hhmm: str | None = None) -> int:
    rows = ds.rows_from_prices(
        ds.ANON_FIGI, ev, ds.day_prices(ev, base=100.0, overrides={"12:00": 101.0})
    )
    if drop_hhmm is not None:
        rows = ds.without(rows, {a7.label_ts(ev, drop_hhmm)})
    candles = [
        EngineCandle(ts=r["ts"], open=float(r["open"]), high=float(r["high"]),
                     low=float(r["low"]), close=float(r["close"]), volume=float(r["volume"]))
        for r in rows
    ]
    pipe.orch.hub.seed_1m(figi, candles, source="db", fire=False)
    return len(candles)


def _row_for(ev, figi: str, hhmm: str) -> dict:
    return dict(_row_at(ev, hhmm), figi=figi)


def _hub_ts(pipe: Pipeline, figi: str) -> set:
    return {c.ts for c in pipe.orch.hub.series(figi, "1min").snapshot()}


def _publish_fail_hook(pipe: Pipeline, monkeypatch, *, rest: dict, db: dict | None = None):
    """Обёртка hub.seed_1m: инъекция только на границе публикации (rest) / восстановления (db)."""
    db = db or {"on": False}
    original = pipe.orch.hub.seed_1m

    def _seed(figi, candles, **kw):
        src = kw.get("source")
        if src == "rest" and rest["on"]:
            raise RuntimeError("hub publish failed")
        if src == "db" and db["on"]:
            raise RuntimeError("recovery db seed failed")
        return original(figi, candles, **kw)

    monkeypatch.setattr(pipe.orch.hub, "seed_1m", _seed)
    return original


async def test_r3_pending_recovery_is_per_figi(db_session, monkeypatch):
    """R3/B1: pending-recovery одного FIGI не сбрасывается вызовом fill_gaps другого FIGI
    (у которого дыр нет); committed-свеча X обязана восстановиться из БД при недоступном
    источнике, БЕЗ обращения к источнику."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    _seed_figi(pipe, pipe.figi, ev, drop_hhmm="12:01")
    _seed_figi(pipe, _OTHER_FIGI, ev, drop_hhmm=None)
    x_ts = a7.label_ts(ev, "12:01")

    calls: list[str] = []
    state = {"phase": "initial"}

    def _src(figi, interval, a, b):
        calls.append(figi)
        if state["phase"] == "initial" and figi == pipe.figi:
            return [_row_for(ev, pipe.figi, "12:01")]
        raise RuntimeError("source down")

    monkeypatch.setattr(cho, "fetch_candles", _src)
    fail_rest = {"on": True}
    _publish_fail_hook(pipe, monkeypatch, rest=fail_rest)

    r1 = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    assert r1.committed == 1 and r1.hub_publish_errors

    state["phase"] = "down"
    r2 = await pipe.orch.fill_gaps_detailed(db_session, _OTHER_FIGI)
    assert r2.gaps_total == 0

    fail_rest["on"] = False
    await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)

    assert x_ts in _hub_ts(pipe, pipe.figi), (
        "R3: committed X не восстановлен из БД — pending сброшен вызовом другого FIGI"
    )
    assert calls.count(pipe.figi) == 1, (
        f"R3: при успешном DB-recovery не должно быть обращения к источнику X ({calls})"
    )


async def test_r3_two_figi_pending_independent(db_session, monkeypatch):
    """R3/B1: у X и Y независимые pending; восстановление X не стирает pending Y;
    оба восстанавливаются из БД при недоступном источнике, без обращения к источнику."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    _seed_figi(pipe, pipe.figi, ev, drop_hhmm="12:01")
    _seed_figi(pipe, _OTHER_FIGI, ev, drop_hhmm="12:01")
    x_ts = a7.label_ts(ev, "12:01")

    calls: list[str] = []
    state = {"phase": "initial"}

    def _src(figi, interval, a, b):
        calls.append(figi)
        if state["phase"] == "initial":
            return [_row_for(ev, figi, "12:01")]
        raise RuntimeError("source down")

    monkeypatch.setattr(cho, "fetch_candles", _src)
    fail_rest = {"on": True}
    _publish_fail_hook(pipe, monkeypatch, rest=fail_rest)

    rx = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    ry = await pipe.orch.fill_gaps_detailed(db_session, _OTHER_FIGI)
    assert rx.committed == 1 and rx.hub_publish_errors
    assert ry.committed == 1 and ry.hub_publish_errors

    state["phase"] = "down"
    fail_rest["on"] = False
    await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    await pipe.orch.fill_gaps_detailed(db_session, _OTHER_FIGI)

    assert x_ts in _hub_ts(pipe, pipe.figi), "R3: X не восстановлен из БД"
    assert x_ts in _hub_ts(pipe, _OTHER_FIGI), "R3: pending Y стёрт восстановлением X"
    assert calls.count(pipe.figi) == 1 and calls.count(_OTHER_FIGI) == 1, (
        f"R3: лишние обращения к источнику при DB-recovery ({calls})"
    )


async def test_r3_recovery_failure_keeps_pending(db_session, monkeypatch):
    """R3/B1: сбой попытки восстановления не теряет pending; посторонний вызов другого
    FIGI его не сбрасывает; следующий успешный recovery восстанавливает hub из БД."""
    _warm, ev, _ = _happy_corpus()
    pipe = Pipeline(db_session)
    _seed_figi(pipe, pipe.figi, ev, drop_hhmm="12:01")
    _seed_figi(pipe, _OTHER_FIGI, ev, drop_hhmm=None)
    x_ts = a7.label_ts(ev, "12:01")

    state = {"phase": "initial"}

    def _src(figi, interval, a, b):
        if state["phase"] == "initial" and figi == pipe.figi:
            return [_row_for(ev, pipe.figi, "12:01")]
        raise RuntimeError("source down")

    monkeypatch.setattr(cho, "fetch_candles", _src)
    fail_rest = {"on": True}
    fail_db = {"on": True}
    _publish_fail_hook(pipe, monkeypatch, rest=fail_rest, db=fail_db)

    r1 = await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    assert r1.committed == 1 and r1.hub_publish_errors

    state["phase"] = "down"
    await pipe.orch.fill_gaps_detailed(db_session, _OTHER_FIGI)

    fail_rest["on"] = False
    try:
        await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)
    except BaseException:  # noqa: BLE001 — сбой recovery допустим как сигнал, важна сохранность pending
        pass

    fail_db["on"] = False
    await pipe.orch.fill_gaps_detailed(db_session, pipe.figi)

    assert x_ts in _hub_ts(pipe, pipe.figi), (
        "R3: после сбоя recovery pending потерян — hub X не восстановлен из БД"
    )


# ============================================================
# 7. Сам контур и гейт: fail-closed и коды возврата (герметично, без БД)
# ============================================================
def test_contour_fail_closed(monkeypatch):
    """Контур обязан отвергать рабочую БД, имя без `test`, не-PostgreSQL и чужой host."""
    for bad in (
        "postgresql+asyncpg://u:p@127.0.0.1:5432/deeptrading",        # имя рабочей БД
        "postgresql+asyncpg://u:p@127.0.0.1:5432/candle",            # без `test`
        "sqlite+aiosqlite:////tmp/pipeline.db",                      # не PostgreSQL
        "postgresql+asyncpg://u:p@evil.example.com:5432/deeptrading_test",  # host вне allowlist
        "postgresql+asyncpg://u:p@10.0.0.9:5432/deeptrading_test",   # host вне allowlist
    ):
        monkeypatch.setenv(tdb.ENV_VAR, bad)
        with pytest.raises(tdb.PipelineContourError):
            tdb.resolve_test_target()

    monkeypatch.setenv(tdb.ENV_VAR, "postgresql+asyncpg://u:p@127.0.0.1:5432/deeptrading_test")
    assert tdb.resolve_test_target().database == "deeptrading_test"
    monkeypatch.setenv(tdb.ENV_VAR, "postgresql+asyncpg://u:p@localhost:5432/deeptrading_test")
    assert tdb.resolve_test_target().host == "localhost"


def test_contour_allowed_host_env(monkeypatch):
    """Хост вне loopback допустим ТОЛЬКО при явном разрешении PIPELINE_TEST_DB_ALLOWED_HOST."""
    monkeypatch.setenv(tdb.ENV_VAR, "postgresql+asyncpg://u:p@db.internal:5432/deeptrading_test")
    with pytest.raises(tdb.PipelineContourError):
        tdb.resolve_test_target()
    monkeypatch.setenv(tdb.HOST_ENV_VAR, "db.internal")
    assert tdb.resolve_test_target().host == "db.internal"


async def test_prepare_schema_validates_url_before_connect():
    """prepare_schema не доверяет произвольной строке: валидация ДО соединения."""
    for bad in (
        "postgresql+asyncpg://u:p@127.0.0.1:5432/deeptrading",
        "postgresql+asyncpg://u:p@evil.example.com:5432/deeptrading_test",
        "sqlite+aiosqlite:////tmp/pipeline.db",
    ):
        with pytest.raises(tdb.PipelineContourError):
            await tdb.prepare_schema(bad)


async def test_truncate_guard_rejects_non_test_session():
    """Destructive-граница: TRUNCATE запрещён на сессии, привязанной не к тестовой БД."""
    engine, maker = tdb.make_sessionmaker(
        "postgresql+asyncpg://u:p@127.0.0.1:5432/deeptrading"
    )
    try:
        async with maker() as session:
            with pytest.raises(tdb.PipelineContourError):
                await tdb.truncate(session)
    finally:
        await engine.dispose()


def test_gate_detector_skip_xfail_empty_errors():
    import check_pipeline_contract as gate

    assert gate._skipped_or_xfail("105 passed, 2 skipped in 1.2s")
    assert gate._skipped_or_xfail("3 xfailed, 10 passed")
    assert not gate._skipped_or_xfail("18 passed in 22s")
    assert gate._empty_collection("no tests ran in 0.01s")
    assert not gate._empty_collection("18 passed")
    assert gate._errors("1 error in 0.5s") == 1
    assert gate._errors("2 errors") == 2
    assert gate._errors("18 passed") == 0


def test_gate_exit_no_contour():
    """Без тестового контура гейт падает кодом 2, а не зелёным skip."""
    import subprocess

    env = dict(os.environ)
    env.pop(tdb.ENV_VAR, None)
    proc = subprocess.run(
        [sys.executable, "scripts/check_pipeline_contract.py"],
        cwd=str(BACKEND), env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_gate_main_exit_codes(monkeypatch):
    """Прямая проверка кодов возврата гейта: OK/Failed/NoContour/Empty/Skipped."""
    import check_pipeline_contract as gate

    monkeypatch.setattr(gate, "_check_contour", lambda: gate.EXIT_OK)
    monkeypatch.setattr(gate, "_run_pytest", lambda: (0, "10 passed in 1.0s"))
    assert gate.main([]) == gate.EXIT_OK

    monkeypatch.setattr(gate, "_run_pytest", lambda: (1, "2 failed, 3 passed in 1.0s"))
    assert gate.main([]) == gate.EXIT_FAILED

    monkeypatch.setattr(gate, "_run_pytest", lambda: (0, "5 passed, 1 skipped in 1.0s"))
    assert gate.main([]) == gate.EXIT_SKIPPED

    monkeypatch.setattr(gate, "_run_pytest", lambda: (0, "3 xfailed, 2 passed in 1.0s"))
    assert gate.main([]) == gate.EXIT_SKIPPED

    monkeypatch.setattr(gate, "_run_pytest", lambda: (5, "no tests ran in 0.01s"))
    assert gate.main([]) == gate.EXIT_EMPTY

    monkeypatch.setattr(gate, "_check_contour", lambda: gate.EXIT_NO_CONTOUR)
    assert gate.main([]) == gate.EXIT_NO_CONTOUR


def _ci_job_block(text: str, job: str) -> str:
    """Срез YAML-job по отступу 2 пробела, без комментариев и без внешних зависимостей."""
    import re

    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    m = re.search(rf"^  {re.escape(job)}:\s*$", body, re.M)
    assert m, f"CI: нет job {job}"
    rest = body[m.end():]
    nxt = re.search(r"^  \S", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def test_ci_pipeline_contract_job_wired():
    """CI-проводка: отдельный job, отдельная тестовая БД, без continue-on-error
    и без исключения новых тестов через artifact/integration."""
    text = (BACKEND.parent / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    block = _ci_job_block(text, "pipeline-contract")

    assert "continue-on-error" not in block, "CI: pipeline-contract не должен быть continue-on-error"
    assert "POSTGRES_DB: deeptrading_test" in block, "CI: job должен использовать отдельную тестовую БД"
    assert "postgres:16" in block, "CI: нужен сервис postgres:16"
    assert "PIPELINE_TEST_DATABASE_URL:" in block, "CI: нет строки подключения тестовой БД"
    assert "check_pipeline_contract.py" in block, "CI: job не запускает гейт"
    assert "artifact" not in block and "integration" not in block, (
        "CI: гейт не должен исключать новые тесты через artifact/integration"
    )
