from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import time

from app.config import get_settings
from t_tech.invest import Client, GetMaxLotsRequest, InstrumentIdType


def main():
    output = Path('data/sandbox_capabilities_20261115.json')
    if output.exists():
        raise FileExistsError(output)
    settings = get_settings()
    baseline = json.loads(Path('data/sandbox_baseline_20k_20261115.json').read_text())
    account = settings.get_account('sandbox')
    assert account == baseline['new_account']
    assert settings.get_target('sandbox') == 'sandbox-invest-public-api.tbank.ru'
    now = datetime.now(timezone.utc)
    report = {'as_of': now.isoformat(), 'scope': 'read-only sandbox; no order submission or account mutation', 'checks': []}
    def probe(name, call):
        started = time.monotonic()
        try:
            value = call()
            row = {'method': name, 'status': 'OK', 'response': asdict(value)}
        except Exception as exc:
            row = {'method': name, 'status': 'ERROR', 'error_type': type(exc).__name__, 'message': str(exc)[:400]}
        row['elapsed_seconds'] = round(time.monotonic()-started, 3)
        report['checks'].append(row)
        output.write_text(json.dumps(report, default=str, indent=2)+'\n', encoding='utf-8')
        print(json.dumps({k:v for k,v in row.items() if k != 'response'}), flush=True)
    with Client(settings.get_token('sandbox'), target=settings.get_target('sandbox')) as svc:
        api = svc.sandbox
        probe('GetSandboxPositions', lambda: api.get_sandbox_positions(account_id=account))
        probe('GetSandboxOperations', lambda: api.get_sandbox_operations(account_id=account, from_=now-timedelta(days=1), to=now))
        probe('GetSandboxWithdrawLimits', lambda: api.get_sandbox_withdraw_limits(account_id=account))
        probe('GetSandboxMaxLots', lambda: api.get_sandbox_max_lots(request=GetMaxLotsRequest(account_id=account, instrument_id='BBG004730N88')))
        probe('TradingSchedules_MOEX', lambda: svc.instruments.trading_schedules(exchange='MOEX', from_=now, to=now+timedelta(days=2)))
        probe('SBER_metadata', lambda: svc.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id='BBG004730N88'))
        probe('SBER_trading_status', lambda: svc.market_data.get_trading_status(figi='BBG004730N88'))
    with Client(settings.feed_token) as svc:
        probe('SBER_production_metadata_read_only', lambda: svc.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id='BBG004730N88'))
    print(json.dumps({'file':str(output),'passed':sum(r['status']=='OK' for r in report['checks']),'total':len(report['checks'])}))


if __name__ == '__main__':
    main()
