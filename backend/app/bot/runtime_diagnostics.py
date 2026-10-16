from __future__ import annotations

import logging
import time
import traceback
from collections import OrderedDict

from app.services.loghub import _redact

MAX_EXCEPTION_GROUPS = 512
AUDIT_SWALLOW_SEEN: OrderedDict[str, dict] = OrderedDict()
logger = logging.getLogger("app.bot.runtime")


def audit_swallow(where: str, exc: BaseException | None = None) -> None:
    """Record recoverable failures with bounded, redacted exception groups."""
    exc_type = type(exc).__name__ if exc is not None else "-"
    key = f"{where}:{exc_type}"
    now = time.time()
    rec = AUDIT_SWALLOW_SEEN.get(key)
    if rec is None:
        if len(AUDIT_SWALLOW_SEEN) >= MAX_EXCEPTION_GROUPS:
            AUDIT_SWALLOW_SEEN.popitem(last=False)
        details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc is not None else ""
        rec = {
            "where": where,
            "exc_type": exc_type,
            "exc_msg": _redact(str(exc))[:200] if exc is not None else "",
            "count": 0,
            "first_seen": now,
            "last_seen": now,
            "traceback": _redact(details)[:2000],
        }
        AUDIT_SWALLOW_SEEN[key] = rec
    AUDIT_SWALLOW_SEEN.move_to_end(key)
    rec["count"] += 1
    rec["last_seen"] = now
    try:
        if rec["count"] == 1:
            logger.warning("SILENT-EXCEPT %s: %s: %s\n%s", where, exc_type, rec["exc_msg"], rec["traceback"])
        elif rec["count"] in (10, 100, 1000):
            logger.warning("SILENT-EXCEPT %s: %s (count=%d)", where, exc_type, rec["count"])
    except Exception:
        return
