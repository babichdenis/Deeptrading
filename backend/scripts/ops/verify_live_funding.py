import argparse
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from app.config import get_settings
from t_tech.invest import Client, OperationState, OperationType


def amount(value):
    return Decimal(value.units) + Decimal(value.nano) / Decimal(1_000_000_000)


def main(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    start = datetime.fromisoformat(args.since)
    if start.tzinfo is None:
        raise ValueError('Explicit timezone required')
    now = datetime.now(timezone.utc)
    if start > now:
        raise ValueError('Observation start is in future')
    settings = get_settings()
    account = settings.get_account('live')
    if not account or not settings.get_token('live'):
        raise RuntimeError('Live connection not configured')
    report = {'since': start.isoformat(), 'as_of': now.isoformat(),
              'scope': 'production read-only portfolio and executed operations',
              'account_fingerprint': hashlib.sha256(account.encode()).hexdigest()}
    with Client(settings.get_token('live')) as services:
        portfolio = services.operations.get_portfolio(account_id=account)
        response = services.operations.get_operations(
            account_id=account, from_=start, to=now,
            state=OperationState.OPERATION_STATE_EXECUTED)
        orders = services.orders.get_orders(account_id=account).orders
    operations = response.operations
    flows = []
    for operation in operations:
        if operation.operation_type not in (OperationType.OPERATION_TYPE_INPUT,
                                            OperationType.OPERATION_TYPE_OUTPUT):
            continue
        flows.append({'id_hash': hashlib.sha256(operation.id.encode()).hexdigest(),
                      'date': operation.date.isoformat(),
                      'type': operation.operation_type.name,
                      'currency': operation.payment.currency,
                      'payment': str(amount(operation.payment))})
    report.update({
        'cash_currency_value': str(amount(portfolio.total_amount_currencies)),
        'currency': portfolio.total_amount_currencies.currency,
        'equity': str(amount(portfolio.total_amount_portfolio)),
        'active_orders': len(orders),
        'noncurrency_positions': sum(p.instrument_type != 'currency' and amount(p.quantity) != 0
                                     for p in portfolio.positions),
        'executed_operations_count': len(operations), 'observed_external_flows': flows,
        'observed_rub_net_flow': str(sum((Decimal(f['payment']) for f in flows
                                        if f['currency'].lower() == 'rub'), Decimal(0))),
        'limitations': 'Observed operations within requested window only; no historical PnL or full-account performance claim.'
    })
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', required=True)
    parser.add_argument('--output', required=True)
    main(parser.parse_args())
