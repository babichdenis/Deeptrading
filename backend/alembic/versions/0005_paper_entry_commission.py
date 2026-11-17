from alembic import op

revision: str = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '10s'")
    op.execute("ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS entry_commission NUMERIC(20, 6)")
    op.execute("ALTER TABLE paper_trades ADD COLUMN IF NOT EXISTS entry_commission NUMERIC(20, 6)")


def downgrade() -> None:
    op.execute("ALTER TABLE paper_trades DROP COLUMN IF EXISTS entry_commission")
    op.execute("ALTER TABLE paper_positions DROP COLUMN IF EXISTS entry_commission")
