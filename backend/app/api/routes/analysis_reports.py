"""Analytics API — просмотр прогонов из reports/ (таблицы report_*).

Методология владельца: PnL сам по себе не показатель (бумаги по 200₽ и 5000₽),
поэтому в ответах на первый план вынесены количество сделок и gross win/loss
И в штуках, и в рублях; срезы по сессии/режиму/ER; полная информация по сделкам
(SL/TP, MAE/MFE, время, причина выхода).

Схема /api/v1/analysis:
  GET /reports                       — список прогонов
  GET /reports/{id}                  — строки + сводка по стратегиям
  GET /reports/{id}/slices?dim=…     — срезы (session|regime_adx|er|ticker|hour|weekday)
  GET /reports/{id}/trades           — сделки с полной инфой (+min/max SL/TP по выборке)
  GET /market/leaders?window=…       — лидеры/аутсайдеры рынка за день/неделю/месяц
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.reports import ReportRow, ReportRun, ReportSlice, ReportTrade
from app.services.report_slices import DIM_LABELS, DIMS, ordered_buckets

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis-reports"])

WIN_WINDOW_BARS = {"day": (60, 1), "week": (24, 8), "month": (24, 32)}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _wr(wins: int, trades: int) -> float | None:
    return round(wins / trades * 100, 2) if trades else None


def _pf(gw: float, gl: float) -> float | None:
    if gl > 0:
        return round(gw / gl, 3)
    return 999.0 if gw > 0 else None


def _empty_bucket(bucket: str) -> dict:
    return {"bucket": bucket, "trades": 0, "wins": 0, "losses": 0, "gw": 0.0, "gl": 0.0,
            "net": 0.0, "wr": None, "pf": None}


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return datetime.min.replace(tzinfo=UTC)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fill(row: dict) -> dict:
    row["losses"] = row["trades"] - row["wins"]
    row["wr"] = _wr(row["wins"], row["trades"])
    row["pf"] = _pf(row["gw"], row["gl"])
    row["net"] = round(row["gw"] - row["gl"], 4)
    row["gw"] = round(row["gw"], 4)
    row["gl"] = round(row["gl"], 4)
    return row


# --------------------------------------------------------------------------- list

@router.get("/reports")
async def list_reports(
    kind: str | None = Query(None, description="real|wf|matrix|exp"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ReportRun)
    if kind:
        stmt = stmt.where(ReportRun.kind == kind)
    runs = list((await db.execute(stmt)).scalars().all())
    if not runs:
        return {"runs": [], "count": 0}

    ids = [r.id for r in runs]
    agg_rows = (await db.execute(
        select(
            ReportRow.run_id,
            func.sum(ReportRow.trades),
            func.sum(ReportRow.wins),
            func.sum(ReportRow.gw),
            func.sum(ReportRow.gl),
            func.sum(ReportRow.net),
            func.count(func.distinct(ReportRow.strategy)),
        ).where(ReportRow.run_id.in_(ids)).group_by(ReportRow.run_id)
    )).all()
    agg = {r[0]: r for r in agg_rows}
    trade_rows = (await db.execute(
        select(ReportTrade.run_id, func.count(ReportTrade.id))
        .where(ReportTrade.run_id.in_(ids)).group_by(ReportTrade.run_id)
    )).all()
    detail = {r[0]: r[1] for r in trade_rows}

    out = []
    for r in sorted(runs, key=lambda x: _aware(x.created_at or x.mtime or x.imported_at), reverse=True):
        a = agg.get(r.id)
        meta = r.meta or {}
        out.append({
            "id": r.id,
            "file_name": r.file_name,
            "kind": r.kind,
            "name": r.name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "imported_at": r.imported_at.isoformat() if r.imported_at else None,
            "mtime": r.mtime.isoformat() if r.mtime else None,
            "period": meta.get("period"),
            "interval": meta.get("interval"),
            "robots": int(a[6]) if a else 0,
            "trades": int(a[1] or 0) if a else 0,
            "wins": int(a[2] or 0) if a else 0,
            "gw": round(float(a[3] or 0), 2) if a else 0.0,
            "gl": round(float(a[4] or 0), 2) if a else 0.0,
            "net": round(float(a[5] or 0), 2) if a else 0.0,
            "detail_trades": int(detail.get(r.id, 0)),
        })
    return {"runs": out, "count": len(out)}


# ------------------------------------------------------------------ run detail

async def _get_run(db: AsyncSession, run_id: int) -> ReportRun:
    run = await db.get(ReportRun, run_id)
    if run is None:
        raise HTTPException(404, f"прогон #{run_id} не найден")
    return run


@router.get("/reports/{run_id}")
async def get_report(run_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    run = await _get_run(db, run_id)
    rows = list((await db.execute(
        select(ReportRow).where(ReportRow.run_id == run_id)
    )).scalars().all())

    by_strategy: dict[str, dict] = {}
    for r in rows:
        s = by_strategy.setdefault(r.strategy, {
            "strategy": r.strategy, "trades": 0, "wins": 0, "gw": 0.0, "gl": 0.0,
            "net": 0.0, "commission": 0.0, "max_dd_pct": 0.0, "tickers": set(),
            "exits": set(),
        })
        s["trades"] += r.trades
        s["wins"] += r.wins
        s["gw"] += r.gw
        s["gl"] += r.gl
        s["net"] += r.net
        s["commission"] += r.commission or 0.0
        s["max_dd_pct"] = max(s["max_dd_pct"], r.max_dd_pct or 0.0)
        if r.ticker:
            s["tickers"].add(r.ticker)
        if r.exit:
            s["exits"].add(r.exit)

    strategies = []
    for s in by_strategy.values():
        s["tickers"] = len(s["tickers"])
        s["exits"] = sorted(s["exits"])
        strategies.append(_fill(s))
    strategies.sort(key=lambda x: (-x["trades"], x["strategy"]))

    total = _fill({
        "trades": sum(s["trades"] for s in strategies),
        "wins": sum(s["wins"] for s in strategies),
        "gw": sum(s["gw"] for s in strategies),
        "gl": sum(s["gl"] for s in strategies),
        "commission": round(sum(s["commission"] for s in strategies), 4),
        "max_dd_pct": max((s["max_dd_pct"] for s in strategies), default=0.0),
    })
    total["expectancy"] = round(total["net"] / total["trades"], 4) if total["trades"] else None
    total["strategies"] = len(strategies)
    total["tickers"] = len({r.ticker for r in rows if r.ticker})

    dims = [d for d, in (await db.execute(
        select(ReportSlice.dim).where(ReportSlice.run_id == run_id).distinct()
    )).all()]

    meta = run.meta or {}
    return {
        "run": {
            "id": run.id, "file_name": run.file_name, "kind": run.kind, "name": run.name,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "period": meta.get("period"), "interval": meta.get("interval"),
            "tickers": meta.get("tickers"), "commission": meta.get("commission"),
            "slippage_bps": meta.get("slippage_bps"),
        },
        "summary": total,
        "strategies": strategies,
        "rows": [
            {
                "id": r.id, "strategy": r.strategy, "exit": r.exit, "ticker": r.ticker,
                "trades": r.trades, "wins": r.wins, "gw": round(r.gw, 4), "gl": round(r.gl, 4),
                "net": round(r.net, 4), "pf": _pf(r.gw, r.gl), "max_dd_pct": r.max_dd_pct,
                "commission": r.commission, "raw": r.raw,
            }
            for r in sorted(rows, key=lambda x: (-x.trades, x.strategy, x.ticker))
        ],
        "dims": dims,
        "dim_labels": DIM_LABELS,
    }


# ----------------------------------------------------------------------- slices

@router.get("/reports/{run_id}/slices")
async def get_report_slices(
    run_id: int,
    dim: str | None = Query(None, description="session|regime_adx|er|ticker|hour|weekday"),
    strategy: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_run(db, run_id)
    if dim and dim not in DIMS:
        raise HTTPException(400, f"неизвестный dim={dim}. Доступно: {', '.join(DIMS)}")
    stmt = select(ReportSlice).where(ReportSlice.run_id == run_id)
    if dim:
        stmt = stmt.where(ReportSlice.dim == dim)
    if strategy:
        stmt = stmt.where(ReportSlice.strategy == strategy)
    slices = list((await db.execute(stmt)).scalars().all())

    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for s in slices:
        cell = grouped[s.strategy].setdefault(
            s.bucket,
            {"bucket": s.bucket, "trades": 0, "wins": 0, "gw": 0.0, "gl": 0.0},
        )
        cell["trades"] += s.trades
        cell["wins"] += s.wins
        cell["gw"] += s.gw
        cell["gl"] += s.gl

    by_dim: dict[str, dict] = {}
    for s in slices:
        d = by_dim.setdefault(s.dim, {"dim": s.dim, "label": DIM_LABELS.get(s.dim, s.dim),
                                      "buckets": set(), "strategies": set()})
        d["buckets"].add(s.bucket)
        d["strategies"].add(s.strategy)

    dims_out = []
    for dname, d in sorted(by_dim.items()):
        buckets = ordered_buckets(dname, d["buckets"])
        table = []
        for strategy in sorted(d["strategies"]):
            cells = []
            for b in buckets:
                cell = grouped[strategy].get(b)
                if cell is None:
                    cells.append(_empty_bucket(b))
                else:
                    cells.append(_fill(dict(cell)))
            total = _fill({
                "trades": sum(c["trades"] for c in cells),
                "wins": sum(c["wins"] for c in cells),
                "gw": sum(c["gw"] for c in cells),
                "gl": sum(c["gl"] for c in cells),
            })
            best = None
            for c in cells:
                if c["trades"] >= 3 and (best is None or c["net"] > best["net"]):
                    best = c
            table.append({"strategy": strategy, "cells": cells, "total": total,
                          "best_bucket": best["bucket"] if best else None})
        dims_out.append({"dim": dname, "label": DIM_LABELS.get(dname, dname),
                         "buckets": buckets, "table": table})

    return {"run_id": run_id, "dim": dim, "dims": dims_out}


# ----------------------------------------------------------------------- trades

@router.get("/reports/{run_id}/trades")
async def get_report_trades(
    run_id: int,
    strategy: str | None = Query(None),
    ticker: str | None = Query(None),
    exit_code: str | None = Query(None, alias="exit"),
    session_: str | None = Query(None, alias="session"),
    regime_adx: str | None = Query(None),
    side: str | None = Query(None),
    outcome: str | None = Query(None, description="win|loss"),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _get_run(db, run_id)
    criteria = [ReportTrade.run_id == run_id]
    if strategy:
        criteria.append(ReportTrade.strategy == strategy)
    if ticker:
        criteria.append(ReportTrade.ticker == ticker)
    if exit_code:
        criteria.append(ReportTrade.exit == exit_code)
    if session_:
        criteria.append(ReportTrade.session == session_)
    if regime_adx:
        criteria.append(ReportTrade.regime_adx == regime_adx)
    if side:
        criteria.append(ReportTrade.side == side.upper())
    if outcome == "win":
        criteria.append(ReportTrade.net_pnl > 0)
    elif outcome == "loss":
        criteria.append(ReportTrade.net_pnl <= 0)

    total = int((await db.execute(
        select(func.count()).select_from(ReportTrade).where(*criteria)
    )).scalar_one())

    agg = (await db.execute(
        select(
            func.count(ReportTrade.id),
            func.sum(case((ReportTrade.net_pnl > 0, 1), else_=0)),
            func.sum(case((ReportTrade.net_pnl > 0, ReportTrade.net_pnl), else_=0.0)),
            func.sum(case((ReportTrade.net_pnl <= 0, -ReportTrade.net_pnl), else_=0.0)),
            func.min(ReportTrade.sl_price),
            func.max(ReportTrade.sl_price),
            func.min(ReportTrade.tp_price),
            func.max(ReportTrade.tp_price),
            func.min(ReportTrade.net_pnl),
            func.max(ReportTrade.net_pnl),
            func.max(ReportTrade.mae_atr),
            func.max(ReportTrade.mfe_atr),
            func.avg(ReportTrade.bars_held),
        ).where(*criteria)
    )).one()

    gw = float(agg[2] or 0.0)
    gl = float(agg[3] or 0.0)
    n = int(agg[0] or 0)
    summary = {
        "trades": n,
        "wins": int(agg[1] or 0),
        "losses": n - int(agg[1] or 0),
        "gw": round(gw, 4),
        "gl": round(gl, 4),
        "net": round(gw - gl, 4),
        "wr": _wr(int(agg[1] or 0), n),
        "pf": _pf(gw, gl),
        "expectancy": round((gw - gl) / n, 4) if n else None,
        "sl_min": agg[4], "sl_max": agg[5],
        "tp_min": agg[6], "tp_max": agg[7],
        "worst": round(float(agg[8]), 4) if agg[8] is not None else None,
        "best": round(float(agg[9]), 4) if agg[9] is not None else None,
        "mae_atr_max": round(float(agg[10]), 3) if agg[10] is not None else None,
        "mfe_atr_max": round(float(agg[11]), 3) if agg[11] is not None else None,
        "bars_avg": round(float(agg[12]), 1) if agg[12] is not None else None,
    }

    stmt = select(ReportTrade).where(*criteria)
    items = (await db.execute(
        stmt.order_by(ReportTrade.entry_time.desc().nullslast(), ReportTrade.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()

    return {
        "run_id": run_id,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary,
        "items": [
            {
                "id": t.id, "strategy": t.strategy, "exit": t.exit, "ticker": t.ticker,
                "side": t.side,
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "exit_reason": t.exit_reason, "bars_held": t.bars_held,
                "net_pnl": round(t.net_pnl, 4),
                "sl_price": t.sl_price, "tp_price": t.tp_price,
                "mae_atr": t.mae_atr, "mfe_atr": t.mfe_atr,
                "session": t.session, "regime_adx": t.regime_adx, "er_in": t.er_in,
            }
            for t in items
        ],
    }


# ---------------------------------------------------------------- market leaders

def _vol(pcts: list[float]) -> float | None:
    if len(pcts) < 2:
        return None
    mean = sum(pcts) / len(pcts)
    var = sum((x - mean) ** 2 for x in pcts) / (len(pcts) - 1)
    return round(math.sqrt(var), 3)


@router.get("/market/leaders")
async def market_leaders(
    window: str = Query("week", pattern="^(day|week|month)$"),
    limit: int = Query(15, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> dict:
    interval, days = WIN_WINDOW_BARS[window]
    since = datetime.now(UTC) - timedelta(days=days)
    rows = (await db.execute(text(
        """
        SELECT i.ticker, c.ts, c.open, c.close
        FROM instruments i
        JOIN candles c ON c.figi = i.figi AND c.interval = :iv
        WHERE c.ts >= :since AND i.ticker IS NOT NULL AND i.ticker <> ''
        ORDER BY i.ticker, c.ts
        """
    ), {"iv": interval, "since": since})).all()

    by_ticker: dict[str, list[tuple[datetime, float, float]]] = defaultdict(list)
    for ticker, ts, o, c in rows:
        by_ticker[str(ticker)].append((ts, float(o), float(c)))

    items = []
    for ticker, series in by_ticker.items():
        if len(series) < 2:
            continue
        first_open = series[0][1]
        last_close = series[-1][2]
        if first_open <= 0:
            continue
        rets = [
            (series[i][2] / series[i - 1][2] - 1) * 100
            for i in range(1, len(series)) if series[i - 1][2] > 0
        ]
        items.append({
            "ticker": ticker,
            "pct": round((last_close / first_open - 1) * 100, 2),
            "vol": _vol(rets),
            "close": round(last_close, 2),
            "bars": len(series),
            "first_ts": _iso(series[0][0]),
            "last_ts": _iso(series[-1][0]),
        })
    items.sort(key=lambda x: x["pct"], reverse=True)
    top = items[:limit]
    bottom = list(reversed(items[-limit:])) if len(items) > limit else []
    return {
        "window": window,
        "interval": interval,
        "since": since.isoformat(),
        "count": len(items),
        "leaders": top,
        "outsiders": bottom,
        "items": items,
    }
