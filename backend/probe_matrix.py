import asyncio
import os
import sys
import json
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sqlalchemy import text
from app.database import SessionLocal


async def main():
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT ticker, side, exit_reason, net_pnl, meta "
            "FROM sandbox_trades WHERE test_name='week1' AND exit_time IS NOT NULL"))).fetchall()
    reg_strat = defaultdict(lambda: defaultdict(lambda: [0, 0.0, 0]))  # reg -> sid -> [n, net, wins]
    reg_net = defaultdict(lambda: [0, 0.0])
    tick_net = defaultdict(lambda: [0, 0.0])
    for r in rows:
        try:
            m = json.loads(r[4]) if r[4] else {}
        except Exception:
            m = {}
        reg = m.get("regime") or "—"
        qe = m.get("quorum_event") or {}
        members = qe.get("members_for") or ["—"]
        net = float(r[3] or 0)
        reg_net[reg][0] += 1
        reg_net[reg][1] += net
        tick_net[r[0]][0] += 1
        tick_net[r[0]][1] += net
        for sid in members:
            reg_strat[reg][sid][0] += 1
            reg_strat[reg][sid][1] += net
            if net > 0:
                reg_strat[reg][sid][2] += 1
    sids = sorted({s for v in reg_strat.values() for s in v})
    print("=== МАТРИЦА режим × стратегия (net, [n]) ===")
    print("режим".ljust(16) + "".join(s[:12].ljust(14) for s in sids))
    for reg in sorted(reg_net, key=lambda x: -reg_net[x][1]):
        line = f"{reg} (n={reg_net[reg][0]}, {reg_net[reg][1]:+.0f})".ljust(16)
        for s in sids:
            v = reg_strat[reg].get(s)
            line += (f"{v[1]:+.0f}[{v[0]}]" if v else "—").ljust(14)
        print(line)
    print("\n=== По акциям (net) ===")
    for tk, v in sorted(tick_net.items(), key=lambda x: x[1][1]):
        print(f"  {tk:8} n={v[0]:3} net={v[1]:+9.2f}")


asyncio.run(main())
