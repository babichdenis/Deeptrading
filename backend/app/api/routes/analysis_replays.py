"""Analytics API — живые проектные тесты реплея (sandbox_trades mode='paper').

В отличие от /analysis/reports (файлы reports/ → таблицы report_*), эти данные
читаются НА ЛЕТУ из БД: один тест = имя test_name, окно — bot_test_runs,
настройки — сайдкар пресета (app.services.preset_tags).

Схема /api/v1/analysis:
  GET /replays                      — список тестов + теги настроек
  GET /replays/{name}               — сводка + строки + группы тегов
  GET /replays/{name}/slices?dim=   — срезы на лету (session|regime|ticker|hour|weekday|side|exit|entry)
  GET /replays/{name}/trades        — сделки с полной инфой (+min/max SL/TP по выборке)
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.sandbox_trade import SandboxTrade
from app.services.preset_tags import load_sidecar, preset_id, tag_groups, tags_for_names
from app.services.report_slices import hour_bucket, ordered_buckets, session_bucket, weekday_bucket

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis-replays"])

REPLAY_DIM_LABELS: dict[str, str] = {
    "session": "Сессия (МСК)",
    "regime": "Режим (вход)",
    "ticker": "Тикер",
    "hour": "Час (МСК)",
    "weekday": "День недели",
    "side": "Сторона",
    "exit": "Причина выхода",
    "entry": "Причина входа",
}
REPLAY_DIMS: tuple[str, ...] = tuple(REPLAY_DIM_LABELS)

TF_MINUTES: dict[str, int] = {
    "1min": 1, "5min": 5, "10min": 10, "15min": 15, "30min": 30,
    "hour": 60, "2h": 120, "4h": 240, "day": 1440,
}


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


def _fill(row: dict) -> dict:
    row["losses"] = row["trades"] - row["wins"]
    row["wr"] = _wr(row["wins"], row["trades"])
    row["pf"] = _pf(row["gw"], row["gl"])
    row["net"] = round(row["gw"] - row["gl"], 4)
    row["gw"] = round(row["gw"], 4)
    row["gl"] = round(row["gl"], 4)
    return row


def _meta_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _engine_label(sidecar: dict | None, test_name: str) -> str:
    if sidecar:
        payload = sidecar.get("payload") or {}
        engine = str(payload.get("test_engine") or "").strip()
        if engine:
            return engine
        robots = ((sidecar.get("preset") or {}).get("harness") or {}).get("robots") or []
        if robots and isinstance(robots[0], dict) and robots[0].get("robot"):
            return str(robots[0]["robot"])
    return test_name


def _tf_minutes(sidecar: dict | None) -> int | None:
    if not sidecar:
        return None
    payload = sidecar.get("payload") or {}
    iv = str(payload.get("test_interval") or "").strip().lower()
    if not iv:
        iv = str((((sidecar.get("preset") or {}).get("harness") or {}).get("timeframe")) or "").strip().lower()
    return TF_MINUTES.get(iv)


async def _windows(db: AsyncSession) -> dict[str, tuple]:
    """name -> (replay_start, replay_end, updated_at) в ISO; в sqlite это str, в PG — datetime."""
    try:
        rows = (await db.execute(text(
            "SELECT name, replay_start, replay_end, updated_at FROM bot_test_runs"))).all()
        return {str(r[0]): (_iso(r[1]), _iso(r[2]), _iso(r[3])) for r in rows}
    except Exception:
        return {}


async def _names(db: AsyncSession) -> list[str]:
    traded = [str(x) for x, in (await db.execute(
        select(SandboxTrade.test_name)
        .where(SandboxTrade.mode == "paper", SandboxTrade.test_name.is_not(None))
        .distinct())).all() if x]
    return traded


def _closed(name: Any):
    return and_(
        SandboxTrade.test_name == name,
        SandboxTrade.mode == "paper",
        SandboxTrade.exit_time.is_not(None),
        SandboxTrade.net_pnl.is_not(None),
    )


def _agg_exprs(name: Any) -> list:
    closed = _closed(name)
    return [
        func.count(SandboxTrade.id),
        func.sum(case((closed, 1), else_=0)),
        func.sum(case((and_(SandboxTrade.exit_time.is_(None)), 1), else_=0)),
        func.sum(case((and_(closed, SandboxTrade.net_pnl > 0), 1), else_=0)),
        func.sum(case((and_(closed, SandboxTrade.net_pnl > 0), SandboxTrade.net_pnl), else_=0.0)),
        func.sum(case((and_(closed, SandboxTrade.net_pnl < 0), -SandboxTrade.net_pnl), else_=0.0)),
        func.sum(case((closed, SandboxTrade.commission), else_=0.0)),
        func.count(func.distinct(case((closed, SandboxTrade.ticker), else_=None))),
        func.min(case((closed, SandboxTrade.entry_time), else_=None)),
        func.max(case((closed, SandboxTrade.exit_time), else_=None)),
    ]


def _metrics_from(agg: tuple) -> dict:
    total, closed_n, open_n, wins, gw, gl, commission, tickers, first, last = agg
    gw = float(gw or 0.0)
    gl = float(gl or 0.0)
    n = int(closed_n or 0)
    out = {
        "trades": n,
        "wins": int(wins or 0),
        "losses": n - int(wins or 0),
        "gw": round(gw, 4),
        "gl": round(gl, 4),
        "net": round(gw - gl, 4),
        "wr": _wr(int(wins or 0), n),
        "pf": _pf(gw, gl),
        "open": int(open_n or 0),
        "commission": round(float(commission or 0.0), 4),
        "tickers": int(tickers or 0),
        "first_entry": _iso(first),
        "last_exit": _iso(last),
        "total_rows": int(total or 0),
    }
    out["expectancy"] = round((gw - gl) / n, 4) if n else None
    return out


# --------------------------------------------------------------------------- list

@router.get("/replays")
async def list_replays(db: AsyncSession = Depends(get_db)) -> dict:
    names = set(await _names(db))
    windows = await _windows(db)
    names.update(windows)
    if not names:
        return {"runs": [], "count": 0}

    agg_rows = (await db.execute(
        select(SandboxTrade.test_name, *_agg_exprs(SandboxTrade.test_name))
        .where(SandboxTrade.mode == "paper", SandboxTrade.test_name.is_not(None))
        .group_by(SandboxTrade.test_name)
    )).all()
    agg = {str(r[0]): r[1:] for r in agg_rows}

    sidecars = tags_for_names(sorted(names))
    out = []
    for name in sorted(names):
        a = agg.get(name)
        m = _metrics_from(a) if a else _metrics_from((0, 0, 0, 0, 0, 0, 0, 0, None, None))
        w = windows.get(name)
        sc_info = sidecars.get(name) or {}
        period = [
            (w[0] if w and w[0] else m["first_entry"]),
            (w[1] if w and w[1] else m["last_exit"]),
        ] if (w and w[0]) or m["first_entry"] else None
        created = (w[2] if w and w[2] else m["first_entry"])
        out.append({
            "id": name,
            "file_name": name,
            "kind": "replay",
            "name": sc_info.get("preset_id"),
            "created_at": created,
            "mtime": (w[2] if w and w[2] else None),
            "updated_at": (w[2] if w and w[2] else m["last_exit"]),
            "period": period,
            "interval": None,
            "robots": 1 if a else 0,
            "trades": m["trades"],
            "wins": m["wins"],
            "losses": m["losses"],
            "gw": m["gw"],
            "gl": m["gl"],
            "net": m["net"],
            "pf": m["pf"],
            "winrate": m["wr"],
            "open": m["open"],
            "detail_trades": m["trades"],
            "preset_id": sc_info.get("preset_id"),
            "has_sidecar": bool(sc_info),
        })
    out.sort(key=lambda x: x["updated_at"] or x["created_at"] or "", reverse=True)
    return {"runs": out, "count": len(out)}


# ------------------------------------------------------------------ run detail

async def _require(db: AsyncSession, name: str) -> dict:
    windows = await _windows(db)
    traded = name in set(await _names(db)) or name in windows
    if not traded:
        raise HTTPException(404, f"тест {name!r} не найден")
    sidecar = load_sidecar(name)
    m = _metrics_from((await db.execute(
        select(*_agg_exprs(name)).where(SandboxTrade.test_name == name,
                                         SandboxTrade.mode == "paper")
    )).one())
    m["strategies"] = 1
    return {"sidecar": sidecar, "m": m, "engine": _engine_label(sidecar, name),
            "tf_minutes": _tf_minutes(sidecar), "w": windows.get(name)}


@router.get("/replays/{name}")
async def get_replay(name: str, db: AsyncSession = Depends(get_db)) -> dict:
    ctx = await _require(db, name)
    m, sidecar, engine = ctx["m"], ctx["sidecar"], ctx["engine"]

    by_ticker = (await db.execute(
        select(
            SandboxTrade.ticker,
            func.count(SandboxTrade.id),
            func.sum(case((_closed(name) & (SandboxTrade.net_pnl > 0), 1), else_=0)),
            func.sum(case((_closed(name) & (SandboxTrade.net_pnl > 0), SandboxTrade.net_pnl), else_=0.0)),
            func.sum(case((_closed(name) & (SandboxTrade.net_pnl < 0), -SandboxTrade.net_pnl), else_=0.0)),
            func.count(case((_closed(name), 1), else_=None)),
        ).where(SandboxTrade.test_name == name, SandboxTrade.mode == "paper")
        .group_by(SandboxTrade.ticker)
    )).all()

    rows = []
    for ticker, _total, wins, gw, gl, closed_n in by_ticker:
        r = _fill({"strategy": engine, "exit": "", "ticker": ticker or "?",
                   "trades": int(closed_n or 0), "wins": int(wins or 0),
                   "gw": float(gw or 0.0), "gl": float(gl or 0.0),
                   "commission": 0.0, "max_dd_pct": 0.0,
                   "tickers": 1, "exits": [], "id": 0})
        rows.append(r)
    rows.sort(key=lambda x: (-x["trades"], x["ticker"]))

    exit_rows = (await db.execute(
        select(SandboxTrade.exit_reason).where(
            _closed(name), SandboxTrade.exit_reason.is_not(None)).distinct()
    )).all()
    exits = {str(e[0]) for e in exit_rows if e[0]}

    strategy = _fill({
        "strategy": engine, "trades": m["trades"], "wins": m["wins"],
        "gw": m["gw"], "gl": m["gl"], "commission": m["commission"],
        "max_dd_pct": 0.0, "tickers": m["tickers"],
        "exits": sorted(exits),
    })

    tag_info = tag_groups(sidecar.get("preset") or {}) if sidecar else []
    w = ctx.get("w")
    period = [(w[0] if w and w[0] else m["first_entry"]),
              (w[1] if w and w[1] else m["last_exit"])]
    if not any(period):
        period = None
    return {
        "run": {
            "id": name, "file_name": name, "kind": "replay",
            "name": ((sidecar or {}).get("preset") or {}).get("preset", {}).get("name") if sidecar else None,
            "created_at": (w[2] if w and w[2] else m["first_entry"]),
            "period": period,
            "interval": None,
            "commission": m["commission"],
            "slippage_bps": None,
        },
        "summary": {
            "trades": m["trades"], "wins": m["wins"], "losses": m["losses"],
            "gw": m["gw"], "gl": m["gl"], "net": m["net"], "wr": m["wr"],
            "pf": m["pf"], "expectancy": m["expectancy"],
            "commission": m["commission"], "max_dd_pct": None,
            "strategies": 1, "tickers": m["tickers"],
            "open_positions": m["open"],
        },
        "strategies": [strategy],
        "rows": rows,
        "dims": list(REPLAY_DIMS),
        "dim_labels": REPLAY_DIM_LABELS,
        "config_tags": {
            "has_sidecar": bool(sidecar),
            "preset_id": preset_id(sidecar),
            "notes": ((((sidecar or {}).get("preset") or {}).get("preset") or {}).get("notes") or ""),
            "groups": tag_info,
        },
    }


# ----------------------------------------------------------------------- slices

def _bucket(dim: str, t: dict) -> str | None:
    if dim == "session":
        return session_bucket(t["entry_time"])
    if dim == "regime":
        return t["regime"] or "нет данных"
    if dim == "ticker":
        return t["ticker"] or "?"
    if dim == "hour":
        return hour_bucket(t["entry_time"])
    if dim == "weekday":
        return weekday_bucket(t["entry_time"])
    if dim == "side":
        return t["side"] or "?"
    if dim == "exit":
        return t["exit_reason"] or "без причины"
    if dim == "entry":
        return t["entry_reason"] or "нет данных"
    return None


async def _closed_rows(db: AsyncSession, name: str) -> list[dict]:
    rows = (await db.execute(
        select(SandboxTrade.entry_time, SandboxTrade.exit_time, SandboxTrade.net_pnl,
               SandboxTrade.ticker, SandboxTrade.side, SandboxTrade.exit_reason,
               SandboxTrade.entry_reason, SandboxTrade.meta)
        .where(_closed(name))
        .order_by(SandboxTrade.entry_time.asc().nullslast(), SandboxTrade.id.asc())
    )).all()
    out = []
    for entry_time, exit_time, net_pnl, ticker, side, exit_reason, entry_reason, meta in rows:
        md = _meta_dict(meta)
        entry = md.get("entry") if isinstance(md.get("entry"), dict) else {}
        out.append({
            "entry_time": entry_time, "exit_time": exit_time,
            "net_pnl": float(net_pnl or 0.0), "ticker": ticker or "?",
            "side": side or "?", "exit_reason": exit_reason,
            "entry_reason": entry_reason or entry.get("reason"),
            "regime": md.get("entry_regime") or md.get("regime"),
        })
    return out


@router.get("/replays/{name}/slices")
async def get_replay_slices(
    name: str,
    dim: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ctx = await _require(db, name)
    engine = ctx["engine"]
    if dim and dim not in REPLAY_DIMS:
        raise HTTPException(400, f"неизвестный dim={dim}. Доступно: {', '.join(REPLAY_DIMS)}")
    wanted = [dim] if dim else list(REPLAY_DIMS)
    rows = await _closed_rows(db, name)

    acc: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0, 0, 0.0, 0.0])
    buckets_by_dim: dict[str, set[str]] = defaultdict(set)
    for t in rows:
        net = t["net_pnl"]
        for d in wanted:
            b = _bucket(d, t)
            if b is None:
                continue
            buckets_by_dim[d].add(b)
            cell = acc[(d, b)]
            cell[0] += 1
            cell[1] += 1 if net > 0 else 0
            cell[2] += net if net > 0 else 0.0
            cell[3] += -net if net < 0 else 0.0

    dims_out = []
    for d in wanted:
        buckets = ordered_buckets(d, buckets_by_dim.get(d, set())) \
            if d in ("session", "hour", "weekday") else sorted(buckets_by_dim.get(d, set()))
        cells = []
        for b in buckets:
            n, wins, gw, gl = acc.get((d, b), [0, 0, 0.0, 0.0])
            cells.append(_fill({"bucket": b, "trades": int(n), "wins": int(wins),
                                "gw": gw, "gl": gl}))
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
        dims_out.append({
            "dim": d, "label": REPLAY_DIM_LABELS.get(d, d), "buckets": buckets,
            "table": [{"strategy": engine, "cells": cells, "total": total,
                       "best_bucket": best["bucket"] if best else None}],
        })
    return {"name": name, "dim": dim, "dims": dims_out}


# ----------------------------------------------------------------------- trades

@router.get("/replays/{name}/trades")
async def get_replay_trades(
    name: str,
    strategy: str | None = Query(None),
    ticker: str | None = Query(None),
    exit_code: str | None = Query(None, alias="exit"),
    side: str | None = Query(None),
    outcome: str | None = Query(None, description="win|loss"),
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict:
    ctx = await _require(db, name)
    tf = ctx["tf_minutes"]
    engine = ctx["engine"]
    criteria = [SandboxTrade.test_name == name, SandboxTrade.mode == "paper",
                SandboxTrade.exit_time.is_not(None), SandboxTrade.net_pnl.is_not(None)]
    if ticker:
        criteria.append(SandboxTrade.ticker == ticker.upper())
    if side:
        criteria.append(SandboxTrade.side == side.upper())
    if exit_code:
        criteria.append(SandboxTrade.exit_reason == exit_code)
    if outcome == "win":
        criteria.append(SandboxTrade.net_pnl > 0)
    elif outcome == "loss":
        criteria.append(SandboxTrade.net_pnl <= 0)

    total = int((await db.execute(
        select(func.count()).select_from(SandboxTrade).where(*criteria)
    )).scalar_one())

    agg = (await db.execute(
        select(
            func.count(SandboxTrade.id),
            func.sum(case((SandboxTrade.net_pnl > 0, 1), else_=0)),
            func.sum(case((SandboxTrade.net_pnl > 0, SandboxTrade.net_pnl), else_=0.0)),
            func.sum(case((SandboxTrade.net_pnl <= 0, -SandboxTrade.net_pnl), else_=0.0)),
            func.min(SandboxTrade.stop_loss),
            func.max(SandboxTrade.stop_loss),
            func.min(SandboxTrade.take_profit),
            func.max(SandboxTrade.take_profit),
            func.min(SandboxTrade.net_pnl),
            func.max(SandboxTrade.net_pnl),
            func.avg(SandboxTrade.commission),
        ).where(*criteria)
    )).one()
    gw = float(agg[2] or 0.0)
    gl = float(agg[3] or 0.0)
    n = int(agg[0] or 0)
    wins = int(agg[1] or 0)

    def _num(v: Any) -> float | None:
        return float(v) if v is not None else None

    summary = {
        "trades": n, "wins": wins, "losses": n - wins,
        "gw": round(gw, 4), "gl": round(gl, 4), "net": round(gw - gl, 4),
        "wr": _wr(wins, n), "pf": _pf(gw, gl),
        "expectancy": round((gw - gl) / n, 4) if n else None,
        "sl_min": _num(agg[4]), "sl_max": _num(agg[5]),
        "tp_min": _num(agg[6]), "tp_max": _num(agg[7]),
        "worst": round(float(agg[8]), 4) if agg[8] is not None else None,
        "best": round(float(agg[9]), 4) if agg[9] is not None else None,
        "mae_atr_max": None, "mfe_atr_max": None,
        "bars_avg": None, "commission_avg": round(float(agg[10] or 0.0), 4) if n else None,
    }

    items_raw = (await db.execute(
        select(SandboxTrade).where(*criteria)
        .order_by(SandboxTrade.entry_time.desc().nullslast(), SandboxTrade.id.desc())
        .limit(limit).offset(offset)
    )).scalars().all()

    bars: list[float] = []
    items = []
    for t in items_raw:
        md = _meta_dict(t.meta)
        entry = md.get("entry") if isinstance(md.get("entry"), dict) else {}
        bars_held = None
        if tf and t.entry_time and t.exit_time:
            held = (t.exit_time - t.entry_time).total_seconds() / 60.0
            bars_held = round(held / tf, 1)
            bars.append(bars_held)
        items.append({
            "id": t.id, "strategy": engine, "exit": t.exit_reason or "",
            "ticker": t.ticker, "side": t.side,
            "entry_time": _iso(t.entry_time), "exit_time": _iso(t.exit_time),
            "entry_price": float(t.entry_price) if t.entry_price is not None else None,
            "exit_price": float(t.exit_price) if t.exit_price is not None else None,
            "exit_reason": t.exit_reason, "bars_held": bars_held,
            "net_pnl": round(float(t.net_pnl or 0.0), 4),
            "sl_price": float(t.stop_loss) if t.stop_loss is not None else None,
            "tp_price": float(t.take_profit) if t.take_profit is not None else None,
            "mae_atr": None, "mfe_atr": None,
            "session": session_bucket(t.entry_time),
            "regime_adx": md.get("entry_regime") or md.get("regime"),
            "er_in": None,
            "entry_reason": t.entry_reason or entry.get("reason"),
        })
    if bars:
        summary["bars_avg"] = round(sum(bars) / len(bars), 1)

    return {"name": name, "total": total, "limit": limit, "offset": offset,
            "summary": summary, "items": items}
