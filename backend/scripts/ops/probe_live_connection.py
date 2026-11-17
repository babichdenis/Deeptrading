import json
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings
from t_tech.invest import Client


def main():
    output = Path('data/live_connection_20261115.json')
    if output.exists():
        raise FileExistsError(output)
    settings = get_settings()
    account = settings.get_account('live')
    report = {'as_of': datetime.now(timezone.utc).isoformat(),
              'scope': 'production read-only; no orders, funding or account changes',
              'token_configured': bool(settings.get_token('live')),
              'account_configured': bool(account)}
    if settings.get_token('live'):
        with Client(settings.get_token('live')) as services:
            accounts = services.users.get_accounts().accounts
            report['visible_accounts'] = len(accounts)
            report['configured_account_visible'] = any(a.id == account for a in accounts)
            if account and report['configured_account_visible']:
                portfolio = services.operations.get_portfolio(account_id=account)
                orders = services.orders.get_orders(account_id=account)
                money = lambda v: v.units + v.nano / 1e9
                report['currency_value'] = money(portfolio.total_amount_currencies)
                report['currency'] = portfolio.total_amount_currencies.currency
                report['noncurrency_positions'] = sum(
                    p.instrument_type != 'currency' and money(p.quantity) != 0 for p in portfolio.positions)
                report['active_orders'] = len(orders.orders)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
