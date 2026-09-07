import asyncio
from datetime import datetime, timezone, timedelta
from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
from app.services.signals import _load_candles as _lc
from app.database import SessionLocal

async def main():
    figi = 'BBG004730RP0'  # GAZP
    async with SessionLocal() as db:
        candles = await _lc(db, figi, 1,
                            date_from=datetime(2026, 8, 1, tzinfo=timezone.utc) - timedelta(days=60),
                            date_to=datetime(2026, 9, 1, tzinfo=timezone.utc))
    print('candles:', len(candles))
    for sess in ['main', 'all']:
        strat = EnsembleV4Strategy(EnsembleParams(
            figi=figi, lot=10, capital=1000, quorum=2,
            session=sess, sessions=['morning', 'day', 'evening']))
        n = 0
        total = 0
        # только первые 150 вызовов на 5м-границах (чтобы быстро)
        for i in range(200, min(len(candles), 200 + 150 * 5), 5):
            c = candles[i]
            if c.ts.minute % 5 != 0:
                continue
            total += 1
            try:
                sig = strat.on_bar(candles[max(0, i - 2000):i + 1])
            except Exception as e:
                print('ERR at', i, type(e).__name__, e)
                break
            if sig:
                n += 1
                print('  SIG', sess, sig.side, sig.time)
        print('session=%s on_bar_calls=%d signals=%d' % (sess, total, n))

asyncio.run(main())
