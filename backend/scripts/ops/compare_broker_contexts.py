import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.bot.live_broker import LiveBroker


def main():
    output = Path('reports/architect_20261115/broker_context_comparison_r1.json')
    if output.exists():
        raise FileExistsError(output)
    rows = []
    for mode in ('sandbox', 'live'):
        broker = LiveBroker(None, config=SimpleNamespace(mode=mode), mode=mode)
        try:
            services = broker._get_services()
            if mode == 'sandbox':
                portfolio = services.sandbox.get_sandbox_portfolio(account_id=broker._account)
                orders = services.sandbox.get_sandbox_orders(account_id=broker._account).orders
            else:
                portfolio = services.operations.get_portfolio(account_id=broker._account)
                orders = services.orders.get_orders(account_id=broker._account).orders
            number = lambda value: value.units + value.nano / 1e9
            rows.append({'mode': broker.mode, 'target': broker._target or 'production default',
                         'account_fingerprint': hashlib.sha256(broker._account.encode()).hexdigest(),
                         'cash_currency_value': number(portfolio.total_amount_currencies),
                         'equity': number(portfolio.total_amount_portfolio),
                         'positions': sum(p.instrument_type != 'currency' and number(p.quantity) != 0
                                          for p in portfolio.positions), 'active_orders': len(orders)})
        finally:
            broker.close()
    report = {'as_of': datetime.now(timezone.utc).isoformat(), 'contexts': rows,
              'distinct_accounts': len({row['account_fingerprint'] for row in rows}) == 2,
              'scope': 'actual project adapter connections, read-only portfolio/orders; selected app mode unchanged',
              'limitations': 'No HTTP mode switching, runtime start, replay or order submission.'}
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
