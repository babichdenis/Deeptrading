from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import threading
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.engine.costs import CostModel
from app.engine.models import Side
from app.models.paper import PaperAccount, PaperTrade
from app.config import get_settings


DEFAULT_ACCOUNT = "default"


@dataclass
class LivePosition:
    figi: str
    ticker: str
    side: str
    qty: int
    entry_price: float
    entry_time: datetime
    stop_loss: float | None
    take_profit: float | None
    strategy_id: str = "v4_enhanced"


@dataclass
class LiveFill:
    figi: str
    side: str
    qty: int
    price: float


class LiveBroker:
    """Брокер поверх T-Invest (sandbox или live по config.settings().bot_mode).

    Переключение профилей — через переменные в .env (BOT_MODE + соответствующие
    токен/аккаунт), без правки кода.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], cost_model: CostModel | None = None, config=None, mode: str | None = None):
        self.config = config
        self.sessions = session_factory
        self.costs = cost_model or CostModel()
        if self.config is None:
            from app.bot.runtime import BotConfig
            self.config = BotConfig()
        _cfg_mode = getattr(self.config, "mode", None)
        _s = get_settings()
        self.mode = mode or (_cfg_mode if _cfg_mode in ("sandbox", "live") else _s.bot_mode)
        if self.mode not in ("sandbox", "live"):
            self.mode = "sandbox"
        self._token = _s.get_token(self.mode)
        self._target = _s.get_target(self.mode)
        self._account = _s.get_account(self.mode)
        self._services = None
        self._portfolio_snapshot = None
        self._portfolio_ts = 0.0
        self._portfolio_ttl = 15.0
        self._portfolio_retries = 6
        self._portfolio_lock = threading.Lock()

    def _get_services(self):
        if self._services is None:
            from t_tech.invest import Client

            client = Client(self._token, target=self._target)
            self._services = client.__enter__()
        return self._services

    # --- API-ветвление sandbox/live (одни и те же схемы, разные сервисы) ---
    def _api_post_order(self, **kwargs):
        """Выставить ордер: live → orders.post_order (+confirm_margin), sandbox → sandbox.post_sandbox_order."""
        services = self._get_services()
        if self.mode == "live":
            return services.orders.post_order(confirm_margin_trade=True, **kwargs)
        return services.sandbox.post_sandbox_order(**kwargs)

    def _api_get_max_lots(self, request):
        """Макс. лотов: live → orders.get_max_lots, sandbox → sandbox.get_sandbox_max_lots."""
        services = self._get_services()
        if self.mode == "live":
            return services.orders.get_max_lots(request=request)
        return services.sandbox.get_sandbox_max_lots(request=request)

    async def ensure_account(self, initial_cash: float = 100_000.0) -> PaperAccount | None:
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc is None:
                acc = PaperAccount(name=DEFAULT_ACCOUNT, initial_cash=initial_cash, cash=initial_cash)
                db.add(acc)
                await db.commit()
                await db.refresh(acc)
            return acc

    async def reset(self, initial_cash: float = 100_000.0) -> None:
        # live счёт не сбрасывается — только историю paper-учёта
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc:
                await db.delete(acc)
                await db.commit()
        await self.ensure_account(initial_cash)

    def _q(self, v):
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if hasattr(v, "units") and hasattr(v, "nano"):
            return float(v.units + v.nano / 1e9)
        return float(v)

    def _get_portfolio_sync(self):
        return self._get_services().operations.get_portfolio(account_id=self._account)

    def _is_rate_exhausted(self, exc) -> bool:
        from grpc import StatusCode
        try:
            if getattr(exc, "code", None) == StatusCode.RESOURCE_EXHAUSTED:
                return True
        except Exception:
            pass
        return "RESOURCE_EXHAUSTED" in str(exc)

    def _rate_sleep_seconds(self, exc) -> float:
        try:
            meta = getattr(exc, "metadata", None)
            reset = float(meta.ratelimit_reset) if meta is not None else None
        except (TypeError, ValueError, AttributeError):
            reset = None
        return min(reset if reset and reset > 0 else 10.0, 30.0)

    def _get_portfolio_with_retry(self):
        last = None
        for _ in range(self._portfolio_retries):
            try:
                return self._get_portfolio_sync()
            except Exception as e:
                last = e
                if not self._is_rate_exhausted(e):
                    raise
                time.sleep(self._rate_sleep_seconds(e))
        if self._portfolio_snapshot is not None:
            return self._portfolio_snapshot
        raise last

    def _get_portfolio_safe(self):
        now = time.monotonic()
        with self._portfolio_lock:
            if self._portfolio_snapshot is None or now - self._portfolio_ts >= self._portfolio_ttl:
                self._portfolio_snapshot = self._get_portfolio_with_retry()
                self._portfolio_ts = time.monotonic()
            return self._portfolio_snapshot

    def _flush_portfolio(self):
        """Принудительно сбросить кэш портфеля — следующий вызов problems() перечитает данные."""
        with self._portfolio_lock:
            self._portfolio_snapshot = None
            self._portfolio_ts = 0.0

    async def flush_portfolio(self):
        """Async-обёртка над _flush_portfolio() для вызова из runtime."""
        await asyncio.to_thread(self._flush_portfolio)

    async def cash(self) -> float:
        """Свободные денежные средства на счёте (руб)."""
        import asyncio

        def _f():
            p = self._get_portfolio_safe()
            return self._q(p.total_amount_currencies)

        return await asyncio.to_thread(_f)

    async def equity(self) -> float:
        """Текущий капитал = деньги + стоимость позиций (для аллокации 20%)."""
        import asyncio

        def _f():
            p = self._get_portfolio_safe()
            cur = self._q(p.total_amount_currencies)
            shares = self._q(p.total_amount_shares) if hasattr(p, "total_amount_shares") else 0.0
            return cur + shares

        return await asyncio.to_thread(_f)

    async def market_value(self) -> float:
        """Стоимость открытых позиций (сумма |qty × current| по non-currency)."""
        import asyncio

        def _f():
            p = self._get_portfolio_safe()
            total = 0.0
            for pos in p.positions:
                if pos.instrument_type == "currency":
                    continue
                qty = pos.quantity.units + pos.quantity.nano / 1e9
                px = self._q(pos.current_price)
                total += abs(qty) * px
            return total

        return await asyncio.to_thread(_f)

    async def free_cash(self) -> float:
        """Свободные собственные деньги = equity − стоимость позиций (для гейта входа)."""
        eq = await self.equity()
        mv = await self.market_value()
        return max(0.0, eq - mv)

    async def free_funds(self) -> float:
        """Реально свободные средства для сделок = ликвидный портфель − начальная маржа.
        (Свободные деньги на счёте искажены шортами; начальная маржа — замороженное обеспечение.)"""
        import asyncio

        def _f():
            try:
                services = self._get_services()
                ma = services.users.get_margin_attributes(account_id=self._account)
                liquid = self._q(ma.liquid_portfolio)
                start = self._q(ma.starting_margin)
                return max(0.0, liquid - start)
            except Exception:
                # fallback: equity − стоимость позиций
                p = self._get_portfolio_safe()
                eq = self._q(p.total_amount_currencies) + (self._q(p.total_amount_shares) if hasattr(p, "total_amount_shares") else 0.0)
                mv = 0.0
                for pos in p.positions:
                    if pos.instrument_type == "currency":
                        continue
                    q = pos.quantity.units + pos.quantity.nano / 1e9
                    mv += abs(q) * self._q(pos.current_price)
                return max(0.0, eq - mv)

        return await asyncio.to_thread(_f)

    async def verify_position(self, figi: str) -> LivePosition | None:
        """Фактическая позиция в T-Invest (для сверки входа/выхода). None — позиции нет."""
        return await self.get_position(figi)

    async def positions(self) -> list[LivePosition]:
        import asyncio

        def _fetch():
            p = self._get_portfolio_safe()
            out = []
            for pos in p.positions:
                if pos.instrument_type == "currency":
                    continue
                qty = pos.quantity.units + pos.quantity.nano / 1e9
                if abs(qty) < 1:
                    continue
                out.append(LivePosition(
                    figi=pos.figi,
                    ticker=pos.figi[:8],
                    side="LONG" if qty > 0 else "SHORT",
                    qty=abs(int(qty)),
                    entry_price=self._q(pos.average_position_price),
                    entry_time=datetime.now(timezone.utc),
                    stop_loss=None,
                    take_profit=None,
                ))
            return out

        return await asyncio.to_thread(_fetch)

    async def _get_lot(self, figi: str) -> int:
        try:
            from sqlalchemy import text
            async with self.sessions() as db:
                r = await db.execute(text("SELECT lot FROM instruments WHERE figi = :f"), {"f": figi})
                row = r.first()
                if row:
                    return int(row[0])
        except Exception:
            pass
        return 1

    async def get_position(self, figi: str) -> LivePosition | None:
        for p in await self.positions():
            if p.figi == figi:
                return p
        return None

    async def get_max_lots(self, figi: str):
        import asyncio
        from dataclasses import dataclass as _dc

        @_dc
        class _ML:
            buy_cash: int = 0
            buy_margin: int = 0
            sell_cash: int = 0
            sell_margin: int = 0
            buy_money: float = 0.0
            buy_margin_money: float = 0.0
            leverage: float = 1.0

        def _fetch():
            from t_tech.invest.schemas import GetMaxLotsRequest
            req = GetMaxLotsRequest(account_id=self._account, instrument_id=figi)
            ml = self._api_get_max_lots(req)
            bc = ml.buy_limits
            bm = ml.buy_margin_limits
            sc = ml.sell_limits
            sm = ml.sell_margin_limits
            bmu = bc.buy_money_amount.units + bc.buy_money_amount.nano / 1e9
            bmmu = bm.buy_money_amount.units + bm.buy_money_amount.nano / 1e9
            lev = round(bm.buy_max_lots / bc.buy_max_lots, 2) if bc.buy_max_lots > 0 and bm.buy_max_lots > 0 else 1.0
            return _ML(
                buy_cash=bc.buy_max_lots,
                buy_margin=bm.buy_max_lots,
                sell_cash=sc.sell_max_lots,
                sell_margin=sm.sell_max_lots,
                buy_money=bmu,
                buy_margin_money=bmmu,
                leverage=lev,
            )
        return await asyncio.to_thread(_fetch)

    async def get_trading_status(self, figi: str) -> str:
        """Текущий статус торгов инструмента из API (SecurityTradingStatus enum name).
        Возвращает имя константы, например SECURITY_TRADING_STATUS_NORMAL_TRADING."""
        import asyncio

        def _fetch():
            services = self._get_services()
            return services.market_data.get_trading_status(instrument_id=figi)

        try:
            resp = await asyncio.to_thread(_fetch)
            return str(resp.trading_status)
        except Exception:
            return ""

    async def main_session_active(self) -> bool:
        """Основная торговая сессия идёт (по живым статусам API, без привязки к акции).
        Истина, когда большинство инструментов в NORMAL_TRADING — это и есть основная
        сессия Мосбиржи. Расписания не используем — позари на статусы в моменте."""
        import asyncio

        def _fetch():
            services = self._get_services()
            return services.market_data.get_trading_statuses()

        try:
            resp = await asyncio.to_thread(_fetch)
            total = len(resp.trading_statuses)
            if total == 0:
                return False
            n_trading = sum(
                1 for x in resp.trading_statuses
                if int(x.trading_status) == 5  # SECURITY_TRADING_STATUS_NORMAL_TRADING
            )
            return n_trading / total > 0.5
        except Exception:
            return False

    async def open_position(
        self,
        figi: str,
        ticker: str,
        side: str,
        qty: int,
        price: float,
        stop_loss: float | None,
        take_profit: float | None,
        strategy_id: str,
    ) -> float | None:
        import asyncio
        from uuid import uuid4
        from t_tech.invest import OrderDirection, OrderType

        def _place():
            direction = OrderDirection.ORDER_DIRECTION_BUY if side == "BUY" else OrderDirection.ORDER_DIRECTION_SELL
            resp = self._api_post_order(
                instrument_id=figi,
                quantity=qty,
                direction=direction,
                account_id=self._account,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=str(uuid4()),
            )
            return resp

        resp = await asyncio.to_thread(_place)
        try:
            eop = resp.executed_order_price
            fill = float(eop.units + eop.nano / 1e9) if eop and (eop.units or eop.nano) else None
            if fill and fill > 0:
                # Apply entry commission (0.3% per T-Investments)
                entry_commission = fill * qty * self.config.commission_rate if hasattr(self, 'config') else fill * qty * 0.003
            return fill
        except Exception:
            return None

    async def close_position(self, figi: str, price: float, reason: str) -> PaperTrade | None:
        import asyncio
        from uuid import uuid4
        from t_tech.invest import OrderDirection, OrderType

        pos = await self.get_position(figi)
        if pos is None:
            return None
        direction = OrderDirection.ORDER_DIRECTION_SELL if pos.side == "LONG" else OrderDirection.ORDER_DIRECTION_BUY

        lot = await self._get_lot(figi)
        order_qty = max(pos.qty // lot, 1) if lot > 0 else pos.qty

        def _place():
            resp = self._api_post_order(
                instrument_id=figi,
                quantity=order_qty,
                direction=direction,
                account_id=self._account,
                order_type=OrderType.ORDER_TYPE_MARKET,
                order_id=str(uuid4()),
            )
            return resp

        resp = await asyncio.to_thread(_place)
        try:
            eop = resp.executed_order_price
            actual_exit = float(eop.units + eop.nano / 1e9) if eop and (eop.units or eop.nano) else price
        except Exception:
            actual_exit = price

        direction_mult = 1 if pos.side == "LONG" else -1
        gross = (actual_exit - pos.entry_price) * pos.qty * direction_mult
        cr = self.config.commission_rate if (hasattr(self, 'config') and self.config.commission_rate) else 0.003
        # Commission: cr on entry + cr on exit (T-Investments: per trade)
        entry_commission = pos.entry_price * pos.qty * cr
        exit_commission = actual_exit * pos.qty * cr
        commission = entry_commission + exit_commission
        net = gross - commission
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            trade = PaperTrade(
                account_id=acc.id if acc else 1,
                figi=pos.figi,
                ticker=pos.ticker,
                side=pos.side,
                qty=pos.qty,
                entry_time=pos.entry_time,
                entry_price=Decimal(str(round(pos.entry_price, 6))),
                exit_time=datetime.now(timezone.utc),
                exit_price=Decimal(str(round(actual_exit, 6))),
                gross_pnl=Decimal(str(round(gross, 6))),
                commission=Decimal(str(round(commission, 6))),
                slippage=Decimal(0),
                net_pnl=Decimal(str(round(net, 6))),
                exit_reason=reason,
                strategy_id=pos.strategy_id,
            )
            db.add(trade)
            await db.commit()
            return trade

    async def update_protective_levels(self, figi: str, stop: float | None, target: float | None) -> None:
        pass  # live stop-заявки не ставим — выход по сигналу

    async def trades_history(self, limit: int = 100) -> list[PaperTrade]:
        from sqlalchemy import desc

        async with self.sessions() as db:
            res = await db.execute(
                select(PaperTrade).order_by(desc(PaperTrade.exit_time)).limit(limit)
            )
            return list(res.scalars().all())
