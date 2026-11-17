import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.database import SessionLocal
from app.services.candle_integrity import check, interval_overlap_audit


async def audit(manifest, start, end, *, context_start=None, context_end=None):
    instruments = json.loads(manifest.read_text(encoding="utf-8"))["reference_instruments"]
    figis = {item["figi"]: item["ticker"] for item in instruments if item["ticker"] != "__MOEX__"}
    if (context_start is None) != (context_end is None):
        raise ValueError("Provide both context boundaries")
    if context_start is not None and (
        start.utcoffset() is None or end.utcoffset() is None
        or context_start.utcoffset() is None or context_end.utcoffset() is None
        or context_start > start or context_end < end
    ):
        raise ValueError("Timezone-aware context must cover the audited interval")
    async with SessionLocal() as db:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await db.execute(text("SET LOCAL statement_timeout = '30s'"))
        counts = await interval_overlap_audit(db, list(figis), start, end)

        coverage = (
            await check(db, list(figis), 1, context_start, context_end, auto_baseline=False)
            if context_start is not None else None
        )

    return {
        "start": start.isoformat(),
        "end_exclusive": end.isoformat(),
        "coverage_context_start": context_start.isoformat() if context_start is not None else None,
        "coverage_context_end": context_end.isoformat() if context_end is not None else None,
        "coverage_issues": coverage,
        "series": [
            dict(figi=figi, ticker=figis[figi], **values)
            for figi, values in counts.items()
        ],
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--end", required=True, type=datetime.fromisoformat)
    parser.add_argument("--context-start", type=datetime.fromisoformat)
    parser.add_argument("--context-end", type=datetime.fromisoformat)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(audit(args.manifest, args.start, args.end, context_start=args.context_start, context_end=args.context_end)), indent=2))


if __name__ == "__main__":
    main()
