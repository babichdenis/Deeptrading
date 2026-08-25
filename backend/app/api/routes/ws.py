from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.eventbus import event_bus

router = APIRouter(tags=["ws"])


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=500)
    channels: set[str] = set()
    reader_task: asyncio.Task | None = None

    async def reader() -> None:
        nonlocal channels
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "SUBSCRIBE":
                new_channels = set(msg.get("channels", []))
                for ch in new_channels - channels:
                    await event_bus.subscribe(ch, queue)
                channels = new_channels
                await ws.send_json({"type": "SUBSCRIBED", "channels": sorted(channels)})
                for ch in sorted(channels):
                    events = await event_bus.events_after(ch, 0, limit=50)
                    if events:
                        last = events[-1]
                        snap = {
                            "type": "SNAPSHOT",
                            "channel": ch,
                            "last_sequence": last["sequence"],
                            "events": events,
                        }
                    else:
                        snap = {"type": "SNAPSHOT", "channel": ch, "last_sequence": 0, "events": []}
                    await ws.send_json(snap)
            elif mtype == "RESUME":
                ch = msg.get("channel")
                last_seq = int(msg.get("last_sequence", 0))
                events = await event_bus.events_after(ch, last_seq)
                await ws.send_json(
                    {"type": "RESUMED", "channel": ch, "last_sequence": last_seq, "events": events}
                )

    try:
        reader_task = asyncio.create_task(reader())
        while True:
            payload = await queue.get()
            await ws.send_text(payload)
    except WebSocketDisconnect:
        pass
    except asyncio.CancelledError:
        pass
    finally:
        if reader_task:
            reader_task.cancel()
        for ch in channels:
            await event_bus.unsubscribe(ch, queue)
