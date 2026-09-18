"""Догоняющая миграция: контуры сделок и имя теста в sandbox_trades.

Раньше эти колонки добавлялись вручную при старте приложения (ALTER TABLE в
app/main.py). Переносим в миграции. IF NOT EXISTS — чтобы не падать на БД,
где create_all из текущих моделей уже создал их.

Revision ID: 0001
Revises:
Create Date: 2026-09-18
"""
from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS mode VARCHAR(8) DEFAULT 'sandbox'"
    )
    op.execute(
        "ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS test_name VARCHAR(64)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sandbox_trades_test_name "
        "ON sandbox_trades (test_name)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_sandbox_trades_test_name")
    op.execute("ALTER TABLE sandbox_trades DROP COLUMN IF EXISTS test_name")
    op.execute("ALTER TABLE sandbox_trades DROP COLUMN IF EXISTS mode")