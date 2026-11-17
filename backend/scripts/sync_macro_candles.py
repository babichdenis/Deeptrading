import argparse
import asyncio
import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal, engine
from app.services.macro_candle_ingestion import sync_macro_cycle
from app.services.macro_candle_source import CNYRUB, INDEX_SERIES, futures_contract, listed_futures_for_collection


async def run(history_start, contracts, *, as_of=None, assets=()):
    instruments = (*INDEX_SERIES, CNYRUB, *(futures_contract(code) for code in contracts))
    if as_of is not None and assets:
        raise ValueError("Current futures catalog cannot be used for historical as-of")
    now = datetime.now(timezone.utc)
    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None or as_of > now):
        raise ValueError("As-of must be timezone-aware and no later than the current time")
    as_of = (as_of or now).astimezone(timezone.utc).replace(second=0, microsecond=0)
    if history_start.tzinfo is None or history_start.utcoffset() is None or history_start >= as_of:
        raise ValueError("History start must be timezone-aware and before the last closed minute")
    catalog_errors = {}
    if assets:
        try:
            selected = await asyncio.to_thread(listed_futures_for_collection, assets, as_of=as_of)
        except Exception as error:
            catalog_errors = {asset: type(error).__name__ for asset in assets}
        else:
            for asset, candidates in selected.items():
                instruments += candidates
                if not candidates:
                    catalog_errors[asset] = "NoListedContracts"
    report = {"as_of": as_of.isoformat(), "series": {}, "catalog_errors": catalog_errors}
    db_engine = engine
    try:
        async with db_engine.begin() as lock_connection:
            locked = await lock_connection.scalar(text(
                "SELECT pg_try_advisory_xact_lock(hashtext('deeptrading:macro_candles')::bigint)"
            ))
            if locked:
                report["series"] = await sync_macro_cycle(
                    SessionLocal, instruments, history_start=history_start, as_of=as_of
                )
            else:
                report["status"] = "collector_busy"
    except (SQLAlchemyError, OSError) as error:
        report["status"] = "database_error"
        report["error_type"] = type(error).__name__
    finally:
        await db_engine.dispose()
    print(json.dumps(report), flush=True)
    return int(
        "status" in report or bool(catalog_errors)
        or any(result["status"] in {"error", "partial"} for result in report["series"].values())
    )


async def run_periodically(history_start, contracts, period_seconds, *, assets=()):
    loop = asyncio.get_running_loop()
    while True:
        started = loop.time()
        await run(history_start, contracts, assets=assets)
        await asyncio.sleep(max(0, period_seconds - (loop.time() - started)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--history-start", required=True, type=datetime.fromisoformat)
    parser.add_argument("--futures-contract", action="append", default=[], metavar="SECID")
    parser.add_argument("--futures-asset", action="append", default=[], choices=("BR", "NG", "Si"))
    parser.add_argument("--as-of", type=datetime.fromisoformat)
    parser.add_argument("--period-seconds", type=int)
    args = parser.parse_args()
    if args.as_of is not None and args.futures_asset:
        parser.error("--futures-asset cannot be combined with historical --as-of")
    if args.period_seconds is not None:
        if args.period_seconds < 60 or args.as_of is not None:
            parser.error("--period-seconds must be at least 60 and cannot be combined with --as-of")
        return asyncio.run(run_periodically(args.history_start, args.futures_contract, args.period_seconds, assets=args.futures_asset))
    return asyncio.run(run(args.history_start, args.futures_contract, as_of=args.as_of, assets=args.futures_asset))


if __name__ == "__main__":
    raise SystemExit(main())
