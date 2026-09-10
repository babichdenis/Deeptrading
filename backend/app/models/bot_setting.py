from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BotSetting(Base):
    """Персистентные настройки бота (переживают перезапуск backend/бота).

    key — имя набора (напр. "runtime_config"), value — JSONB со словарём
    изменяемых полей BotConfig (sessions, margin_sessions, margin_leverage, ...).
    """

    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
