from datetime import datetime,timedelta,timezone
from dataclasses import replace
import pytest
from app.engine.models import Candle
from app.engine.hourly_context import build_hourly_context,hour_boundary


T=datetime(2026,9,1,10,tzinfo=timezone.utc)
M=timedelta(minutes=1)


def series():
    bars=[Candle(T-timedelta(hours=1)-M+i*M,100,102,99,100+i/60,10) for i in range(61)]
    return {'INDEX':bars,'A':[replace(b,close=100+i/30,high=103) for i,b in enumerate(bars)]}


def build(data=None,**kwargs):
    return build_hourly_context(series() if data is None else data,market_id='INDEX',as_of=T,available_at=T,**kwargs)


def test_closed_hour_exact_endpoints_and_relative_return():
    result=build();assert result.features['A'].observed_minutes==60
    assert result.features['A'].return_60m==pytest.approx(.02)
    assert result.features['A'].relative_return_60m==pytest.approx(.01)


def test_forming_or_future_price_changes_do_not_change_context_or_hash():
    data=series();baseline=build(data)
    data['A'] += [Candle(T,1,9999,1,9999,1),Candle(T+M,1,9999,1,9999,1)]
    changed=build(data);assert changed.input_hash==baseline.input_hash
    assert changed.features==baseline.features


def test_missing_market_is_unknown_not_neutral():
    data=series();del data['INDEX'];assert build(data).features['A'].status=='MARKET_UNKNOWN'


def test_insufficient_observations_and_missing_endpoint_are_distinct():
    data=series();data['A']=data['A'][::2]
    assert build(data).features['A'].status=='INSUFFICIENT_OBSERVATIONS'
    data=series();data['A']=data['A'][:-1]
    assert build(data).features['A'].status=='ENDPOINT_UNKNOWN'


def test_publication_and_expiry_prevent_early_or_stale_use():
    result=build_hourly_context(series(),market_id='INDEX',as_of=T,available_at=T+M)
    assert not result.usable_at(T)
    assert result.usable_at(T+M)
    assert not result.usable_at(T+timedelta(hours=1))
    with pytest.raises(TypeError):result.features['A']=None


def test_hour_boundary_is_timezone_independent():
    assert hour_boundary((T+M).astimezone(timezone(timedelta(hours=3))))==T


def test_equivalent_timezone_inputs_have_identical_snapshot_identity():
    expected = build()
    offset = timezone(timedelta(hours=3))
    shifted = {key: [replace(bar, ts=bar.ts.astimezone(offset)) for bar in bars] for key, bars in series().items()}
    actual = build_hourly_context(shifted, market_id='INDEX', as_of=T.astimezone(offset), available_at=T.astimezone(offset))
    assert actual.input_hash == expected.input_hash
    assert actual.features == expected.features
    assert actual.as_of.tzinfo == timezone.utc
    assert actual.available_at.tzinfo == timezone.utc
    assert all(feature.last_closed_at.tzinfo == timezone.utc for feature in actual.features.values())


def test_hourly_return_cannot_cross_futures_contracts():
    market = series()["INDEX"]
    old = [replace(bar, close=87.12) for bar in market]
    new = [replace(bar, close=86.29) for bar in market]
    old_id, new_id = "MOEX:RFUD:BRU6", "MOEX:RFUD:BRV6"

    split = build({"INDEX": market, old_id: old[:31], new_id: new[31:]})
    for contract_id in (old_id, new_id):
        assert split.features[contract_id].status == "ENDPOINT_UNKNOWN"
        assert split.features[contract_id].return_60m is None

    overlapping = build({"INDEX": market, old_id: old, new_id: new})
    for contract_id in (old_id, new_id):
        assert overlapping.features[contract_id].status == "KNOWN"
        assert overlapping.features[contract_id].return_60m == 0
