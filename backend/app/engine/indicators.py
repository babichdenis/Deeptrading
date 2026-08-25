from __future__ import annotations

from typing import Sequence

from app.engine.models import Candle


def atr(bars: Sequence[Candle], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return result
    trs: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            trs.append(bar.high - bar.low)
        else:
            prev_close = bars[i - 1].close
            trs.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )
    value = sum(trs[:period]) / period
    result[period - 1] = value
    for i in range(period, len(bars)):
        value = (value * (period - 1) + trs[i]) / period
        result[i] = value
    return result
