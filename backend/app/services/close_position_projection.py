import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select

from app.models.broker_order_intent import BrokerOrderIntent
from app.models.sandbox_trade import SandboxTrade
from app.services.sandbox_order_evidence import accounting_totals


def price_equal(first, second):
    return Decimal(str(first)).quantize(Decimal('0.000001')) == Decimal(str(second)).quantize(Decimal('0.000001'))


async def project_close_fills(db, intent, trades):
    """Update the explicitly linked position inside the journal acknowledgement transaction."""
    snapshot = intent.position_snapshot
    if snapshot is None or snapshot.local_position_id is None or not snapshot.entry_client_order_id:
        raise ValueError('Explicit original local position link required')
    intent.validate()
    ledger = await db.get(BrokerOrderIntent, (intent.mode, intent.broker_account_id, intent.client_order_id))
    if ledger is None or ledger.runtime_accounted_units is None:
        raise ValueError('Durable local accounting checkpoint required')
    position = await db.scalar(select(SandboxTrade).where(
        SandboxTrade.id == snapshot.local_position_id).with_for_update())
    remaining = intent.requested_units - ledger.runtime_accounted_units
    expected_side = 'BUY' if snapshot.position_side == 'LONG' else 'SELL'
    if (position is None or position.exit_time is not None
            or (position.mode, position.broker_account_id, position.figi)
            != (intent.mode, intent.broker_account_id, intent.figi)
            or position.side not in (snapshot.position_side, expected_side)
            or position.qty != remaining or position.entry_time != snapshot.entry_time
            or not price_equal(position.entry_price, snapshot.entry_price)):
        raise ValueError('Linked local position differs from original execution cycle')
    metadata = json.loads(position.meta or '{}')
    if (not isinstance(metadata, dict)
            or metadata.get('client_order_id') != snapshot.entry_client_order_id
            or metadata.get('entry_time_source') != snapshot.entry_time_source):
        raise ValueError('Linked local position has different entry provenance')
    if snapshot.entry_time_source == 'sandbox_order_timestamp':
        evidence = metadata.get('entry_fill_evidence')
        if (not isinstance(evidence, dict) or evidence.get('source') != 'sandbox_order_aggregate'
                or evidence.get('client_order_id') != snapshot.entry_client_order_id
                or evidence.get('figi') != intent.figi or evidence.get('side') != expected_side
                or evidence.get('lot_size') != intent.lot_size):
            raise ValueError('Linked local position has different sandbox entry evidence')
        units, price, recorded_at = accounting_totals(evidence)
        if units < intent.requested_units or not price_equal(price, snapshot.entry_price):
            raise ValueError('Linked local position differs from sandbox entry evidence')
    else:
        confirmed = metadata.get('entry_time_confirmed')
        recorded_at = datetime.fromisoformat(confirmed) if isinstance(confirmed, str) else None
    if recorded_at != snapshot.entry_time:
        raise ValueError('Linked local position has different entry time')
    total = sum(trade.qty for trade in trades)
    if total <= 0 or total > remaining:
        raise ValueError('Local close delta exceeds the linked position')
    for trade in trades:
        if (trade.figi != intent.figi or trade.side != snapshot.position_side
                or trade.entry_time != snapshot.entry_time
                or not price_equal(trade.entry_price, snapshot.entry_price)
                or trade.account_id != snapshot.internal_account_id
                or type(trade.qty) is not int or trade.qty <= 0 or trade.qty > remaining):
            raise ValueError('Durable trade differs from linked local position')
        if trade.qty < remaining:
            values = {column.key: getattr(position, column.key) for column in SandboxTrade.__table__.columns
                      if column.key != 'id'}
            closed = SandboxTrade(**values)
            closed.qty = trade.qty
            db.add(closed)
            position.qty = remaining - trade.qty
        else:
            closed = position
        closed.exit_time = trade.exit_time
        closed.exit_price = trade.exit_price
        closed.exit_reason = trade.exit_reason
        closed.commission = trade.commission
        closed.net_pnl = trade.net_pnl
        closed.exit_meta = json.dumps(dict(
            version=1, close_client_order_id=intent.client_order_id,
            entry_client_order_id=snapshot.entry_client_order_id,
            original_local_position_id=snapshot.local_position_id,
            paper_trade_id=trade.id, executed_units=trade.qty,
            cost_basis=snapshot.cost_basis,
            commission_rate=str(snapshot.commission_rate) if snapshot.commission_rate is not None else None,
            slippage_basis='included_in_execution_price'), sort_keys=True)
        remaining -= trade.qty
    await db.flush()
