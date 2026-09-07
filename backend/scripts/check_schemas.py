"""Find old backtest engine results — the one with commissions/holds used in MTF_V4_2026."""
import asyncio
from sqlalchemy import text
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        # Check schemas
        for tbl in ["experiments", "ensemble_runs", "strategy_runs", "experiment_trades",
                     "paper_trades", "paper_accounts", "paper_positions",
                     "test_runs", "configurations"]:
            try:
                cols = (await db.execute(text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = '%s' ORDER BY ordinal_position" % tbl
                ))).fetchall()
                print("=== %s (%d cols) ===" % (tbl, len(cols)))
                for c in cols:
                    print("  %s %s" % (c[0], c[1]))
                print()
            except Exception as e:
                print("=== %s: ERROR %s ===" % (tbl, e))
                print()

asyncio.run(main())
