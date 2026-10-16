from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

import pytest

from app.bot import runtime_diagnostics as diagnostics
from app.bot.runtime_persistence import RuntimePersistenceMixin


class Session:
    def __init__(self):
        self.calls = []
        self.fail_commit = False
        self.fail_request_id = False
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def execute(self, statement, payload):
        self.calls.append((str(statement), payload))
        if self.fail_request_id and "request_id" in str(statement):
            raise RuntimeError("legacy log schema")

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("database unavailable")
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class Runtime(RuntimePersistenceMixin):
    def __init__(self, session):
        self.session = session
        self._persist_queue = deque(maxlen=1000)
        self._persist_queue_5m = deque(maxlen=1000)
        self._log_persist_queue = deque(maxlen=1000)
        self.metrics = dict(persist_ms_total=0, persist_ms_n=0, persist_ms_max=0, persist_dropped=0, persist_failures=0)
        self._persist_flushes = 0
        self.logs = []

    def _database_session(self):
        return self.session

    def _log(self, message, **kwargs):
        self.logs.append((message, kwargs))


def candle(close=100):
    return ("FIGI", datetime(2026, 10, 1, tzinfo=timezone.utc), 100, 101, 99, close, 42)


@pytest.fixture(autouse=True)
def isolate_invalidation(monkeypatch):
    async def invalidate(*_args):
        return None
    monkeypatch.setattr("app.services.candle_integrity.invalidate", invalidate)
    diagnostics.AUDIT_SWALLOW_SEEN.clear()
    yield
    diagnostics.AUDIT_SWALLOW_SEEN.clear()


@pytest.mark.asyncio
async def test_candles_use_one_ordered_batch_per_timeframe():
    db = Session()
    rt = Runtime(db)
    rt._persist_queue.extend([candle(100), candle(101)] * 50)
    rt._persist_queue_5m.append(candle(102))
    await rt._flush_persist_once()
    assert len(db.calls) == 2
    assert len(db.calls[0][1]) == 100
    assert [row["c"] for row in db.calls[0][1]][:4] == [100, 101, 100, 101]
    assert db.calls[1][1][0]["c"] == 102
    assert db.commits == 2
    assert not rt._persist_queue and not rt._persist_queue_5m


@pytest.mark.asyncio
async def test_failed_batch_requeues_once_then_reports_recovery():
    db = Session()
    db.fail_commit = True
    rt = Runtime(db)
    rt._persist_queue.extend([candle(100), candle(101)])
    for _ in range(2):
        await rt._flush_persist_once()
        assert list(rt._persist_queue) == [candle(100), candle(101)]
    assert rt.metrics["persist_failures"] == 2
    assert sum("PERSIST_ERR" in msg for msg, _ in rt.logs) == 1
    db.fail_commit = False
    await rt._flush_persist_once()
    assert not rt._persist_queue
    assert sum("PERSIST_RECOVERED" in msg for msg, _ in rt.logs) == 1


@pytest.mark.asyncio
async def test_logs_keep_request_id_and_legacy_fallback():
    import json
    db = Session()
    db.fail_request_id = True
    rt = Runtime(db)
    rt._log_persist_queue.append(("info", "bot", "message", "2026-10-01 12:00:00", "rid-1"))
    await rt._flush_persist_once()
    assert len(db.calls) == 2
    assert json.loads(db.calls[0][1]["rows"])[0]["request_id"] == "rid-1"
    assert "request_id" not in json.loads(db.calls[1][1]["rows"])[0]
    assert db.rollbacks == 1 and db.commits == 1
    assert not rt._log_persist_queue
    assert "persist.logs_request_id_fallback:RuntimeError" in diagnostics.AUDIT_SWALLOW_SEEN


@pytest.mark.asyncio
async def test_integrity_failure_is_visible_without_requeuing_committed_candles(monkeypatch):
    async def invalidate(*_args):
        raise ValueError("integrity unavailable")
    monkeypatch.setattr("app.services.candle_integrity.invalidate", invalidate)
    rt = Runtime(Session())
    rt._persist_queue.append(candle())
    await rt._flush_persist_once()
    assert not rt._persist_queue
    assert rt.session.commits == 1
    assert "persist.integrity_invalidation:ValueError" in diagnostics.AUDIT_SWALLOW_SEEN


def test_diagnostic_groups_are_bounded_and_ignore_variable_exception_messages(monkeypatch):
    monkeypatch.setattr(diagnostics, "MAX_EXCEPTION_GROUPS", 3)
    for i in range(20):
        diagnostics.audit_swallow("operation", ValueError(f"instrument {i}"))
    assert len(diagnostics.AUDIT_SWALLOW_SEEN) == 1
    assert diagnostics.AUDIT_SWALLOW_SEEN["operation:ValueError"]["count"] == 20
    for i in range(5):
        diagnostics.audit_swallow(f"operation-{i}", ValueError("failure"))
    assert len(diagnostics.AUDIT_SWALLOW_SEEN) == 3


def test_diagnostic_storage_and_logging_redact_secrets(caplog):
    diagnostics.audit_swallow("operation", ValueError("token=abcdefgh123456 password=private123"))
    text = str(diagnostics.AUDIT_SWALLOW_SEEN) + caplog.text
    assert "abcdefgh123456" not in text
    assert "private123" not in text
    assert "REDACTED" in text


def test_five_minute_cache_invalidates_at_same_minute_in_next_hour():
    from datetime import timedelta
    from app.bot.runtime import PaperBotRuntime
    from app.engine.models import Candle
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt._5m_cache = {}
    rt._5m_last_close = {}
    t = datetime(2026, 10, 1, 7, tzinfo=timezone.utc)
    first = Candle(ts=t, open=100, high=100, low=100, close=100, volume=1)
    second = Candle(ts=t + timedelta(hours=1), open=200, high=200, low=200, close=200, volume=1)
    cached = rt._get_5m_bars("FIGI", [first])
    assert cached[-1].close == 100
    assert rt._get_5m_bars("FIGI", [first]) is cached
    refreshed = rt._get_5m_bars("FIGI", [first, second])
    assert refreshed[-1].close == 200
    assert len(refreshed) == 2
