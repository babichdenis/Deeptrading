from datetime import timedelta


RECLAIM_SETUPS = frozenset(('rsi_reversal', 'bollinger_reclaim'))
BREAKOUT_SETUPS = frozenset(('donchian_breakout', 'range_compression_breakout'))


def regime_entry_rejection(*, label, closed_at, as_of, buy_setups):
    if as_of.utcoffset() is None:
        raise ValueError('Aware decision time required')
    if closed_at is None or closed_at.utcoffset() is None:
        return 'regime_missing_or_stale'
    age = as_of - closed_at
    if age < timedelta(0) or age > timedelta(hours=1):
        return 'regime_missing_or_stale'
    votes = frozenset(buy_setups)
    if label not in ('RANGE', 'TREND_UP'):
        return 'regime_not_allowed'
    if 'pullback_ema' not in votes:
        return 'missing_pullback_confirmation'
    if label == 'RANGE' and not votes.intersection(RECLAIM_SETUPS):
        return 'missing_reclaim_trigger'
    if label == 'TREND_UP' and not votes.intersection(BREAKOUT_SETUPS):
        return 'missing_breakout_trigger'
    return None
