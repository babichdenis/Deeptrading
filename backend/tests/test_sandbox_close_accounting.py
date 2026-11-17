from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.close_position_snapshot import ClosePositionSnapshot
from app.services.close_fill_accounting import build_close_delta
from app.services.sandbox_order_evidence import SandboxOrderAggregate
from tests.test_close_stage_settlement_postgres import NOW
from tests.test_broker_order_journal_postgres import intent


def inputs():
    snapshot = ClosePositionSnapshot('TEST','LONG',20,Decimal('100'),NOW,
        NOW-timedelta(hours=1),'broker_history','TEST','STOP',1,Decimal('.001'),'configured_estimate')
    original = intent(mode='sandbox', created_at=NOW, position_snapshot=snapshot, side='SELL')
    proof = SandboxOrderAggregate(original.broker_account_id,original.client_order_id,'EXCHANGE',
        original.figi,'SELL',10,2,20,Decimal('2100'),Decimal('1.05'),
        NOW+timedelta(seconds=1),NOW+timedelta(seconds=2),'rub')
    return original,proof


def test_close_aggregate_retains_broker_fee_and_order_time_provenance():
    original,proof=inputs()
    trade,payload=build_close_delta(original,proof,0,[])
    assert trade.qty==20 and trade.exit_price==105 and trade.net_pnl==Decimal('96.95')
    assert trade.commission==Decimal('3.05')
    assert 'fills' not in payload and payload['exit_time_source']=='sandbox_order_timestamp'
    record=SimpleNamespace(execution_evidence=payload,executed_units=20,cumulative_units=20)
    assert build_close_delta(original,proof,20,[record])==(None,None)
    with pytest.raises(ValueError,match='changed'):
        build_close_delta(original,replace(proof,notional=Decimal('2101')),20,[record])


def test_live_cannot_use_sandbox_close_aggregate():
    original,proof=inputs()
    with pytest.raises(ValueError):
        build_close_delta(replace(original,mode='live'),proof,0,[])


def test_confirmed_entry_fee_overrides_configured_estimate():
    original,proof=inputs()
    trade,payload=build_close_delta(original,proof,0,[],confirmed_entry_commission=Decimal('.75'))
    assert trade.entry_commission==Decimal('.75')
    assert trade.commission==Decimal('1.80') and trade.net_pnl==Decimal('98.20')
    assert payload['cost_basis']=='broker_commissions'
