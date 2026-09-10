"""screener.py — правый сайдбар: все акции из БД + цена/оборот/волатильность из TQBR quotes.

Данные MOEX ISS (0.7с на весь рынок) кэшируются на CACHE_TTL секунд.
Волатильность = RNG% = (HIGH−LOW)/WAPRICE за день. Оборот = VALTODAY (₽).
"""
import time
import urllib.request
import json
from fastapi import APIRouter, Depends
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.instrument import Instrument

router = APIRouter(prefix="/api/v1/screener", tags=["screener"])

TQBR_URL = (
    "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR"
    "/securities.json?iss.only=marketdata"
)
CACHE_TTL = 15  # сек — MOEX ISS обновляет TQBR quotes каждые 2-5с в сессию
MICRO_CACHE_TTL = 2
_cached: dict = {"ts": 0.0, "quotes": None}


def _fetch_quotes_uncached() -> dict:
    data = json.loads(urllib.request.urlopen(TQBR_URL, timeout=20).read())
    md = data["marketdata"]
    cols = {n: i for i, n in enumerate(md["columns"])}

    def _f(r, k):
        try:
            return float(r[cols[k]])
        except (TypeError, ValueError):
            return None

    out = {}
    for r in md["data"]:
        secid = r[cols["SECID"]]
        if not secid:
            continue
        wap = _f(r, "WAPRICE")
        last = _f(r, "LAST")
        hi = _f(r, "HIGH")
        lo = _f(r, "LOW")
        val = _f(r, "VALTODAY")
        price = last if last and last > 0 else wap
        rng = ((hi - lo) / wap * 100) if (wap and wap > 0 and hi and lo and hi > 0) else 0.0
        out[secid] = {
            "ticker": secid,
            "price": price or 0.0,
            "turnover": val or 0.0,
            "rng_pct": rng,
        }
    return out


def fetch_tqbr_market() -> dict:
    now = time.monotonic()
    if _cached["quotes"] is not None and now - _cached["ts"] < CACHE_TTL:
        return _cached["quotes"]
    quotes = _fetch_quotes_uncached()
    _cached.update(ts=now, quotes=quotes)
    return quotes


@router.get("")
async def screener(db: AsyncSession = Depends(get_db)) -> dict:
    quotes = fetch_tqbr_market()
    result = await db.execute(select(Instrument).where(Instrument.class_code == "TQBR"))
    universe = set(
        row[0] for row in (await db.execute(
            text("SELECT ticker FROM universe WHERE eligible_tier = 'eligible'")
        )).all()
    )
    rows = []
    for inst in result.scalars():
        q = quotes.get(inst.ticker)
        rows.append({
            "figi": inst.figi,
            "ticker": inst.ticker,
            "name": inst.name,
            "lot": inst.lot,
            "price": q["price"] if q else None,
            "turnover": q["turnover"] if q else None,
            "rng_pct": q["rng_pct"] if q else None,
            "in_universe": inst.ticker in universe,
        })
    rows.sort(key=lambda x: (x["turnover"] or 0), reverse=True)
    return {"count": len(rows), "items": rows}