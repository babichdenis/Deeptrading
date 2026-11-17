import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.engine.exits import AtrStopPolicy, AtrTrailingPolicy, FixedSlTpPolicy
from app.engine.models import Candle, Side
from app.services.exit_policy_snapshot import ExitPolicySnapshot


@pytest.mark.parametrize('side', [Side.BUY, Side.SELL])
@pytest.mark.parametrize('policy,distance,target', [
    (FixedSlTpPolicy(stop_pct=0.05, target_pct=0.1), 5, 10),
    (AtrStopPolicy(period=7, multiplier=3, risk_reward=2, trail_activation_r=1, trail_distance_r=2), 6, 12),
    (AtrTrailingPolicy(period=7, initial_stop_atr=2, activation_atr=1, trail_distance_atr=2), 4, None)])
def test_restored_policy_reproduces_entry_and_trailing_golden(policy, distance, target, side):
    start = datetime(2026, 11, 16, tzinfo=timezone.utc)
    bars = [Candle(start + timedelta(minutes=i), 100, 101, 99, 100, 1000) for i in range(30)]
    original = policy.plan_entry(side, 100, bars)
    sign = 1 if side == Side.BUY else -1
    assert original.stop_loss == 100 - sign * distance
    assert original.take_profit == (pytest.approx(100 + sign * target) if target is not None else None)
    payload = json.loads(json.dumps(ExitPolicySnapshot.capture(policy).to_payload()))
    assert '_atr_cache' not in payload['parameters']
    restored = ExitPolicySnapshot.from_payload(payload).restore()
    assert restored.plan_entry(side, 100, bars) == original
    if hasattr(policy, 'update_stop'):
        last = original.stop_loss
        for peak in (100, 105, 110, 104):
            actual_peak = peak if side == Side.BUY else 200 - peak
            expected = policy.update_stop(side, 100, last, bars, qty=10, commission=2, peak_price=actual_peak)
            assert restored.update_stop(side, 100, last, bars, qty=10, commission=2, peak_price=actual_peak) == expected
            last = expected


@pytest.mark.parametrize('mutation', ['unknown_policy', 'identity_type', 'version', 'missing', 'extra', 'nan', 'bool', 'null'])
def test_unknown_or_incomplete_policy_cannot_use_current_defaults(mutation):
    payload = ExitPolicySnapshot.capture(FixedSlTpPolicy()).to_payload()
    if mutation == 'unknown_policy': payload['policy_id'] = 'unknown'
    elif mutation == 'identity_type': payload['policy_id'] = []
    elif mutation == 'version': payload['policy_version'] = 'next'
    elif mutation == 'missing': del payload['parameters']['stop_pct']
    elif mutation == 'extra': payload['parameters']['new_default'] = 1
    elif mutation == 'nan': payload['parameters']['stop_pct'] = float('nan')
    elif mutation == 'bool': payload['parameters']['stop_pct'] = True
    else: payload['parameters']['stop_pct'] = None
    with pytest.raises(ValueError): ExitPolicySnapshot.from_payload(payload)
