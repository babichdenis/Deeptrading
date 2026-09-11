"""Персистентная копия live-логов бота (включая replay/parity реплеи).

Таблица нужна чтобы `_live_logs` (deque, in-memory) не были единственным местом
хранения: после рестарта бота история восстанавливается из bot_logs, а
Live/UI могут фильтровать по уровню (debug/info/warn/error) и source
(bot/replay/parity).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BotLog(Base):
    __tablename__ = "bot_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")  # debug/info/warn/error
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="bot")  # bot/replay/parity
    msg: Mapped[str] = mapped_column(Text, nullable=False)
