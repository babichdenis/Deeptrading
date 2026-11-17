import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    receipt = json.loads(Path('data/sandbox_baseline_20k_20261115.json').read_text(encoding='utf-8'))
    from app.config import get_settings
    settings = get_settings()
    checks = {'configured_account_matches_receipt': settings.get_account('sandbox') == receipt['new_account'],
              'configured_mode_sandbox': settings.bot_mode == 'sandbox'}
    responses = {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, path in {'status': '/api/v1/sandbox/status', 'positions': '/api/v1/sandbox/positions',
                       'orders': '/api/v1/sandbox/orders', 'trades': '/api/v1/sandbox/trades',
                       'bot': '/api/v1/bot/status'}.items():
        try:
            with opener.open(args.base_url.rstrip('/')+path, timeout=20) as response:
                data = json.load(response)
            checks[name+'_response_ok'] = isinstance(data, dict) and not data.get('error')
            responses[name] = data
        except Exception as exc:
            checks[name+'_response_ok'] = False
            responses[name] = {'probe_error_type': type(exc).__name__}
    status = responses['status']
    portfolio = status.get('portfolio', {})
    expected = receipt['target_cash']
    checks.update({'api_mode_sandbox': status.get('mode') == 'SANDBOX',
                   'bot_stopped': responses['bot'].get('running') is False and not responses['bot'].get('starting'),
                   'cash_matches_baseline': portfolio.get('cash') == expected,
                   'equity_matches_baseline': portfolio.get('equity') == expected,
                   'initial_cash_matches_baseline': portfolio.get('initial_cash') == expected,
                   'pnl_zero': portfolio.get('pnl') == 0,
                   'position_stat_zero': portfolio.get('positions_open') == 0,
                   'trade_stat_zero': portfolio.get('trades', {}).get('total') == 0})
    for name in ('positions', 'orders', 'trades'):
        checks[name+'_empty'] = responses[name].get('count') == 0 and responses[name].get(name) == []
    result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'passed': all(checks.values()),
              'checks': checks, 'failed_checks': [key for key, value in checks.items() if not value],
              'observed': {'cash': portfolio.get('cash'), 'equity': portfolio.get('equity'), 'pnl': portfolio.get('pnl'),
                           'trade_statistics_count': portfolio.get('trades', {}).get('total'),
                           **{name+'_count': responses[name].get('count') for name in ('positions','orders','trades')}},
              'scope': 'Read-only pre-trading baseline acceptance; no orders, account changes or DB writes. Run only before trading begins.'}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
