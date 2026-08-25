from __future__ import annotations

import asyncio
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.bot.events import EventLog
from app.bot.feed import STEP_SEC, CandleFeed
from app.bot.paper_broker import PaperBroker
from app.bot.risk import RiskSnapshot
from app.bot.session import session_state
from app.config import get_settings
from app.database import SessionLocal
from app.engine.costs import CostModel
from app.engine.exits import FixedSlTpPolicy, intrabar_exit
from app.engine.models import Candle as EngineCandle, PositionState
from app.engine.policies import SignalPolicy
from app.engine.strategies import build_strategy
from app.models.instrument import Instrument
from app.models.paper import PaperTrade
from app.services.tinvest import INTERVAL_NAMES

MAX_BUFFER = 300
DAILY_PNL_TTL = timedelta(seconds=30)


@dataclass
class BotConfig:
    strategy_id: str = "rsi_reversal"
    params: dict = field(default_factory=dict)
    interval_name: str = "5min"
    top_n: int = 6
    qty_per_trade: int = 1
    stop_pct: float = 0.01
    target_pct: float = 0.02
    allow_short: bool = False
    initial_cash: float = 100_000.0
    daily_loss_limit: float = 1000.0


@dataclass
class BotOrder:
    id: str
    figi: str
    ticker: str
    action: str
    side: str
    qty: int
    status: str = "SUBMITTED"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    filled_at: datetime | None = None
    price: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "figi": self.figi,
            "ticker": self.ticker,
            "action": self.action,
            "side": self.side,
            "qty": self.qty,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "price": round(self.price, 6) if self.price is not None else None,
        }


def _new_order_id() -> str:
    return f"paper-{uuid.uuid4().hex[:12]}"


