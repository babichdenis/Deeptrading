from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.engine.costs import CostModel
from app.engine.models import Side
from app.models.paper import PaperAccount, PaperPosition, PaperTrade

DEFAULT_ACCOUNT = "default"


@dataclass
class Fill:
    figi: str
    side: str
    qty: int
    price: float


class PaperBroker:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], cost_model: CostModel | None = None):
        self.sessions = session_factory
        self.costs = cost_model or CostModel()
        # Кэш позиций: get_position зовётся на КАЖДЫЙ бар (hot-path) — без кэша это
        # 500k+ SQL-запросов за тест. Инвалидируется при open/close/reset.
        self._pos_cache: dict[str, PaperPosition | None] = {}

    async def ensure_account(self, initial_cash: float = 10_000.0) -> PaperAccount:
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc is None:
                acc = PaperAccount(name=DEFAULT_ACCOUNT, initial_cash=initial_cash, cash=initial_cash)
                db.add(acc)
                await db.commit()
                await db.refresh(acc)
            return acc

    async def reset(self, initial_cash: float = 10_000.0) -> None:
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc:
                await db.delete(acc)
                await db.commit()
        self._pos_cache.clear()
        await self.ensure_account(initial_cash)

    async def positions(self) -> list[PaperPosition]:
        async with self.sessions() as db:
            res = await db.execute(select(PaperPosition))
            return list(res.scalars().all())

    async def get_max_lots(self, figi: str, side: str = "BUY"):
        """Заглушка маржи для тест/replay-режима: максимум из БД.

        У LiveBroker плечо запрашивается у T-Invest, у PaperBroker брокера нет —
        поэтому берём теоретический максимум из `instruments.long_lev/short_lev`
        (те же данные, что используют бэктесты). Формат ответа совпадает с
        LiveBroker.get_max_lots, чтобы блок MARGIN в runtime работал одинаково.
        """
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

        async def _fetch():
            from app.models.instrument import Instrument
            async with self.sessions() as db:
                row = await db.scalar(select(Instrument).where(Instrument.figi == figi))
                long_lev = float(getattr(row, "long_lev", 0.0) or 0.0)
                short_lev = float(getattr(row, "short_lev", 0.0) or 0.0)
            # В бэктестах отсутствие/0 = плечо 1.0 (без маржи). Потолки лотов не
            # лимитируем (big): реальный размер ограничен бюджетом в _submit_order.
            _BIG = 10 ** 9
            lev = long_lev if side == "BUY" else short_lev
            return _ML(
                buy_cash=_BIG,
                buy_margin=_BIG,
                sell_cash=_BIG,
                sell_margin=_BIG,
                buy_money=float(lev) if lev >= 1 else 0.0,
                buy_margin_money=0.0,
                leverage=max(1.0, lev, 1.0),
            )

        return await _fetch()

    async def get_position(self, figi: str) -> PaperPosition | None:
        if figi in self._pos_cache:
            return self._pos_cache[figi]
        async with self.sessions() as db:
            pos = await db.scalar(select(PaperPosition).where(PaperPosition.figi == figi))
        self._pos_cache[figi] = pos
        return pos

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
    ) -> None:
        if await self.get_position(figi) is not None:
            return
        fill = self.costs.fill_price(price, Side.BUY if side == "BUY" else Side.SELL)
        commission = self.costs.commission(fill * qty)
        async with self.sessions() as db:
            acc = await db.scalar(select(PaperAccount).where(PaperAccount.name == DEFAULT_ACCOUNT))
            if acc is None:
                return
            acc.cash -= Decimal(str(commission))
            db.add(
                PaperPosition(
                    account_id=acc.id,
                    figi=figi,
                    ticker=ticker,
                    side=side,
                    qty=qty,
                    entry_price=Decimal(str(round(fill, 6))),
                    entry_time=datetime.now(timezone.utc),
                    stop_loss=Decimal(str(stop_loss)) if stop_loss is not None else None,
                    take_profit=Decimal(str(take_profit)) if take_profit is not None else None,
                    strategy_id=strategy_id,
                )
            )
            await db.commit()
        self._pos_cache.pop(figi, None)

    async def close_position(self, figi: str, price: float, reason: str) -> PaperTrade | None:
        pos = await self.get_position(figi)
        if pos is None:
            return None
        exit_side = Side.BUY if pos.side == "SHORT" else Side.SELL
        fill = self.costs.fill_price(price, exit_side)
        exit_price = float(fill)
        entry_price = float(pos.entry_price)
        direction = 1 if pos.side == "LONG" else -1
        gross = (exit_price - entry_price) * pos.qty * direction
        commission = self.costs.commission(exit_price * pos.qty)
        slippage_cost = abs(exit_price - price) * pos.qty
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
                entry_price=pos.entry_price,
                exit_time=datetime.now(timezone.utc),
                exit_price=Decimal(str(round(exit_price, 6))),
                gross_pnl=Decimal(str(round(gross, 6))),
                commission=Decimal(str(round(commission, 6))),
                slippage=Decimal(str(round(slippage_cost, 6))),
                net_pnl=Decimal(str(round(net, 6))),
                exit_reason=reason,
                strategy_id=pos.strategy_id,
            )
            db.add(trade)
            if acc:
                acc.cash += Decimal(str(round(net, 2)))
            await db.execute(delete(PaperPosition).where(PaperPosition.id == pos.id))
            await db.commit()
            self._pos_cache.pop(figi, None)
            return trade

    async def update_protective_levels(self, figi: str, stop: float | None, target: float | None) -> None:
        async with self.sessions() as db:
            pos = await db.scalar(select(PaperPosition).where(PaperPosition.figi == figi))
            if pos is None:
                return
            pos.stop_loss = Decimal(str(stop)) if stop is not None else None
            pos.take_profit = Decimal(str(target)) if target is not None else None
            await db.commit()
        self._pos_cache.pop(figi, None)

    async def trades_history(self, limit: int = 100) -> list[PaperTrade]:
        async with self.sessions() as db:
            from sqlalchemy import desc

            res = await db.execute(
                select(PaperTrade).order_by(desc(PaperTrade.exit_time)).limit(limit)
            )
            return list(res.scalars().all())
