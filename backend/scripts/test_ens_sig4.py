import asyncio
from datetime import datetime, timezone, timedelta
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
from app.services.signals import _load_candles as _lc
from app.database import SessionLocal

async def main():
    figi = 'BBG004730RP0'
    async with SessionLocal() as db:
        candles = await _lc(db, figi, 1,
                            date_from=datetime(2026, 8, 1, tzinfo=timezone.utc) - timedelta(days=60),
                            date_to=datetime(2026, 9, 1, tzinfo=timezone.utc))
    print('candles:', len(candles))
    strat = EnsembleV4Strategy(EnsembleParams(
        figi=figi, lot=10, capital=1000, quorum=2,
        session='main', sessions=['morning', 'day', 'evening']))
    # тестовые точки: 5 случайных 5m-баров в августе (день)
    import random
    random.seed(42)
    pts = []
    for i in range(3000, len(candles)):
        c = candles[i]
        if c.ts.minute % 5 == 0:
            h = c.ts.astimezone(__import__('zoneinfo').ZoneInfo('Europe/Moscow')).hour
            if 10 <= h <= 17:
                pts.append(i)
    sample = random.sample(pts, min(6, len(pts)))
    for i in sorted(sample):
        try:
            sig = strat.on_bar(candles[max(0, i - 2000):i + 1])
            print('bar', candles[i].ts, '->', sig.side if sig else 'NONE')
        except Exception as e:
            print('ERR at', i, type(e).__name__, str(e)[:150])

asyncio.run(main())
