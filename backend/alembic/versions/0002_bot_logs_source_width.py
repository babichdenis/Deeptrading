"""bot_logs source 16->64, add request_id

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-03
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("bot_logs", "source",
                    existing_type=sa.String(16),
                    type_=sa.String(64),
                    existing_nullable=False)
    op.add_column("bot_logs", sa.Column("request_id", sa.String(32), nullable=True))
    op.create_index("ix_bot_logs_request_id", "bot_logs", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_bot_logs_request_id", table_name="bot_logs")
    op.drop_column("bot_logs", "request_id")
    op.alter_column("bot_logs", "source",
                    existing_type=sa.String(64),
                    type_=sa.String(16),
                    existing_nullable=False)
