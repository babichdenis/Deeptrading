from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from app.bot.universe.domain import (
    InstrumentRef,
    OrderIntent,
    PolicyResult,
    RebalanceAction,
    RebalanceActionType,
    RebalanceContext,
    RebalancePlan,
    RejectedAction,
    RejectionReason,
)
from app.engine.models import Side


def _key(instrument: InstrumentRef) -> str:
    return instrument.figi if instrument.figi else instrument.ticker


class RebalancePolicy(Protocol):
    """Отделяет «какие изменения допустимо превратить в executable intent».

    Policy чистая и детерминированная: все данные приходят через контекст,
    никаких DB/Broker/HTTP/wall-clock внутри. НЕ создаёт ордера и НЕ меняет
    TargetPortfolio/RebalancePlan (immutable inputs).
    """

    def apply(self, *, plan: RebalancePlan, context: RebalanceContext) -> PolicyResult:
        ...


class DirectRebalancePolicy:
    """Baseline policy: BUY→BUY, SELL→SELL, REMOVE→SELL + настраиваемые фильтры.

    Ограничения конфигурируются в конструкторе; их отсутствие (None/0) = фильтр
    выключен. Правило: policy НИКОГДА не меняет quantity ради ограничения —
    только accept/reject (§13, §21).
    """

    def __init__(
        self,
        *,
        min_trade_value: float = 0.0,
        max_turnover_pct: float | None = None,
        estimated_cost_rate: float = 0.0,
        max_estimated_cost: float | None = None,
        cooldown: timedelta | None = None,
        allow_exit_during_cooldown: bool = False,
    ):
        if min_trade_value < 0 or (max_turnover_pct is not None and max_turnover_pct < 0) \
                or estimated_cost_rate < 0 or (max_estimated_cost is not None and max_estimated_cost < 0):
            raise ValueError("пороги policy не могут быть отрицательными")
        self.min_trade_value = min_trade_value
        self.max_turnover_pct = max_turnover_pct
        self.estimated_cost_rate = estimated_cost_rate
        self.max_estimated_cost = max_estimated_cost
        self.cooldown = cooldown
        self.allow_exit_during_cooldown = allow_exit_during_cooldown

    def apply(self, *, plan: RebalancePlan, context: RebalanceContext) -> PolicyResult:
        actions = plan.actions
        as_of = context.as_of if context.as_of is not None else plan.as_of

        # Плановые ограничения уровня всего портфеля (§13 Вариант A, §24):
        # применяются ко ВСЕМУ плану, а не к отдельным action.
        block: RejectionReason | None = None
        if self.max_turnover_pct is not None and context.equity > 0:
            gross = sum(abs(a.delta_notional) for a in actions)
            if gross / context.equity > self.max_turnover_pct:
                block = RejectionReason.MAX_TURNOVER_EXCEEDED
        if block is None and context.available_cash is not None:
            buy_notional = sum(
                a.delta_qty * a.price for a in actions if a.delta_qty > 0
            )
            if buy_notional > context.available_cash + 1e-9:
                block = RejectionReason.INSUFFICIENT_CASH

        if block is not None:
            rejected = tuple(
                RejectedAction(
                    instrument=a.instrument,
                    action=a.action,
                    reason=block,
                    delta_qty=a.delta_qty,
                    delta_notional=a.delta_notional,
                )
                for a in actions
            )
            return PolicyResult(as_of=as_of, accepted=(), rejected=rejected)

        accepted: list[OrderIntent] = []
        rejected: list[RejectedAction] = []
        for a in actions:
            intent, rej = self._convert(a, context, as_of)
            if intent is not None:
                accepted.append(intent)
            if rej is not None:
                rejected.append(rej)
        return PolicyResult(
            as_of=as_of,
            accepted=tuple(accepted),
            rejected=tuple(rejected),
        )

    def _convert(
        self, a: RebalanceAction, ctx: RebalanceContext, as_of: datetime | None
    ) -> tuple[OrderIntent | None, RejectedAction | None]:
        key = _key(a.instrument)
        qty = a.delta_qty
        side = Side.BUY if qty > 0 else Side.SELL
        qty_mag = abs(qty)

        if qty == 0:
            return None, self._reject(a, RejectionReason.ZERO_QUANTITY)
        if a.price <= 0:
            return None, self._reject(a, RejectionReason.INVALID_PRICE)
        if ctx.lot_sizes is not None:
            lot = int(ctx.lot_sizes.get(key, ctx.lot_sizes.get(a.instrument.ticker, 0)))
            if lot > 0 and qty_mag % lot != 0:
                return None, self._reject(a, RejectionReason.INVALID_QUANTITY)

        notional_mag = abs(a.delta_notional) or qty_mag * a.price
        if self.min_trade_value and notional_mag < self.min_trade_value:
            return None, self._reject(a, RejectionReason.BELOW_MIN_TRADE_VALUE)

        if self.estimated_cost_rate and self.max_estimated_cost is not None:
            cost = notional_mag * self.estimated_cost_rate
            if cost > self.max_estimated_cost:
                return None, self._reject(a, RejectionReason.MAX_COST_EXCEEDED)

        if self.cooldown is not None and as_of is not None and ctx.last_rebalance_at:
            last = ctx.last_rebalance_at.get(key)
            if last is not None and as_of - last < self.cooldown:
                if not (self.allow_exit_during_cooldown and side is Side.SELL):
                    return None, self._reject(a, RejectionReason.COOLDOWN)

        intent = OrderIntent(
            instrument=a.instrument,
            side=side,
            quantity=qty_mag,
            price=a.price,
            notional=round(qty_mag * a.price, 10),
            source="rebalance",
            action=a.action,
        )
        return intent, None

    @staticmethod
    def _reject(a: RebalanceAction, reason: RejectionReason) -> RejectedAction:
        return RejectedAction(
            instrument=a.instrument,
            action=a.action,
            reason=reason,
            delta_qty=a.delta_qty,
            delta_notional=a.delta_notional,
        )


def order_intent_to_legacy(intent: OrderIntent) -> dict:
    """НЕисполняющий адаптер к формату существующего Order runtime (§28).

    Возвращает скелет будущего BotOrder-поля по умолчанию БЕЗ id/filled/status:
    order_id и execution-состояние появится только в execution layer.
    """
    return {
        "figi": intent.instrument.figi,
        "ticker": intent.instrument.ticker,
        "side": intent.side.value,
        "qty": intent.quantity,
        "price": round(intent.price, 6),
        "source": intent.source,
        "action": intent.action.value if intent.action else "open",
    }