from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe.domain import InstrumentRef
from app.bot.universe.trend import compute_trend_features, compute_trend_features_many
from app.bot.universe.volatility import compute_volatility_features, compute_volatility_features_many
from app.engine.models import Candle


@pytest.mark.parametrize('minutes', [1, 5, 15, 60, 240, 1440])
@pytest.mark.parametrize('measure,batch', [
    (compute_trend_features, compute_trend_features_many),
    (compute_volatility_features, compute_volatility_features_many),
])
def test_closed_bar_boundary_and_batch_passthrough(minutes, measure, batch):
    step = timedelta(minutes=minutes)
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    ref = InstrumentRef('SBER', 'test')
    bars = [Candle(ts=start + i * step, open=100+i, high=102+i,
                   low=99+i, close=101+i, volume=1000) for i in range(45)]
    bars[-1] = Candle(ts=bars[-1].ts, open=144, high=1000, low=1, close=900, volume=1000)
    before = bars[-1].ts + step - timedelta(microseconds=1)
    kwargs = dict(as_of=before, window=30, interval=step)
    result = measure(ref, bars, **kwargs)
    assert result.valid
    assert result == measure(ref, bars[:-1], **kwargs)
    assert result == batch([ref], {'test': bars}, **kwargs)[0]
    closed = measure(ref, bars, as_of=before + timedelta(microseconds=1), window=30, interval=step)
    assert closed.valid
    assert closed.bars_used == result.bars_used + 1
    field = 'atr' if measure is compute_volatility_features else 'slope'
    assert getattr(closed, field) != getattr(result, field)


@pytest.mark.parametrize('measure', [compute_trend_features, compute_volatility_features])
@pytest.mark.parametrize('minutes', [0, -1])
def test_invalid_interval_rejected(measure, minutes):
    with pytest.raises(ValueError, match='interval must be positive'):
        measure(InstrumentRef('SBER'), [], as_of=datetime.now(timezone.utc),
                window=30, interval=timedelta(minutes=minutes))
