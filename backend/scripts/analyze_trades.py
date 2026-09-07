"""
Per-dimension trade analysis: breakdown by session, side, exit_reason, bars_held, figi.
Reads sandbox_trades + exit_meta from DB.
"""
import json
import asyncio
from datetime import datetime
from collections import defaultdict
from sqlalchemy import text
from app.database import SessionLocal


async def analyze():
    stats = {
        "total": 0, "wins": 0, "losses": 0,
        "by_session": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "by_side": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "by_exit_reason": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "by_bars_held_bucket": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
        "by_figi": defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0}),
    }
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT figi, ticker, side, entry_price, exit_price, net_pnl, exit_reason, exit_meta, "
            "entry_time FROM sandbox_trades WHERE net_pnl IS NOT NULL"
        ))).fetchall()

    for r in rows:
        figi = r[0]
        ticker = r[1]
        side = r[2]
        net_pnl = r[5]
        exit_reason = r[6]
        exit_meta_raw = r[7]
        entry_time = r[8]

        pnl = float(net_pnl) if net_pnl else 0
        meta = json.loads(exit_meta_raw) if exit_meta_raw else {}

        stats["total"] += 1
        if pnl > 0:
            stats["wins"] += 1
        else:
            stats["losses"] += 1

        session = "unknown"
        if entry_time:
            from zoneinfo import ZoneInfo
            ms = entry_time.astimezone(ZoneInfo("Europe/Moscow"))
            h = ms.hour
            if 6 <= h < 10:
                session = "morning"
            elif 10 <= h < 19:
                session = "day"
            elif 19 <= h < 24:
                session = "evening"
            else:
                session = "night"
        d = stats["by_session"][session]
        d["count"] += 1
        d["wins"] += (1 if pnl > 0 else 0)
        d["pnl"] += pnl

        d = stats["by_side"][side]
        d["count"] += 1
        d["wins"] += (1 if pnl > 0 else 0)
        d["pnl"] += pnl

        reason = exit_reason or "unknown"
        d = stats["by_exit_reason"][reason]
        d["count"] += 1
        d["wins"] += (1 if pnl > 0 else 0)
        d["pnl"] += pnl

        bh = meta.get("bars_held", 0)
        if bh <= 5:
            bucket = "0-5"
        elif bh <= 15:
            bucket = "6-15"
        elif bh <= 30:
            bucket = "16-30"
        elif bh <= 60:
            bucket = "31-60"
        else:
            bucket = "60+"
        d = stats["by_bars_held_bucket"][bucket]
        d["count"] += 1
        d["wins"] += (1 if pnl > 0 else 0)
        d["pnl"] += pnl

        d = stats["by_figi"][figi]
        d["count"] += 1
        d["wins"] += (1 if pnl > 0 else 0)
        d["pnl"] += pnl

    def wr(w, t):
        return f"{w/t*100:.1f}%" if t > 0 else "N/A"

    total = stats["total"]
    w_count = stats["wins"]
    l_count = stats["losses"]
    print(f"=== TRADE ANALYSIS ({total} trades) ===")
    print(f"Wins: {w_count} ({wr(w_count, total)}) | Losses: {l_count}")
    print()

    for dim in ["by_session", "by_side", "by_exit_reason", "by_bars_held_bucket", "by_figi"]:
        print(f"--- {dim} ---")
        for k, v in sorted(stats[dim].items(), key=lambda x: -x[1]["pnl"]):
            w = v["wins"]
            c = v["count"]
            pnl = v["pnl"]
            print(f"  {k:20s}  trades={c:5d}  WR={wr(w, c):6s}  PnL={pnl:+.0f}")
        print()

    print("=== DONE ===")

asyncio.run(analyze())
