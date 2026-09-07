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
    for sess in ['main', 'all']:
        strat = EnsembleV4Strategy(EnsembleParams(
            figi=figi, lot=10, capital=1000, quorum=2,
            session=sess, sessions=['morning', 'day', 'evening']))
        # ищем бары с ts.minute%5==0, делаем вызовы только в этих точках (с шагом ~100 баров)
        n = 0
        total = 0
        prev = None
        for i in range(5000, len(candles)):
            if candles[i].ts.minute % 5 == 0:
                if prev is not None and i - prev < 100:
                    continue
                prev = i
                total += 1
                try:
                    sig = strat.on_bar(candles[max(0, i - 2000):i + 1])
                except Exception as e:
                    print('ERR', sess, type(e).__name__, str(e)[:100])
                    break
                if sig:
                    n += 1
        print('session=%s on_bar_calls=%d signals=%d' % (sess, total, n))

asyncio.run(main())
