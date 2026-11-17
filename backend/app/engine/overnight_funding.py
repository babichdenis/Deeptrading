from dataclasses import dataclass
from datetime import date, datetime
from math import isfinite
from zoneinfo import ZoneInfo

from app.engine.broker_tariffs import daily_uncovered_fee


MSK = ZoneInfo('Europe/Moscow')


@dataclass(frozen=True)
class FundingSession:
    price_date: date
    evidence_id: str


@dataclass(frozen=True)
class FundingClose:
    price: float
    price_date: date
    available_at: datetime
    evidence_id: str


@dataclass(frozen=True)
class FundingAssessment:
    at: datetime
    billing_date: date
    amount: float | None
    uncovered_value: float | None
    reason: str
    evidence_ids: tuple[str, ...]


class OvernightFundingPolicy:
    """Explicit tariff-window scenario; calendar/previous-close evidence supplied separately.

    Uses aggregate RUB-equity borrowing, never a fee per trade or gross exposure.
    The chosen minute is an assumption until actual broker valuation timing is known.
    """

    def __init__(self, *, plan, valuation_minute_msk, small_balance_exempt,
                 previous_session=None, closing_quote=None):
        if type(valuation_minute_msk) is not int or not 0 <= valuation_minute_msk <= 240:
            raise ValueError('Explicit valuation minute within 00:00–04:00 MSK required')
        daily_uncovered_fee(0, plan=plan, small_balance_exempt=small_balance_exempt)
        self.plan = plan
        self.valuation_minute_msk = valuation_minute_msk
        self.small_balance_exempt = small_balance_exempt
        self.previous_session = previous_session
        self.closing_quote = closing_quote

    def evaluate(self, at, account):
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError('Aware funding clock required')
        local = at.astimezone(MSK)
        if local.hour * 60 + local.minute != self.valuation_minute_msk or local.second or local.microsecond:
            return None
        billing_date = local.date()
        snapshot = account.snapshot(at=at)
        uncovered = snapshot.uncovered_cash
        evidence = []
        shorts = [position for position in account.positions if position.side == 'SELL']

        def unknown(reason):
            return FundingAssessment(at, billing_date, None, None, reason, tuple(evidence))

        if shorts:
            session = self.previous_session(billing_date) if self.previous_session else None
            if not isinstance(session, FundingSession) or not session.evidence_id or session.price_date >= billing_date:
                return unknown('PREVIOUS_SESSION_UNKNOWN')
            evidence.append(session.evidence_id)
            for position in shorts:
                quote = self.closing_quote(position.figi, session.price_date) if self.closing_quote else None
                if not isinstance(quote, FundingClose) or not quote.evidence_id or quote.price_date != session.price_date:
                    return unknown('CLOSING_QUOTE_UNKNOWN')
                if (quote.available_at.tzinfo is None or quote.available_at.utcoffset() is None or
                        quote.available_at > at or isinstance(quote.price, bool) or
                        not isinstance(quote.price, (int, float)) or not isfinite(quote.price) or quote.price <= 0):
                    return unknown('CLOSING_QUOTE_INVALID_OR_FUTURE')
                uncovered += position.shares * quote.price
                evidence.append(quote.evidence_id)
        if not isfinite(uncovered) or uncovered > 10000000:
            return unknown('PERCENTAGE_FEE_ROUNDING_UNRESOLVED_OR_INVALID_BASIS')
        fee = daily_uncovered_fee(uncovered, plan=self.plan, small_balance_exempt=self.small_balance_exempt)
        return FundingAssessment(at, billing_date, fee, uncovered,
                                 'KNOWN' if fee is not None else 'SMALL_BALANCE_EXEMPTION_UNKNOWN', tuple(evidence))
