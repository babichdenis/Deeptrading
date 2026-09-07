"""Detailed analysis of experiment_trades (old backtest engine, MTF_V4_2026 results)."""
import asyncio
from sqlalchemy import text
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        # Get columns first
        cols = (await db.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = 'experiment_trades' ORDER BY ordinal_position"
        ))).fetchall()
        print("COLUMNS:")
        for c in cols:
            print("  %s (%s)" % (c[0], c[1]))
        print()

        # Sample rows
        rows = (await db.execute(text(
            "SELECT * FROM experiment_trades LIMIT 3"
        ))).fetchall()
        print("SAMPLE (3 rows):")
        for r in rows:
            print("  ", list(r))
        print()

        # Total
        total = (await db.execute(text("SELECT count(*) FROM experiment_trades"))).fetchone()[0]
        print("Total rows: %d" % total)

        # Check experiment_runs
        try:
            runs = (await db.execute(text(
                "SELECT id, name, params, created_at FROM experiment_runs ORDER BY created_at DESC LIMIT 5"
            ))).fetchall()
            print("\nRecent experiment_runs:")
            for r in runs:
                print("  id=%s name=%s created=%s" % (r[0], r[1], r[3]))
        except Exception as e:
            print("No experiment_runs: %s" % e)

asyncio.run(main())
