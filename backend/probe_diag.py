import asyncio
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import text
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT ticker, side, entry_price, stop_loss, exit_price, exit_reason, net_pnl, "
            "meta, exit_meta FROM sandbox_trades WHERE test_name='jul15' "
            "AND exit_reason='stop_loss' ORDER BY entry_time LIMIT 12"))).fetchall()
        print("ticker side entry sl_db exit net | sl_initial trail")
        for r in rows:
            try:
                m = json.loads(r[7]) if r[7] else {}
            except Exception:
                m = {}
            try:
                em = json.loads(r[8]) if r[8] else {}
            except Exception:
                em = {}
            print(f"  {r[0]} {r[1]} entry={r[2]} sl_db={r[3]} exit={r[4]} net={r[6]} "
                  f"| sl_init={m.get('sl_initial')} entry0={m.get('entry_price0')} trail={em.get('trail_active')}")
        r = (await db.execute(text(
            "SELECT count(*), sum(net_pnl), sum(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) "
            "FROM sandbox_trades WHERE test_name='jul15'"))).first()
        print("\ntotal trades, net, wins =", r)


asyncio.run(main())
