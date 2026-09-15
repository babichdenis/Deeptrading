"""Тесты тройного подтверждения входа (entry_confirm_closes) и held-стороны.

Правило: для сторон из entry_confirm_closes_sides вход возможен только если последние
N 1м-ЗАКРЫТИЙ строго по направлению (BUY: каждое выше предыдущего). Сигнальный бар —
третий из трёх «вверх» (без задержки: подтверждение проверяется на баре сигнала).

skip_entry_side: held-тикер не рассматривает входы в сторону позиции
(противоположные остаются для выходов/флипов).
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from app.engine.models import Candle
from app.services.ensemble import compute_ensemble

T0 = datetime(2026, 8, 1, 6, 50, tzinfo=timezone.utc)


def _candles(n: int = 600, base: float = 100.0, drift: float = 0.0, seed: int = 7):
    rng = random.Random(seed)
    out, price = [], base
    for i in range(n):
        price = max(price * (1 + drift + rng.gauss(0, 0.0012)), 1.0)
        o = price * (1 + rng.gauss(0, 0.0004))
        c = price
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.0004)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.0004)))
        out.append(Candle(ts=T0 + timedelta(minutes=i), open=o, high=h, low=l,
                          close=c, volume=int(rng.uniform(500, 3000))))
    return out


def _req(**over):
    req = {
        "figi": "TEST_CONFIRM",
        "lot": 1,
        "capital": 100_000,
        "setups": [{"strategy_id": "donchian_breakout", "tf": "5min", "params": {"period": 10}}],
        "quorum": 1,
        "entry": {"tf": "1min", "lookback": 3},
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 4.0, "risk_reward": 4.0}},
        "from_ts": T0.isoformat(),
        "to_ts": (T0 + timedelta(hours=9)).isoformat(),
        "entry_from_setups": True,
    }
    req.update(over)
    return req


def _entries(res):
    return ((res.get("static") or {}).get("entries") or [])


def _funnel(res):
    return (res.get("static") or {}).get("funnel") or {}


def test_confirm_closes_invariant_buy():
    """Все принятые BUY-входы имеют 3 строго растущих 1м-закрытия на баре сигнала."""
    candles = _candles(drift=0.0002)
    req = _req(entry_confirm_closes=3, entry_confirm_closes_sides=["BUY"])
    res = compute_ensemble(candles, req)
    assert "error" not in res, res.get("error")
    idx = {c.ts: i for i, c in enumerate(candles)}
    checked = 0
    for e in _entries(res):
        if str(e.get("side")).upper() != "BUY":
            continue
        ts = e.get("ts")
        ts = datetime.fromisoformat(ts) if isinstance(ts, str) else ts
        i = idx.get(ts)
        assert i is not None and i >= 2, f"нет истории для {ts}"
        cl = [candles[i - k].close for k in range(3)]
        assert cl[0] > cl[1] > cl[2], f"BUY без 3 растущих закрытий на {ts}: {cl}"
        checked += 1
    assert "entry_confirm_rejected" in _funnel(res)


def test_confirm_closes_off_keeps_more_or_equal():
    """Выключенный фильтр не режет входы (baseline >= с фильтром)."""
    candles = _candles(drift=0.0002)
    base = compute_ensemble(candles, _req())
    filt = compute_ensemble(candles, _req(entry_confirm_closes=3, entry_confirm_closes_sides=["BUY"]))
    assert "error" not in base and "error" not in filt
    n_base = _funnel(base).get("entries_raw")
    n_filt = _funnel(filt).get("entries_raw")
    rej = _funnel(filt).get("entry_confirm_rejected")
    assert rej is not None
    assert n_base >= n_filt
    assert rej == (n_base or 0) - (n_filt or 0)


def test_confirm_only_for_listed_sides():
    """При sides=["SELL"] BUY-входы не режутся."""
    candles = _candles(drift=0.0002)
    base = compute_ensemble(candles, _req())
    filt = compute_ensemble(candles, _req(entry_confirm_closes=3, entry_confirm_closes_sides=["SELL"]))
    assert _funnel(base).get("entries_raw") == _funnel(filt).get("entries_raw")


def test_skip_entry_side_removes_side():
    """skip_entry_side=BUY: принятых BUY-входов нет, счётчик отброшенных есть."""
    candles = _candles(drift=0.0002)
    res = compute_ensemble(candles, _req(skip_entry_side="BUY"))
    assert "error" not in res, res.get("error")
    assert all(str(e.get("side")).upper() != "BUY" for e in _entries(res))
    assert "skip_side_rejected" in _funnel(res)


def test_skip_entry_side_sell():
    """skip_entry_side=SELL: принятых SELL-входов нет."""
    candles = _candles(drift=-0.0002)
    res = compute_ensemble(candles, _req(skip_entry_side="SELL"))
    assert "error" not in res, res.get("error")
    assert all(str(e.get("side")).upper() != "SELL" for e in _entries(res))
