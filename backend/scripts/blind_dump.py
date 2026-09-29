#!/usr/bin/env python3
"""blind_dump.py — Stage 16/A step 1: BLIND data dump from .4 DB (read-only).

Universe = the same 26 figis as every stage sim (keys of data/q2optuna_data.pkl).
Window (UTC): 2026-08-24T21:00 .. 2026-09-12T12:00
  = MSK 25.08 00:00 .. 12.09 15:00
  (warmup 25-31.08 + blind entries window 01-11.09 MSK).
The live bot (Q2rsi2 replay) is NOT touched: plain SELECTs only.
Output: data/blind_data.pkl — {figi: {"ticker": str, "candles":
[(ts, open, high, low, close, volume), ...]}}, interval=1 bars.

Run on .4:  cd ~/Dev/Deeptrading/backend && .venv/bin/python3 /tmp/blind_dump.py
"""
from __future__ import annotations

import asyncio
import os
import pickle
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402

MSK = timezone(timedelta(hours=3))
A = datetime(2026, 8, 24, 21, 0, tzinfo=timezone.utc)
B = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
BLIND_FIRST = datetime(2026, 9, 1, tzinfo=MSK).date()
BLIND_LAST = datetime(2026, 9, 11, tzinfo=MSK).date()


async def main() -> None:
    src = os.path.join("data", "q2optuna_data.pkl")
    with open(src, "rb") as fh:
        uni = sorted(pickle.load(fh).keys())
    print("universe from q2optuna_data.pkl: %d figis" % len(uni))
    print("window UTC: %s .. %s" % (A.isoformat(), B.isoformat()))
    print("        MSK: %s .. %s" % (
        A.astimezone(MSK).isoformat(), B.astimezone(MSK).isoformat()))

    out = {}
    per_day = defaultdict(lambda: defaultdict(int))  # msk date -> figi -> bars
    async with SessionLocal() as db:
        for f in uni:
            row = (await db.execute(
                text("SELECT ticker FROM instruments WHERE figi=:f"),
                {"f": f})).first()
            tk = str(row[0]) if row else f
            rows = (await db.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": f, "a": A, "b": B})).all()
            print("  %-8s %-24s bars=%d" % (tk, f, len(rows)))
            if len(rows) < 60:
                continue
            out[f] = {"ticker": tk, "candles": [
                (r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]),
                 int(r[5] or 0)) for r in rows]}
            for r in rows:
                per_day[r[0].astimezone(MSK).date()][f] += 1

    print()
    print("=== coverage by MSK date (figis / bars) ===")
    for d in sorted(per_day):
        mark = "  <- BLIND" if BLIND_FIRST <= d <= BLIND_LAST else ""
        print("  %s  figis=%2d  bars=%6d%s" % (
            d.isoformat(), len(per_day[d]), sum(per_day[d].values()), mark))

    blind_days = [d for d in sorted(per_day) if BLIND_FIRST <= d <= BLIND_LAST]
    print()
    print("blind days present: %d (%s .. %s)" % (
        len(blind_days),
        blind_days[0].isoformat() if blind_days else "-",
        blind_days[-1].isoformat() if blind_days else "-"))

    dst = os.path.join("data", "blind_data.pkl")
    with open(dst, "wb") as fh:
        pickle.dump(out, fh, protocol=4)
    print("saved: %s (%d figis, %.1f MB)" % (
        dst, len(out), os.path.getsize(dst) / 1e6))


if __name__ == "__main__":
    asyncio.run(main())
