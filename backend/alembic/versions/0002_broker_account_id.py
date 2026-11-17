"""Consolidates the two historical 0002 definitions; inspect stamped databases before upgrades."""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '10s'")
    op.execute("ALTER TABLE bot_logs ALTER COLUMN source TYPE VARCHAR(64)")
    op.execute("ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS request_id VARCHAR(32)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_bot_logs_request_id ON bot_logs (request_id)")
    op.execute("ALTER TABLE sandbox_trades ADD COLUMN IF NOT EXISTS broker_account_id VARCHAR(40)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sandbox_trades_broker_account_id ON sandbox_trades (broker_account_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_sandbox_trades_broker_account_id")
    op.execute("ALTER TABLE sandbox_trades DROP COLUMN IF EXISTS broker_account_id")
    op.execute("DROP INDEX IF EXISTS ix_bot_logs_request_id")
    op.execute("ALTER TABLE bot_logs DROP COLUMN IF EXISTS request_id")
    op.execute("ALTER TABLE bot_logs ALTER COLUMN source TYPE VARCHAR(16)")
