"""Загрузка исторических CSV (Т-Инвест history-data) в базу проекта.

CSV-формат (разделитель ';'): figi;time;open;close;high;low;volume;
Вставляем как 1-минутные свечи в таблицу candles (interval=1min).
Политика: сначала в базу, потом из базы отдаём потребителю.
"""
import glob
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models.candle import Candle

INTERVAL_1MIN = 1  # CandleInterval.CANDLE_INTERVAL_1_MIN


async def main(csv_dir: str, figi_by_file: dict[str, str]) -> None:
    engine = create_async_engine(get_settings().database_url)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    print(f"файлов: {len(files)}")
    total = 0
    async with Session() as db:
        for path in files:
            fname = os.path.basename(path)
            uid = fname.split("_")[0]
            figi = figi_by_file.get(uid)
            if not figi:
                continue
            rows = []
            with open(path) as f:
                for line in f:
                    parts = line.strip().rstrip(";").split(";")
                    if len(parts) < 7:
                        continue
                    _, ts, o, c, h, l, v = parts[:7]
                    try:
                        ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        rows.append({
                            "figi": figi, "interval": INTERVAL_1MIN, "ts": ts_dt,
                            "open": float(o), "high": float(h), "low": float(l),
                            "close": float(c), "volume": int(float(v)),
                        })
                    except (ValueError, IndexError):
                        continue
            if not rows:
                continue
            stmt = pg_insert(Candle).values(rows).on_conflict_do_nothing(
                index_elements=["figi", "interval", "ts"]
            )
            await db.execute(stmt)
            total += len(rows)
            if total % 50000 < 5000:
                await db.commit()
                print(f"  загружено ~{total} свечей", flush=True)
        await db.commit()
    print(f"ИТОГО загружено: {total} свечей")
    await engine.dispose()


UID_TO_FIGI = {
    "f866872b-8f68-4b6e-930f-749fe9aa79c0": "BBG008F2T3T2",   # RUAL
    "1c69e020-f3b1-455c-affa-45f8b8049234": "BBG004S683W7",   # AFLT
    "a797f14a-8513-4b84-b15e-a3b98dc4cc00": "BBG004S681M2",   # SNGSP
    "cf1c6158-a303-43ac-89eb-9b1db8f96043": "BBG004S68CP5",   # MVID
    "161eb0d0-aaac-4451-b374-f5d0eeb1b508": "BBG004S681B4",   # NLMK
}

if __name__ == "__main__":
    import asyncio
    asyncio.run(main("/tmp/hist", UID_TO_FIGI))
