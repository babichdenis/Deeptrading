from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.runtime import BotConfig, runtime

_sandbox_broker = None

def _get_sandbox_broker():
    global _sandbox_broker
    if _sandbox_broker is None:
        from app.bot.live_broker import LiveBroker
        _sandbox_broker = LiveBroker(SessionLocal)
    return _sandbox_broker
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
    mode: str = "paper"  # paper | sandbox | live
    use_ensemble: bool = False
    ensemble_capital: float = 2000.0
    ensemble_quorum: int = 2
    ensemble_session: str = "main"


@router.post("/start")
async def bot_start(req: StartRequest) -> dict:
    if req.interval_name not in ("1min", "5min", "10min", "15min"):
        raise HTTPException(400, "бот работает на интрадей-таймфреймах 1min/5min/10min/15min")
    if not req.use_ensemble:
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
        mode=req.mode if req.mode in ("paper", "sandbox", "live") else "paper",
        use_ensemble=req.use_ensemble,
        ensemble_capital=req.ensemble_capital,
        ensemble_quorum=max(1, req.ensemble_quorum),
        ensemble_session=req.ensemble_session if req.ensemble_session in ("main", "all") else "main",
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


@router.post("/positions/close")
async def bot_close_position(figi: str) -> dict:
    import asyncio as _aio
    positions = await runtime.broker.positions()
    pos = next((p for p in positions if p.figi == figi), None)
    if not pos:
        from app.api.routes.sandbox import _get_portfolio, _q
        p = await _aio.to_thread(_get_portfolio)
        bbg = figi
        from app.api.routes.sandbox import _resolve_bbg
        sip = next((x for x in p.positions if x.figi == figi), None)
        if sip is None:
            tcs = runtime.tcs_to_bbg.get(figi)
            if tcs:
                sip = next((x for x in p.positions if x.figi == tcs), None)
        if sip is None:
            for x in p.positions:
                resolved = await _resolve_bbg(x.figi)
                if resolved == figi:
                    sip = x
                    break
        if sip is None:
            raise HTTPException(404, f"позиция {figi} не найдена")
        cur = _q(sip.current_price)
        lb = _get_sandbox_broker()
        try:
            trade = await lb.close_position(figi, cur, "manual_close")
        except Exception as e:
            raise HTTPException(502, f"ошибка закрытия: {e}")
        runtime._held.discard(figi)
        runtime._held.discard(runtime.tcs_to_bbg.get(figi, figi))
        return {"closed": True, "figi": figi, "ticker": figi[:12], "price": float(cur)}
    bbg = runtime.tcs_to_bbg.get(figi, figi)
    buf = runtime.buffers.get(bbg) or runtime.buffers.get(figi)
    price = float(buf[-1].close) if buf else float(pos.entry_price)
    try:
        trade = await runtime.broker.close_position(figi, price, "manual_close")
    except Exception as e:
        raise HTTPException(502, f"ошибка закрытия: {e}")
    runtime._held.discard(figi)
    runtime._held.discard(bbg)
    return {"closed": True, "figi": figi, "ticker": pos.ticker, "price": price}


@router.get("/logconfig")
async def bot_logconfig_get() -> dict:
    return {"log_candles": runtime.log_candles, "buffer_max": runtime._live_logs.maxlen}


@router.post("/logconfig")
async def bot_logconfig_set(payload: dict) -> dict:
    if "log_candles" in payload:
        runtime.log_candles = bool(payload["log_candles"])
    return {"log_candles": runtime.log_candles}


@router.post("/logs/clear")
async def bot_logs_clear() -> dict:
    runtime._live_logs.clear()
    return {"cleared": True}


@router.get("/logs")
async def bot_logs(limit: int = 200) -> dict:
    logs = list(runtime._live_logs)[-limit:]
    return {"logs": logs, "count": len(logs)}


@router.get("/status")
async def bot_status() -> dict:
    status = runtime.status

    if runtime.running:
        try:
            risk = runtime.risk_snapshot()
            status["risk"] = {
                "state": risk.state,
                "daily_pnl": round(risk.daily_pnl, 2),
                "daily_loss_limit": risk.daily_loss_limit,
                "entries_paused": risk.entries_paused,
            }
        except Exception:
            pass

    return {
        **status,
        "portfolio": {
            "cash": 0,
            "initial_cash": 10000,
            "market_value": 0,
            "equity": 0,
            "pnl": 0,
            "positions_open": len(runtime.buffers),
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
