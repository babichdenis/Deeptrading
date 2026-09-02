"""REST-мост для оркестратора: публикация сообщений в event_bus (канал orchestrator).

Оркестратор (отдельный процесс) шлёт сюда POST — фронт (WS) видит событие
в канале `orchestrator`. Решения (approve/reject) принимаются через
decisions.json в orchestrator/ (CLI) — или здесь же можно добавить эндпоинт.
"""
from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.eventbus import event_bus

router = APIRouter(prefix="/api/v1/orchestrator", tags=["orchestrator"])


class OrchestratorMessage(BaseModel):
    text: str
    category: str = "info"
    reply_markup: dict | None = None


@router.post("/message")
async def orchestrator_message(msg: OrchestratorMessage) -> dict:
    await event_bus.publish(
        "orchestrator", "MESSAGE",
        {"text": msg.text, "category": msg.category, "reply_markup": msg.reply_markup},
        entity_type="orchestrator",
    )
    return {"ok": True}
