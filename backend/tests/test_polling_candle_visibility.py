from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from app.bot.feed import polling_candle_closed

T = datetime(2026, 11, 16, 9, 25, tzinfo=timezone.utc)


@pytest.mark.parametrize('complete,seconds,expected', [(False, 299, False), (True, 299, False), (False, 300, False), (True, 300, True), (True, 301, True)])
def test_polling_requires_broker_completeness_and_closed_interval(complete, seconds, expected):
    candle = SimpleNamespace(time=T, is_complete=complete)
    assert polling_candle_closed(candle, '5min', T+timedelta(seconds=seconds)) == expected


def test_incomplete_observation_can_later_become_closed():
    candle = SimpleNamespace(time=T, is_complete=False)
    assert not polling_candle_closed(candle, '5min', T+timedelta(minutes=4))
    candle.is_complete = True
    assert polling_candle_closed(candle, '5min', T+timedelta(minutes=5))


def test_naive_clock_is_rejected():
    with pytest.raises(ValueError):
        polling_candle_closed(SimpleNamespace(time=T, is_complete=True), '5min', T.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_actual_polling_does_not_remember_or_persist_incomplete_candle(monkeypatch):
    import asyncio
    from app.bot import feed as module
    from t_tech.invest import Quotation
    observed = [T + timedelta(minutes=4)]
    calls = []
    persisted = []

    class Clock:
        @staticmethod
        def now(tz):
            return observed[0]

    async def advance(seconds):
        observed[0] += timedelta(seconds=seconds)

    class Client:
        def __init__(self, *args, **kwargs):
            self.market_data = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get_candles(self, **kwargs):
            calls.append(kwargs)
            value = Quotation(units=100, nano=0)
            return SimpleNamespace(candles=[SimpleNamespace(
                time=T, is_complete=len(calls) > 1, open=value, high=value,
                low=value, close=value, volume=1)])

    monkeypatch.setattr(module, 'datetime', Clock)
    monkeypatch.setattr(module, 'AsyncClient', Client)
    monkeypatch.setattr(module.asyncio, 'sleep', advance)
    subject = module.CandleFeed('OFFLINE', '5min', ['FIGI'])
    monkeypatch.setattr(subject, '_queue_persist', persisted.append)
    stream = subject._polling()
    try:
        candle = await asyncio.wait_for(anext(stream), timeout=2)
        assert len(calls) == 2
        assert candle.ts == T
        assert persisted == [candle]
        assert calls[0]['to'] - calls[0]['from_'] == timedelta(minutes=20)
    finally:
        await stream.aclose()


def test_history_fetch_drops_incomplete_broker_candles(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace

    from app.services import tinvest

    start = datetime(2026, 10, 9, 7, tzinfo=timezone.utc)
    prices = dict(open=11, high=11, low=11, close=11, volume=1)
    candles = [
        SimpleNamespace(time=start, is_complete=True, **prices),
        SimpleNamespace(time=start + timedelta(minutes=1), is_complete=False, **prices),
    ]
    market_data = SimpleNamespace(get_candles=lambda **kwargs: SimpleNamespace(candles=candles))
    monkeypatch.setattr(tinvest, "Client", lambda _: nullcontext(SimpleNamespace(market_data=market_data)))
    monkeypatch.setattr(tinvest, "get_settings", lambda: SimpleNamespace(tinkoff_token="fixture"))
    monkeypatch.setattr(tinvest, "quotation_to_decimal", lambda value: value)
    rows = tinvest.fetch_candles(
        "FIGI", tinvest.INTERVAL_NAMES["1min"], start, start + timedelta(minutes=2)
    )
    assert [row["ts"] for row in rows] == [start]
