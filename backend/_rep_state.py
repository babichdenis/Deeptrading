import asyncio
import json

import httpx
from sqlalchemy import text

from app.database import SessionLocal

BASE = "http://127.0.0.1:8000/api/v1"


def get(path):
    try:
        r = httpx.get(BASE + path, timeout=20)
        return r.status_code, r.json()
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


async def data():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "select count(*) as n, max(ts) as mx, count(distinct figi) as f from candles"
        ))).mappings().one()
        return dict(rows)


code, status = get("/bot/status")
print("bot_status", code)
if isinstance(status, dict):
    keep = {k: status.get(k) for k in (
        "running", "starting", "broker_mode", "entries_paused",
        "entry_pause_reasons", "error", "pending_orders", "positions",
        "signals", "candles", "last_bar_ts",
    )}
    print(json.dumps(keep, ensure_ascii=False, default=str))

code2, sb = get("/sandbox/status")
print("sandbox_status", code2)
if isinstance(sb, dict):
    print(json.dumps(sb, ensure_ascii=False, default=str)[:700])

print("data", json.dumps(asyncio.run(data()), ensure_ascii=False, default=str))
