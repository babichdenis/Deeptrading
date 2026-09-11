"""Централизованная настройка логирования.

Уровень задаётся через .env: LOG_LEVEL=DEBUG|INFO|WARNING|ERROR.
LOG_DEBUG_ENGINE=1 — включает подробные debug-логи горячего пути движка.
"""
from __future__ import annotations

import logging
import os


def setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "").upper()
    if not level_name:
        try:
            from app.config import get_settings
            level_name = str(get_settings().log_level).upper()
        except Exception:
            level_name = "INFO"
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    else:
        root.setLevel(level)
    # Шумные библиотеки — не ниже WARNING, если не включён DEBUG.
    if level > logging.DEBUG:
        for noisy in ("httpx", "httpcore", "grpc", "asyncio", "sqlalchemy.engine", "uvicorn.access"):
            logging.getLogger(noisy).setLevel(logging.WARNING)


def engine_debug_enabled() -> bool:
    v = os.environ.get("LOG_DEBUG_ENGINE", "")
    if v:
        return v.lower() in ("1", "true", "yes", "on")
    try:
        from app.config import get_settings
        return bool(get_settings().log_debug_engine)
    except Exception:
        return False
