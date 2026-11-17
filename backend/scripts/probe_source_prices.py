import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.tinvest import INTERVAL_NAMES, fetch_candles, to_thread  # noqa: E402


async def main() -> None:
    cases = [
        ("BBG004730N88", "SBER", "2025-11-28T07:00:00+00:00", "2025-11-28T19:00:00+00:00"),
        ("BBG00475K2X9", "HYDR", "2026-06-22T07:00:00+00:00", "2026-06-22T19:00:00+00:00"),
        ("BBG004S68CP5", "MVID", "2026-08-22T07:00:00+00:00", "2026-08-22T19:00:00+00:00"),
    ]
    for figi, ticker, a, b in cases:
        from_ = datetime.fromisoformat(a)
        to_ = datetime.fromisoformat(b)
        rows = await to_thread(fetch_candles, figi, INTERVAL_NAMES["1min"], from_, to_)
        closes = [float(r["close"]) for r in rows]
        if not closes:
            print(f"{ticker}: no rows from source")
            continue
        print(f"{ticker}: rows={len(closes)} min={min(closes):.3f} max={max(closes):.3f} first={closes[0]:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
