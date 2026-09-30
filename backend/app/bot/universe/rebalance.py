from __future__ import annotations

from datetime import datetime

from app.bot.universe.domain import (
    CurrentPortfolio,
    CurrentPosition,
    InstrumentRef,
    RebalanceAction,
    RebalanceActionType,
    RebalancePlan,
    TargetPosition,
    TargetPortfolio,
)


def _key(instrument: InstrumentRef) -> str:
    return instrument.figi if instrument.figi else instrument.ticker


class RebalancePlanner:
    """Слой между желаемым (TargetPortfolio) и текущим (CurrentPortfolio) состоянием.

    Чистый и детерминированный: никаких Broker/DB/wall-clock/исполнения.
    Отвечает только на вопрос «что нужно изменить», не «как исполнить».
    """

    planner = "direct_target"

    def plan(
        self,
        *,
        target: TargetPortfolio,
        current: CurrentPortfolio | None = None,
        as_of: datetime | None = None,
    ) -> RebalancePlan:
        raise NotImplementedError


class DirectTargetRebalance(RebalancePlanner):
    """Первый planner: каждая разница target_qty != current_qty даёт action.

    Без threshold/cooldown/min trade/turnover limit/cost filter — эти
    ограничения появятся отдельной policy в следующих фазах.
    """

    def plan(
        self,
        *,
        target: TargetPortfolio,
        current: CurrentPortfolio | None = None,
        as_of: datetime | None = None,
    ) -> RebalancePlan:
        target_violations = target.validate()
        if target_violations:
            raise ValueError(
                "TargetPortfolio невалиден: " + "; ".join(target_violations)
            )
        current = current or CurrentPortfolio(as_of=None, equity=0.0, cash=0.0)

        now = as_of if as_of is not None else target.as_of or current.as_of
        target_by_key = {_key(p.instrument): p for p in target.positions}
        current_by_key = {_key(p.instrument): p for p in current.positions}

        # Детерминированный порядок (§10): сначала target (порядок selection),
        # затем только-current позиции в их исходном порядке.
        ordered_current_only = [p for p in current.positions if _key(p.instrument) not in target_by_key]

        union_order: list[tuple[CurrentPosition | None, TargetPosition | None]] = []
        for tpos in target.positions:
            union_order.append((current_by_key.get(_key(tpos.instrument)), tpos))
        for cpos in ordered_current_only:
            union_order.append((cpos, None))

        actions = []
        for cpos, tpos in union_order:
            qty = tpos.target_qty if tpos is not None else 0
            cur_qty = cpos.current_qty if cpos is not None else 0
            price = tpos.price if tpos is not None else (
                cpos.price if cpos is not None else 0.0
            )
            delta_qty = qty - cur_qty
            if delta_qty > 0:
                action = RebalanceActionType.BUY
            elif delta_qty < 0:
                if qty == 0 and cur_qty > 0:
                    action = RebalanceActionType.REMOVE
                else:
                    action = RebalanceActionType.SELL
            else:
                continue  # Вариант B: NOOP не попадает в actions

            instrument = tpos.instrument if tpos is not None else cpos.instrument
            cur_notional = cur_qty * price
            tgt_notional = qty * price
            actions.append(
                RebalanceAction(
                    instrument=instrument,
                    action=action,
                    current_qty=cur_qty,
                    target_qty=qty,
                    delta_qty=delta_qty,
                    current_notional=round(cur_notional, 10),
                    target_notional=round(tgt_notional, 10),
                    delta_notional=round(delta_qty * price, 10),
                    price=price,
                )
            )

        current_cash = float(current.cash or 0.0)
        target_cash = float(target.unallocated_cash)
        return RebalancePlan(
            as_of=now,
            current_equity=float(current.equity),
            target_equity=float(target.equity),
            current_cash=current_cash,
            target_cash=target_cash,
            cash_delta=round(target_cash - current_cash, 10),
            actions=tuple(actions),
            planner=self.planner,
        )


def current_portfolio_from_dicts(
    as_of: datetime | None,
    equity: float,
    positions: list,
    cash: float = 0.0,
) -> CurrentPortfolio:
    """Переиспользуемый адаптер из dict-позиций (как runtime) в CurrentPortfolio.

    positions: [{figi, ticker, qty, price}] — поле price может называться
    entry/last; расхождение формы остаётся на вызывающей стороне. Это удобная
    обёртка, а не вторая система портфеля.
    """
    out = []
    for p in positions or []:
        price = float(p.get("price") or p.get("entry") or p.get("last") or 0.0)
        qty = int(p.get("qty") or 0)
        figi = str(p.get("figi") or "")
        ticker = str(p.get("ticker") or "?")
        instrument = InstrumentRef(ticker=ticker, figi=figi)
        out.append(
            CurrentPosition(
                instrument=instrument,
                current_qty=qty,
                price=price,
                current_notional=round(qty * price, 10),
            )
        )
    return CurrentPortfolio(as_of=as_of, equity=equity, positions=tuple(out), cash=cash)


def plan_target_positions_are_unique(target: TargetPortfolio) -> tuple[str, ...]:
    """Проверка уникальности identity target-позиций (figi/ticker)."""
    seen: set[str] = set()
    dups: list[str] = []
    for p in target.positions:
        key = _key(p.instrument)
        if key in seen:
            dups.append(key)
        seen.add(key)
    return tuple(dict.fromkeys(dups))