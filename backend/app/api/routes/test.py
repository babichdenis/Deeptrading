from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.engine.models import Candle as EngineCandle
from app.models.candle import Candle
from app.models.instrument import Instrument
from app.services.candle_cache import ensure_candles
from app.services.ceiling import (
    buy_and_hold,
    ceiling_per_candle,
    predictability,
    simulate_perfect_swing,
)
from app.services.ensemble import compute_ensemble, request_hash
from app.services.tinvest import INTERVAL_NAMES

router = APIRouter(prefix="/api/v1/test", tags=["test"])


class MaxProfitRequest(BaseModel):
    figi: str = "BBG008F2T3T2"
    interval_name: str = "1min"
    days: int = Field(30, ge=1, le=365)
    limit: int = Field(5000, ge=100, le=50000)
    threshold_pct: float = Field(0.5, ge=0.05, le=5.0)
    fee_rate_pct: float = Field(0.05, ge=0.0, le=1.0)
    capital: float = Field(100_000, ge=1000, le=100_000_000)
    from_ts: str | None = Field(None, description="ISO начало периода (для видимого окна графика)")
    to_ts: str | None = Field(None, description="ISO конец периода")


@router.post("/max-profit")
async def max_profit(req: MaxProfitRequest, db: AsyncSession = Depends(get_db)) -> dict:
    interval = INTERVAL_NAMES.get(req.interval_name)
    if interval is None:
        raise HTTPException(400, f"Unknown interval. Available: {', '.join(INTERVAL_NAMES)}")

    instrument = await db.scalar(select(Instrument).where(Instrument.figi == req.figi))
    if not instrument:
        raise HTTPException(404, "Instrument not found, call /api/instruments/sync first")

    stmt = (
        select(Candle)
        .where(Candle.figi == req.figi, Candle.interval == int(getattr(interval, "value", interval)))
        .order_by(Candle.ts.desc())
    )
    if req.from_ts:
        stmt = stmt.where(Candle.ts >= datetime.fromisoformat(req.from_ts.replace("Z", "+00:00")))
    if req.to_ts:
        stmt = stmt.where(Candle.ts <= datetime.fromisoformat(req.to_ts.replace("Z", "+00:00")))
    stmt = stmt.limit(req.limit)
    rows = (await db.execute(stmt)).scalars().all()
    candles = [
        {
            "ts": c.ts.isoformat(),
            "open": float(c.open),
            "high": float(c.high),
            "low": float(c.low),
            "close": float(c.close),
            "volume": c.volume,
        }
        for c in reversed(rows)
    ]
    if not candles:
        return {"ticker": instrument.ticker, "name": instrument.name, "bars": 0, "error": "no candles"}

    lot = instrument.lot
    min_move = req.threshold_pct / 100.0
    fee = req.fee_rate_pct / 100.0
    slippage = 0.005 / max(float(candles[-1]["close"]), 1e-9)  # 1 тик от цены

    ceiling = ceiling_per_candle(candles, fee, lot, req.capital)
    intraday = simulate_perfect_swing(candles, fee, req.capital, lot, min_move, slippage, multiday=False)
    multiday = simulate_perfect_swing(candles, fee, req.capital, lot, min_move, slippage, multiday=True)
    bh = buy_and_hold(candles, fee, req.capital, lot)
    pred = predictability(candles, min_move)

    # equity-кривая идеального свинг-трейдера (мультиднев) по времени выхода
    equity = []
    cum = 0.0
    for t in multiday["trades"]:
        cum += t["pnl"]
        equity.append({"ts": t["exit_ts"], "equity": round(cum + req.capital, 2)})

    return {
        "ticker": instrument.ticker,
        "name": instrument.name,
        "figi": req.figi,
        "interval": req.interval_name,
        "bars": len(candles),
        "lot": lot,
        "from": candles[0]["ts"],
        "to": candles[-1]["ts"],
        "params": {
            "threshold_pct": req.threshold_pct,
            "fee_rate_pct": req.fee_rate_pct,
            "capital": req.capital,
            "slippage_tick": round(slippage * 100, 4),
        },
        "candles": candles,
        "ceiling_1lot": ceiling,
        "perfect_intraday": {"pnl": intraday["pnl"], "trades": len(intraday["trades"])},
        "perfect_multiday": {"pnl": multiday["pnl"], "trades": len(multiday["trades"])},
        "buy_hold": bh,
        "trades": multiday["trades"],
        "equity": equity,
        "predictability": pred,
    }


