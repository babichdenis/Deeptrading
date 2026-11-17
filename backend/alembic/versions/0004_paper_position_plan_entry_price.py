"""Цена, от которой считался план входа, в paper_positions.

Стоп на входе строится от цены решения, а `entry_price` — фактическое
исполнение; без цены плана `(initial_sl - entry_price) / atr_at_entry` даёт не
множитель политики, а множитель с поправкой на проскальзывание.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-15
"""
from alembic import op

revision: str = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS plan_entry_price NUMERIC(20, 6)")


def downgrade() -> None:
    op.execute("ALTER TABLE paper_positions DROP COLUMN IF EXISTS plan_entry_price")
