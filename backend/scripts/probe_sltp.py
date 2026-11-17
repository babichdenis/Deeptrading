"""Воспроизведение стопов ASTR/AFLT/SBER/MTSS: ATR на 5-минутках → plan_entry.

Инвест-контур живой (test:tg_atr4, paper) отдаёт stop_loss на 5000% от цены:
ASTR при 213.17 → 10873.71, AFLT при 31.89 → 1626.90, SBER при 276.64 → -13552.42.
Дистанция во всех случаях ≈50× цены, то есть масштабируется вместе с ценой, что
не похоже на ATR в рублях.

Скрипт строит 5-минутные бары тем же resample, что и рантайм, считает ATR и
печатает, при каких параметрах получается наблюдаемое значение.

Запуск: .venv\\Scripts\\python.exe scripts\\probe_sltp.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.engine.exits import AtrStopPolicy  # noqa: E402
from app.engine.models import Candle, Side  # noqa: E402
from app.models.candle import Candle as CandleRow  # noqa: E402
from app.services.ensemble import resample  # noqa: E402

CASES = [
    ("RU000A106T36", "ASTR", Side.SELL, 213.17, 10873.71),
    ("BBG004S683W7", "AFLT", Side.SELL, 31.89, 1626.90),
    ("BBG004730N88", "SBER", Side.BUY, 276.64, -13552.42),
    ("BBG004S681W1", "MTSS", Side.BUY, 186.43, -9133.11),
]


async def load(figi: str, limit: int = 1200) -> list[Candle]:
    async with SessionLocal() as db:
        rows = (await db.execute(
            select(CandleRow).where(CandleRow.figi == figi, CandleRow.interval == 1)
            .order_by(CandleRow.ts.desc()).limit(limit)
        )).scalars().all()
    rows = list(reversed(rows))
    return [Candle(ts=r.ts,
                   open=float(r.open), high=float(r.high), low=float(r.low),
                   close=float(r.close), volume=int(r.volume or 0)) for r in rows]


def true_range(bar: Candle, prev_close: float | None) -> float:
    if prev_close is None:
        return bar.high - bar.low
    return max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))


def wilder_atr(bars: list[Candle], period: int = 14) -> float | None:
    if len(bars) < period:
        return None
    trs = [true_range(b, bars[i - 1].close if i else None) for i, b in enumerate(bars)]
    v = sum(trs[:period]) / period
    for tr in trs[period:]:
        v = (v * (period - 1) + tr) / period
    return v


async def main() -> None:
    for figi, ticker, side, entry, live_sl in CASES:
        bars_1m = await load(figi)
        if not bars_1m:
            print(f"{ticker}: нет баров в БД")
            continue
        bars_5m = resample(bars_1m, 300)
        dist_live = abs(live_sl - entry)
        atr5 = wilder_atr(bars_5m, 14)
        atr1 = wilder_atr(bars_1m, 14)
        print(f"\n=== {ticker} {figi} side={side.name} entry={entry} live_sl={live_sl}")
        print(f"  баров 1м={len(bars_1m)} 5м={len(bars_5m)}")
        print(f"  цена 1м: {bars_1m[0].close:.2f} .. {bars_1m[-1].close:.2f}")
        print(f"  цена 5м: {bars_5m[0].close:.2f} .. {bars_5m[-1].close:.2f}")
        print(f"  |sl-entry|={dist_live:.2f}  = {dist_live / entry:.3f} × entry")
        print(f"  ATR(14) 1м={atr1:.4f}  5м={atr5:.4f}" if atr1 and atr5 else "  ATR: мало данных")
        print(f"  ATR5/entry={atr5 / entry:.4f}" if atr5 else "")
        for mult in (1.0, 2.0, 4.0):
            pol = AtrStopPolicy(period=14, multiplier=mult, risk_reward=1.0)
            plan = pol.plan_entry(side, entry, bars_5m)
            print(f"  mult={mult}: sl={plan.stop_loss:.2f} tp={plan.take_profit:.2f} "
                  f"(dist {abs(plan.stop_loss - entry):.2f})")
        # при каком множителе ATR даёт живой стоп
        if atr5 and atr5 > 0:
            print(f"  живой стоп соответствует mult = {dist_live / atr5:.3f} (от ATR 5м)")


if __name__ == "__main__":
    asyncio.run(main())
