"""Мониторинг CandleHub: статистика рядов, дыры, события.

UI может вызывать этот endpoint для отображения состояния движка свечей.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.candle_hub_orchestrator import CandleHubOrchestrator

router = APIRouter(prefix="/api/candlehub", tags=["candlehub"])

_orchestrator: CandleHubOrchestrator | None = None


def get_orchestrator() -> CandleHubOrchestrator:
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = CandleHubOrchestrator()
    return _orchestrator


@router.get("/stats")
async def get_stats() -> dict:
    """Статистика всех рядов в hub.

    Возвращает по каждому (figi, tf): число закрытых баров, наличие partial,
    счётчики skipped/rejected/rebuilds.
    """
    orch = get_orchestrator()
    stats = orch.get_stats()
    return {
        "series": [
            {
                "figi": key[0],
                "tf_seconds": key[1],
                **value,
            }
            for key, value in stats.items()
        ]
    }


@router.get("/gaps/{figi}")
async def get_gaps(figi: str) -> dict:
    """Дыры 1m-истории figi — что докачивать.

    Возвращает список [(первая_минута, последняя_минута)].
    """
    orch = get_orchestrator()
    gaps = orch.hub.gap_report(figi)
    return {
        "figi": figi,
        "gaps": [
            {"from": start.isoformat(), "to": end.isoformat()}
            for start, end in gaps
        ],
    }


@router.get("/series/{figi}/{tf}")
async def get_series(figi: str, tf: str, limit: int = 100) -> dict:
    """Снимок последних limit закрытых баров ряда figi×tf."""
    orch = get_orchestrator()
    try:
        candles = orch.get_snapshot(figi, tf, n=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "figi": figi,
        "tf": tf,
        "count": len(candles),
        "candles": [
            {
                "ts": c.ts.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
    }


@router.post("/ensure/{figi}")
async def ensure_ready(
    figi: str,
    days: int = 120,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Прогреть hub из БД и докачить дыры для figi.

    Вызывается при старте или по требованию из UI.
    """
    orch = get_orchestrator()
    result = await orch.ensure_ready(db, figi, days=days)
    return result
