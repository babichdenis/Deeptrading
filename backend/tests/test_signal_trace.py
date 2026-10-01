"""Signal Trace P0: контракты и JSONL-эмиттер (hermetic, без БД)."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from app.engine.trace import RunInfo, Stage, Status, TraceEvent, make_signal_id
from app.services.signal_trace import SignalTraceEmitter


def _run(run_key: str = "test-run") -> RunInfo:
    return RunInfo(run_id="r1", run_key=run_key, strategy_id="rsi_trade_hub", interval="10min")


def test_signal_id_stable_and_distinct():
    kw = dict(
        run_id="r1", contour="runtime", figi="F", strategy_id="s", strategy_version="1",
        interval="10min", bar_ts=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
        side="SELL", kind="entry",
    )
    a = make_signal_id(**kw)
    b = make_signal_id(**kw)
    assert a == b and len(a) == 32
    c = make_signal_id(**{**kw, "side": "BUY"})
    d = make_signal_id(**{**kw, "figi": "G"})
    assert len({a, c, d}) == 3


def test_jsonl_roundtrip_and_close(tmp_path):
    async def body():
        em = SignalTraceEmitter(_run(), directory=tmp_path, flush_every=0.01, flush_batch=1)
        em.start()
        em.emit(TraceEvent(
            stage=Stage.RAW, status=Status.CREATED,
            ts_bar=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            figi="F", ticker="SBER", signal_id="abc", side="BUY", kind="entry",
            reason="rsi_up_down", features={"rsi": 30.0},
        ))
        return await em.aclose()

    summ = asyncio.run(body())
    assert summ["events"] == 1 and summ["dropped"] == 0 and summ["max_seq"] == 1
    lines = [json.loads(x) for x in (tmp_path / "test-run.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 1
    doc = lines[0]
    assert doc["event"] == "RAW" and doc["status"] == "CREATED" and doc["seq"] == 1
    assert doc["signal"]["signal_id"] == "abc"
    assert doc["run"]["run_key"] == "test-run"
    assert doc["schema"].startswith("signal_trace/")


def test_drop_counter_on_overflow(tmp_path):
    em = SignalTraceEmitter(_run("drop-run"), directory=tmp_path, max_queue=1)
    ev = TraceEvent(stage=Stage.EVAL, status=Status.CREATED)
    em.emit(ev)
    em.emit(ev)
    em.emit(ev)
    assert em.events == 1 and em.dropped == 2 and em.seq == 3
    asyncio.run(em.aclose())


def test_db_writer_receives_batches(tmp_path):
    written = []

    async def writer(docs):
        written.extend(docs)

    async def body():
        em = SignalTraceEmitter(_run("db-run"), directory=tmp_path, flush_every=0.01,
                                flush_batch=10, db_writer=writer)
        em.start()
        em.emit(TraceEvent(stage=Stage.RUN_OPEN, status=Status.CREATED))
        em.emit(TraceEvent(stage=Stage.RAW, status=Status.CREATED, figi="F",
                           signal_id="s1", side="BUY"))
        return await em.aclose()

    summ = asyncio.run(body())
    assert summ["events"] == 2
    assert len(written) == 2
    assert written[0]["event"] == "RUN_OPEN"
    assert written[1]["signal"]["signal_id"] == "s1"


def test_models_registered():
    from app.models.signal_trace import SignalTraceEvent, SignalTraceOutcome, SignalTraceRun
    assert SignalTraceRun.__tablename__ == "signal_trace_runs"
    assert SignalTraceEvent.__tablename__ == "signal_trace_events"
    assert SignalTraceOutcome.__tablename__ == "signal_trace_outcomes"
