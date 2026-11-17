import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.request

from app.config import get_settings
from t_tech.invest import Client, MoneyValue


RECEIPT = Path('data/sandbox_baseline_20k_20261115.json')


def save(value):
    temp = RECEIPT.with_suffix('.tmp')
    temp.write_text(json.dumps(value, default=str, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, RECEIPT)


def amount(value):
    return value.units + value.nano / 1e9


async def baseline(receipt):
    from app.database import SessionLocal
    from sqlalchemy import text
    async with SessionLocal() as db:
        rows = (await db.execute(text("SELECT * FROM paper_accounts WHERE name='default' FOR UPDATE"))).mappings().all()
        if len(rows) != 1:
            raise RuntimeError('Expected one existing local baseline account')
        if 'previous_local_baseline' not in receipt:
            receipt['previous_local_baseline'] = [dict(row) for row in rows]
            save(receipt)
        await db.execute(text("UPDATE paper_accounts SET initial_cash=20000, cash=20000 WHERE name='default'"))
        await db.commit()


def main():
    with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/bot/status', timeout=10) as response:
        status = json.load(response)
    if status.get('running') is not False or status.get('starting'):
        raise RuntimeError('Bot must be stopped')
    wrapper = Path('C:/Users/nadts/start_backend.ps1').read_text(encoding='utf-8-sig')
    if '$env:BOT_AUTOSTART = "0"' not in wrapper:
        raise RuntimeError('Autostart must remain disabled')
    settings = get_settings()
    target = settings.get_target('sandbox')
    assert target == 'sandbox-invest-public-api.tbank.ru'
    receipt = json.loads(RECEIPT.read_text(encoding='utf-8')) if RECEIPT.exists() else {'started_at': datetime.now(timezone.utc).isoformat(), 'target_cash': 20000, 'old_account': settings.get_account('sandbox')}
    with Client(settings.get_token('sandbox'), target=target) as client:
        api = client.sandbox
        if 'old_portfolio' not in receipt:
            receipt['old_portfolio'] = asdict(api.get_sandbox_portfolio(account_id=receipt['old_account']))
            receipt['old_orders'] = asdict(api.get_sandbox_orders(account_id=receipt['old_account']))
            save(receipt)
        if 'new_account' not in receipt:
            name = 'Deeptrading-baseline-20k-20261115'
            matches = [a for a in api.get_sandbox_accounts().accounts if a.name == name]
            if len(matches) > 1:
                raise RuntimeError('Ambiguous baseline accounts')
            receipt['new_account'] = matches[0].id if matches else api.open_sandbox_account(name=name).account_id
            save(receipt)
        account = receipt['new_account']
        portfolio = api.get_sandbox_portfolio(account_id=account)
        cash = amount(portfolio.total_amount_currencies)
        if cash == 0 and not receipt.get('funding_submitted'):
            receipt['funding_submitted'] = True
            save(receipt)
            api.sandbox_pay_in(account_id=account, amount=MoneyValue(currency='rub', units=20000, nano=0))
        portfolio = api.get_sandbox_portfolio(account_id=account)
        orders = api.get_sandbox_orders(account_id=account)
        cash = amount(portfolio.total_amount_currencies)
        equity = amount(portfolio.total_amount_portfolio)
        positions = [p for p in portfolio.positions if p.instrument_type != 'currency' and amount(p.quantity) != 0]
        if cash != 20000 or equity != 20000 or positions or orders.orders:
            raise RuntimeError('New account baseline not confirmed; no automatic duplicate funding')
        receipt['verified'] = {'at': datetime.now(timezone.utc).isoformat(), 'cash': cash, 'equity': equity, 'positions': len(positions), 'orders': len(orders.orders)}
        save(receipt)
    asyncio.run(baseline(receipt))
    path = Path('.env')
    raw = path.read_text(encoding='utf-8-sig')
    for key, value in [('SANDBOX_ACCOUNT', receipt['new_account']), ('BOT_MODE', 'sandbox')]:
        pattern = rf'(?m)^{key}=.*$'
        raw = re.sub(pattern, f'{key}={value}', raw) if re.search(pattern, raw) else raw.rstrip() + f'\n{key}={value}\n'
    temp = path.with_name('.env.sandbox-switch.tmp')
    temp.write_text(raw, encoding='utf-8')
    os.replace(temp, path)
    receipt['configured'] = True
    save(receipt)
    print(json.dumps({'configured': True, **receipt['verified'], 'old_account_retained': True, 'receipt': str(RECEIPT)}))


if __name__ == '__main__':
    main()
