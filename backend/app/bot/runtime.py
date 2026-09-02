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
from app.bot.live_broker import LiveBroker
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
from app.services.signals import _load_candles as _lc
from app.services.tinvest import INTERVAL_NAMES

MAX_BUFFER = 300
ENSEMBLE_BUFFER = 100000
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
    mode: str = "paper"  # paper | sandbox | live
    use_ensemble: bool = False
    ensemble_capital: float = 2000.0
    ensemble_quorum: int = 2
    ensemble_session: str = "main"


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
        self.broker_mode = "paper"
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
        self._signal_busy: set[str] = set()
        self._held: set[str] = set()
        self._live_logs: deque[str] = deque(maxlen=400)
        self.log_candles = True
        self._last_candle_log_ts: float = 0.0

    def _log(self, msg: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._live_logs.append(f"[{ts}] {msg}")

    async def _st_open(self, figi, ticker, side, qty, price, sl, tp) -> None:
        from app.models.sandbox_trade import SandboxTrade
        try:
            async with SessionLocal() as db:
                db.add(SandboxTrade(
                    figi=figi, ticker=ticker, side=side, qty=int(qty),
                    entry_time=datetime.now(timezone.utc),
                    entry_price=float(price),
                    stop_loss=float(sl) if sl else None,
                    take_profit=float(tp) if tp else None,
                ))
                await db.commit()
        except Exception:
            pass

    async def _st_close(self, figi, exit_price, reason="", net=None) -> None:
        from app.models.sandbox_trade import SandboxTrade
        try:
            async with SessionLocal() as db:
                res = await db.execute(
                    select(SandboxTrade)
                    .where(SandboxTrade.figi == figi, SandboxTrade.exit_time.is_(None))
                    .order_by(SandboxTrade.entry_time.desc())
                    .limit(1)
                )
                row = res.scalar_one_or_none()
                if row is not None:
                    row.exit_time = datetime.now(timezone.utc)
                    row.exit_price = float(exit_price)
                    row.exit_reason = reason or ""
                    if net is not None:
                        row.net_pnl = float(net)
                    await db.commit()
        except Exception:
            pass

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
                "mode": self.config.mode,
                "use_ensemble": self.config.use_ensemble,
                "ensemble_capital": self.config.ensemble_capital,
                "ensemble_quorum": self.config.ensemble_quorum,
                "ensemble_session": self.config.ensemble_session,
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
        if cfg.use_ensemble:
            cfg.interval_name = "1min"
        self.error = None
        self.candles_seen = 0
        self.signals_seen = 0
        self.strategies = {}
        self.buffers = {}
        self.pending_orders = {}
        self.tickers = {}
        self.entries_paused = False
        self.last_candle_ts = None
        self.broker_mode = cfg.mode
        if cfg.mode == "sandbox" or cfg.mode == "live":
            self.broker = LiveBroker(SessionLocal)
            try:
                real_cash = await self.broker.cash()
                if real_cash and real_cash > 0:
                    cfg.initial_cash = real_cash
                    cfg.ensemble_capital = real_cash / max(2, cfg.top_n)
                    self._log(f"КАПИТАЛ со счёта: {real_cash:.0f} ₽ · на инструмент {cfg.ensemble_capital:.0f}")
            except Exception as e:
                self._log(f"КАПИТАЛ не получен: {str(e)[:80]}")
        else:
            self.broker = PaperBroker(SessionLocal)
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

            from sqlalchemy import text
            ii_rows = await db.execute(text(
                "SELECT figi, ticker FROM instrument_info"))
            self.bbg_to_tcs = {}
            self.tcs_to_bbg = {}
            for ii_figi, ii_ticker in ii_rows.all():
                bb = figi_by_ticker.get(ii_ticker)
                if bb and ii_figi:
                    self.bbg_to_tcs[bb] = ii_figi
                    self.tcs_to_bbg[ii_figi] = bb

            from app.bot.universe import select_all_tradeable, select_volatile_universe

            async with SessionLocal() as db:
                if cfg.use_ensemble:
                    self.universe = await select_all_tradeable(db)
                else:
                    self.universe = await select_volatile_universe(
                        db, figi_by_ticker, top_n=cfg.top_n
                    )
            if not self.universe:
                raise RuntimeError("universe is empty")
            for u in self.universe:
                if u.get("ticker"):
                    self.tickers.setdefault(u["figi"], u["ticker"])
                    self.tickers.setdefault(self.tcs_to_bbg.get(u["figi"], u["figi"]), u["ticker"])

            from app.engine.models import Candle as EC

            async def _load_one(u):
                if cfg.use_ensemble:
                    from app.bot.ensemble_strategy import EnsembleParams, EnsembleV4Strategy
                    proto = EnsembleV4Strategy(EnsembleParams(
                        figi=u["figi"],
                        lot=10,
                        capital=cfg.ensemble_capital,
                        quorum=cfg.ensemble_quorum,
                        session=cfg.ensemble_session,
                    ))
                else:
                    proto = build_strategy(cfg.strategy_id, cfg.params)
                buf = deque(maxlen=ENSEMBLE_BUFFER if cfg.use_ensemble else MAX_BUFFER)
                async with SessionLocal() as db:
                    if cfg.use_ensemble:
                        from app.bot.moex import ensure_moex_candles
                        await ensure_moex_candles(u["figi"], u.get("ticker", ""), days=10)
                        candles = await _lc(db, u["figi"], 1,
                                            date_from=datetime.now(timezone.utc) - timedelta(days=60))
                        for row in candles:
                            buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                          low=row.low, close=row.close, volume=row.volume))
                    else:
                        from app.services.candle_cache import ensure_candles
                        await ensure_candles(db, u["figi"], cfg.interval_name, days=7)
                        interval_value = self._interval_value()
                        candles = await _lc(db, u["figi"], interval_value,
                                            date_from=datetime.now(timezone.utc) - timedelta(days=60))
                        for row in candles[-MAX_BUFFER:]:
                            buf.append(EC(ts=row.ts, open=row.open, high=row.high,
                                          low=row.low, close=row.close, volume=row.volume))
                return u["figi"], proto, buf

            sem = asyncio.Semaphore(10)

            async def _load_bounded(u):
                async with sem:
                    return await _load_one(u)

            results = await asyncio.gather(*[_load_bounded(u) for u in self.universe])
            for figi, proto, buf in results:
                self.strategies[figi] = proto
                self.buffers[figi] = buf

            try:
                held_now = await self.broker.positions()
                self._held = {p.figi for p in held_now}
                if self._held:
                    self._log(f"ОТКРЫТО при старте: {len(self._held)} поз.")
            except Exception:
                self._held = set()
            self.running = True
            self._log("БОТ ЗАПУЩЕН")
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
        if self.config.use_ensemble:
            return 1
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
        figi = self.tcs_to_bbg.get(c.figi, c.figi)
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
        if self.log_candles:
            _now = datetime.now(timezone.utc).timestamp()
            if _now - self._last_candle_log_ts >= 3.0:
                self._last_candle_log_ts = _now
                self._log(f"СВЕЧА {figi[-6:]} o={c.open:.2f} h={c.high:.2f} l={c.low:.2f} c={c.close:.2f} v={c.volume}")

        _lookup = self.tcs_to_bbg.get(figi, figi)
        pos = await self.broker.get_position(_lookup) or await self.broker.get_position(figi)
        if pos is not None:
            state = PositionState.LONG if pos.side == "LONG" else PositionState.SHORT
            stop = float(pos.stop_loss) if pos.stop_loss is not None else None
            target = float(pos.take_profit) if pos.take_profit is not None else None
            price, reason = intrabar_exit(c, state, stop, target)
            if price is not None:
                trade = await self.broker.close_position(figi, price, reason)
                self._held.discard(figi)
                await self._st_close(figi, price, reason=reason,
                                      net=float(trade.net_pnl) if trade else None)
                pnl = float(trade.net_pnl) if trade else 0
                self._log(f"ВЫХОД {figi[-6:]} ({reason}) pnl={pnl:+.2f}")
                self.events.log("POSITION_CLOSED", figi=figi, ticker=pos.ticker,
                                reason=reason, net_pnl=float(trade.net_pnl) if trade else None)
                await self._check_circuit_breaker()

        if figi in self._signal_busy:
            return
        self._signal_busy.add(figi)
        try:
            sig = await asyncio.to_thread(strategy.on_bar, list(buffer))
        except Exception as e:
            self.events.log("SIGNAL_ERROR", figi=figi, reason=str(e)[:200])
            sig = None
        finally:
            self._signal_busy.discard(figi)
        if sig is None:
            return
        self.signals_seen += 1
        ticker = self.tickers.get(figi, "")
        self._log(f"СИГНАЛ {ticker} {sig.side.value} ({sig.kind})")
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
            if figi in self._held:
                self._log(f"ПРОПУСК ВХОДА {ticker}: поз. уже открыта")
                return
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
        cfg = self.config
        qty = cfg.qty_per_trade
        if action == "open" and cfg.use_ensemble:
            buf = self.buffers.get(figi)
            price = float(buf[-1].close) if buf else 0.0
            lot = 10
            for u in self.universe:
                if u.get("figi") == figi and u.get("lot"):
                    lot = int(u["lot"])
                    break
            budget = cfg.ensemble_capital
            if isinstance(self.broker, LiveBroker):
                try:
                    live_cash = await self.broker.cash()
                    budget = min(cfg.ensemble_capital, live_cash)
                except Exception:
                    pass
            lot_cost = price * lot
            if price <= 0 or lot <= 0 or lot_cost <= 0 or budget < lot_cost:
                self._log(f"ПРОПУСК СДЕЛКИ {ticker}: бюджет {budget:.0f} < стоимость лота {lot_cost:.0f}")
                return
            qty = max(1, int(budget / lot_cost))
        order = BotOrder(
            id=_new_order_id(),
            figi=figi,
            ticker=ticker,
            action=action,
            side=side,
            qty=qty,
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
            self._held.discard(figi)
            await self._st_close(figi, c.open, reason="signal_exit",
                                  net=float(trade.net_pnl) if trade else None)
            pnl = float(trade.net_pnl) if trade else 0
            self._log(f"СДЕЛКА ЗАКРЫТИЕ {order.ticker} @ {c.open:.2f} pnl={pnl:+.2f}")
            self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                            order_id=order.id, price=c.open, action="close",
                            net_pnl=float(trade.net_pnl) if trade else None)
            return
        from app.engine.models import Side
        side = Side(order.side)
        if cfg.use_ensemble:
            from app.engine.exits import AtrStopPolicy, FixedSlTpPolicy
            # SL/TP из UI работают как override (stop_pct/target_pct заданы не дефолт)
            if cfg.stop_pct != 0.01 or cfg.target_pct != 0.02:
                exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
                plan = exit_policy.plan_entry(side, c.open, [])
            else:
                exit_policy = AtrStopPolicy(period=14, multiplier=2.0, risk_reward=2.0)
                plan = exit_policy.plan_entry(side, c.open, list(self.buffers.get(figi, [])))
        else:
            exit_policy = FixedSlTpPolicy(stop_pct=cfg.stop_pct, target_pct=cfg.target_pct)
            plan = exit_policy.plan_entry(side, c.open, [])
        await self.broker.open_position(
            figi=figi,
            ticker=order.ticker,
            side=order.side,
            qty=order.qty,
            price=c.open,
            stop_loss=round(plan.stop_loss, 6) if plan.stop_loss is not None else None,
            take_profit=round(plan.take_profit, 6) if plan.take_profit is not None else None,
            strategy_id=cfg.strategy_id,
        )
        order.status = "FILLED"
        order.filled_at = datetime.now(timezone.utc)
        order.price = c.open
        self._held.add(figi)
        await self._st_open(figi, order.ticker, order.side, order.qty, c.open,
                            plan.stop_loss, plan.take_profit)
        self._log(f"СДЕЛКА ВХОД {order.ticker} {order.side} qty={order.qty} @ {c.open:.2f}")
        self.events.log("ORDER_FILLED", figi=figi, ticker=order.ticker,
                        order_id=order.id, price=c.open, action="open")
        self.events.log("POSITION_OPENED", figi=figi, ticker=order.ticker,
                        side=order.side, qty=order.qty, entry_price=c.open)

    async def _check_circuit_breaker(self) -> None:
        risk = RiskSnapshot(daily_pnl=await self.refresh_daily_pnl(),
                            daily_loss_limit=self.config.daily_loss_limit,
                            entries_paused=self.entries_paused)
        if risk.state == "LOSS_LIMIT" and not self.entries_paused:
            self.entries_paused = True
            self.events.log("CIRCUIT_BREAKER_TRIGGERED",
                            reason="LOSS_LIMIT", daily_pnl=risk.daily_pnl)


runtime = PaperBotRuntime()
