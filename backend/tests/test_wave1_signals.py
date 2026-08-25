from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.engine.strategies import (
    ParamValidationError,
    build_strategy,
    validate_params,
)
from app.engine.wave1 import (
    BollingerReclaimParams,
    BollingerReclaimStrategy,
    PullbackEmaParams,
    PullbackEmaStrategy,
    RsiReversalParams,
    RsiReversalStrategy,
    SqueezeBreakoutParams,
    SqueezeBreakoutStrategy,
    VwapReclaimParams,
    VwapReclaimStrategy,
)
from app.engine.models import Candle, Side

T0 = datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)


def candles_from_closes(closes: list[float], spread: float = 0.05) -> list:
    out = []
    prev_close = closes[0]
    for i, c in enumerate(closes):
        o = prev_close
        h = max(o, c) + spread
        low = min(o, c) - spread
        out.append(
            Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=low, close=c, volume=1000)
        )
        prev_close = c
    return out


def collect(strategy, candles) -> list:
    out = []
    warmup = strategy.warmup_bars()
    for i in range(1, len(candles) + 1):
        sig = strategy.on_bar(candles[:i])
        if sig is not None and i > warmup:
            out.append(sig)
    return out


def test_rsi_reversal_long_after_downtrend():
    closes = [100 - i * 1.0 for i in range(10)]
    candles = candles_from_closes(closes)
    last = candles[-1]
    bounce = Candle(
        ts=last.ts + timedelta(minutes=5),
        open=last.close,
        high=last.close + 2.0,
        low=last.close - 0.3,
        close=last.close + 1.5,
        volume=1000,
    )
    candles.append(bounce)
    sigs = collect(RsiReversalStrategy(RsiReversalParams(period=5)), candles)
    buys = [s for s in sigs if s.side is Side.BUY]
    assert len(buys) >= 1
    assert buys[-1].reason == "rsi_turn_up_oversold"


def test_rsi_reversal_no_signal_in_pure_fall():
    closes = [100 - i * 1.0 for i in range(15)]
    candles = candles_from_closes(closes)
    sigs = collect(RsiReversalStrategy(RsiReversalParams(period=5)), candles)
    assert all(s.side is not Side.BUY for s in sigs)


def test_rsi_reversal_short_after_uptrend():
    closes = [100 + i * 1.0 for i in range(10)]
    candles = candles_from_closes(closes)
    last = candles[-1]
    drop = Candle(
        ts=last.ts + timedelta(minutes=5),
        open=last.close,
        high=last.close + 0.3,
        low=last.close - 2.0,
        close=last.close - 1.5,
        volume=1000,
    )
    candles.append(drop)
    sigs = collect(RsiReversalStrategy(RsiReversalParams(period=5)), candles)
    sells = [s for s in sigs if s.side is Side.SELL]
    assert len(sells) >= 1
    assert sells[-1].reason == "rsi_turn_down_overbought"


def test_bollinger_reclaim_long():
    closes = [100, 100.5, 99.5, 100, 99, 96, 90, 93]
    sigs = collect(
        BollingerReclaimStrategy(BollingerReclaimParams(period=5, k=2)),
        candles_from_closes(closes),
    )
    buys = [s for s in sigs if s.side is Side.BUY]
    assert len(buys) >= 1
    assert buys[-1].reason == "reclaim_lower_band"


def test_bollinger_reclaim_short():
    closes = [100, 99.5, 100.5, 100, 101, 104, 110, 107]
    sigs = collect(
        BollingerReclaimStrategy(BollingerReclaimParams(period=5, k=2)),
        candles_from_closes(closes),
    )
    sells = [s for s in sigs if s.side is Side.SELL]
    assert len(sells) >= 1
    assert sells[-1].reason == "reclaim_upper_band"


def test_pullback_ema_long():
    rows = []
    price = 100.0
    for i in range(12):
        rows.append((price + 0.9, price + 1.1, price - 0.4, price))
        price += 1.0
    crash_o = price
    rows.append((crash_o, crash_o + 0.2, crash_o - 6.5, crash_o - 6.2))
    resume_base = rows[-1][3]
    rows.append((resume_base, resume_base + 7.0, resume_base - 1.5, resume_base + 6.7))
    candles = [
        Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c, volume=1000)
        for i, (o, h, l, c) in enumerate(rows)
    ]
    sigs = collect(
        PullbackEmaStrategy(PullbackEmaParams(trend_ema=10, pull_ema=3)),
        candles,
    )
    buys = [s for s in sigs if s.side is Side.BUY]
    assert len(buys) >= 1


def test_vwap_reclaim_long_intraday():
    rows = []
    price = 100.0
    for i in range(14):
        drift = -0.35 if i < 12 else 3.2
        price += drift
        rows.append((price - drift, max(price, price - drift) + 0.1,
                     min(price, price - drift) - 0.1, price))
    candles = [
        Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c, volume=5000)
        for i, (o, h, l, c) in enumerate(rows)
    ]
    sigs = collect(VwapReclaimStrategy(VwapReclaimParams(k=1.0)), candles)
    buys = [s for s in sigs if s.side is Side.BUY]
    assert len(buys) >= 1
    assert buys[-1].reason == "vwap_reclaim_up"


def test_squeeze_breakout_long():
    rows = [(99.95, 100.07, 99.93, 100.0)] * 20
    rows.append((100.0, 103.4, 99.95, 103.0))
    candles = [
        Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c, volume=1000)
        for i, (o, h, l, c) in enumerate(rows)
    ]
    sigs = collect(
        SqueezeBreakoutStrategy(SqueezeBreakoutParams(lookback=10, atr_period=5, pct=25)),
        candles,
    )
    buys = [s for s in sigs if s.side is Side.BUY]
    assert len(buys) >= 1
    assert buys[-1].reason == "squeeze_breakout_up"


def test_squeeze_needs_compression():
    rows = []
    price = 100.0
    swings = [3.0, 1.2, 2.2, 1.6, 2.8]
    spreads = [0.4, 0.7, 0.55, 0.8, 0.45]
    for i in range(30):
        swing = swings[i % 5] * (-1 if i % 2 else 1)
        sp = spreads[i % 5]
        rows.append((price, price + abs(swing) + sp, price - abs(swing) - sp, price + swing))
        price += swing
    rows.append((price, price + 6.0, price - 0.5, price + 5.5))
    candles = [
        Candle(ts=T0 + timedelta(minutes=5 * i), open=o, high=h, low=l, close=c, volume=1000)
        for i, (o, h, l, c) in enumerate(rows)
    ]
    sigs = collect(
        SqueezeBreakoutStrategy(SqueezeBreakoutParams(lookback=10, atr_period=5, pct=25)),
        candles,
    )
    assert len(sigs) == 0


def test_validate_params_bounds_and_unknown():
    ok = validate_params("rsi_reversal", {"period": 7})
    assert ok["period"] == 7
    assert ok["oversold"] == 35
    with pytest.raises(ParamValidationError):
        validate_params("rsi_reversal", {"period": 999})
    with pytest.raises(ParamValidationError):
        validate_params("rsi_reversal", {"nope": 1})
    with pytest.raises(ParamValidationError):
        validate_params("unknown_strategy", {})


def test_build_strategy_all_catalog_ids():
    from app.engine.catalog import STRATEGY_CATALOG

    for sid in STRATEGY_CATALOG:
        strategy = build_strategy(sid, {})
        assert hasattr(strategy, "on_bar")
        assert strategy.strategy_id == sid
