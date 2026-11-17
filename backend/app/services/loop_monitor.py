"""След зависания event loop: поток-сэмплер пишет стек в файл независимо от цикла.

Причина: 15.11.2026 бэкенд трижды за час перестал отвечать на `/api/v1/bot/state`
(10:01, 10:26, 10:57), каждый раз лечился перезапуском, и ни один лог не назвал
того, что держало цикл. След должен переживать внешний kill процесса, поэтому
пишет его отдельный поток, а не задача внутри цикла.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time

logger = logging.getLogger("loop_monitor")

INTERVAL = 2.0
LAG_WARN = 1.5
HEARTBEAT_EVERY = 300.0
SAMPLE_INTERVAL = 1.0
STALL_SECONDS = 5.0
WRITE_EVERY = 10.0
STACK_DEPTH = 10
STALL_LOG_MAX = 2 * 1024 * 1024

_stall_dir = os.environ.get("DEEPTRADING_LOG_DIR") or r"X:\run"
STALL_LOG = os.path.join(_stall_dir, "loop_stall.log")

_lock = threading.Lock()
_stop = threading.Event()
_thread: threading.Thread | None = None
_loop_thread_id: int | None = None
_started = 0.0
_heartbeat = 0.0


def _frames_of(thread_id: int) -> str:
    frame = sys._current_frames().get(thread_id)
    if frame is None:
        return f"поток {thread_id}: кадров нет"
    parts: list[str] = []
    depth = 0
    while frame is not None and depth < STACK_DEPTH:
        code = frame.f_code
        name = code.co_filename.replace("\\", "/").split("/")[-1]
        parts.append(f"{name}:{frame.f_lineno} {code.co_name}")
        frame = frame.f_back
        depth += 1
    return " <- ".join(parts)


def _all_threads() -> list[str]:
    lines: list[str] = []
    for thread in threading.enumerate():
        ident = thread.ident or 0
        mark = " [loop]" if ident == _loop_thread_id else ""
        lines.append(f"  поток {thread.name}{mark}: {_frames_of(ident)}")
    return lines


def _task_brief(task: asyncio.Task) -> str:
    name = task.get_name()
    stack = task.get_stack(limit=6)
    if not stack:
        return f"{name} (стек недоступен)"
    frames: list[str] = []
    for frame in stack:
        code = frame.f_code
        fname = code.co_filename.replace("\\", "/").split("/")[-1]
        frames.append(f"{fname}:{frame.f_lineno} {code.co_name}")
    return f"{name} <- " + " | ".join(frames)


def dump_tasks(limit: int = 25) -> list[str]:
    """Стопки живых задач цикла, самые глубокие первыми."""
    out: list[str] = []
    for task in asyncio.all_tasks():
        if task.done():
            continue
        out.append(_task_brief(task))
    out.sort(key=len, reverse=True)
    return out[:limit]


def _rotate() -> None:
    try:
        if os.path.getsize(STALL_LOG) > STALL_LOG_MAX:
            os.replace(STALL_LOG, STALL_LOG + ".1")
    except OSError:
        pass


def _append(text: str) -> None:
    try:
        os.makedirs(_stall_dir, exist_ok=True)
        _rotate()
        with open(STALL_LOG, "a", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        logger.warning("loop_stall: запись не удалась: %s", exc)


def _stall_text(stalled: float) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    head = (
        f"\n=== {stamp} EVENTLOOP_STALL {stalled:.1f}s "
        f"uptime={(time.monotonic() - _started) / 60.0:.1f}m "
        f"threads={threading.active_count()} ===\n"
    )
    body = "\n".join(_all_threads())
    return head + body + "\n"


def _sampler_loop(thread_id: int, stop: threading.Event) -> None:
    last_written = 0.0
    while not stop.wait(SAMPLE_INTERVAL):
        now = time.monotonic()
        stalled = now - _heartbeat
        if stalled < STALL_SECONDS or now - last_written < WRITE_EVERY:
            continue
        last_written = now
        _append(_stall_text(stalled))


def start_sampler(loop_thread_id: int | None = None) -> bool:
    """Запускает поток-сэмплер; повторный вызов не создаёт второй поток."""
    global _stop, _thread, _loop_thread_id, _started, _heartbeat
    with _lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop = threading.Event()
        _loop_thread_id = loop_thread_id or threading.get_ident()
        _started = time.monotonic()
        _heartbeat = _started
        _thread = threading.Thread(
            target=_sampler_loop,
            args=(_loop_thread_id, _stop),
            name="loop-stall-sampler",
            daemon=True,
        )
        _thread.start()
        return True


def stop_sampler() -> None:
    global _thread
    with _lock:
        stop = _stop
        thread = _thread
        _thread = None
        stop.set()
    if thread is not None and thread.is_alive():
        thread.join(timeout=2.0)


def sampler_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


async def monitor_loop() -> None:
    """Держит heartbeat цикла и пишет лаг в обычный лог."""
    global _heartbeat
    start_sampler()
    started = time.monotonic()
    _heartbeat = started
    last_beat = started
    worst = 0.0
    try:
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(INTERVAL)
            now = time.monotonic()
            _heartbeat = now
            lag = now - t0 - INTERVAL
            if lag > worst:
                worst = lag
            if lag > LAG_WARN:
                logger.warning(
                    "EVENTLOOP_LAG %.2fs (ожидали %.1fs): цикл был занят",
                    lag, INTERVAL)
                for line in dump_tasks():
                    logger.warning("  task: %s", line)
            if now - last_beat >= HEARTBEAT_EVERY:
                last_beat = now
                logger.info(
                    "loop_monitor: uptime %.0f мин, худший лаг %.2fs, задач %d",
                    (now - started) / 60.0, worst, len(asyncio.all_tasks()))
                worst = 0.0
    finally:
        stop_sampler()
