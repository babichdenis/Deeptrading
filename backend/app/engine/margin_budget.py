from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class MarginDecision:
    allowed: bool
    reason: str
    projected_margin: float | None = None
    limit: float | None = None


def margin_number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def check_margin_budget(*, liquid_value, committed_margin, additional_notional,
                        initial_risk_rate, max_use_fraction):
    """Budget for increasing one RUB equity position, with no netting assumption.

    Caller supplies liquid value after modeled execution costs and committed margin
    including outstanding orders. This is not maintenance-margin liquidation logic.
    """
    values = [margin_number(value) for value in (liquid_value, committed_margin,
              additional_notional, initial_risk_rate, max_use_fraction)]
    if any(value is None for value in values):
        return MarginDecision(False, 'MARGIN_INPUT_UNKNOWN')
    liquid, committed, notional, rate, fraction = values
    if liquid <= 0 or committed < 0 or notional <= 0 or rate <= 0 or not 0 < fraction <= 1:
        return MarginDecision(False, 'MARGIN_INPUT_INVALID')
    required = committed + notional * rate
    limit = liquid * fraction
    if not isfinite(required) or not isfinite(limit):
        return MarginDecision(False, 'MARGIN_INPUT_INVALID')
    if required >= liquid or required > limit:
        return MarginDecision(False, 'MARGIN_BUDGET_EXCEEDED', required, limit)
    return MarginDecision(True, 'MARGIN_BUDGET_ALLOWED', required, limit)
