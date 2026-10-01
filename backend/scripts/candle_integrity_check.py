#!/usr/bin/env python3
"""Watchdog целостности свечей: baseline / check / repair / report.

См. DECISIONS.md 2026-10-01 20:30 и app/services/candle_integrity.py.
Запуск (на .7, из backend/):
  .venv/bin/python scripts/candle_integrity_check.py baseline --eligible --from 2026-08-20 --to 2026-09-05
  .venv/bin/python scripts/candle_integrity_check.py check    --eligible --from 2026-09-01 --to 2026-09-05
  .venv/bin/python scripts/candle_integrity_check.py repair   --eligible --from 2026-09-01 --to 2026-09-05
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services.candle_integrity import baseline, check, repair  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("cmd", choices=("baseline", "check", "repair", "report"))
    p.add_argument("--from", dest="dfrom", required=True)
    p.add_argument("--to", dest="dto", required=True)
    p.add_argument("--interval", type=int, default=1)
    p.add_argument("--eligible", action="store_true", help="фиги универса (tier=eligible)")
    p.add_argument("--tickers", default="")
    return p.parse_args()


async def _resolve_figis(db, args) -> list[str]:
    """Тикеры → фиги, у которых есть 1m-данные в окне (иначе алиас без свечей пропускаем)."""
    if args.tickers:
        tks = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        rows = (await db.execute(text("SELECT DISTINCT figi FROM instruments WHERE upper(ticker) = ANY(:t)"),
                                 {"t": tks})).scalars().all()
    elif args.eligible:
        rows = (await db.execute(text(
            "SELECT i.figi FROM universe u JOIN instruments i ON upper(i.ticker)=upper(u.ticker) "
            "WHERE u.eligible_tier = 'eligible'"))).scalars().all()
    else:
        rows = (await db.execute(text("SELECT DISTINCT figi FROM candles WHERE interval=:i LIMIT 200"),
                                 {"i": args.interval})).scalars().all()
    if not rows:
        return []
    a = datetime.fromisoformat(args.dfrom).replace(tzinfo=timezone.utc)
    b = datetime.fromisoformat(args.dto).replace(tzinfo=timezone.utc) + timedelta(days=1)
    have = set((await db.execute(text(
        "SELECT DISTINCT figi FROM candles WHERE interval=:i AND ts >= :a AND ts < :b AND figi = ANY(:figis)"
    ), {"i": args.interval, "a": a, "b": b, "figis": list(rows)})).scalars().all())
    return sorted(have)


async def main() -> int:
    args = parse_args()
    dfrom = datetime.fromisoformat(args.dfrom).replace(tzinfo=timezone.utc)
    dto = datetime.fromisoformat(args.dto).replace(tzinfo=timezone.utc) + timedelta(days=1)
    async with SessionLocal() as db:
        figis = await _resolve_figis(db, args)
        print(f"{args.cmd}: интервал {args.interval} · {len(figis)} фиг · {dfrom.date()}..{args.dto}")
        if not figis:
            print("фиги не найдены")
            return 1
        if args.cmd == "baseline":
            n = await baseline(db, figis, args.interval, dfrom, dto)
            print(f"baseline записан: {n} (figi,day)")
        elif args.cmd in ("check", "report"):
            rep = await check(db, figis, args.interval, dfrom, dto)
            cnt = Counter(r["status"] for r in rep)
            print("статусы:", dict(cnt) or "(все OK/NO_BASELINE)")
            for r in rep[:40]:
                print(f"  {r['status']:<18} {r['figi'][-6:]} {r['day']} rows={r['rows']} missing={r['missing_active']}")
        elif args.cmd == "repair":
            rep = await check(db, figis, args.interval, dfrom, dto)
            bad = [r for r in rep if r["status"] not in ("OK", "NO_BASELINE")]
            print(f"проблемных (figi,day): {len(bad)}; докачиваю {len({r['figi'] for r in bad})} фиг")
            n = await repair(db, figis, args.interval, dfrom, dto, report=rep)
            print(f"докачано строк: {n}")
            rep2 = await check(db, figis, args.interval, dfrom, dto)
            print("после докачки:", dict(Counter(r["status"] for r in rep2)) or "(чисто)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
