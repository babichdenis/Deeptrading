from dataclasses import dataclass
from datetime import datetime
from math import isfinite

from app.engine.margin_budget import MarginDecision, check_margin_budget, margin_number


@dataclass(frozen=True)
class RiskRate:
    figi: str
    side: str
    initial_rate: float
    collateral_eligible: bool
    available_at: datetime
    valid_from: datetime
    valid_until: datetime
    evidence_id: str


@dataclass(frozen=True)
class MarginExposure:
    figi: str
    side: str
    notional: float


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


class PortfolioMarginPolicy:
    """Initial-margin admission for RUB cash and eligible RUB equity positions.

    Exact rate validity and collateral eligibility must be supplied as evidence.
    No offsetting, pending orders, FX or maintenance liquidation are simulated here.
    """

    def __init__(self, rates, *, max_use_fraction):
        fraction = margin_number(max_use_fraction)
        if fraction is None or not 0 < fraction <= 1:
            raise ValueError('Explicit margin-use fraction in (0, 1] required')
        self.max_use_fraction = fraction
        records = {}
        for rate in rates:
            if (not isinstance(rate, RiskRate) or not rate.figi or rate.side not in ('BUY', 'SELL') or
                    margin_number(rate.initial_rate) is None or float(rate.initial_rate) <= 0 or
                    type(rate.collateral_eligible) is not bool or not rate.evidence_id or
                    not all(_aware(t) for t in (rate.available_at, rate.valid_from, rate.valid_until)) or
                    rate.valid_from >= rate.valid_until):
                raise ValueError('Valid evidenced risk rate and explicit validity interval required')
            records.setdefault((rate.figi, rate.side), []).append(rate)
        self._rates = {key: tuple(values) for key, values in records.items()}

    def _rate(self, exposure, at):
        records = [rate for rate in self._rates.get((exposure.figi, exposure.side), ())
                   if rate.available_at <= at and rate.valid_from <= at < rate.valid_until]
        if not records:
            return None, 'MARGIN_RATE_UNKNOWN'
        values = {(float(rate.initial_rate), rate.collateral_eligible) for rate in records}
        if len(values) != 1:
            return None, 'MARGIN_RATE_CONFLICT'
        rate, eligible = values.pop()
        if not eligible:
            return None, 'MARGIN_COLLATERAL_SCOPE_UNSUPPORTED'
        return rate, None

    def evaluate(self, *, at, positions, proposal, projected_liquid):
        if not _aware(at):
            raise ValueError('Aware margin decision clock required')
        committed = 0.0
        positions = tuple(positions)
        proposal_rate = None
        for index, exposure in enumerate((*positions, proposal)):
            if (not isinstance(exposure, MarginExposure) or not exposure.figi or
                    exposure.side not in ('BUY', 'SELL') or margin_number(exposure.notional) is None or
                    float(exposure.notional) <= 0):
                return MarginDecision(False, 'MARGIN_EXPOSURE_INVALID')
            rate, reason = self._rate(exposure, at)
            if reason is not None:
                return MarginDecision(False, reason)
            if index < len(positions):
                committed += float(exposure.notional) * rate
            else:
                proposal_rate = rate
        if not isfinite(committed):
            return MarginDecision(False, 'MARGIN_EXPOSURE_INVALID')
        return check_margin_budget(liquid_value=projected_liquid, committed_margin=committed,
                                   additional_notional=proposal.notional, initial_risk_rate=proposal_rate,
                                   max_use_fraction=self.max_use_fraction)
