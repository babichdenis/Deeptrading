import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from t_tech.invest import Client


def main(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    settings = get_settings()
    rows = []
    with Client(settings.get_token('live')) as services:
        accounts = services.users.get_accounts().accounts
        for account in accounts:
            row = {'account_fingerprint': hashlib.sha256(account.id.encode()).hexdigest(),
                   'name': account.name, 'type': account.type.name,
                   'status': account.status.name, 'access_level': account.access_level.name,
                   'selected': account.id == settings.get_account('live')}
            try:
                portfolio = services.operations.get_portfolio(account_id=account.id)
                orders = services.orders.get_orders(account_id=account.id)
                money = lambda value: value.units + value.nano / 1e9
                row.update(cash_currency_value=money(portfolio.total_amount_currencies),
                           equity=money(portfolio.total_amount_portfolio),
                           currency=portfolio.total_amount_portfolio.currency,
                           positions=sum(p.instrument_type != 'currency' and money(p.quantity) != 0
                                         for p in portfolio.positions), active_orders=len(orders.orders))
            except Exception as exc:
                row['read_error_type'] = type(exc).__name__
            rows.append(row)
    report = {'as_of': datetime.now(timezone.utc).isoformat(), 'accounts': rows,
              'scope': 'production read-only accounts/portfolios/orders, no selected-account change',
              'limitation': 'Only accounts visible to current token; missing account does not prove nonexistence.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    main(parser.parse_args())
