import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from t_tech.invest import Client
from t_tech.invest.grpc import operations_pb2

from app.config import get_settings


def main(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    start = datetime.fromisoformat(args.since)
    if start.utcoffset() is None:
        raise ValueError('Timezone required')
    end = datetime.now(timezone.utc)
    settings = get_settings()
    account = settings.get_account('live')
    if not account:
        raise RuntimeError('No configured live account')
    items = []
    cursor = ''
    seen_cursors = set()
    complete = False
    with Client(settings.get_token('live')) as services:
        for page in range(10):
            request = operations_pb2.GetOperationsByCursorRequest(
                account_id=account, limit=100, cursor=cursor,
                without_commissions=False, without_trades=False, without_overnights=False)
            getattr(request, 'from').FromDatetime(start)
            request.to.FromDatetime(end)
            response = services.operations.stub.GetOperationsByCursor(
                request, metadata=services.operations.metadata, timeout=25)
            data = MessageToDict(response)
            for item in data.get('items', []):
                items.append({'id_hash': hashlib.sha256(item.get('id', '').encode()).hexdigest(),
                              **{key: item.get(key) for key in ('date', 'type', 'state', 'payment')}})
            if not response.has_next:
                complete = True
                break
            cursor = response.next_cursor
            if not cursor or cursor in seen_cursors:
                raise RuntimeError('Invalid operation cursor progress')
            seen_cursors.add(cursor)
    report = {'since': start.isoformat(), 'as_of': end.isoformat(),
              'scope': 'read-only production operations; all types/states, fees/trades/overnights included',
              'pages': page + 1, 'pagination_complete': complete, 'items': items,
              'limitation': 'Server pagination complete does not prove reporting latency is zero.'}
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--since', required=True)
    parser.add_argument('--output', required=True)
    main(parser.parse_args())
