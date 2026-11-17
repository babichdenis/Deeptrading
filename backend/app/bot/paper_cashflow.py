from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math


@dataclass(frozen=True)
class PaperExitCashflow:
    cash_credit: float
    total_net: float | None
    total_commission: float | None


def charge_paper_entry_commission(commission: float) -> float:
    if not math.isfinite(commission) or commission < 0:
        raise ValueError("Commission must be finite and nonnegative")
    return float(Decimal(str(commission)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def settle_paper_exit(gross: float, exit_commission: float,
                      charged_entry_commission: float | None) -> PaperExitCashflow:
    if not math.isfinite(gross):
        raise ValueError("Gross must be finite")
    for fee in (exit_commission, charged_entry_commission):
        if fee is not None and (not math.isfinite(fee) or fee < 0):
            raise ValueError("Commission must be finite and nonnegative")
    cash_credit = round(gross - exit_commission, 2)
    if charged_entry_commission is None:
        return PaperExitCashflow(cash_credit, None, None)
    return PaperExitCashflow(
        cash_credit,
        round(cash_credit - charged_entry_commission, 6),
        round(charged_entry_commission + exit_commission, 6),
    )
