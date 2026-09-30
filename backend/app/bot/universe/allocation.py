from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.bot.universe.domain import (
    AllocationInput,
    InstrumentRef,
    SelectionResult,
    TargetPortfolio,
    TargetPosition,
)


def _lookup(mapping: dict, instrument: InstrumentRef, default):
    if instrument.figi and instrument.figi in mapping:
        return mapping[instrument.figi]
    return mapping.get(instrument.ticker, default)


class AllocationPolicy(Protocol):
    """Отделяет «что распределяем» (AllocationInput) от «как» (policy).

    Policy чистая и детерминированная: никаких DB/broker/скрытых зависимостей
    и никакого wall-clock внутри расчёта — as_of приходит снаружи.
    """

    allocation_method: str

    def allocate(
        self,
        *,
        selection: SelectionResult,
        equity: float,
        prices: dict,
        lot_sizes: dict,
        as_of: datetime | None = None,
    ) -> TargetPortfolio:
        ...


class EqualWeightAllocation:
    """Первый production-ready policy: равные веса 1/N по всем selected.

    Капитал, доступный под позиции: equity * (1 - reserve_cash_pct), остальное
    уходит в cash_target. Количество округляется ВНИЗ до допустимого лота;
    остаток после floor-rounding остаётся в unallocated_cash и НЕ
    перераспределяется (Phase 6 намеренно это не делает).
    """

    allocation_method = "equal_weight"

    def __init__(self, reserve_cash_pct: float = 0.0):
        if not 0.0 <= reserve_cash_pct <= 1.0:
            raise ValueError("reserve_cash_pct должен быть в [0, 1]")
        self.reserve_cash_pct = reserve_cash_pct

    def allocate(
        self,
        *,
        selection: SelectionResult,
        equity: float,
        prices: dict,
        lot_sizes: dict,
        as_of: datetime | None = None,
    ) -> TargetPortfolio:
        return _allocate_equal_weight(
            AllocationInput(
                selection=selection,
                equity=equity,
                prices=prices,
                lot_sizes=lot_sizes,
                as_of=as_of,
            ),
            reserve_cash_pct=self.reserve_cash_pct,
        )


def _allocate_equal_weight(
    inp: AllocationInput,
    *,
    reserve_cash_pct: float,
) -> TargetPortfolio:
    equity = float(inp.equity or 0.0)
    as_of = inp.as_of if inp.as_of is not None else inp.selection.as_of
    n = len(inp.selection.selected)

    if equity <= 0 or n == 0:
        return TargetPortfolio(
            as_of=as_of,
            equity=equity,
            positions=(),
            cash_target=equity,
            unallocated_cash=equity,
            allocation_method="equal_weight",
            reserve_cash_pct=reserve_cash_pct,
        )

    investable = equity * (1.0 - reserve_cash_pct)
    weight = 1.0 / n
    positions: list[TargetPosition] = []
    used = 0.0

    for item in inp.selection.selected:
        price = _lookup(inp.prices, item.instrument, 0.0)
        lot = _lookup(inp.lot_sizes, item.instrument, 0)
        if price is None or lot is None or float(price) <= 0 or int(lot) <= 0:
            continue
        price = float(price)
        lot = int(lot)

        target_notional = investable * weight
        raw_qty = target_notional / price
        target_qty = int(raw_qty // lot) * lot
        actual_notional = target_qty * price
        actual_weight = actual_notional / equity if equity else 0.0
        used += actual_notional
        positions.append(
            TargetPosition(
                instrument=item.instrument,
                target_weight=weight,
                actual_weight=round(actual_weight, 10),
                target_qty=target_qty,
                actual_qty=target_qty,
                target_notional=round(target_notional, 10),
                actual_notional=round(actual_notional, 10),
                price=price,
                lot_size=lot,
            )
        )

    return TargetPortfolio(
        as_of=as_of,
        equity=equity,
        positions=tuple(positions),
        cash_target=round(equity * reserve_cash_pct, 10),
        unallocated_cash=round(equity - used, 10),
        allocation_method="equal_weight",
        reserve_cash_pct=reserve_cash_pct,
    )