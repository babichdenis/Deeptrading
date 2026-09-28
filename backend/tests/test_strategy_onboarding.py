"""Onboarding-гейт стратегий: ЛЮБАЯ single-series стратегия через StrategyState
== batch generate_signals (закрытые + зонд forming).

Шаблон приёмки новых роботов (в т.ч. 5 incoming): добавить (sid, params)
в MATRIX, прогнать — паритет обязан сойтись без изменения движка.
Исключены сознательно: *_ensemble, momentum_1bar, ensemble_vote
(композитные — отдельный путь онбординга), trend_up/down, range_reversion
покрыты как stateless (окна без состояния).

Ограничение для переносчиков: состояние стратегии обязано быть O(1) на бар
(unbounded списки как у Stochastic._highs запрещены — см. отчёт L2.7).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.signals import generate_signals
from app.services.strategy_state import StrategyState

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)

MATRIX = [
    ("rsi_reversal", {"period": 16, "oversold": 30, "overbought": 80}),
    ("macd_cross", {"fast": 12, "slow": 26, "signal_period": 9}),
    ("donchian_breakout", {"period": 45}),
    ("bollinger_reclaim", {"period": 15, "k": 1.0}),
    ("stochastic", {"k_period": 14, "d_period": 3, "oversold": 20, "overbought": 80}),
    ("pullback_ema", {"trend_ema": 50, "pull_ema": 20}),
    ("vwap_reclaim", {"k": 2.0}),
    ("range_compression_breakout", {"lookback": 20, "atr_period": 14, "pct": 25.0}),
    ("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}),
    ("volume_climax", {"ma_len": 20, "climax_ratio": 3.0, "wick_frac": 0.5}),
    ("volume_divergence", {"div_n": 20}),
    ("trend_up", None),
    ("trend_down", None),
    ("range_reversion", None),
]


def _bars(n=900, seed=77):
    import random
    rng = random.Random(seed)
    out = []
    px = 100.0
    for i in range(n):
        ts = T0 + timedelta(minutes=5 * i)
        if 250 <= i < 330:
            px *= 1.0015
        elif 550 <= i < 630:
            px *= 0.9985
        else:
            px = max(px + rng.gauss(0, 0.0015) * px, 1.0)
        o = px
        spread = abs(rng.gauss(0, 0.0008))
        h = px * (1 + spread)
        lo = px * (1 - spread)
        c = min(max(px + rng.gauss(0, 0.0004) * px, lo), h)
        v = int(rng.uniform(500, 3000))
        if i % 97 == 0 and i > 0:
            # детерминированный шип объёма + широкий диапазон вниз:
            # volume_drop срабатывает структурно (ratio ~6 > 1.5, close < open)
            v = 12000
            h = px * 1.004
            lo = px * 0.996
            o = px * 1.002
            c = lo
        elif i % 149 == 0 and i > 0:
            # широкий бар вверх: volume_climax (climax_long) структурно
            v = 15000
            h = px * 1.006
            lo = px * 0.999
            o = px * 1.0005
            c = px * 1.004
        out.append(Candle(ts=ts, open=o, high=h, low=lo, close=c, volume=v))
    return out


def _norm(sigs):
    return [(s["ts"].isoformat(), s["side"], s["status"], s["reason"],
             tuple(sorted((k, repr(v)) for k, v in (s["features"] or {}).items())))
            for s in sigs]


def test_all_setups_parity():
    bars = _bars()
    total = 0
    for sid, params in MATRIX:
        st = StrategyState(sid, params)
        for i in range(len(bars)):
            st.update(bars[:i + 1])
        got = _norm(st.sigs)
        want = _norm(generate_signals(sid, params, bars))
        assert got == want, f"{sid}: {len(got)} vs {len(want)}"
        total += len(got)
    assert total > 30, f"матрица тривиальна: {total}"


def test_all_setups_forming_probe():
    bars = _bars()
    for sid, params in MATRIX:
        st = StrategyState(sid, params)
        for i in range(len(bars) - 1):
            st.update(bars[:i + 1])
        got = st.evaluate(bars[:-1], bars[-1])
        batch = generate_signals(sid, params, bars)
        last_ts = bars[-1].ts.isoformat()
        batch_last = [s for s in batch if s["ts"].isoformat() == last_ts]
        if batch_last:
            assert got is not None, f"{sid}: batch видит, зонд — нет"
            assert _norm([got]) == _norm(batch_last), sid
        else:
            assert got is None, f"{sid}: зонд видит лишний сигнал"


def test_volume_drop_fires_structurally():
    bars = _bars()
    sigs = generate_signals("volume_drop", {"ma_len": 20, "drop_ratio": 1.5}, bars)
    assert len(sigs) > 0, "шипы не сработали — сломан генератор данных"


def test_evaluate_does_not_pollute_matrix():
    bars = _bars()
    for sid, params in MATRIX:
        st = StrategyState(sid, params)
        for i in range(400):
            st.update(bars[:i + 1])
        for _ in range(5):
            st.evaluate(bars[:400], bars[400])
        for i in range(400, 500):
            st.update(bars[:i + 1])
        assert _norm(st.sigs) == _norm(generate_signals(sid, params, bars[:500])), sid
