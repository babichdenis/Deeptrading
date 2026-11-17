from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BorrowingPermission:
    figi: str
    side: str
    allowed: bool
    available_at: datetime
    valid_from: datetime
    valid_until: datetime
    evidence_id: str


@dataclass(frozen=True)
class BorrowingDecision:
    allowed: bool
    reason: str
    evidence_id: str | None = None


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


class BorrowingEligibility:
    """Point-in-time instrument permission, not portfolio margin or a risk guarantee.

    Permission validity must come from evidence; a metadata update timestamp alone
    does not establish an eligibility interval. Overlapping contradictory records
    fail closed rather than selecting a convenient source.
    """

    def __init__(self, permissions):
        self._permissions = {}
        for permission in permissions:
            if (not isinstance(permission, BorrowingPermission) or not permission.figi or
                    permission.side not in ('BUY', 'SELL') or type(permission.allowed) is not bool or
                    not permission.evidence_id or not all(_aware(value) for value in (
                        permission.available_at, permission.valid_from, permission.valid_until)) or
                    permission.valid_from >= permission.valid_until):
                raise ValueError('Explicit evidenced borrowing permission and validity interval required')
            self._permissions.setdefault((permission.figi, permission.side), []).append(permission)
        self._permissions = {key: tuple(values) for key, values in self._permissions.items()}

    def evaluate(self, *, figi, side, at):
        if not _aware(at) or side not in ('BUY', 'SELL'):
            raise ValueError('Aware decision clock and BUY/SELL side required')
        known = [permission for permission in self._permissions.get((figi, side), ())
                 if permission.available_at <= at and permission.valid_from <= at < permission.valid_until]
        if not known:
            return BorrowingDecision(False, 'BORROWING_PERMISSION_UNKNOWN')
        if len({permission.allowed for permission in known}) != 1:
            return BorrowingDecision(False, 'BORROWING_PERMISSION_CONFLICT')
        evidence = '|'.join(sorted({permission.evidence_id for permission in known}))
        allowed = known[0].allowed
        return BorrowingDecision(allowed, 'BORROWING_PERMISSION_ALLOWED' if allowed else
                                 'BORROWING_PERMISSION_DENIED', evidence)
