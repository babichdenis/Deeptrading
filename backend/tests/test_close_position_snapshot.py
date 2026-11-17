from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.services.broker_order_journal import CloseIntent
from app.services.close_position_snapshot import ClosePositionSnapshot


def snapshot(**changes):
    value = ClosePositionSnapshot('TEST','LONG',20,Decimal('100.123456'),datetime(2026,11,16,12,tzinfo=timezone.utc),
                                  datetime(2026,11,16,10,tzinfo=timezone.utc),'execution_ledger',
                                  'STRATEGY','STOP',1,Decimal('0.0005'),'configured_estimate')
    return replace(value, **changes)


def test_snapshot_roundtrip_preserves_decimal_and_utc():
    original = snapshot(observed_at=datetime(2026,11,16,15,tzinfo=timezone(timedelta(hours=3))))
    payload = original.to_payload()
    assert payload['entry_price'] == '100.123456'
    assert payload['observed_at'] == '2026-11-16T12:00:00+00:00'
    assert ClosePositionSnapshot.from_payload(payload) == original


def test_unknown_entry_time_remains_unknown():
    original = snapshot(entry_time=None,entry_time_source='unknown',commission_rate=None,cost_basis='unknown')
    restored = ClosePositionSnapshot.from_payload(original.to_payload())
    assert restored.entry_time is None and restored.commission_rate is None


@pytest.mark.parametrize('changes', [
    {'entry_price':float('nan')}, {'position_units':True}, {'entry_time_source':'snapshot'},
    {'entry_time_source':'unknown'}, {'observed_at':datetime(2026,11,16)},
    {'entry_time':datetime(2026,11,17,tzinfo=timezone.utc)}, {'cost_basis':'unknown'},
    {'commission_rate':-1}, {'internal_account_id':0},
    {'local_position_id':7}, {'entry_client_order_id':'ENTRY'},
    {'local_position_id':True,'entry_client_order_id':'ENTRY'},
    {'local_position_id':7,'entry_client_order_id':' '},
])
def test_invalid_provenance_or_position_is_rejected(changes):
    with pytest.raises(ValueError): snapshot(**changes).to_payload()


@pytest.mark.parametrize('changes', [{'requested_units':10},{'side':'BUY'},
    {'created_at':datetime(2026,11,16,11,tzinfo=timezone.utc)}])
def test_snapshot_must_match_original_request(changes):
    value = CloseIntent('live','OFFLINE','REQUEST','FIGI','SELL',20,10,
                        datetime(2026,11,16,12,tzinfo=timezone.utc),snapshot())
    with pytest.raises(ValueError): replace(value,**changes).validate()


def test_original_local_identity_survives_snapshot_roundtrip():
    original=snapshot(local_position_id=7,entry_client_order_id='ENTRY_1')
    assert ClosePositionSnapshot.from_payload(original.to_payload())==original
