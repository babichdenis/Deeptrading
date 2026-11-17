from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import hashlib
import json
from math import isfinite
from types import MappingProxyType


MINUTE=timedelta(minutes=1)
HOUR=timedelta(hours=1)


def hour_boundary(at):
    if not isinstance(at,datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('Aware context clock required')
    return at.astimezone(timezone.utc).replace(minute=0,second=0,microsecond=0)


@dataclass(frozen=True)
class HourlyFeature:
    status: str
    return_60m: float | None
    relative_return_60m: float | None
    observed_minutes: int
    last_closed_at: datetime | None


@dataclass(frozen=True)
class HourlyContext:
    as_of: datetime
    available_at: datetime
    expires_at: datetime
    market_id: str
    features: object
    input_hash: str

    def usable_at(self, at):
        hour_boundary(at)
        return self.available_at <= at < self.expires_at


def build_hourly_context(series, *, market_id, as_of, available_at, minimum_observations=48):
    """One-hour observed return context, independent of strategy and broker I/O.

    Exact endpoint closes are required. Coverage counts measure observations, not
    proof of liquidity or a trading calendar. Missing values never become neutral.
    """
    boundary=hour_boundary(as_of)
    hour_boundary(available_at)
    if as_of!=boundary or not as_of<=available_at<as_of+HOUR:
        raise ValueError('Context must publish within its own hourly interval')
    as_of = boundary
    available_at = available_at.astimezone(timezone.utc)
    if type(minimum_observations) is not int or not 1<=minimum_observations<=60 or not market_id:
        raise ValueError('Explicit market and valid observation threshold required')
    inputs={};raw_features={}
    for figi,bars in sorted(series.items()):
        previous=None;closed={}
        for bar in bars:
            hour_boundary(bar.ts)
            if previous is not None and bar.ts<=previous:
                raise ValueError('Strictly ordered unique minute bars required')
            previous=bar.ts
            if bar.ts.timestamp()%60:
                raise ValueError('Minute-start timestamp required')
            at=bar.ts.astimezone(timezone.utc)+MINUTE
            if not as_of-HOUR<=at<=as_of:
                continue
            if isinstance(bar.close,bool) or not isinstance(bar.close,(int,float)) or not isfinite(bar.close) or bar.close<=0:
                raise ValueError('Positive finite closed price required')
            closed[at]=float(bar.close)
        inputs[figi]=[(at.isoformat(),price) for at,price in sorted(closed.items())]
        count=sum(as_of-HOUR<at<=as_of for at in closed)
        last=max(closed) if closed else None
        if as_of-HOUR not in closed or as_of not in closed:
            raw_features[figi]=HourlyFeature('ENDPOINT_UNKNOWN',None,None,count,last)
        elif count<minimum_observations:
            raw_features[figi]=HourlyFeature('INSUFFICIENT_OBSERVATIONS',None,None,count,last)
        else:
            raw_features[figi]=HourlyFeature('KNOWN',closed[as_of]/closed[as_of-HOUR]-1,None,count,last)
    market=raw_features.get(market_id)
    features={}
    for figi,feature in raw_features.items():
        relative=None
        status=feature.status
        if status=='KNOWN':
            if market is None or market.status!='KNOWN':status='MARKET_UNKNOWN'
            else:relative=feature.return_60m-market.return_60m
        features[figi]=HourlyFeature(status,feature.return_60m,relative,feature.observed_minutes,feature.last_closed_at)
    payload={'as_of':as_of.isoformat(),'market_id':market_id,'minimum_observations':minimum_observations,'inputs':inputs}
    fingerprint=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    return HourlyContext(as_of,available_at,as_of+HOUR,market_id,MappingProxyType(features),fingerprint)
