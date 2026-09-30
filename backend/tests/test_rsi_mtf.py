"""RSI MTF (мультифрейм): старший ТФ как фильтр направления входов младшего."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle, Side
from app.engine.strategies import RsiMtfHubParams, RsiMtfHubStrategy

T0 = datetime(2026, 6, 15, 7, 0, tzinfo=timezone.utc)


def _series(n: int = 300, tf_min: int = 10):
    """1h-аптренд → неглубокий откат (10m RSI низко, 1h RSI ещё высоко) → возобновление.

    Именно на такой «просадке внутри часового тренда» MTF-фильтр должен пропустить
    LONG-крест 10m RSI: часовой режим остался бычьим.
    """
    closes = []
    for i in range(150):
        closes.append(100.0 + 0.05 * i)
    for i in range(60):
        closes.append(107.5 - 0.05 * i)
    for i in range(max(0, n - 210)):
        closes.append(104.5 + 0.05 * i)
    return [
        Candle(ts=T0 + timedelta(minutes=tf_min * i), open=c, high=c + 0.05,
               low=c - 0.05, close=c, volume=1.0)
        for i, c in enumerate(closes)
    ]


def _signals(strat, bars):
    out = []
    for i in range(1, len(bars) + 1):
        s = strat.on_bar(bars[:i])
        if s is not None:
            out.append(s)
    return out


def test_rsi_mtf_bias_gates_entries():
    bars = _series()
    base = RsiMtfHubStrategy(RsiMtfHubParams(rsi_length=14, upline=70, downline=30, bias_gap=0.0))
    sigs = _signals(base, bars)
    assert any(s.side == Side.BUY for s in sigs), "ожидаем LONG на откате внутри 1h-аптренда"
    blocked = RsiMtfHubStrategy(RsiMtfHubParams(rsi_length=14, upline=70, downline=30, bias_gap=60.0))
    assert _signals(blocked, bars) == [], "огромная мёртвая зона должна блокировать все входы"


def test_rsi_mtf_sides_match_bias():
    bars = _series()
    strat = RsiMtfHubStrategy(RsiMtfHubParams(rsi_length=14, upline=70, downline=30, bias_gap=0.0))
    sigs = _signals(strat, bars)
    assert sigs
    for s in sigs:
        bias = s.features["bias"]
        if s.side == Side.BUY:
            assert bias >= 50.0, f"LONG при bias={bias}"
        else:
            assert bias <= 50.0, f"SHORT при bias={bias}"
