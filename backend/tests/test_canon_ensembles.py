"""Тесты канонных hub-стратегий и ансамбля голосования (research-слой)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle, Side
from app.engine.strategies import build_strategy

T0 = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)


def _series(vals) -> list[Candle]:
    return [
        Candle(ts=T0 + timedelta(minutes=10 * i), open=float(v),
               high=float(v) + 0.5, low=float(v) - 0.5, close=float(v))
        for i, v in enumerate(vals)
    ]


def _fall_then_rise() -> list[float]:
    # падение (RSI < 35) → рост: крест вверх через downline во время роста
    return [100 - 0.4 * i for i in range(60)] + [80.0, 82.0, 84.0, 86.0, 88.0, 90.0]


def test_rsi_trade_hub_cross_up_emits_buy():
    vals = _fall_then_rise()
    s = build_strategy("rsi_trade_hub", None)
    last = None
    for i in range(1, len(vals) + 1):
        out = s.on_bar(_series(vals[:i]))
        if out is not None:
            last = out
    assert last is not None
    assert last.side is Side.BUY


def test_canon_ensemble_quorum_two_blocks_single_member():
    vals = _fall_then_rise()
    never = build_strategy("canon_ensemble", {"members": "rsi_trade_hub", "quorum": 2})
    outs = []
    for i in range(1, len(vals) + 1):
        out = never.on_bar(_series(vals[:i]))
        if out is not None:
            outs.append(out)
    # один член не может дать кворум 2; exit-интентов у rsi_trade_hub нет
    assert outs == []


def test_canon_ensemble_builds_with_members_and_quorum():
    s = build_strategy("canon_ensemble",
                       {"members": "rsi_trade_hub,envelop_trend_hub", "quorum": 2})
    assert s.warmup_bars() > 0
