"""
StreamManager — единый источник правды для позиций, сделок и ордеров.

Вместо poll GetPositions/GetPortfolio — подписываемся на gRPC стримы:
  - PositionsStream → реальные позиции в реальном времени
  - TradesStream    → факт исполнения сделок
  - OrderStateStream → жизненный цикл ордеров

Архитектура:
  1. StreamManager запускает фоновые задачи для каждого стрима
  2. Бот читает из _server_positions (а не poll)
  3. PostOrderAsync + OrderStateStream вместо PostSandboxOrder sync
  4. Автоматический reconnect с exponential backoff
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ServerPosition:
    """Позиция с сервера (PositionsStream)."""
    figi: str
    ticker: str
    side: str  # "LONG" | "SHORT"
    qty: int
    entry_price: float
    blocked: int = 0
    instrument_uid: str = ""
    updated_at: float = field(default_factory=time.monotonic)


@dataclass
class ServerTrade:
    """Сделка с сервера (TradesStream)."""
    order_id: str
    figi: str
    direction: str  # "BUY" | "SELL"
    price: float
    quantity: int
    trade_id: str
    executed_at: datetime


@dataclass
class ServerOrder:
    """Ордер с сервера (OrderStateStream)."""
    order_id: str
    figi: str
    direction: str
    status: str  # NEW | FILLED | CANCELLED | REJECTED
    quantity: int = 0
    filled_quantity: int = 0
    price: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)


class StreamManager:
    """
    Управляет gRPC стримами T-Investments.

    Использование:
        sm = StreamManager(token, account_id)
        await sm.start()

        pos = sm.get_position("BBG004730N88")
        order = sm.get_order("order-123")

        await sm.stop()
    """

    def __init__(self, token: str, account_id: str, target: str | None = None):
        self.token = token
        self.account_id = account_id
        self.target = target  # None = production, "sandbox-invest-public-api.tbank.ru" = sandbox

        # In-memory state
        self._positions: dict[str, ServerPosition] = {}  # figi -> ServerPosition
        self._trades: dict[str, ServerTrade] = {}        # trade_id -> ServerTrade
        self._orders: dict[str, ServerOrder] = {}        # order_id -> ServerOrder
        self._pending_orders: dict[str, asyncio.Future] = {}  # order_id -> Future
        self._uid_to_figi: dict[str, str] = {}           # instrument_uid -> figi

        # Tasks
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._reconnect_count = 0
        self._max_reconnect = 50

        # Stats
        self._last_positions_update: float = 0.0
        self._last_trades_update: float = 0.0
        self._last_orders_update: float = 0.0

    @property
    def running(self) -> bool:
        return self._running

    def get_position(self, figi: str) -> ServerPosition | None:
        """Получить позицию по FIGI из in-memory кэша."""
        return self._positions.get(figi)

    def get_positions(self) -> list[ServerPosition]:
        """Получить все позиции из in-memory кэша."""
        return list(self._positions.values())

    def get_trade(self, trade_id: str) -> ServerTrade | None:
        return self._trades.get(trade_id)

    def get_order(self, order_id: str) -> ServerOrder | None:
        return self._orders.get(order_id)

    async def wait_order(self, order_id: str, timeout: float = 30.0) -> ServerOrder | None:
        """
        Ждать подтверждения ордера (filled/cancelled/rejected).

        Возвращает ServerOrder или None при timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            order = self._orders.get(order_id)
            if order and order.status in ("FILLED", "CANCELLED", "REJECTED"):
                return order
            await asyncio.sleep(0.2)
        return None

    async def start(self):
        """Запустить все стримы."""
        if self._running:
            return

        self._running = True
        self._reconnect_count = 0

        self._tasks = [
            asyncio.create_task(self._positions_loop(), name="stream-positions"),
            asyncio.create_task(self._trades_loop(), name="stream-trades"),
            asyncio.create_task(self._orders_loop(), name="stream-orders"),
        ]
        logger.info("StreamManager started (3 streams)")

    async def stop(self):
        """Остановить все стримы."""
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("StreamManager stopped")

    # ─── Positions Stream ─────────────────────────────────────

    async def _positions_loop(self):
        """Фоновая задача: PositionsStream → _positions."""
        while self._running:
            try:
                await self._run_positions_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._reconnect_count += 1
                delay = min(2 ** self._reconnect_count, 300)
                logger.warning(
                    f"PositionsStream disconnected: {e}, "
                    f"reconnect in {delay}s (attempt {self._reconnect_count})"
                )
                if self._reconnect_count > self._max_reconnect:
                    logger.error("PositionsStream: max reconnect attempts reached")
                    break
                await asyncio.sleep(delay)

    async def _run_positions_stream(self):
        """Запустить PositionsStream (блокирующий until disconnect)."""
        import asyncio
        from t_tech.invest import AsyncClient

        async with AsyncClient(self.token, target=self.target) as client:
            account_ids = [self.account_id]

            # --- Snapshot: load current positions via non-stream API first ---
            # Sandbox positions_stream does NOT push existing positions on
            # subscribe — it only sends deltas. So prime state from get_positions.
            try:
                for acc_id in account_ids:
                    pos_resp = await client.operations.get_positions(account_id=acc_id)
                    for sec in pos_resp.securities:
                        qty = sec.balance
                        if abs(qty) < 1:
                            continue
                        side = "LONG" if qty > 0 else "SHORT"
                        sp = ServerPosition(
                            figi=sec.figi,
                            ticker=sec.ticker or sec.figi[:8],
                            side=side,
                            qty=abs(qty),
                            entry_price=0.0,
                            instrument_uid=sec.instrument_uid,
                        )
                        self._positions[sec.figi] = sp
                        self._uid_to_figi[sec.instrument_uid] = sec.figi
                        logger.info(f"PositionsSnapshot: {sec.figi} {side} qty={abs(qty)}")
                self._last_positions_update = time.monotonic()
                logger.info(f"PositionsSnapshot: {len(self._positions)} positions loaded")
            except Exception as e:
                logger.warning(f"PositionsSnapshot failed: {e}")

            logger.info(f"PositionsStream: subscribing to {len(account_ids)} accounts")

            async for response in client.operations_stream.positions_stream(
                accounts=account_ids,
                with_initial_positions=True,
            ):
                # Subscription confirmation
                if response.subscriptions:
                    for sub in response.subscriptions.accounts:
                        logger.info(f"PositionsStream: account {sub.account_id} status={sub.subscription_status}")

                # Initial positions (PositionsResponse with .securities)
                if response.initial_positions:
                    for sec in response.initial_positions.securities:
                        qty = sec.balance
                        if abs(qty) < 1:
                            continue
                        side = "LONG" if qty > 0 else "SHORT"
                        sp = ServerPosition(
                            figi=sec.figi,
                            ticker=sec.ticker or sec.figi[:8],
                            side=side,
                            qty=abs(int(qty)),
                            entry_price=0.0,
                            instrument_uid=sec.instrument_uid,
                        )
                        self._positions[sec.figi] = sp
                        self._uid_to_figi[sec.instrument_uid] = sec.figi
                        logger.debug(f"PositionsStream: initial {sec.figi} {side} qty={abs(int(qty))}")
                    self._last_positions_update = time.monotonic()

                # Position update
                if response.position:
                    pd = response.position
                    for sec in pd.securities:
                        qty = sec.balance
                        side = "LONG" if qty > 0 else "SHORT"
                        if abs(qty) < 1:
                            self._positions.pop(sec.figi, None)
                            self._uid_to_figi.pop(sec.instrument_uid, None)
                            self._last_positions_update = time.monotonic()
                            continue
                        old = self._positions.get(sec.figi)
                        entry = old.entry_price if old else 0.0
                        sp = ServerPosition(
                            figi=sec.figi,
                            ticker=sec.ticker or sec.figi[:8],
                            side=side,
                            qty=abs(qty),
                            entry_price=entry,
                            blocked=sec.blocked,
                            instrument_uid=sec.instrument_uid,
                        )
                        self._positions[sec.figi] = sp
                        self._uid_to_figi[sec.instrument_uid] = sec.figi
                        self._last_positions_update = time.monotonic()

                # Ping keepalive
                if response.ping:
                    logger.debug("PositionsStream: ping")

    # ─── Trades Stream ────────────────────────────────────────

    async def _trades_loop(self):
        """Фоновая задача: TradesStream → _trades."""
        while self._running:
            try:
                await self._run_trades_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._reconnect_count += 1
                delay = min(2 ** self._reconnect_count, 300)
                logger.warning(
                    f"TradesStream disconnected: {e}, "
                    f"reconnect in {delay}s (attempt {self._reconnect_count})"
                )
                if self._reconnect_count > self._max_reconnect:
                    logger.error("TradesStream: max reconnect attempts reached")
                    break
                await asyncio.sleep(delay)

    async def _run_trades_stream(self):
        """Запустить TradesStream."""
        from t_tech.invest import AsyncClient

        async with AsyncClient(self.token, target=self.target) as client:
            account_ids = [self.account_id]

            logger.info(f"TradesStream: subscribing to {len(account_ids)} accounts")

            async for response in client.orders_stream.trades_stream(
                accounts=account_ids,
            ):
                if response.subscription:
                    logger.info(f"TradesStream: subscription status={response.subscription}")

                if response.order_trades:
                    ot = response.order_trades
                    for trade in ot.trades:
                        st = ServerTrade(
                            order_id=ot.order_id,
                            figi=ot.figi,
                            direction="BUY" if ot.direction == 1 else "SELL",
                            price=self._q(trade.price),
                            quantity=trade.quantity,
                            trade_id=trade.trade_id,
                            executed_at=trade.date_time,
                        )
                        self._trades[trade.trade_id] = st
                        logger.info(
                            f"TradesStream: {ot.figi} {st.direction} "
                            f"qty={st.quantity} price={st.price:.2f} "
                            f"order={ot.order_id[:12]}"
                        )

                        # Resolve pending order future
                        future = self._pending_orders.get(ot.order_id)
                        if future and not future.done():
                            future.set_result(True)

                if response.ping:
                    logger.debug("TradesStream: ping")

    # ─── Order State Stream ───────────────────────────────────

    async def _orders_loop(self):
        """Фоновая задача: OrderStateStream → _orders."""
        while self._running:
            try:
                await self._run_orders_stream()
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._reconnect_count += 1
                delay = min(2 ** self._reconnect_count, 300)
                logger.warning(
                    f"OrderStateStream disconnected: {e}, "
                    f"reconnect in {delay}s (attempt {self._reconnect_count})"
                )
                if self._reconnect_count > self._max_reconnect:
                    logger.error("OrderStateStream: max reconnect attempts reached")
                    break
                await asyncio.sleep(delay)

    async def _run_orders_stream(self):
        """Запустить OrderStateStream."""
        from t_tech.invest import AsyncClient
        from t_tech.invest.schemas import OrderStateStreamRequest

        async with AsyncClient(self.token, target=self.target) as client:
            request = OrderStateStreamRequest()
            request.ping_delay_millis = 10_000

            logger.info("OrderStateStream: subscribing")

            stream = client.orders_stream.order_state_stream(request=request)
            async for response in stream:
                if response.subscription:
                    logger.info(f"OrderStateStream: subscription status={response.subscription}")

                if response.order_state:
                    try:
                        os_data = response.order_state
                        status = self._map_order_status(os_data.execution_report_status)
                        # OrderStateStreamOrderState НЕ имеет поля figi/price:
                        # figi резолвим через instrument_uid (из PositionsStream),
                        # price берём из initial_order_price / executed_order_price.
                        figi = self._uid_to_figi.get(os_data.instrument_uid, "")
                        price = 0.0
                        for p in (os_data.initial_order_price, os_data.order_price,
                                  os_data.executed_order_price):
                            if p is not None:
                                price = self._q(p)
                                break
                        so = ServerOrder(
                            order_id=os_data.order_id,
                            figi=figi,
                            direction="BUY" if os_data.direction == 1 else "SELL",
                            status=status,
                            quantity=os_data.lots_requested,
                            filled_quantity=os_data.lots_executed,
                            price=price,
                        )
                        self._orders[os_data.order_id] = so
                        logger.info(
                            f"OrderStateStream: figi={figi or os_data.ticker} {so.direction} "
                            f"status={status} order={os_data.order_id[:12]}"
                        )
                    except Exception as e:
                        # Битое сообщение ордера не должно ронять весь стрим
                        logger.warning(f"OrderStateStream: skip bad message {type(e).__name__}: {str(e)[:120]}")

                if response.ping:
                    logger.debug("OrderStateStream: ping")

    @staticmethod
    def _map_order_status(status) -> str:
        """Map T-Investments order status to string."""
        # OrderExecutionReportStatus enum values
        status_map = {
            1: "NEW",
            2: "PARTIALLY_FILLED",
            3: "FILLED",
            4: "CANCELLED",
            5: "REJECTED",
            6: "DEAL_PENDING",
            7: "REPLACE_PENDING",
            8: "CANCEL_PENDING",
        }
        val = status if isinstance(status, int) else getattr(status, "value", 0)
        return status_map.get(val, f"UNKNOWN_{val}")

    @staticmethod
    def _q(v) -> float:
        """Quotation → float."""
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if hasattr(v, "units") and hasattr(v, "nano"):
            return float(v.units + v.nano / 1e9)
        return float(v)
