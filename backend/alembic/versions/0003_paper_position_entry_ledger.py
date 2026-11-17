"""Поля учёта входа в paper_positions: стоп на входе и параметры ATR.

Нужны, чтобы проверять политику выхода по записанным значениям, а не по
пересчёту от текущей цены: `dist_sl_atr` в API считается от текущей цены и
текущего ATR и дефект входа не показывает.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-15
"""
from alembic import op

revision: str = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("initial_sl", "NUMERIC(20, 6)"),
    ("atr_at_entry", "NUMERIC(20, 8)"),
    ("atr_tf", "VARCHAR(8)"),
    ("atr_period", "INTEGER"),
    ("atr_as_of", "TIMESTAMP WITH TIME ZONE"),
    ("atr_multiplier", "NUMERIC(10, 4)"),
    ("policy_source", "VARCHAR(32)"),
)


def upgrade() -> None:
    for name, kind in _COLUMNS:
        op.execute(f"ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    for name, _kind in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE paper_positions DROP COLUMN IF EXISTS {name}")
