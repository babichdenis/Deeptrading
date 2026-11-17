"""Жизненный цикл сэмплера зависаний: один поток, остановка, повторный старт."""
from __future__ import annotations

import os
import threading
import time

import pytest

from app.services import loop_monitor


@pytest.fixture(autouse=True)
def clean_sampler():
    loop_monitor.stop_sampler()
    yield
    loop_monitor.stop_sampler()


def _sampler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "loop-stall-sampler"]


def test_start_creates_single_thread():
    assert loop_monitor.start_sampler() is True
    assert loop_monitor.sampler_running() is True
    assert len(_sampler_threads()) == 1


def test_second_start_does_not_duplicate_thread():
    loop_monitor.start_sampler()
    assert loop_monitor.start_sampler() is False
    assert len(_sampler_threads()) == 1


def test_stop_removes_thread():
    loop_monitor.start_sampler()
    loop_monitor.stop_sampler()
    assert loop_monitor.sampler_running() is False
    assert _sampler_threads() == []


def test_restart_after_stop_gives_one_thread():
    loop_monitor.start_sampler()
    loop_monitor.stop_sampler()
    assert loop_monitor.start_sampler() is True
    assert len(_sampler_threads()) == 1


def test_explicit_loop_thread_is_marked():
    loop_monitor.start_sampler(loop_thread_id=threading.get_ident())
    lines = loop_monitor._all_threads()
    marked = [line for line in lines if "[loop]" in line]
    assert len(marked) == 1
    assert "MainThread" in marked[0]


def test_stall_text_names_blocking_frame():
    loop_monitor.start_sampler(loop_thread_id=threading.get_ident())
    time.sleep(0.05)
    text = loop_monitor._stall_text(7.5)
    assert "EVENTLOOP_STALL 7.5s" in text
    assert "[loop]" in text
    assert "test_stall_text_names_blocking_frame" in text


def test_rotate_moves_full_file(tmp_path, monkeypatch):
    target = tmp_path / "loop_stall.log"
    target.write_text("x" * 2048, encoding="utf-8")
    monkeypatch.setattr(loop_monitor, "STALL_LOG", str(target))
    monkeypatch.setattr(loop_monitor, "STALL_LOG_MAX", 1024)
    loop_monitor._rotate()
    assert (tmp_path / "loop_stall.log.1").exists()
    assert not target.exists()


def test_append_writes_file(tmp_path, monkeypatch):
    target = tmp_path / "loop_stall.log"
    monkeypatch.setattr(loop_monitor, "STALL_LOG", str(target))
    monkeypatch.setattr(loop_monitor, "_stall_dir", str(tmp_path))
    loop_monitor._append("проверка записи\n")
    assert target.read_text(encoding="utf-8") == "проверка записи\n"


def test_heartbeat_older_than_threshold_is_stall():
    loop_monitor.start_sampler()
    loop_monitor._heartbeat = time.monotonic() - loop_monitor.STALL_SECONDS - 1.0
    assert time.monotonic() - loop_monitor._heartbeat > loop_monitor.STALL_SECONDS