class PaperBotRuntime:
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.startup_task: asyncio.Task | None = None
        self.running = False
        self.starting = False
        self.mode = "—"
        self.started_at: datetime | None = None
        self.error: str | None = None
        self.config = BotConfig()
        self.broker = PaperBroker(SessionLocal)
        self.events = EventLog()
        self.strategies: dict[str, object] = {}
        self.buffers: dict[str, deque] = {}
        self.tickers: dict[str, str] = {}
        self.pending_orders: dict[str, BotOrder] = {}
        self.orders: deque[BotOrder] = deque(maxlen=200)
        self.universe: list[dict] = []
        self.candles_seen = 0
        self.signals_seen = 0
        self.entries_paused = False
        self.last_candle_ts: datetime | None = None
        self.data_source = "—"
        self.feed: CandleFeed | None = None
        self._daily_pnl_cache: tuple[datetime, float] | None = None

    @property
    def status(self) -> dict:
        step = STEP_SEC.get(self.config.interval_name, 300)
        if self.last_candle_ts is None:
            health = "NO_DATA"
        elif datetime.now(timezone.utc) - self.last_candle_ts <= timedelta(seconds=3 * step):
            health = "HEALTHY"
        else:
            health = "STALE"
        risk = self.risk_snapshot()
        return {
            "running": self.running,
            "starting": self.starting,
            "mode": self.mode,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "error": self.error,
            "config": {
                "strategy_id": self.config.strategy_id,
                "interval_name": self.config.interval_name,
                "top_n": self.config.top_n,
                "qty": self.config.qty_per_trade,
                "stop_pct": self.config.stop_pct,
                "target_pct": self.config.target_pct,
            },
            "universe": self.universe,
            "candles_seen": self.candles_seen,
            "signals_seen": self.signals_seen,
            "pending_orders": len(self.pending_orders),
            "session": session_state(),
            "data": {
                "health": health,
                "source": self.data_source,
                "last_candle_ts": self.last_candle_ts.isoformat() if self.last_candle_ts else None,
            },
            "risk": {
                "state": risk.state,
                "daily_pnl": round(risk.daily_pnl, 2),
                "daily_loss_limit": risk.daily_loss_limit,
                "entries_paused": risk.entries_paused,
            },
        }

    def risk_snapshot(self) -> RiskSnapshot:
        return RiskSnapshot(
            daily_pnl=self.daily_pnl_cached(),
            daily_loss_limit=self.config.daily_loss_limit,
            entries_paused=self.entries_paused,
        )

    def daily_pnl_cached(self) -> float:
        now = datetime.now(timezone.utc)
        if self._daily_pnl_cache and now - self._daily_pnl_cache[0] < DAILY_PNL_TTL:
            return self._daily_pnl_cache[1]
        self._daily_pnl_cache = (now, 0.0)
        return self._daily_pnl_cache[1]

    async def refresh_daily_pnl(self) -> float:
        msk_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3)))
        day_start = msk_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        async with SessionLocal() as db:
            res = await db.execute(
                select(PaperTrade.net_pnl).where(PaperTrade.exit_time >= day_start)
            )
            total = sum(float(v) for v in res.scalars())
        self._daily_pnl_cache = (datetime.now(timezone.utc), total)
        return total

    async def start(self, cfg: BotConfig) -> dict:
        if self.running or self.starting:
            raise RuntimeError("bot already running")
        self.config = cfg
        self.error = None
        self.candles_seen = 0
        self.signals_seen = 0
        self.strategies = {}
        self.buffers = {}
        self.pending_orders = {}
        self.tickers = {}
        self.entries_paused = False
        self.last_candle_ts = None
        await self.broker.ensure_account(cfg.initial_cash)
        await self.refresh_daily_pnl()
        self.starting = True
        self.startup_task = asyncio.create_task(self._startup(cfg))
        return {"started": True, "async": True}

    async def _startup(self, cfg: BotConfig) -> None:
        try:
            async with SessionLocal() as db:
                rows = await db.execute(select(Instrument.ticker, Instrument.figi))
                figi_by_ticker = dict(rows.all())
                tick_rows = await db.execute(select(Instrument.figi, Instrument.ticker))
                self.tickers = dict(tick_rows.all())

            from app.bot.universe import select_volatile_universe

            async with SessionLocal() as db:
                self.universe = await select_volatile_universe(
                    db, figi_by_ticker, top_n=cfg.top_n
                )
            if not self.universe:
                raise RuntimeError("universe is empty")

            from app.engine.models import Candle as EC

            for u in self.universe:
                proto = build_strategy(cfg.strategy_id, cfg.params)
                self.strategies[u["figi"]] = proto
                buf = deque(maxlen=MAX_BUFFER)
                async with SessionLocal() as db:
                    from app.services.candle_cache import ensure_candles
                    await ensure_candles(db, u["figi"], cfg.interval_name, days=7)
                    from app.services.signals import _load_candles as _lc
                    interval_value = self._interval_value()
                    candles = await _lc(db, u["figi"], interval_value)
                    for row in candles[-MAX_BUFFER:]:
                        buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                      low=row.low, close=row.close, volume=row.volume))
                self.buffers[u["figi"]] = buf

            self.running = True
            self.started_at = datetime.now(timezone.utc)
            self.task = asyncio.create_task(self._run())
            self.events.log("BOT_STARTED", reason=f"{cfg.strategy_id}@{cfg.interval_name} "
                                                 f"universe={len(self.universe)} limit={cfg.daily_loss_limit}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)[:300]
            self.events.log("ERROR", reason=self.error)
        finally:
            self.starting = False

    async def stop(self) -> dict:
        was_running = self.running
        self.running = False
        if self.feed is not None:
            self.feed.request_stop()
        for t in (self.startup_task, self.task):
            if t and not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self.startup_task = None
        self.task = None
        self.starting = False
        self.mode = "—"
        self.events.log("BOT_STOPPED", reason="manual" if was_running else "was_not_running")
        return {"stopped": True, "was_running": was_running}

    async def set_entries_paused(self, paused: bool) -> dict:
        prev = self.entries_paused
        self.entries_paused = paused
        if paused != prev:
            self.events.log("PAUSE_NEW_ENTRIES" if paused else "RESUME_NEW_ENTRIES",
                            reason="manual")
        return {"entries_paused": self.entries_paused}

    async def cancel_pending(self) -> dict:
        cancelled = []
        for figi, order in list(self.pending_orders.items()):
            order.status = "CANCELLED"
            cancelled.append(order.to_dict())
            del self.pending_orders[figi]
            self.events.log("ORDER_CANCELLED", figi=figi, ticker=order.ticker,
                            reason="manual", order_id=order.id)
        return {"cancelled": len(cancelled), "orders": cancelled}

    async def close_all(self) -> dict:
        positions = await self.broker.positions()
        closed = []
        for p in positions:
            buf = self.buffers.get(p.figi)
            price = float(buf[-1].close) if buf else float(p.entry_price)
            trade = await self.broker.close_position(p.figi, price, "kill_switch_close_all")
            closed.append({"figi": p.figi, "ticker": p.ticker,
                           "price": round(price, 6),
                           "net_pnl": float(trade.net_pnl) if trade else None})
            self.events.log("POSITION_CLOSED", figi=p.figi, ticker=p.ticker,
                            reason="kill_switch_close_all", net_pnl=closed[-1]["net_pnl"])
        self.events.log("CLOSE_ALL", reason=f"closed={len(closed)}")
        return {"closed": len(closed), "positions": closed}

    def _interval_value(self) -> int:
        interval = INTERVAL_NAMES[self.config.interval_name]
        return int(getattr(interval, "value", interval))

    async def _run(self) -> None:
        settings = get_settings()
        feed = CandleFeed(settings.tinkoff_token, self.config.interval_name,
                          [u["figi"] for u in self.universe])
        self.feed = feed
        exited = "stream_exhausted"
        try:
            async for candle in feed.stream():
                if not self.running:
                    exited = "running_flag_false"
                    break
                self.mode = feed.mode
                await self._process_candle(candle)
        except asyncio.CancelledError:
            exited = "cancelled"
            raise
        except Exception as e:
            self.error = str(e)[:300]
            exited = f"exception: {self.error[:80]}"
            self.events.log("ERROR", reason=self.error)
        else:
            self.mode = feed.mode
        finally:
            self.running = False
            self.events.log("BOT_LOOP_EXITED", reason=exited)

    async def _process_candle(self, c) -> None:
        figi = c.figi
        strategy = self.strategies.get(figi)
        buffer = self.buffers.get(figi)
        if strategy is None or buffer is None:
            return

        await self._execute_pending(figi, c)

        buffer.append(EngineCandle(ts=c.ts, open=c.open, high=c.high,
                                   low=c.low, close=c.close, volume=c.volume))
        self.candles_seen += 1
        self.last_candle_ts = c.ts
        self.data_source = self.mode

        pos = await self.broker.get_position(figi)
        if pos is not None:
            state = PositionState.LONG if pos.side == "LONG" else PositionState.SHORT
            stop = float(pos.stop_loss) if pos.stop_loss is not None else None
            target = float(pos.take_profit) if pos.take_profit is not None else None
            price, reason = intrabar_exit(c, state, stop, target)
            if price is not None:
                trade = await self.broker.close_position(figi, price, reason)
                self.events.log("POSITION_CLOSED", figi=figi, ticker=pos.ticker,
                                reason=reason, net_pnl=float(trade.net_pnl) if trade else None)
                await self._check_circuit_breaker()

        sig = strategy.on_bar(list(buffer))
        if sig is None:
            return
        self.signals_seen += 1
        ticker = self.tickers.get(figi, "")
        self.events.log("SIGNAL_CREATED", figi=figi, ticker=ticker,
                        side=sig.side.value)

        pos_now = await self.broker.get_position(figi)
        state_now = PositionState.LONG if (pos_now and pos_now.side == "LONG") else (
            PositionState.SHORT if (pos_now and pos_now.side == "SHORT") else PositionState.FLAT
        )
        bars_held = 0
        policy = SignalPolicy()
        action, note = policy.decide(sig, state_now, bars_held)

        from app.engine.models import DecisionAction
        if action is DecisionAction.ACCEPT_ENTRY:
            if self.entries_paused:
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="ENTRIES_PAUSED")
                return
            if sig.side.value == "SELL" and not self.config.allow_short:
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason="SHORT_DISABLED")
                return
            risk = self.risk_snapshot()
            if not risk.entries_allowed():
                self.events.log("SIGNAL_REJECTED", figi=figi, ticker=ticker,
                                reason=f"RISK_{risk.state}",
                                daily_pnl=risk.daily_pnl)
                return
            await self._submit_order(figi, ticker, "open", sig.side.value)
        elif action is DecisionAction.ACCEPT_EXIT:
            await self._submit_order(figi, ticker, "close", sig.side.value)
        else:
            self.events.log("SIGNAL_IGNORED", figi=figi, ticker=ticker,
                            reason=str(note))

    async def _submit_order(self, figi: str, ticker: str, action: str, side: str) -> None:
        order = BotOrder(
            id=_new_order_id(),
            figi=figi,
            ticker=ticker,
            action=action,
            side=side,
            qty=self.config.qty_per_trade,
        )
        self.pending_orders[figi] = order
        self.orders.append(order)
        self.events.log("ORDER_SUBMITTED", figi=figi, ticker=ticker,
                        order_id=order.id, action=action, side=side)

    async def _execute_pending(self, figi: str, c) -> None:
        order = self.pending_orders.pop(figi, None)
        if order is None or order.status == "CANCELLED":
            return
        cfg = self.config
        if order.action == "close":
            trade = await self.broker.close_position(figi, c.open, "signal_exit")
            order.status = "FILLED"
            order.filled_at = datetime.now(timezone.utc)
            order.price = c.open
            self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                            order_id=order.id, price=c.open, action="close",
                            net_pnl=float(trade.net_pnl) if trade else None)
            return
        from app.engine.models import Side
        side = Side(order.side)
        exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
        plan = exit_policy.plan_entry(side, c.open, [])
        await self.broker.open_position(
            figi=figi,
            ticker=order.ticker,
            side=order.side,
            qty=cfg.qty_per_trade,
            price=c.open,
            stop_loss=round(plan.stop_loss, 6) if plan.stop_loss is not None else None,
            take_profit=round(plan.take_profit, 6) if plan.take_profit is not None else None,
            strategy_id=cfg.strategy_id,
        )
        order.status = "FILLED"
        order.filled_at = datetime.now(timezone.utc)
        order.price = c.open
        self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                        order_id=order.id, price=c.open, action="open")
        self.events.log("POSITION_OPENED", figi=figi, ticker=order.ticker,
                        side=order.side, qty=cfg.qty_per_trade, entry_price=c.open)

    async def _check_circuit_breaker(self) -> None:
        risk = RiskSnapshot(daily_pnl=await self.refresh_daily_pnl(),
                            daily_loss_limit=self.config.daily_loss_limit,
                            entries_paused=self.entries_paused)
        if risk.state == "LOSS_LIMIT" and not self.entries_paused:
            self.entries_paused = True
            self.events.log("CIRCUIT_BREAKER_TRIGGERED",
                            reason="LOSS_LIMIT", daily_pnl=risk.daily_pnl)


runtime = PaperBotRuntime()
