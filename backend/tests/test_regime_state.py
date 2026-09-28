"""L2.3 parity-гейт режима: RegimeState == RegimeDetector.compute.

- closed-строки дословно (state/reason/features с округлениями) на префиксах;
- forming через clone().update == последняя batch-строка, оригинал не меняется;
- связка с DataContext: состояние двигается только по закрытию режимного ТФ;
- нестандартные параметры детектора (clone обязан их сохранять).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.data_context import DataContext
from app.services.regime import RegimeDetector, detect_regime
from app.services.regime_state import RegimeState

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _make_bars(n=900, seed=21, minutes=5):
    import random
    rng = random.Random(seed)
    out = []
    price = 100.0
    for i in range(n):
        ts = T0 + timedelta(minutes=minutes * i)
        # трендовые куски + пила + флэт: все ветки дерева решений
        if 200 <= i < 350:
            price *= 1.0012
        elif 500 <= i < 650:
            price *= 0.9988
        elif 750 <= i < 780:
            pass
        else:
            price = max(price + rng.gauss(0, 0.0015) * price, 1.0)
        o = price
        h = price * (1 + abs(rng.gauss(0, 0.0008)))
        l = price * (1 - abs(rng.gauss(0, 0.0008)))
        out.append(Candle(ts=ts, open=o, high=h, low=l,
                          close=price + rng.gauss(0, 0.0004) * price,
                          volume=int(rng.uniform(500, 3000))))
    return out


def _norm(rows):
    return [(r["ts"].isoformat(), r["state"], r["reason"],
             None if r["features"] is None else tuple(sorted(r["features"].items())))
            for r in rows]


def test_rows_equal_batch_on_prefixes():
    bars = _make_bars()
    st = RegimeState()
    det = RegimeDetector()
    # batch на префиксе короче warmup возвращает [] — конвейер всегда зовёт
    # детектор на сотнях баров; сравниваем там, где batch определён
    tiny = RegimeState()
    for b in bars[:10]:
        r = tiny.update(b)
        assert (r["state"], r["reason"]) == ("NEUTRAL", "warmup")
    for i, b in enumerate(bars):
        st.update(b)
        if i % 13 == 0 or i == len(bars) - 1:
            want = det.compute(bars[:i + 1])
            if want:
                assert _norm(st.rows) == _norm(want), f"prefix={i}"


def test_all_states_covered():
    bars = _make_bars()
    st = RegimeState()
    for b in bars:
        st.update(b)
    states = {r["state"] for r in st.rows}
    assert {"TREND_UP", "TREND_DOWN", "RANGE"} <= states, states


def test_forming_via_clone_matches_batch_last_row():
    bars = _make_bars()
    st = RegimeState()
    for b in bars[:-1]:
        st.update(b)
    n_before = len(st.rows)
    snap_rows = [dict(r) for r in st.rows]
    got = st.evaluate(bars[-1])
    det = RegimeDetector()
    want = det.compute(bars)[-1]
    assert _norm([got]) == _norm([want])
    # оригинал не изменился
    assert len(st.rows) == n_before
    assert _norm(st.rows) == _norm(snap_rows)


def test_custom_detector_params():
    bars = _make_bars()
    kw = {"adx_threshold": 25.0, "drift_bars": 8, "cons_bars": 8, "window": 100}
    st = RegimeState(**kw)
    for b in bars[:-1]:
        st.update(b)
    assert _norm(st.rows) == _norm(RegimeDetector(**kw).compute(bars[:-1]))
    got = st.evaluate(bars[-1])
    assert _norm([got]) == _norm([RegimeDetector(**kw).compute(bars)[-1]])


def test_composition_with_data_context():
    """Связка L2.1+L2.3: часовые свечи → закрытые H1 → RegimeState == batch.
    (Свёртка M1→H1 — территория L2.1, уже покрыта; здесь — события закрытия.)"""
    h1 = _make_bars(n=600, minutes=60)
    ctx = DataContext((3600,))
    st = RegimeState()
    for c in h1:
        ev = ctx.append(c)
        if ev[3600] == "closed":
            st.update(ctx.closed(3600)[-1])
    closed = ctx.closed(3600)
    assert _norm(st.rows) == _norm(RegimeDetector().compute(closed))
    # forming H1 через клон == batch по всем барам
    assert _norm([st.evaluate(ctx.forming(3600))]) == \
           _norm([RegimeDetector().compute(ctx.bars(3600))[-1]])
