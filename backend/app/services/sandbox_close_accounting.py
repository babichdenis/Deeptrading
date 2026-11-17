from decimal import Decimal

from app.models.paper import PaperTrade
from app.services.sandbox_order_evidence import aggregate_metadata


def build_sandbox_close_delta(intent, evidence, accounted_units, records, *, confirmed_entry_commission=None):
    intent.validate()
    snapshot = intent.position_snapshot
    if (snapshot is None or snapshot.entry_time is None or snapshot.internal_account_id is None
            or snapshot.cost_basis != 'configured_estimate'):
        raise ValueError('Original position and cost provenance required')
    payload = aggregate_metadata(intent, evidence, evidence.observed_at)['entry_fill_evidence']
    if confirmed_entry_commission is not None and (not isinstance(confirmed_entry_commission, Decimal)
            or not confirmed_entry_commission.is_finite() or confirmed_entry_commission < 0):
        raise ValueError('Confirmed entry commission invalid')
    payload = {**payload, 'cost_basis': 'entry_estimate_exit_broker' if confirmed_entry_commission is None else 'broker_commissions',
               'confirmed_entry_commission': str(confirmed_entry_commission) if confirmed_entry_commission is not None else None,
               'entry_commission_rate': str(snapshot.commission_rate),
               'exit_time_source': 'sandbox_order_timestamp'}
    if type(accounted_units) is not int or accounted_units not in (0, evidence.units):
        raise ValueError('Partial aggregate accounting unsupported')
    if accounted_units:
        if (len(records) != 1 or records[0].execution_evidence != payload
                or records[0].executed_units != evidence.units
                or records[0].cumulative_units != evidence.units):
            raise ValueError('Previously accounted sandbox close changed')
        return None, None
    if records:
        raise ValueError('Unexpected sandbox close records')
    entry_notional = snapshot.entry_price * evidence.units
    entry_commission = (entry_notional * snapshot.commission_rate
                        if confirmed_entry_commission is None else confirmed_entry_commission)
    commission = entry_commission + evidence.commission
    gross = (evidence.notional - entry_notional) * (1 if snapshot.position_side == 'LONG' else -1)
    trade = PaperTrade(account_id=snapshot.internal_account_id, figi=intent.figi,
        ticker=snapshot.ticker, side=snapshot.position_side, qty=evidence.units,
        entry_time=snapshot.entry_time, entry_price=snapshot.entry_price,
        entry_commission=entry_commission, exit_time=evidence.order_at,
        exit_price=evidence.average_price, gross_pnl=gross, commission=commission,
        slippage=Decimal(0), net_pnl=gross-commission,
        exit_reason=snapshot.exit_reason, strategy_id=snapshot.strategy_id)
    return trade, payload
