from datetime import datetime, timezone
from decimal import Decimal

from app.models.paper import PaperTrade
from app.services.broker_fill_evidence import OrderFillEvidence
from app.services.close_position_snapshot import finite_decimal


def build_close_delta(intent, evidence, accounted_units, records, *, confirmed_entry_commission=None):
    """Match immutable broker stages before accounting only previously unseen executions."""
    from app.services.sandbox_order_evidence import SandboxOrderAggregate
    if isinstance(evidence, SandboxOrderAggregate):
        from app.services.sandbox_close_accounting import build_sandbox_close_delta
        return build_sandbox_close_delta(intent, evidence, accounted_units, records,
            confirmed_entry_commission=confirmed_entry_commission)
    intent.validate()
    snapshot = intent.position_snapshot
    if snapshot is None or snapshot.entry_time is None or snapshot.internal_account_id is None:
        raise ValueError('Confirmed original position context required for late accounting')
    if snapshot.cost_basis != 'configured_estimate':
        raise ValueError('Explicit saved cost basis required for late accounting')
    if (not isinstance(evidence, OrderFillEvidence)
            or (evidence.mode, evidence.broker_account_id, evidence.client_order_id, evidence.figi, evidence.side)
            != (intent.mode, intent.broker_account_id, intent.client_order_id, intent.figi, intent.side)
            or evidence.requested_lots != intent.requested_units // intent.lot_size
            or evidence.lot_size != intent.lot_size
            or type(evidence.executed_lots) is not int
            or not 0 < evidence.executed_lots <= evidence.requested_lots
            or not isinstance(evidence.currency, str) or not evidence.currency.strip()
            or type(accounted_units) is not int or not 0 <= accounted_units <= evidence.units):
        raise ValueError('Close execution evidence differs from original intent')
    stages = {}
    for fill in evidence.fills:
        if (not isinstance(fill.trade_id, str) or not fill.trade_id.strip() or fill.trade_id in stages
                or type(fill.units) is not int or fill.units <= 0 or fill.units % intent.lot_size
                or not isinstance(fill.executed_at, datetime) or fill.executed_at.utcoffset() is None
                or fill.executed_at < intent.created_at):
            raise ValueError('Invalid close execution stage')
        stages[fill.trade_id] = dict(trade_id=fill.trade_id, units=fill.units,
            price=str(finite_decimal(fill.price, positive=True)),
            executed_at=fill.executed_at.astimezone(timezone.utc).isoformat())
    if evidence.units != evidence.executed_lots * intent.lot_size:
        raise ValueError('Close stages do not cover cumulative execution')
    known = set()
    recorded_units = 0
    for record in records:
        payload = record.execution_evidence
        if (not isinstance(payload, dict) or payload.get('version') != 1
                or payload.get('currency') != evidence.currency
                or not isinstance(payload.get('fills'), list)):
            raise ValueError('Previously accounted execution has no matching stage evidence')
        record_units = 0
        for previous in payload['fills']:
            if not isinstance(previous, dict):
                raise ValueError('Invalid stored execution stage')
            trade_id = previous.get('trade_id')
            if not isinstance(trade_id, str) or trade_id in known or stages.get(trade_id) != previous:
                raise ValueError('Previously accounted broker stage changed or disappeared')
            known.add(trade_id)
            record_units += previous['units']
        recorded_units += record_units
        if record_units != record.executed_units or record.cumulative_units != recorded_units:
            raise ValueError('Stored stage quantities do not match accounting ledger')
    if recorded_units != accounted_units:
        raise ValueError('Stored stages do not cover accounted quantity')
    new_fills = [fill for fill in evidence.fills if fill.trade_id not in known]
    delta = sum(fill.units for fill in new_fills)
    if delta != evidence.units - accounted_units:
        raise ValueError('New stages do not match unaccounted execution')
    if not delta:
        return None, None
    notional = sum((finite_decimal(fill.price, positive=True) * fill.units for fill in new_fills), Decimal(0))
    entry_notional = finite_decimal(snapshot.entry_price, positive=True) * delta
    gross = (notional - entry_notional) * (1 if snapshot.position_side == 'LONG' else -1)
    rate = finite_decimal(snapshot.commission_rate)
    commission = (entry_notional + notional) * rate
    trade = PaperTrade(account_id=snapshot.internal_account_id, figi=intent.figi,
        ticker=snapshot.ticker, side=snapshot.position_side, qty=delta,
        entry_time=snapshot.entry_time, entry_price=snapshot.entry_price,
        entry_commission=entry_notional * rate,
        exit_time=max(fill.executed_at for fill in new_fills), exit_price=notional / delta,
        gross_pnl=gross, commission=commission, slippage=0, net_pnl=gross - commission,
        exit_reason=snapshot.exit_reason, strategy_id=snapshot.strategy_id)
    payload = dict(version=1, currency=evidence.currency,
        cost_basis=snapshot.cost_basis, commission_rate=str(rate),
        slippage_basis='included_in_execution_price',
        fills=[stages[fill.trade_id] for fill in new_fills])
    return trade, payload
