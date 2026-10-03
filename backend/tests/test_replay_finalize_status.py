"""Финал реплея: статус перестаёт вечно показывать active=true (хвост 98.84%)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bot.runtime import PaperBotRuntime

_UTC = timezone.utc
_MSK = timezone(timedelta(hours=3))


def _rt() -> PaperBotRuntime:
    rt = PaperBotRuntime.__new__(PaperBotRuntime)  # без __init__: только статус-хелперы
    rt.config = SimpleNamespace(replay_pace="fast", feed="replay",
                                replay_start="2026-09-01T04:00:00Z",
                                replay_end="2026-09-05T21:59:00Z", test_name="t1")
    rt.mode = "test:t1"
    rt.started_at = datetime(2026, 10, 1, 21, 15, tzinfo=_UTC)
    rt.last_candle_ts = datetime(2026, 9, 5, 20, 50, tzinfo=_UTC)
    rt._replay_from = datetime(2026, 9, 1, 4, 0, tzinfo=_UTC)
    rt._replay_to = datetime(2026, 9, 5, 21, 59, tzinfo=_UTC)
    rt._replay_cur = datetime(2026, 9, 5, 20, 50, tzinfo=_UTC)
    rt._replay_finished = False
    rt._replay_summary = None
    return rt


def test_live_status_reports_active_and_pct():
    rt = _rt()
    d = rt._replay_status_dict()
    assert d["active"] is True and d["finished"] is False
    assert d["start"] == "2026-09-01T04:00:00+00:00"
    assert 98.0 < d["pct"] < 99.5          # окно длиннее последнего бара — не 100%
    assert d["now_msk"].startswith("2026-09-05T23:50")


def test_finished_status_is_frozen_not_active():
    rt = _rt()
    pct = rt._replay_progress_pct()
    rt._replay_summary = {"closed": 3, "start": rt._replay_from.isoformat(),
                          "end": rt._replay_to.isoformat(), "last_ts": rt._replay_cur.isoformat(),
                          "last_msk": rt._replay_cur.astimezone(_MSK).isoformat(),
                          "pct": pct, "test_name": "t1"}
    rt._replay_finished = True
    rt._replay_from = None      # что делает _finalize_replay
    rt._replay_cur = None
    d = rt._replay_status_dict()
    assert d["active"] is False and d["finished"] is True
    assert d["closed"] == 3 and d["pct"] == pct
    assert d["start"] == "2026-09-01T04:00:00+00:00"   # окно не теряется


def test_progress_bar_sees_finished_run_instead_of_none():
    rt = _rt()
    rt._replay_summary = {"closed": 0, "pct": 98.84, "start": rt._replay_from.isoformat(),
                          "end": rt._replay_to.isoformat(), "last_ts": None, "last_msk": None}
    rt._replay_finished = True
    rt._replay_from = None
    rt._replay_cur = None
    p = rt._replay_progress()
    assert p is not None                      # UI отличает «завершён» от «нет данных»
    assert p["active"] is False and p["finished"] is True
    assert p["wall_start"] == rt.started_at.isoformat()


def test_progress_none_outside_test_contour():
    rt = _rt()
    rt.config = SimpleNamespace(replay_pace="fast", feed="stream")
    rt.mode = "live"
    rt._replay_finished = False
    rt._replay_summary = None
    assert rt._replay_progress() is None


def test_fresh_start_resets_finished_flags():
    """Новый прогон не должен наследовать finished-состояние прошлого."""
    rt = _rt()
    rt._replay_finished = True
    rt._replay_summary = {"closed": 5, "pct": 42.0}
    # ровно то, что делает start()
    rt._replay_from = None
    rt._replay_cur = None
    rt._replay_finished = False
    rt._replay_summary = None
    d = rt._replay_status_dict()
    assert d == {"active": False, "finished": False, "status": None, "start": None,
                 "end": None, "now": None, "now_msk": None, "pct": None,
                 "pace": "fast", "closed": None}


# ---------------------------------------------------------------------------
# P1.2: терминальные статусы финализации (COMPLETED/CANCELLED/FAILED/PARTIAL_CLOSE)
# ---------------------------------------------------------------------------


class _FakeBroker:
    """Мини-брокер: позиции, цены и управляемые исходы close_position."""

    def __init__(self, positions=(), closes=None, positions_fail=False):
        self._positions = list(positions)
        self._closes = dict(closes or {})
        self._positions_fail = positions_fail
        self.closed: list[str] = []

    async def positions(self):
        if self._positions_fail:
            raise RuntimeError("portfolio unavailable")
        return list(self._positions)

    async def last_prices(self, figis):
        return {f: 123.4 for f in figis}

    async def close_position(self, figi, price, reason):
        self.closed.append(figi)
        mode = self._closes.get(figi, "ok")
        if mode == "fail":
            raise RuntimeError("order rejected")
        if mode == "none":
            return None
        return SimpleNamespace(exit_price=price, net_pnl=7.5)


def _pos(figi: str) -> SimpleNamespace:
    return SimpleNamespace(figi=figi, entry_price=100.0, ticker=figi, qty=1)


def _rt_finalize(broker, *, held=()):
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.config = SimpleNamespace(feed="replay", replay_pace="fast",
                                replay_start="2026-09-01T04:00:00Z",
                                replay_end="2026-09-05T21:59:00Z", test_name="")
    rt.mode = "test:t1"
    rt.started_at = datetime(2026, 10, 1, 21, 15, tzinfo=_UTC)
    rt._replay_from = datetime(2026, 9, 1, 4, 0, tzinfo=_UTC)
    rt._replay_to = datetime(2026, 9, 5, 21, 59, tzinfo=_UTC)
    rt._replay_cur = datetime(2026, 9, 5, 20, 50, tzinfo=_UTC)
    rt._replay_finished = False
    rt._replay_status = None
    rt._replay_summary = None
    rt._held = set(held)
    rt._exit_plans = {f: object() for f in held}
    rt.broker = broker
    rt._trace = None
    logs: list[str] = []

    def _log(msg, *a, **k):
        logs.append(str(msg))

    rt._log = _log

    async def _st_close(*a, **k):
        return None

    rt._st_close = _st_close
    return rt


def test_normal_exhaustion_is_completed_and_cleans_held():
    broker = _FakeBroker(positions=[_pos("F1"), _pos("F2")])
    rt = _rt_finalize(broker, held=("F1", "F2"))
    asyncio.run(rt._finalize_replay("stream_exhausted"))
    assert rt._replay_status == "COMPLETED"
    assert rt._replay_finished is True
    assert rt._replay_summary["closed"] == 2
    assert rt._replay_summary["remaining_positions"] == []
    assert rt._held == set() and rt._exit_plans == {}
    assert broker.closed == ["F1", "F2"]
    assert rt._replay_status_dict()["status"] == "COMPLETED"


def test_manual_stop_is_cancelled_not_finished():
    broker = _FakeBroker(positions=[_pos("F1")])
    rt = _rt_finalize(broker, held=("F1",))
    asyncio.run(rt._finalize_replay("running_flag_false"))
    assert rt._replay_status == "CANCELLED"
    assert rt._replay_finished is False
    assert rt._replay_summary["closed"] == 1          # закрытие всё равно пытались сделать
    d = rt._replay_status_dict()
    assert d["finished"] is False and d["status"] == "CANCELLED"


def test_processing_exception_marks_failed():
    broker = _FakeBroker()
    rt = _rt_finalize(broker)
    asyncio.run(rt._finalize_replay("exception: boom"))
    assert rt._replay_status == "FAILED"
    assert rt._replay_finished is False


def test_close_failure_keeps_held_state_and_marks_partial():
    broker = _FakeBroker(positions=[_pos("F1"), _pos("F2")], closes={"F2": "fail"})
    rt = _rt_finalize(broker, held=("F1", "F2"))
    asyncio.run(rt._finalize_replay("stream_exhausted"))
    assert rt._replay_status == "PARTIAL_CLOSE"
    assert rt._replay_finished is False
    assert rt._replay_summary["closed"] == 1
    assert rt._replay_summary["remaining_positions"] == ["F2"]
    assert rt._held == {"F2"}                          # незакрытое НЕ теряет held-state
    assert "F2" in rt._exit_plans
    assert rt._replay_summary["close_errors"]
    assert "F2" in rt._replay_summary["close_errors"][0]


def test_close_none_result_counts_as_remaining():
    broker = _FakeBroker(positions=[_pos("F1")], closes={"F1": "none"})
    rt = _rt_finalize(broker, held=("F1",))
    asyncio.run(rt._finalize_replay("stream_exhausted"))
    assert rt._replay_status == "PARTIAL_CLOSE"
    assert rt._replay_summary["remaining_positions"] == ["F1"]
    assert rt._held == {"F1"}


def test_positions_fetch_failure_blocks_completed():
    broker = _FakeBroker(positions_fail=True)
    rt = _rt_finalize(broker, held=("F9",))
    asyncio.run(rt._finalize_replay("stream_exhausted"))
    assert rt._replay_status == "PARTIAL_CLOSE"        # неизвестный портфель ≠ успешный финал
    assert rt._replay_summary["remaining_positions"] == ["F9"]
    assert any("positions" in e for e in rt._replay_summary["close_errors"])


def test_double_finalize_is_idempotent():
    broker = _FakeBroker(positions=[_pos("F1")])
    rt = _rt_finalize(broker, held=("F1",))
    asyncio.run(rt._finalize_replay("stream_exhausted"))
    snapshot = dict(rt._replay_summary)
    asyncio.run(rt._finalize_replay("stream_exhausted"))
    assert broker.closed == ["F1"]                     # второй вызов не пере-закрывает
    assert dict(rt._replay_summary) == snapshot
    assert rt._replay_status == "COMPLETED"
