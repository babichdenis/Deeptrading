"""OBS-PROC-01: фикстуры наблюдателя процессов.

Проверяется поведение на случаях, которые нельзя дождаться вживую: переиспользование
PID, недоступный процесс, битый файл прогресса, границы времени, ложная смерть.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts" / "research"))

import pytest

import process_observer as obs

NOW = datetime(2026, 11, 15, 17, 0, tzinfo=timezone.utc)


def alive_probe(creation: str = "2026-11-15T16:00:00+00:00", cpu: float = 10.0):
    def probe(pid: int) -> dict:
        return {"alive": True, "creation_time": creation, "cpu_seconds": cpu,
                "rss_mb": 100.0, "private_mb": 200.0, "parent_pid": 42}
    return probe


def dead_probe(exit_code: int | None = None, error: str | None = None):
    def probe(pid: int) -> dict:
        out: dict = {"alive": False}
        if exit_code is not None:
            out["exit_code"] = exit_code
        if error is not None:
            out["error"] = error
        return out
    return probe


def beat(age_s: float | None, replay: str | None = "2026-09-01T13:50:00Z"):
    def reader(path: Path) -> dict:
        out: dict = {"path": str(path)}
        if age_s is not None:
            out["age_s"] = age_s
            out["mtime_utc"] = (NOW - timedelta(seconds=age_s)).isoformat()
        if replay is not None:
            out["replay_now"] = replay
        return out
    return reader


def make_observer(probe, beat_reader, stale_after=120.0, heartbeat=Path("x.json")):
    return obs.ProcessObserver(pid=777, heartbeat=heartbeat, interval_s=30.0,
                               stale_after_s=stale_after, clock=lambda: NOW,
                               probe=probe, beat_reader=beat_reader)


def test_alive_with_fresh_heartbeat_is_progressing() -> None:
    rec = make_observer(alive_probe(), beat(age_s=10)).snapshot()
    assert rec["state"] == obs.ALIVE_PROGRESSING
    assert "heartbeat_stale" not in rec["events"]


def test_stale_heartbeat_on_living_process_is_not_death() -> None:
    o = make_observer(alive_probe(), beat(age_s=500))
    rec = o.snapshot()
    assert rec["alive"] is True
    assert rec["state"] == obs.ALIVE_STALE
    assert "heartbeat_stale" in rec["events"]
    assert "process_exit" not in rec["events"]


def test_missing_heartbeat_keeps_process_alive_but_stale() -> None:
    def reader(path: Path) -> dict:
        return {"path": str(path), "error": "heartbeat_missing"}
    rec = make_observer(alive_probe(), reader).snapshot()
    assert rec["state"] == obs.ALIVE_STALE
    assert "observation_error" in rec["events"]
    assert rec["alive"] is True


def test_corrupt_or_partial_json_is_error_not_death(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    path.write_text('{"replay_now": "2026-09-01T13:50:00Z", "pid": 1', encoding="utf-8")
    rec = make_observer(alive_probe(), obs.read_heartbeat, heartbeat=path).snapshot()
    assert rec["heartbeat"]["error"] == "heartbeat_invalid_json"
    assert rec["state"] == obs.ALIVE_STALE
    assert rec["alive"] is True


def test_heartbeat_mtime_age_is_utc(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    path.write_text(json.dumps({"replay_now": "2026-09-01T13:50:00Z", "pid": 777}),
                    encoding="utf-8")
    import os
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=90)).timestamp()
    os.utime(path, (stamp, stamp))
    out = obs.read_heartbeat(path)
    assert out["age_s"] == pytest.approx(90.0, abs=2.0)
    assert out["mtime_utc"].endswith("+00:00")
    assert out["replay_now"] == "2026-09-01T13:50:00Z"


def test_vanished_heartbeat_file() -> None:
    out = obs.read_heartbeat(Path("Z:/definitely/not/here.json"))
    assert out["error"] == "heartbeat_missing"


def test_exited_process_reported_once() -> None:
    o = make_observer(dead_probe(exit_code=0), beat(age_s=10))
    o.first_creation = "2026-11-15T16:00:00+00:00"
    first = o.snapshot()
    second = o.snapshot()
    assert first["state"] == obs.EXITED
    assert "process_exit" in first["events"]
    assert "process_exit" not in second["events"], "событие смерти не дублируется"


def test_unknown_pid_never_seen_is_unknown() -> None:
    rec = make_observer(dead_probe(error="openprocess_failed_87"), beat(age_s=10)).snapshot()
    assert rec["state"] == obs.EXITED, "нет такого PID — процесса нет"
    assert "observation_error" not in rec["events"], "отсутствие процесса — не ошибка наблюдения"


def test_access_denied_is_unknown_not_exit() -> None:
    rec = make_observer(dead_probe(error="openprocess_failed_5"), beat(age_s=10)).snapshot()
    assert rec["state"] == obs.UNKNOWN
    assert "observation_error" in rec["events"]


def test_pid_reuse_detected_by_creation_time() -> None:
    o = make_observer(alive_probe(creation="2026-11-15T16:00:00+00:00"), beat(age_s=10))
    o.snapshot()
    o.probe = alive_probe(creation="2026-11-15T16:59:00+00:00")
    rec = o.snapshot()
    assert rec["pid_reused"] is True


def test_progress_resumed_after_stale() -> None:
    state = {"replay": "2026-09-01T13:50:00Z"}
    def reader(path: Path) -> dict:
        return {"path": str(path), "age_s": 300, "replay_now": state["replay"]}
    o = make_observer(alive_probe(), reader)
    assert o.snapshot()["state"] == obs.ALIVE_STALE
    o.beat_reader = beat(age_s=5, replay="2026-09-01T14:25:00Z")
    rec = o.snapshot()
    assert "progress_resumed" in rec["events"]
    assert rec["state"] == obs.ALIVE_PROGRESSING


def test_cpu_delta_only_after_second_sample() -> None:
    o = make_observer(alive_probe(cpu=10.0), beat(age_s=10))
    assert "cpu_delta_s" not in o.snapshot()
    o.probe = alive_probe(cpu=13.5)
    assert o.snapshot()["cpu_delta_s"] == pytest.approx(3.5)


def test_interval_floor_enforced() -> None:
    with pytest.raises(ValueError):
        obs.ProcessObserver(pid=1, heartbeat=None, interval_s=5.0, stale_after_s=60)


def test_log_is_bounded_and_rotates(tmp_path: Path) -> None:
    writer = obs.RotatingJsonlWriter(tmp_path / "obs.jsonl", max_bytes=4096)
    for i in range(120):
        writer.write({"i": i, "pad": "x" * 80})
    main_log = tmp_path / "obs.jsonl"
    backup = tmp_path / "obs.jsonl.1"
    assert main_log.stat().st_size <= 4096
    assert backup.exists(), "должна появиться одна предыдущая копия"
    assert writer.rotations >= 1


def test_observer_stores_no_commandline_or_env(tmp_path: Path) -> None:
    writer = obs.RotatingJsonlWriter(tmp_path / "obs.jsonl", max_bytes=4096)
    rec = make_observer(alive_probe(), beat(age_s=10)).snapshot()
    blob = json.dumps(rec).lower()
    for forbidden in ("commandline", "cmdline", "environ", "token", "password"):
        assert forbidden not in blob


def test_run_stops_after_exit(tmp_path: Path) -> None:
    o = make_observer(dead_probe(exit_code=1), beat(age_s=10))
    o.first_creation = "2026-11-15T16:00:00+00:00"
    writer = obs.RotatingJsonlWriter(tmp_path / "obs.jsonl", max_bytes=4096)
    out = o.run(writer, duration_s=0)
    assert out["last_state"] == obs.EXITED
    assert out["samples"] == 1
