import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from google.protobuf.json_format import MessageToDict
from t_tech.invest import Client
from t_tech.invest.grpc import users_pb2

from app.config import get_settings


def main():
    output = Path('reports/architect_20261115/live_accounts_all_statuses_r1.json')
    if output.exists():
        raise FileExistsError(output)
    settings = get_settings()
    with Client(settings.get_token('live')) as services:
        response = services.users.stub.GetAccounts(
            users_pb2.GetAccountsRequest(status=users_pb2.ACCOUNT_STATUS_ALL),
            metadata=services.users.metadata,
            timeout=25,
        )
        payload = MessageToDict(response)
    accounts = []
    for row in payload.get('accounts', []):
        accounts.append({
            'fingerprint': hashlib.sha256(row['id'].encode()).hexdigest(),
            'selected': row['id'] == settings.get_account('live'),
            **{key: row.get(key) for key in ('name', 'status', 'type', 'accessLevel', 'openedDate')},
        })
    report = {'as_of': datetime.now(timezone.utc).isoformat(), 'requested_status': 'ACCOUNT_STATUS_ALL',
              'accounts': accounts, 'scope': 'read-only production gRPC GetAccounts ALL via installed protobuf; no mutation'}
    with output.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