class EnsembleRequest(BaseModel):
    figi: str = "BBG008F2T3T2"
    days: int = Field(3, ge=1, le=30)
    capital: float = Field(100_000, ge=1000, le=100_000_000)
    lot: int = Field(10, ge=1, le=1000)
    use_all_setups: bool = False
    drop_useless: bool = False
    bias: dict = Field(default_factory=lambda: {"tf": "hour", "period": 50})
    setups: list[dict] = Field(default_factory=lambda: [
        {"strategy_id": "rsi_reversal", "tf": "5min", "params": {}},
        {"strategy_id": "bollinger_reclaim", "tf": "5min", "params": {}},
    ])
    quorum: int = Field(2, ge=1, le=5)
    entry: dict = Field(default_factory=lambda: {"tf": "1min", "lookback": 1})
    entry_tf: str = Field("1min", pattern="^(1min|5min)$")
    entry_window_min: int = Field(15, ge=1, le=240)
    min_hold_bars: int = Field(0, ge=0, le=100)
    same_side_reentry_cooldown_bars: int = Field(0, ge=0, le=1440)
    exit_confirm_window_bars: int = Field(0, ge=0, le=1440)
    opposite_hold: bool = False
    confirm_flip: bool = False
    bias_mode: str = Field("veto", pattern="^(veto|info|strict_ct)$")
    exit_policy: dict = Field(default_factory=lambda: {
        "id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2}})
    oracle: dict = Field(default_factory=lambda: {"threshold_pct": 0.5, "fee_rate_pct": 0.05})
    regime: dict = Field(default_factory=lambda: {"tf": "5min"})
    adaptive: list[dict] = Field(default_factory=list)
    session: dict = Field(default_factory=lambda: {"overnight": False})
    from_ts: str | None = Field(None, description="ISO начало периода")
    to_ts: str | None = Field(None, description="ISO конец периода")


@router.post("/ensemble")
async def ensemble(req: EnsembleRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Ансамбль ролей (bias → setups+quorum → entry → exits) + сравнение с оракулом.

    MCP: детерминированный результат, request_hash в meta, setup-сигналы кэшируются.
    """
    instrument = await db.scalar(select(Instrument).where(Instrument.figi == req.figi))
    if not instrument:
        raise HTTPException(404, "Instrument not found, call /api/instruments/sync first")

    await ensure_candles(db, req.figi, "1min", req.days)
    interval_value = int(getattr(INTERVAL_NAMES["1min"], "value", INTERVAL_NAMES["1min"]))
    stmt = select(Candle).where(Candle.figi == req.figi, Candle.interval == interval_value)
    if req.from_ts:
        stmt = stmt.where(Candle.ts >= datetime.fromisoformat(req.from_ts.replace("Z", "+00:00")))
    if req.to_ts:
        stmt = stmt.where(Candle.ts <= datetime.fromisoformat(req.to_ts.replace("Z", "+00:00")))
    rows = (await db.execute(stmt.order_by(Candle.ts))).scalars().all()
    candles = [EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high),
                            low=float(r.low), close=float(r.close), volume=float(r.volume))
               for r in rows]

    body = req.model_dump()
    result = compute_ensemble(candles, body)
    result.setdefault("meta", {})["ticker"] = instrument.ticker
    result.setdefault("meta", {})["name"] = instrument.name
    result["meta"]["request_hash"] = request_hash(body)
    return result


class EnsembleSweepRequest(BaseModel):
    figis: list[str] = Field(default_factory=lambda: ["BBG008F2T3T2", "BBG004731032"])
    days: int = Field(3, ge=1, le=60)
    capital: float = Field(100_000, ge=1000, le=100_000_000)
    lot: int = Field(10, ge=1, le=1000)
    price_min: float = Field(1.0, ge=0.01)
    price_max: float = Field(10_000.0, ge=0.01)
    cooldowns: list[int] = Field(default_factory=lambda: [0, 5, 15, 30, 60])
    exit_confirms: list[int] = Field(default_factory=lambda: [0, 3])
    use_all_setups: bool = True
    drop_useless: bool = True
    quorum: int = Field(2, ge=1, le=5)


@router.post("/ensemble-sweep")
async def ensemble_sweep(req: EnsembleSweepRequest, db: AsyncSession = Depends(get_db)) -> dict:
    """Матрица FIGI × cooldown × confirmed_exit (компактный результат для сравнения).

    Цена акции берётся из последней закрытой свечи; figi вне [price_min, price_max]
    отбрасываются с указанием причины. net сравниваем по одинаковому капиталу.
    """
    cells: list[dict] = []
    filtered_out: list[dict] = []
    interval_value = int(getattr(INTERVAL_NAMES["1min"], "value", INTERVAL_NAMES["1min"]))
    for figi in req.figis:
        instrument = await db.scalar(select(Instrument).where(Instrument.figi == figi))
        if not instrument:
            filtered_out.append({"figi": figi, "ticker": "?", "price": None,
                                 "reason": "instrument_not_found"})
            continue
        await ensure_candles(db, figi, "1min", req.days)
        rows = (await db.execute(
            select(Candle).where(Candle.figi == figi, Candle.interval == interval_value)
            .order_by(Candle.ts.desc()).limit(1)
        )).scalars().all()
        price = float(rows[0].close) if rows else None
        if price is None or not (req.price_min <= price <= req.price_max):
            filtered_out.append({"figi": figi, "ticker": instrument.ticker,
                                 "price": price, "reason": "price_out_of_range"})
            continue
        all_rows = (await db.execute(
            select(Candle).where(Candle.figi == figi, Candle.interval == interval_value)
            .order_by(Candle.ts)
        )).scalars().all()
        candles = [EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high),
                                low=float(r.low), close=float(r.close), volume=float(r.volume))
                   for r in all_rows]
        for cd in req.cooldowns:
            for ec in req.exit_confirms:
                body = {
                    "figi": figi, "days": req.days, "capital": req.capital, "lot": req.lot,
                    "use_all_setups": req.use_all_setups, "drop_useless": req.drop_useless,
                    "quorum": req.quorum,
                    "same_side_reentry_cooldown_bars": cd,
                    "exit_confirm_window_bars": ec,
                }
                try:
                    res = compute_ensemble(candles, body)
                except Exception as e:  # noqa: BLE001 — ячейка матрицы не должна валить весь sweep
                    cells.append({"figi": figi, "ticker": instrument.ticker, "price": price,
                                  "cooldown": cd, "exit_confirm": ec, "error": str(e)})
                    continue
                if "error" in res:
                    cells.append({"figi": figi, "ticker": instrument.ticker, "price": price,
                                  "cooldown": cd, "exit_confirm": ec, "error": res["error"]})
                    continue
                st = res["static"]
                e = st["economic"]
                cf = st.get("counterfactual_reentries", {})
                cells.append({
                    "figi": figi, "ticker": instrument.ticker, "price": price,
                    "cooldown": cd, "exit_confirm": ec,
                    "trades": e["trades"], "episodes": st["episodes"]["unique_episodes"],
                    "gross": e["gross"], "costs": e["costs"], "net": e["net"],
                    "pf": e["profit_factor"], "win_rate_pct": e["win_rate_pct"],
                    "break_even_median": e["break_even_by_trade"]["median"] if e.get("break_even_by_trade") else None,
                    "reentry_rejected": st["funnel"].get("reentry_rejected", 0),
                    "counterfactual": {"n": cf.get("n", 0), "mean_net": cf.get("mean_net"),
                                       "median_net": cf.get("median_net"),
                                       "wins_pct": cf.get("wins_pct")},
                })
    return {"cells": cells, "filtered_out": filtered_out,
            "tf_minutes": 1, "cooldown_minutes": {cd: cd for cd in req.cooldowns},
            "note": "cooldown в барах 1m = минуты; net по одинаковому капиталу на позицию"}
