import pytest
from app.bot.universe.domain import InstrumentRef
from app.bot.universe.eligibility import broker_eligibility as raw_broker_eligibility
from datetime import datetime, timedelta, timezone

NOW = datetime(2026,11,15,23,tzinfo=timezone.utc)

def broker_eligibility(*args, **kwargs):
    return raw_broker_eligibility(*args, observed_at=NOW, as_of=NOW, **kwargs)

def evidence():
    return dict(figi='EXACT',uid='uid',class_code='TQBR',lot=1,api_trade_available_flag=True,buy_available_flag=True,sell_available_flag=True,short_enabled_flag=True)

@pytest.mark.parametrize('field,value,reason',[
    ('figi','OTHER','broker_identity_mismatch'),
    ('class_code','37M','trading_class_not_allowed'),
    ('lot',0,'lot_unknown'),
    ('lot',True,'lot_unknown'),
    ('buy_available_flag',False,'buy_available_not_confirmed'),
    ('api_trade_available_flag',None,'api_trade_unavailable'),
])
def test_invalid_evidence_rejects(field,value,reason):
    data=evidence(); data[field]=value
    result=broker_eligibility(InstrumentRef(figi='EXACT',ticker='TEST'),data,side='long')
    assert not result.eligible
    assert reason in result.reasons


def test_short_permission_is_independent_of_long():
    data=evidence();data['short_enabled_flag']=False
    ref=InstrumentRef(figi='EXACT',ticker='TEST')
    assert broker_eligibility(ref,data,side='long').eligible
    assert not broker_eligibility(ref,data,side='short').eligible


def test_tier_cannot_override_broker_verdict():
    ref=InstrumentRef(figi='EXACT',ticker='TEST')
    for tier in ('eligible','eligible2',None):
        data={**evidence(),'eligible_tier':tier}
        assert broker_eligibility(ref,data,side='long').eligible
    assert not broker_eligibility(ref,None,side='long').eligible


@pytest.mark.parametrize('observed,reason',[
    (None,'broker_evidence_time_unknown'),
    (NOW+timedelta(seconds=1),'broker_evidence_from_future'),
    (NOW-timedelta(hours=1,seconds=1),'broker_evidence_stale'),
])
def test_broker_evidence_must_be_available_and_fresh(observed,reason):
    result=raw_broker_eligibility(InstrumentRef(figi='EXACT',ticker='TEST'),evidence(),
                                  side='long',observed_at=observed,as_of=NOW)
    assert not result.eligible
    assert reason in result.reasons


def test_max_age_boundary_is_inclusive():
    result=raw_broker_eligibility(InstrumentRef(figi='EXACT',ticker='TEST'),evidence(),
                                  side='long',observed_at=NOW-timedelta(hours=1),as_of=NOW)
    assert result.eligible
