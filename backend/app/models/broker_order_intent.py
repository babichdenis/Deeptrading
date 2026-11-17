from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BrokerOrderIntent(Base):
    __tablename__ = 'broker_order_intents'
    __table_args__ = (
        UniqueConstraint('mode', 'broker_account_id', 'unresolved_figi', name='uq_broker_unresolved_instrument'),
        CheckConstraint('requested_units > 0 AND lot_size > 0 AND requested_units % lot_size = 0', name='ck_broker_intent_quantity'),
        CheckConstraint("side IN ('BUY', 'SELL')", name='ck_broker_intent_side'),
        CheckConstraint("action IN ('open', 'close')", name='ck_broker_intent_action'),
        CheckConstraint("mode IN ('live', 'sandbox')", name='ck_broker_intent_mode'),
        CheckConstraint('accounted_units >= 0 AND accounted_units <= observed_units AND observed_units <= requested_units', name='ck_broker_intent_accounting'),
        CheckConstraint('runtime_accounted_units IS NULL OR (runtime_accounted_units >= 0 AND runtime_accounted_units <= accounted_units)', name='ck_broker_runtime_accounting'),
        {'info': {'migration_managed': True}},
    )

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action: Mapped[str] = mapped_column(String(8), default='close', server_default='close')
    entry_plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    entry_accounting: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    close_request: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    figi: Mapped[str] = mapped_column(String(32))
    unresolved_figi: Mapped[str | None] = mapped_column(String(32), nullable=True)
    side: Mapped[str] = mapped_column(String(8))
    requested_units: Mapped[int] = mapped_column(Integer)
    lot_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default='PENDING_RECONCILIATION')
    broker_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    broker_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    observed_units: Mapped[int] = mapped_column(Integer, default=0)
    accounted_units: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    runtime_accounted_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    position_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class BrokerOrderFill(Base):
    __tablename__ = 'broker_order_fills'
    __table_args__ = (
        ForeignKeyConstraint(['mode', 'broker_account_id', 'client_order_id'],
                             ['broker_order_intents.mode', 'broker_order_intents.broker_account_id',
                              'broker_order_intents.client_order_id']),
        UniqueConstraint('trade_id', name='uq_broker_fill_trade'),
        CheckConstraint('executed_units > 0 AND cumulative_units >= executed_units', name='ck_broker_fill_quantity'),
        {'info': {'migration_managed': True}},
    )

    mode: Mapped[str] = mapped_column(String(16), primary_key=True)
    broker_account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cumulative_units: Mapped[int] = mapped_column(Integer, primary_key=True)
    executed_units: Mapped[int] = mapped_column(Integer)
    trade_id: Mapped[int] = mapped_column(BigInteger, ForeignKey('paper_trades.id'))

    execution_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
