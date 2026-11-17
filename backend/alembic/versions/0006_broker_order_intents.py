from alembic import op
import sqlalchemy as sa

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET LOCAL lock_timeout = '2s'")
    op.execute("SET LOCAL statement_timeout = '10s'")
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'bot_logs'
                AND column_name = 'source' AND data_type = 'character varying'
                AND character_maximum_length >= 64
            ) OR NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'bot_logs'
                AND column_name = 'request_id' AND data_type = 'character varying'
                AND character_maximum_length >= 32
            ) OR NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'sandbox_trades'
                AND column_name = 'broker_account_id' AND data_type = 'character varying'
                AND character_maximum_length >= 40
            ) THEN
                RAISE EXCEPTION 'Historical revision 0002 schema is incomplete; reconcile both legacy migrations before upgrading';
            END IF;
        END $$
    """)
    op.create_table(
        'broker_order_intents',
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('broker_account_id', sa.String(64), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('action', sa.String(8), nullable=False, server_default='close'),
        sa.Column('entry_plan', sa.JSON(), nullable=True),
        sa.Column('entry_accounting', sa.JSON(), nullable=True),
        sa.Column('close_request', sa.JSON(), nullable=True),
        sa.Column('figi', sa.String(32), nullable=False),
        sa.Column('unresolved_figi', sa.String(32), nullable=True),
        sa.Column('side', sa.String(8), nullable=False),
        sa.Column('requested_units', sa.Integer(), nullable=False),
        sa.Column('lot_size', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(32), nullable=False),
        sa.Column('broker_order_id', sa.String(128), nullable=True),
        sa.Column('broker_status', sa.String(32), nullable=True),
        sa.Column('observed_units', sa.Integer(), nullable=False),
        sa.Column('accounted_units', sa.Integer(), nullable=False),
        sa.Column('runtime_accounted_units', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('position_snapshot', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('mode', 'broker_account_id', 'client_order_id'),
        sa.UniqueConstraint('mode', 'broker_account_id', 'unresolved_figi', name='uq_broker_unresolved_instrument'),
        sa.CheckConstraint('requested_units > 0 AND lot_size > 0 AND requested_units % lot_size = 0', name='ck_broker_intent_quantity'),
        sa.CheckConstraint("side IN ('BUY', 'SELL')", name='ck_broker_intent_side'),
        sa.CheckConstraint("action IN ('open', 'close')", name='ck_broker_intent_action'),
        sa.CheckConstraint("mode IN ('live', 'sandbox')", name='ck_broker_intent_mode'),
        sa.CheckConstraint('accounted_units >= 0 AND accounted_units <= observed_units AND observed_units <= requested_units', name='ck_broker_intent_accounting'),
        sa.CheckConstraint('runtime_accounted_units IS NULL OR (runtime_accounted_units >= 0 AND runtime_accounted_units <= accounted_units)', name='ck_broker_runtime_accounting'),
    )

    op.create_table(
        'broker_order_fills',
        sa.Column('mode', sa.String(16), nullable=False),
        sa.Column('broker_account_id', sa.String(64), nullable=False),
        sa.Column('client_order_id', sa.String(64), nullable=False),
        sa.Column('cumulative_units', sa.Integer(), nullable=False),
        sa.Column('executed_units', sa.Integer(), nullable=False),
        sa.Column('trade_id', sa.BigInteger(), nullable=False),
        sa.Column('execution_evidence', sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint('mode', 'broker_account_id', 'client_order_id', 'cumulative_units'),
        sa.ForeignKeyConstraint(['mode', 'broker_account_id', 'client_order_id'],
                                ['broker_order_intents.mode', 'broker_order_intents.broker_account_id',
                                 'broker_order_intents.client_order_id']),
        sa.UniqueConstraint('trade_id', name='uq_broker_fill_trade'),
        sa.CheckConstraint('executed_units > 0 AND cumulative_units >= executed_units', name='ck_broker_fill_quantity'),
        sa.ForeignKeyConstraint(['trade_id'], ['paper_trades.id']),
    )


def downgrade():
    op.drop_table('broker_order_fills')
    op.drop_table('broker_order_intents')
