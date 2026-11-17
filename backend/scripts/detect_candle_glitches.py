"""Поиск «мерцающих» блоков свечей: ряд уходит на чужой уровень и возвращается.

Отличие от сплита: при сплите цена переходит на новый уровень и там остаётся,
а здесь — блок баров с посторонней ценой, после которого ряд возвращается
к прежнему уровню. Именно такие блоки портят день целиком (валидатор
candle_guard выбрасывает день при >=3 прыжках >50%) и ломают расчёты.

По умолчанию только читает и печатает (--dry-run). Запись — с --apply.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--interval", type=int, default=1)
    ap.add_argument("--jump", type=float, default=0.40, help="порог прыжка, доля")
    ap.add_argument("--max-block-days", type=float, default=45.0)
    ap.add_argument("--limit-figi", type=int, default=0)
    args = ap.parse_args()

    from app.database import SessionLocal  # noqa: WPS433
    from app.models.candle import Candle  # noqa: WPS433

    jump = args.jump
    found: list[dict] = []
    async with SessionLocal() as db:
        figis = [r[0] for r in (await db.execute(
            text("SELECT DISTINCT figi FROM candles WHERE interval=:i ORDER BY 1"),
            {"i": args.interval})).all()]
        if args.limit_figi:
            figis = figis[:args.limit_figi]
        print(f"figis={len(figis)} interval={args.interval} jump>{jump:.0%}", flush=True)

        for n, figi in enumerate(figis, 1):
            rows = (await db.execute(
                select(Candle.id, Candle.ts, Candle.close).where(
                    Candle.figi == figi, Candle.interval == args.interval
                ).order_by(Candle.ts))).all()
            if len(rows) < 10:
                continue
            base_i = None      # индекс последнего «нормального» бара
            in_block = False
            block: list[int] = []
            for i in range(1, len(rows)):
                prev_close = float(rows[i - 1][2])
                cur_close = float(rows[i][2])
                if prev_close <= 0 or cur_close <= 0:
                    continue
                ratio = abs(cur_close / prev_close - 1.0)
                if not in_block:
                    if ratio > jump:
                        in_block = True
                        block = [i]
                        base_i = i - 1
                        base_level = prev_close
                else:
                    block.append(i)
                    back = abs(cur_close / base_level - 1.0) <= jump
                    too_long = (rows[i][1] - rows[base_i][1]) > timedelta(days=args.max_block_days)
                    if back:
                        found.append({
                            "figi": figi, "ts_from": rows[block[0]][1],
                            "ts_to": rows[i][1], "n": len(block),
                            "base": base_level,
                            "block_med": sorted(float(rows[k][2]) for k in block)[len(block) // 2],
                        })
                        in_block = False
                        block = []
                    elif too_long:
                        in_block = False
                        block = []
            if n % 20 == 0:
                print(f"  scanned {n}/{len(figis)} figis, blocks={len(found)}", flush=True)

    print(f"\nBLOCKS FOUND: {len(found)}")
    tot = sum(b["n"] for b in found)
    print(f"BARS IN BLOCKS: {tot}")
    for b in sorted(found, key=lambda x: -x["n"])[:40]:
        print(f"  {b['figi']} {b['ts_from']:%Y-%m-%d %H:%M}..{b['ts_to']:%H:%M} "
              f"bars={b['n']} base={b['base']:.3f} block={b['block_med']:.3f}")

    if args.apply and found:
        async with SessionLocal() as db:
            moved = 0
            for b in found:
                res = await db.execute(text("""
                    WITH b AS (
                      SELECT id, figi, interval, ts, open, high, low, close, volume
                      FROM candles
                      WHERE figi = :f AND interval = :i AND ts >= :a AND ts < :c
                    ), ins AS (
                      INSERT INTO candles_quarantine
                        (orig_id, figi, interval, ts, open, high, low, close, volume, reason)
                      SELECT id, figi, interval, ts, open, high, low, close, volume, 'glitch_block'
                      FROM b
                      RETURNING orig_id
                    )
                    DELETE FROM candles WHERE id IN (SELECT orig_id FROM ins)
                """), {"f": b["figi"], "i": args.interval, "a": b["ts_from"],
                       "c": b["ts_to"] + timedelta(seconds=1)})
                moved += res.rowcount or 0
            await db.commit()
            print(f"\nMOVED TO QUARANTINE: {moved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
