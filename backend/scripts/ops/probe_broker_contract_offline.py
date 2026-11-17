import ast
import asyncio
import hashlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.bot.live_broker import LiveBroker, LivePosition


async def main():
    output = Path('reports/architect_20261115/broker_contract_offline_r1.json')
    if output.exists():
        raise FileExistsError(output)
    runtime_path = Path('app/bot/runtime.py')
    calls = [node for node in ast.walk(ast.parse(runtime_path.read_text(encoding='utf-8')))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'open_position']
    bindings = []
    for call in calls:
        try:
            inspect.signature(LiveBroker.open_position).bind(None, **{kw.arg: None for kw in call.keywords})
            result = 'compatible'
        except TypeError as exc:
            result = str(exc)
        bindings.append({'line': call.lineno, 'signature_binding': result})
    submitted = []
    stored = []
    broker = object.__new__(LiveBroker)
    broker._account = 'OFFLINE_FAKE_ACCOUNT'
    broker.config = SimpleNamespace(commission_rate=0.003)
    broker._log = lambda *args: None

    def post(**kwargs):
        submitted.append(kwargs)
        return SimpleNamespace(executed_order_price=SimpleNamespace(units=301, nano=0),
                               lots_requested=2, lots_executed=1, execution_report_status=5)

    broker._api_post_order = post
    entry = await broker.open_position('FAKE', 'FAKE', 'BUY', 2, 300, None, None, 'offline')
    position = LivePosition('FAKE', 'FAKE', 'LONG', 2, 300,
                            datetime.now(timezone.utc), None, None)

    async def get_position(figi):
        return position

    async def get_lot(figi):
        return 1

    class MemorySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def scalar(self, statement):
            return SimpleNamespace(id=1)

        def add(self, value):
            stored.append(value)

        async def commit(self):
            return None

    broker.sessions = MemorySession
    broker.get_position = get_position
    broker._get_lot = get_lot
    closed = await broker.close_position('FAKE', 301, 'offline')

    def uncertain(**kwargs):
        raise TimeoutError('OFFLINE simulated unknown outcome after send')

    broker._api_post_order = uncertain
    try:
        await broker.open_position('FAKE', 'FAKE', 'BUY', 2, 300, None, None, 'offline')
        timeout_result = 'returned'
    except Exception as exc:
        timeout_result = type(exc).__name__
    report = {
        'scope': 'offline actual adapter methods; fake SDK and in-memory session; no broker/DB calls',
        'as_of': datetime.now(timezone.utc).isoformat(),
        'hashes': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in
                   [runtime_path, Path('app/bot/live_broker.py')]},
        'runtime_open_bindings': bindings,
        'partial_open': {'requested_lots': 2, 'executed_lots': 1, 'returned_price': entry},
        'partial_close': {'requested_lots': 2, 'executed_lots': 1, 'recorded_qty': closed.qty,
                          'stored_trade_count': len(stored)},
        'unknown_outcome_exception': timeout_result,
        'limitations': 'Runtime consequences reviewed statically, not executed. SDK responses simulated.'
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    asyncio.run(main())
