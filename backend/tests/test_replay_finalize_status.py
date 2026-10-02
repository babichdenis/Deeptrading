"""Финал реплея: статус перестаёт вечно показывать active=true (хвост 98.84%)."""
from __future__ import annotations

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
    assert d == {"active": False, "finished": False, "start": None, "end": None,
                 "now": None, "now_msk": None, "pct": None, "pace": "fast", "closed": None}
