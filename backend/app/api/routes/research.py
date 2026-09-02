"""Read-only research pack endpoint.

GET /api/v1/research/pack?from_ts=...&to_ts=...&figis=RUAL,...&include_samples=true

Ограничения:
- период <= 92 дня (MAX_PERIOD_DAYS), иначе 400;
- только read-only: не пишет в БД, не отправляет данные наружу;
- выход: reports/{run_id}/research_pack.json + manifest (+ CSV при csv_bundle).
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from app.services.research_pack import (
    FIGI_TO_TICKER,
    MAX_PERIOD_DAYS,
    SCHEMA_VERSION,
    UNIVERSE,
    build_research_pack,
)

router = APIRouter(prefix="/api/v1/research", tags=["research"])

TICKER_TO_FIGI = {v: k for k, v in FIGI_TO_TICKER.items()}


def _parse_ts(value: str, name: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"{name}: невалидный ISO datetime: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_figis(raw: str) -> list[str]:
    parts = [p.strip().upper() for p in raw.split(",") if p.strip()]
    figis = []
    for p in parts:
        if p in TICKER_TO_FIGI:
            figis.append(TICKER_TO_FIGI[p])
        elif p in UNIVERSE:
            figis.append(p)
        else:
            raise HTTPException(400, f"неизвестный тикер/FIGI: {p}")
    if not figis:
        raise HTTPException(400, "figis пуст")
    return figis


@router.get("/pack")
async def research_pack(
    from_ts: str = Query(..., description="ISO начало периода (UTC)"),
    to_ts: str = Query(..., description="ISO конец периода (UTC)"),
    figis: str = Query("RUAL,AFLT,SNGSP,MVID,NLMK", description="через запятую"),
    strategy_id: str = Query("ensemble_main_v1"),
    include_samples: bool = Query(True),
    max_trade_samples: int = Query(100, ge=1, le=1000),
    max_rejection_samples: int = Query(200, ge=1, le=2000),
    output_format: str = Query("json", pattern="^(json|csv_bundle)$"),
    capital: float = Query(10_000, ge=1_000, le=100_000_000, description="капитал на позицию (руб)"),
) -> dict:
    """Создаёт immutable research pack (read-only) для внешнего анализа."""
    t_from = _parse_ts(from_ts, "from_ts")
    t_to = _parse_ts(to_ts, "to_ts")
    if t_from >= t_to:
        raise HTTPException(400, "from_ts должен быть раньше to_ts")
    days = (t_to - t_from).total_seconds() / 86400
    if days > MAX_PERIOD_DAYS:
        raise HTTPException(400, f"период > {MAX_PERIOD_DAYS} дней не разрешён (получено {days:.1f})")
    resolved = _resolve_figis(figis)
    result = build_research_pack(
        t_from, t_to, resolved,
        include_samples=include_samples,
        max_trade_samples=max_trade_samples,
        max_rejection_samples=max_rejection_samples,
        capital=capital,
    )
    out = {
        "run_id": result["manifest"]["run_id"],
        "schema_version": SCHEMA_VERSION,
        "strategy_id": strategy_id,
        "out_dir": result["out_dir"],
        "manifest": result["manifest"],
        "read_only": True,
    }
    return out
