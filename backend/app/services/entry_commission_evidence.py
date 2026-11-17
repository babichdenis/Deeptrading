from decimal import Decimal

from app.models.broker_order_intent import BrokerOrderIntent


async def linked_entry_commission(db, intent):
    snapshot = intent.position_snapshot
    if snapshot is None or snapshot.entry_client_order_id is None:
        return None
    opening = await db.get(BrokerOrderIntent,
        (intent.mode, intent.broker_account_id, snapshot.entry_client_order_id))
    if opening is None or not opening.entry_accounting:
        raise ValueError('Linked entry accounting unavailable')
    payload = opening.entry_accounting['evidence']
    if payload.get('source') != 'sandbox_order_aggregate':
        return None
    if (intent.mode != 'sandbox' or opening.action != 'open'
            or opening.figi != intent.figi or opening.side == intent.side
            or opening.entry_accounting.get('local_position_id') != snapshot.local_position_id
            or opening.accounted_units != opening.requested_units
            or opening.runtime_accounted_units != opening.accounted_units
            or payload.get('units') != opening.accounted_units
            or payload.get('client_order_id') != opening.client_order_id
            or payload.get('broker_order_id') != opening.broker_order_id
            or payload.get('figi') != intent.figi or payload.get('side') != opening.side
            or payload.get('currency') != 'rub' or payload.get('lot_size') != intent.lot_size
            or not 0 < intent.requested_units <= opening.accounted_units):
        raise ValueError('Linked entry commission identity mismatch')
    commission = Decimal(payload['commission'])
    if not commission.is_finite() or commission < 0:
        raise ValueError('Linked entry commission invalid')
    return commission * Decimal(intent.requested_units) / Decimal(opening.accounted_units)
