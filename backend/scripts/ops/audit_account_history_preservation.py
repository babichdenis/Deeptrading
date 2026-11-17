import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text

from app.database import engine


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), default=str).encode()).hexdigest()


def compare_history(before, after):
    previous = before['rows']
    current = after['rows']
    missing = sorted(set(previous) - set(current))
    changed = sorted(key for key in previous.keys() & current.keys()
                     if previous[key]['content_hash'] != current[key]['content_hash'])
    reassigned = sorted(key for key in previous.keys() & current.keys()
                        if previous[key]['identity_hash'] != current[key]['identity_hash'])
    return {'preserved': not (missing or changed or reassigned), 'missing_ids': missing,
            'changed_ids': changed, 'reassigned_ids': reassigned,
            'added_ids': sorted(set(current) - set(previous))}


async def main(args):
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(output)
    baseline = json.loads(Path(args.baseline).read_text()) if args.baseline else None
    report = {'as_of': datetime.now(timezone.utc).isoformat(),
              'scope': 'sandbox/live trade history, read-only repeatable-read transaction', 'rows': {}}
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                report['identity_column_present'] = bool(await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'sandbox_trades' "
                    "AND column_name = 'broker_account_id')")))
                result = await connection.stream(text(
                    "SELECT id, to_jsonb(t) - 'broker_account_id' AS payload, "
                    "to_jsonb(t)->>'broker_account_id' AS identity FROM sandbox_trades t "
                    "WHERE mode IN ('sandbox', 'live') ORDER BY id"))
                async for row in result:
                    report['rows'][str(row.id)] = {
                        'content_hash': digest(row.payload),
                        'identity_hash': digest(row.identity) if row.identity is not None else None,
                    }
        report['row_count'] = len(report['rows'])
        report['history_hash'] = digest(report['rows'])
        if baseline is not None:
            report['comparison'] = compare_history(baseline, report)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(report, indent=2) + '\n')
        print(json.dumps({key: value for key, value in report.items() if key != 'rows'}))
        return 0 if baseline is None or report['comparison']['preserved'] else 1
    finally:
        await engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--baseline')
    raise SystemExit(asyncio.run(main(parser.parse_args())))
