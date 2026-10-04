"""Execution boundary Phase 9: OrderIntent → существующий PaperBroker.

Тонкий адаптер, НЕ новый execution engine. Существующая цепочка исполнения
(см. runtime._execute_pending) вызывает broker.open_position/close_position —
адаптер делает только это: валидирует intent и передаёт его существующим
методам бумажного брокера. Никаких гейтов/риска/маржи/AI здесь нет: всё это
уже обработано доменом (Policy) до нас. Live_брокер и runtime не вызываются.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.bot.universe.domain import OrderIntent, PolicyResult
from app.engine.models import Side

FILLED_LONG = ("LONG", "BUY")
FILLED_SHORT = ("SHORT", "SELL")


class ExecutionStatus(str, Enum):
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    INVALID_INTENT = "INVALID_INTENT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BROKER_ERROR = "BROKER_ERROR"


@dataclass(frozen=True)
class ExecutionResult:
    """Результат ОДНОЙ попытки исполнить intent существующим брокером.

    Никогда не равен OrderIntent: Intent — желание, этот объект — факт
    попытки. order_id/заполнение берутся из существующего execution path.
    """

    intent: OrderIntent
    status: ExecutionStatus
    order_id: str | None = None
    filled_qty: int = 0
    fill_price: float | None = None
    commission: float | None = None
    error: str | None = None


def _invalid(intent: OrderIntent, error: str) -> ExecutionResult:
    return ExecutionResult(intent=intent, status=ExecutionStatus.INVALID_INTENT, error=error)


def _rejected(intent: OrderIntent, error: str) -> ExecutionResult:
    return ExecutionResult(intent=intent, status=ExecutionStatus.REJECTED, error=error)


async def execute_order_intent(
    broker,
    intent: OrderIntent,
    *,
    strategy_id: str = "universe",
) -> ExecutionResult:
    """Один intent → одна попытка исполнения существующим paper-брокером.

    quantity/price не меняются (policy уже валидировала); lot не трогаем;
    slippage/комиссию считает сам broker (costs.fill_price/commission).
    """
    qty = int(intent.quantity)
    if qty <= 0:
        return _invalid(intent, "quantity <= 0")
    if intent.price is None or intent.price <= 0:
        return _invalid(intent, "price <= 0")

    figi = intent.instrument.figi or intent.instrument.ticker
    try:
        pending = await broker.get_position(figi)
    except Exception as exc:  # noqa: BLE001 — ошибка брокера НЕ проглатывается
        return ExecutionResult(intent=intent, status=ExecutionStatus.BROKER_ERROR,
                               error=f"{type(exc).__name__}: {exc}")

    if intent.side is Side.BUY:
        return await _execute_buy(broker, intent, figi, qty, pending, strategy_id)

    # SELL: существующая длинная позиция → close_position; без позиции → short open
    if pending is not None and pending.side in FILLED_LONG:
        if qty > int(pending.qty):
            return _rejected(intent, "SELL exceeds current position (partial + flip not in PaperBroker)")
        if qty < int(pending.qty):
            return _rejected(intent,
                             "partial close unsupported by existing PaperBroker (close_position closes all)")
        return await _close_existing(broker, intent, figi)
    if pending is not None:
        return _rejected(intent, f"SELL on existing {pending.side} position is not open-close without flip")
    return await _open_short(broker, intent, figi, qty, strategy_id)


async def _execute_buy(broker, intent, figi, qty, pending, strategy_id) -> ExecutionResult:
    if pending is not None:
        return _rejected(intent,
                         f"BUY on existing {pending.side} position unsupported by PaperBroker (open from zero only)")
    try:
        actual = await broker.open_position(
            figi=figi,
            ticker=intent.instrument.ticker,
            side="BUY",
            qty=qty,
            price=float(intent.price),
            stop_loss=None,
            take_profit=None,
            strategy_id=strategy_id,
        )
    except Exception as exc:  # noqa: BLE001 — см. __docstring про границу
        return ExecutionResult(intent=intent, status=ExecutionStatus.BROKER_ERROR,
                               error=f"{type(exc).__name__}: {exc}")
    if actual is None:
        # P1-1: PaperBroker возвращает None только при «позиция уже существует» —
        # это детерминированный отказ, а не «результат неизвестен»
        # (PENDING_RECONCILIATION допустим только в runtime-пути с LiveBroker).
        return _rejected(intent, "open_position returned None (position already exists)")
    fill = float(actual)  # фактическая цена входа от брокера, не пересчёт
    return ExecutionResult(
        intent=intent,
        status=ExecutionStatus.FILLED,
        filled_qty=qty,
        fill_price=fill,
        commission=broker.costs.commission(fill * qty),
    )


async def _open_short(broker, intent, figi, qty, strategy_id) -> ExecutionResult:
    try:
        actual = await broker.open_position(
            figi=figi,
            ticker=intent.instrument.ticker,
            side="SELL",
            qty=qty,
            price=float(intent.price),
            stop_loss=None,
            take_profit=None,
            strategy_id=strategy_id,
        )
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(intent=intent, status=ExecutionStatus.BROKER_ERROR,
                               error=f"{type(exc).__name__}: {exc}")
    if actual is None:
        # P1-1: см. _execute_buy — None = детерминированный отказ, не reconciliation
        return _rejected(intent, "open_position returned None (position already exists)")
    fill = float(actual)  # фактическая цена входа от брокера, не пересчёт
    return ExecutionResult(
        intent=intent,
        status=ExecutionStatus.FILLED,
        filled_qty=qty,
        fill_price=fill,
        commission=broker.costs.commission(fill * qty),
    )


async def _close_existing(broker, intent, figi) -> ExecutionResult:
    try:
        trade = await broker.close_position(figi, float(intent.price), "rebalance_exit")
    except Exception as exc:  # noqa: BLE001
        return ExecutionResult(intent=intent, status=ExecutionStatus.BROKER_ERROR,
                               error=f"{type(exc).__name__}: {exc}")
    if trade is None:
        return _rejected(intent, "broker returned no trade for close")
    return ExecutionResult(
        intent=intent,
        status=ExecutionStatus.FILLED,
        filled_qty=int(trade.qty),
        fill_price=float(trade.exit_price),
        commission=float(trade.commission),
        error=None,
    )


async def execute_policy_result(
    broker,
    result: PolicyResult,
    *,
    strategy_id: str = "universe",
) -> list[ExecutionResult]:
    """Batch: строго в порядке accepted, НИКОГДА не принимает rejected.

    Rejected actions не отправляются брокеру вообще (0 вызовов на них).
    """
    out: list[ExecutionResult] = []
    for intent in result.accepted:
        out.append(await execute_order_intent(broker, intent, strategy_id=strategy_id))
    return out