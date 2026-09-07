from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from t_tech.invest import (
    CandleInterval,
    CandleSubscription,
    MarketDataRequest,
    SubscribeCandlesRequest,
    SubscriptionAction,
)
from t_tech.invest import AsyncClient
from t_tech.invest.utils import quotation_to_decimal


@dataclass
class ClosedCandle:
    figi: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


INTERVAL_ENUM = {
    "1min": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "5min": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "10min": CandleInterval.CANDLE_INTERVAL_10_MIN,
    "15min": CandleInterval.CANDLE_INTERVAL_15_MIN,
}

STEP_SEC = {"1min": 60, "5min": 300, "10min": 600, "15min": 900}


class CandleFeed:
    def __init__(self, token: str, interval_name: str, figis: list[str], target: str | None = None):
        self.token = token
        self.target = target
        self.interval_name = interval_name if interval_name in INTERVAL_ENUM else "5min"
        self.figis = figis
        self.mode = "stream"
        self._stop_requested = False

    def request_stop(self) -> None:
        self._stop_requested = True

    async def stream(self) -> AsyncIterator[ClosedCandle]:
        got_candle = False
        try:
            async for item in self._stream_grpc():
                got_candle = True
                yield item
        except asyncio.CancelledError:
            if self._stop_requested or got_candle:
                raise
        except Exception:
            pass
        self.mode = "polling"
        async for item in self._polling():
            yield item

    async def _stream_grpc(self) -> AsyncIterator[ClosedCandle]:
        interval = INTERVAL_ENUM[self.interval_name]

        async def requests() -> AsyncIterator[MarketDataRequest]:
            yield MarketDataRequest(
                subscribe_candles_request=SubscribeCandlesRequest(
                    subscription_action=SubscriptionAction.SUBSCRIPTION_ACTION_SUBSCRIBE,
                    instruments=[
                        CandleSubscription(figi=f, interval=interval, waiting_close=True)
                        for f in self.figis
                    ],
                )
            )
            await asyncio.Event().wait()

        async with AsyncClient(self.token, target=self.target) as client:
            async for resp in client.market_data_stream.market_data_stream(requests()):
                candle = getattr(resp, "candle", None)
                if candle is None:
                    continue
                yield ClosedCandle(
                    figi=candle.figi,
                    ts=candle.time.replace(tzinfo=timezone.utc),
                    open=float(quotation_to_decimal(candle.open)),
                    high=float(quotation_to_decimal(candle.high)),
                    low=float(quotation_to_decimal(candle.low)),
                    close=float(quotation_to_decimal(candle.close)),
                    volume=float(candle.volume),
                )

    async def _polling(self) -> AsyncIterator[ClosedCandle]:
        step_sec = STEP_SEC[self.interval_name]
        buffers: dict[str, deque] = {f: deque(maxlen=3) for f in self.figis}
        seen: set[tuple[str, datetime]] = set()
        next_poll = datetime.now(timezone.utc)
        async with AsyncClient(self.token, target=self.target) as client:
            while True:
                wait = (next_poll - datetime.now(timezone.utc)).total_seconds()
                if wait > 0:
                    await asyncio.sleep(wait)
                next_poll += timedelta(seconds=step_sec)

                for figi in self.figis:
                    try:
                        resp = await client.market_data.get_candles(
                            figi=figi,
                            interval=INTERVAL_ENUM[self.interval_name],
                            from_=datetime.now(timezone.utc) - timedelta(minutes=step_sec * 4),
                            to=datetime.now(timezone.utc),
                        )
                        for c in resp.candles:
                            key = (figi, c.time)
                            if key in seen:
                                continue
                            seen.add(key)
                            if len(seen) > 5000:
                                seen.clear()
                            yield ClosedCandle(
                                figi=figi,
                                ts=c.time.replace(tzinfo=timezone.utc),
                                open=float(quotation_to_decimal(c.open)),
                                high=float(quotation_to_decimal(c.high)),
                                low=float(quotation_to_decimal(c.low)),
                                close=float(quotation_to_decimal(c.close)),
                                volume=float(c.volume),
                            )
                    except Exception:
                        continue
