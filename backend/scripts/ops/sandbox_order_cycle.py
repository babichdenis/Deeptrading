import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.request
import uuid

from app.bot.live_broker import LiveBroker
from app.config import get_settings
from app.database import SessionLocal
from t_tech.invest import OrderDirection, OrderType, InstrumentIdType


OUT = Path('data/sandbox_order_cycle_20261115.json')
FIGI = 'BBG004730N88'


def q(value):
    return value.units + value.nano / 1e9


def http(path):
    with urllib.request.urlopen('http://127.0.0.1:8000/api/v1/'+path, timeout=20) as r:
        return json.load(r)


def main():
    if OUT.exists():
        raise RuntimeError('Existing run receipt: inspect outcomes before another run')
    status = http('bot/status')
    assert status.get('running') is False and not status.get('starting')
    settings = get_settings()
    baseline = json.loads(Path('data/sandbox_baseline_20k_20261115.json').read_text())
    assert settings.get_account('sandbox') == baseline['new_account']
    broker = LiveBroker(SessionLocal, mode='sandbox')
    assert broker.mode == 'sandbox' and broker._target == 'sandbox-invest-public-api.tbank.ru'
    report = {'started_at': datetime.now(timezone.utc).isoformat(), 'account': broker._account, 'ticker': 'SBER', 'events': []}
    def save():
        OUT.write_text(json.dumps(report, default=str, indent=2)+'\n', encoding='utf-8')
    def event(kind, data):
        report['events'].append({'kind': kind, 'at': datetime.now(timezone.utc).isoformat(), 'data': data});save()
    try:
        svc = broker._get_services()
        api = svc.sandbox
        share = svc.instruments.share_by(id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_FIGI, id=FIGI).instrument
        uid = share.uid
        def holdings():
            pf = api.get_sandbox_portfolio(account_id=broker._account)
            rows = [p for p in pf.positions if p.instrument_type != 'currency' and q(p.quantity) != 0]
            return pf, rows
        pf, positions = holdings()
        assert not positions and not api.get_sandbox_orders(account_id=broker._account).orders
        book = svc.market_data.get_order_book(figi=FIGI, depth=1)
        ask = q(book.asks[0].price) if book.asks else q(book.last_price)
        assert ask > 0 and ask * share.lot <= 5000
        report['lot_size'] = share.lot
        report['before_cash'] = q(pf.total_amount_currencies)
        event('trading_status', asdict(svc.market_data.get_trading_status(figi=FIGI)))
        def submit(side, label):
            request_id = str(uuid.uuid4())
            event(label+'_intent', {'request_id': request_id, 'lots': 1})
            try:
                result = broker._api_post_order(figi=FIGI, quantity=1, direction=side,
                                               account_id=broker._account, order_type=OrderType.ORDER_TYPE_MARKET, order_id=request_id)
            except Exception as exc:
                event(label+'_request_error', {'type': type(exc).__name__, 'message': str(exc)[:400]})
                raise
            event(label+'_response', asdict(result))
            for attempt in range(8):
                state = api.get_sandbox_order_state(account_id=broker._account, order_id=result.order_id)
                if state.lots_executed == 1:
                    event(label+'_filled', asdict(state));return state
                if 'REJECTED' in str(state.execution_report_status) or 'CANCELLED' in str(state.execution_report_status):
                    event(label+'_terminal_unfilled', asdict(state));return None
                time.sleep(1)
            event(label+'_cancel_intent', {'order_id': result.order_id})
            event(label+'_cancel_response', asdict(api.cancel_sandbox_order(account_id=broker._account, order_id=result.order_id)))
            state = api.get_sandbox_order_state(account_id=broker._account, order_id=result.order_id)
            event(label+'_after_cancel', asdict(state))
            return state if state.lots_executed == 1 else None
        bought = submit(OrderDirection.ORDER_DIRECTION_BUY, 'buy')
        event('http_after_buy', {'positions': http('sandbox/positions'), 'orders': http('sandbox/orders')})
        pf, positions = holdings()
        held = sum(q(p.quantity) for p in positions if p.figi == FIGI or getattr(p, 'instrument_uid', '') == uid)
        if held:
            assert held == share.lot, 'Unexpected quantity; do not issue blind close'
            submit(OrderDirection.ORDER_DIRECTION_SELL, 'sell')
        pf, positions = holdings()
        orders = api.get_sandbox_orders(account_id=broker._account).orders
        report['after_cash'] = q(pf.total_amount_currencies)
        report['cash_delta'] = report['after_cash'] - report['before_cash']
        report['remaining_positions'] = len(positions)
        report['remaining_orders'] = len(orders)
        report['status'] = 'round_trip_completed' if bought and not positions and not orders else 'no_round_trip'
        event('http_after_cycle', {'status': http('sandbox/status'), 'positions': http('sandbox/positions'), 'orders': http('sandbox/orders')})
    except Exception as exc:
        report['status'] = 'interrupted';report['error_type'] = type(exc).__name__;save()
        raise
    finally:
        broker.close();save()
        print(json.dumps({k:v for k,v in report.items() if k not in ('account','events')}))


if __name__ == '__main__':
    main()
