from dataclasses import dataclass
from math import floor, isfinite, nextafter

from app.engine.costs import CostModel


_RATES = {'trader': (0.0005, 0.0006), 'investor': (0.003, 0.0031)}
_LIMITS = (50000, 100000, 250000, 500000, 1000000, 2500000, 5000000, 10000000)
_DAILY = {
    'trader': (40, 75, 180, 350, 700, 1750, 3500, 6900),
    'investor': (40, 80, 190, 375, 750, 1850, 3700, 7200),
}
_PERCENT = {'trader': (0.00068, 0.00065, 0.00057), 'investor': (0.00070, 0.00067, 0.00059)}
_VERSIONS = {'trader': 'Т-ТРЕЙД-260522', 'investor': 'Т-ИНВЕСТ-260424'}


def _plan(plan: str) -> None:
    if plan not in _RATES:
        raise ValueError('Unsupported tariff')


def _amount(amount: float) -> None:
    if not isfinite(amount) or amount < 0:
        raise ValueError('Amount must be finite and nonnegative')


def equity_trade_fee(notional: float, *, plan: str, iceberg: bool = False) -> float:
    """One executed base-list equity transaction, in its transaction currency."""
    _plan(plan)
    _amount(notional)
    if notional == 0 or type(iceberg) is not bool:
        raise ValueError('An executed transaction needs positive notional and boolean order type')
    raw = notional * _RATES[plan][int(iceberg)]
    return max(0.01, floor(nextafter(raw * 100, float('inf')) + 0.5) / 100)


def daily_uncovered_fee(uncovered_rub: float | None, *, plan: str,
                        small_balance_exempt: bool | None) -> float | None:
    """One calendar-day account valuation; percentage tiers returned before settlement rounding.

    The caller supplies aggregate uncovered value, not gross exposure, and resolves
    the three-account exemption using all accounts. Missing evidence stays unknown.
    """
    _plan(plan)
    if small_balance_exempt is not None and type(small_balance_exempt) is not bool:
        raise ValueError('Exemption must be boolean or unknown')
    if uncovered_rub is None:
        return None
    _amount(uncovered_rub)
    if uncovered_rub == 0:
        return 0.0
    if uncovered_rub <= 5000:
        return None if small_balance_exempt is None else (0.0 if small_balance_exempt else 5.0)
    for limit, fee in zip(_LIMITS, _DAILY[plan]):
        if uncovered_rub <= limit:
            return float(fee)
    rate = _PERCENT[plan][0 if uncovered_rub <= 25000000 else 1 if uncovered_rub <= 50000000 else 2]
    return uncovered_rub * rate


def billing_period_service_fee(*, plan: str, qualifying_trade: bool | None,
                               prior_full_period_asset_exemption: bool | None,
                               prior_full_period_turnover_exemption: bool | None) -> float | None:
    """One fee across contracts; caller establishes full-period eligibility and billing dates."""
    _plan(plan)
    flags = (qualifying_trade, prior_full_period_asset_exemption, prior_full_period_turnover_exemption)
    if any(flag is not None and type(flag) is not bool for flag in flags):
        raise ValueError('Eligibility must be boolean or unknown')
    if plan == 'investor' or qualifying_trade is False:
        return 0.0
    if prior_full_period_asset_exemption is True or prior_full_period_turnover_exemption is True:
        return 0.0
    if any(flag is None for flag in flags):
        return None
    return 390.0


@dataclass(frozen=True, kw_only=True)
class EquityTariffCostModel(CostModel):
    plan: str
    iceberg: bool = False
    id: str = 'document_equity_tariff'

    def __post_init__(self) -> None:
        _plan(self.plan)
        if type(self.iceberg) is not bool:
            raise ValueError('Order type must be boolean')
        object.__setattr__(self, 'commission_rate', _RATES[self.plan][int(self.iceberg)])
        object.__setattr__(self, 'version', _VERSIONS[self.plan])

    def commission(self, notional: float) -> float:
        return equity_trade_fee(notional, plan=self.plan, iceberg=self.iceberg)
