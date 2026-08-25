from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.runtime import BotConfig, runtime
from app.database import get_db, SessionLocal
from app.engine.strategies import ParamValidationError, build_strategy
from app.models.paper import PaperAccount, PaperPosition, PaperTrade

router = APIRouter(prefix="/api/v1/bot", tags=["bot"])


class StartRequest(BaseModel):
    strategy_id: str = "rsi_reversal"
    params: dict = Field(default_factory=dict)
    interval_name: str = "5min"
    top_n: int = 6
    qty_per_trade: int = 1
    stop_pct: float = 0.01
    target_pct: float = 0.02
    allow_short: bool = False
    initial_cash: float = 100_000.0
    daily_loss_limit: float = Field(1000.0, ge=0)


@router.post("/start")
async def bot_start(req: StartRequest) -> dict:
    if req.interval_name not in ("1min", "5min", "10min", "15min"):
        raise HTTPException(400, "бот работает на интрадей-таймфреймах 1min/5min/10min/15min")
    try:
        build_strategy(req.strategy_id, req.params)
    except ParamValidationError as e:
        raise HTTPException(400, str(e))
    cfg = BotConfig(
        strategy_id=req.strategy_id,
        params=req.params,
        interval_name=req.interval_name,
        top_n=max(2, min(req.top_n, 15)),
        qty_per_trade=max(1, req.qty_per_trade),
        stop_pct=req.stop_pct,
        target_pct=req.target_pct,
        allow_short=req.allow_short,
        initial_cash=req.initial_cash,
        daily_loss_limit=req.daily_loss_limit,
    )
    try:
        result = await runtime.start(cfg)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"status": "STARTING", **result}


@router.post("/stop")
async def bot_stop() -> dict:
    return await runtime.stop()


class PauseRequest(BaseModel):
    paused: bool


@router.post("/pause")
async def bot_pause(req: PauseRequest) -> dict:
    return await runtime.set_entries_paused(req.paused)


@router.post("/orders/cancel-pending")
async def bot_cancel_pending() -> dict:
    return await runtime.cancel_pending()


@router.get("/orders")
async def bot_orders(limit: int = 50) -> dict:
    items = list(runtime.orders)[-limit:]
    return {"count": len(items), "orders": [o.to_dict() for o in reversed(items)]}


@router.get("/events")
async def bot_events(limit: int = 100) -> dict:
    return {"count": min(limit, len(runtime.events)), "events": runtime.events.latest(limit)}


@router.post("/positions/close-all")
async def bot_close_all() -> dict:
    return await runtime.close_all()


@router.get("/status")
async def bot_status() -> dict:
    status = runtime.status
    positions = await runtime.broker.positions()
    async with SessionLocal() as db:
        acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == "default"))
        cash = float(acc.cash) if acc else 0.0
        initial = float(acc.initial_cash) if acc else 0.0

    last_prices: dict[str, float] = {}
    for p in positions:
        last_prices[p.figi] = float(p.entry_price)

    market_value = sum(float(p.entry_price) * p.qty for p in positions)
    equity = cash + market_value
    pnl = equity - initial if initial else 0.0

    if runtime.running:
        await runtime.refresh_daily_pnl()
        risk = runtime.risk_snapshot()
        status["risk"] = {
            "state": risk.state,
            "daily_pnl": round(risk.daily_pnl, 2),
            "daily_loss_limit": risk.daily_loss_limit,
            "entries_paused": risk.entries_paused,
        }

    return {
        **status,
        "portfolio": {
            "cash": round(cash, 2),
            "initial_cash": round(initial, 2),
            "market_value": round(market_value, 2),
            "equity": round(equity, 2),
            "pnl": round(pnl, 2),
            "positions_open": len(positions),
        },
    }


@router.get("/positions")
async def bot_positions(db: AsyncSession = Depends(get_db)) -> dict:
    res = await db.execute(select(PaperPosition))
    items = [
        {
            "figi": p.figi,
            "ticker": p.ticker,
            "side": p.side,
            "qty": p.qty,
            "entry_price": float(p.entry_price),
            "entry_time": p.entry_time.isoformat(),
            "stop_loss": float(p.stop_loss) if p.stop_loss else None,
            "take_profit": float(p.take_profit) if p.take_profit else None,
            "strategy_id": p.strategy_id,
        }
        for p in res.scalars()
    ]
    return {"count": len(items), "positions": items}


@router.get("/trades")
async def bot_trades(limit: int = 50) -> dict:
    trades = await runtime.broker.trades_history(min(limit, 500))
    return {
        "count": len(trades),
        "trades": [
            {
                "figi": t.figi,
                "ticker": t.ticker,
                "side": t.side,
                "qty": t.qty,
                "entry_time": t.entry_time.isoformat(),
                "entry_price": float(t.entry_price),
                "exit_time": t.exit_time.isoformat(),
                "exit_price": float(t.exit_price),
                "net_pnl": float(t.net_pnl),
                "commission": float(t.commission),
                "exit_reason": t.exit_reason,
                "strategy_id": t.strategy_id,
            }
            for t in trades
        ],
    }


@router.post("/reset")
async def bot_reset(initial_cash: float = 100_000.0) -> dict:
    if runtime.running or runtime.starting:
        raise HTTPException(409, "остановите бота перед сбросом")
    await runtime.broker.reset(initial_cash)
    return {"reset": True}
