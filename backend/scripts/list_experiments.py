"""Detailed analysis of experiment_trades — old engine with commissions/holds (MTF_V4_2026)."""
import asyncio, json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import text
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        # Get experiments with trade counts
        exps = (await db.execute(text(
            "SELECT e.id, e.figi, e.from_ts, e.to_ts, e.bars, e.summary, "
            "(SELECT count(*) FROM experiment_trades et WHERE et.experiment_id = e.id) as trade_count "
            "FROM experiments e ORDER BY e.created_at DESC"
        ))).fetchall()

        print("=== ALL EXPERIMENTS ===")
        for e in exps:
            exp_id = str(e[0])[:8]
            figi = e[1]
            bars = e[4]
            tc = e[6]
            from_ts = str(e[2])[:10] if e[2] else "?"
            to_ts = str(e[3])[:10] if e[3] else "?"
            summary = e[5]
            net = ""
            if summary and isinstance(summary, dict):
                net = " net=%s" % summary.get("net_pnl", summary.get("net", ""))
            elif summary and isinstance(summary, str):
                try:
                    s = json.loads(summary)
                    net = " net=%s" % s.get("net_pnl", s.get("net", ""))
                except:
                    pass
            print("  %s  figi=%-20s  bars=%6d  trades=%3d  %s..%s  %s" % (
                exp_id, figi, bars or 0, tc, from_ts, to_ts, net))

asyncio.run(main())
