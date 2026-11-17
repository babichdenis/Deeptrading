from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Sequence

from app.bot.universe.domain import InstrumentRef, MarketFeatures
from app.bot.universe.screener import TrendStrengthScreener
from app.bot.universe.selection import RankedCandidate, select_top_n


@dataclass(frozen=True)
class ResearchSelection:
    as_of: datetime
    selected: tuple[RankedCandidate, ...]
    rejected: tuple[tuple[InstrumentRef, str], ...]


def select_volatile_trends(
    features: Sequence[MarketFeatures],
    *,
    as_of: datetime,
    eligible: frozenset[InstrumentRef],
    min_strength: float,
    top_n: int,
) -> ResearchSelection:
    if as_of.utcoffset() is None:
        raise ValueError('as_of must be timezone aware')
    screener = TrendStrengthScreener(min_strength)
    scores = {}
    rejected = []
    identities = set()
    for item in sorted(features, key=lambda f: f.instrument):
        ref = item.instrument
        identity = ref.figi or ref.ticker
        if identity in identities:
            raise ValueError('duplicate instrument')
        identities.add(identity)
        reason = ''
        vol, trend = item.volatility, item.trend
        if ref not in eligible:
            reason = 'ineligible'
        elif not item.valid:
            reason = 'invalid_features'
        elif any(part.as_of != as_of for part in (item, vol, trend)):
            reason = 'snapshot_mismatch'
        elif vol.instrument != ref or trend.instrument != ref:
            reason = 'instrument_mismatch'
        elif vol.atr_pct is None or not isfinite(vol.atr_pct) or vol.atr_pct <= 0:
            reason = 'invalid_volatility'
        elif not isfinite(trend.strength) or not 0 <= trend.strength <= 1:
            reason = 'invalid_strength'
        else:
            verdict = screener.accepts(item)
            if not verdict.accepted:
                reason = 'weak_trend'
        if reason:
            rejected.append((ref, reason))
        else:
            scores[ref] = vol.atr_pct
    selected = select_top_n(scores, scores.__getitem__, top_n=top_n)
    return ResearchSelection(as_of, selected, tuple(rejected))
