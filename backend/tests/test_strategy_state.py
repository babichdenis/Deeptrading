"""L2.4 parity-гейт стратегий: StrategyState == generate_signals.

- closed-бары по одному: sigs дословно равны batch на каждом чекпоинте;
- forming через evaluate == batch-сигнал последнего бара (или его отсутствие);
- evaluate не загрязняет состояние: продолжение после него совпадает;
- 4 сетапа: rsi_reversal, macd_cross, donchian_breakout, bollinger_reclaim.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.signals import generate_signals
from app.services.strategy_state import StrategyState

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)

SETUPS = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
]


def _make_bars(n=900, seed=31):
    import random
    rng = random.Random(seed)
    out = []
    price = 100.0
    for i in range(n):
        ts = T0 + timedelta(minutes=5 * i)
        if 250 <= i < 330:
            price *= 1.0015
        elif 550 <= i < 630:
            price *= 0.9985
        else:
            price = max(price + rng.gauss(0, 0.0015) * price, 1.0)
        o = price
        h = price * (1 + abs(rng.gauss(0, 0.0008)))
        l = price * (1 - abs(rng.gauss(0, 0.0008)))
        out.append(Candle(ts=ts, open=o, high=h, low=l,
                          close=price + rng.gauss(0, 0.0004) * price,
                          volume=int(rng.uniform(500, 3000))))
    return out


def _norm(sigs):
    return [(s["ts"].isoformat(), s["side"], s["status"], s["reason"],
             tuple(sorted((k, repr(v)) for k, v in (s["features"] or {}).items())))
            for s in sigs]


def test_closed_equals_batch():
    bars = _make_bars()
    for sid, params in SETUPS:
        st = StrategyState(sid, params)
        for i, b in enumerate(bars):
            st.update(bars[:i + 1])
            if i % 25 == 0 or i == len(bars) - 1:
                assert _norm(st.sigs) == _norm(generate_signals(sid, params, bars[:i + 1])), \
                    f"{sid} prefix={i}"


def test_forming_evaluate_equals_batch_last():
    bars = _make_bars()
    for sid, params in SETUPS:
        st = StrategyState(sid, params)
        for i in range(len(bars) - 1):
            st.update(bars[:i + 1])
        got = st.evaluate(bars[:-1], bars[-1])
        batch = generate_signals(sid, params, bars)
        last_ts = bars[-1].ts
        batch_last = [s for s in batch if s["ts"] == last_ts]
        if batch_last:
            assert got is not None, f"{sid}: batch видит сигнал, зонд — нет"
            assert _norm([got]) == _norm(batch_last), sid
        else:
            assert got is None, f"{sid}: зонд видит лишний сигнал"


def test_evaluate_does_not_pollute():
    bars = _make_bars()
    for sid, params in SETUPS:
        st = StrategyState(sid, params)
        for i in range(400):
            st.update(bars[:i + 1])
        for _ in range(5):
            st.evaluate(bars[:400], bars[400])
        for i in range(400, 500):
            st.update(bars[:i + 1])
        assert _norm(st.sigs) == _norm(generate_signals(sid, params, bars[:500])), sid


def test_signals_found():
    """Санити: на этих данных сигналы вообще есть (иначе тесты тривиальны)."""
    bars = _make_bars()
    total = sum(len(generate_signals(sid, params, bars)) for sid, params in SETUPS)
    assert total > 10, total
