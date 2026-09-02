"""Unit-тесты B4-RUN entry_pullback_depth gate (без БД)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone, timedelta
import pytest
from app.engine.models import Candle as C
from app.services.ensemble import entry_pullback_deep_pass

T0 = datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc)
STEP = timedelta(minutes=5)


def _mk(closes):
    out = []
    for i, c in enumerate(closes):
        out.append(C(ts=T0 + i * STEP, open=c, high=c + 1.0, low=c - 1.0, close=c, volume=10))
    return out


def _atr_const(n, val=2.0):
    return [None] * 14 + [val] * (n - 14)


def test_long_deep_pullback_passes():
    # ramp up to 120 (i=20), then pull back to 100 by i=25 -> deep
    closes = [100 + i for i in range(21)] + [120 - 4 * (i - 20) for i in range(21, 26)]
    closes += [100.0] * 5  # pad to 30
    c5 = _mk(closes)
    ok, why = entry_pullback_deep_pass(c5, _atr_const(len(c5)), T0 + 25 * STEP, "BUY")
    assert ok is True, why


def test_long_shallow_pullback_rejected():
    # ramp up to 120 (i=20), modest pullback by i=25, but ATR=20 (huge vol) ->
    # threshold 0.5*ATR/px ~ 837 bps >> realized pullback ~210 bps -> shallow reject
    closes = [100 + i for i in range(21)] + [120 - 0.11 * (i - 20) for i in range(21, 26)]
    closes += [120.0] * 5
    c5 = _mk(closes)
    atr = [None] * 14 + [20.0] * (len(c5) - 14)
    ok, why = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "BUY")
    assert ok is False and why == "PULLBACK_SHALLOW", (ok, why)


def test_short_deep_pullback_passes():
    # ramp down to 100 (i=20), then bounce up to 120 by i=25 -> deep
    closes = [120 - i for i in range(21)] + [100 + 4 * (i - 20) for i in range(21, 26)]
    closes += [120.0] * 5
    c5 = _mk(closes)
    ok, why = entry_pullback_deep_pass(c5, _atr_const(len(c5)), T0 + 25 * STEP, "SELL")
    assert ok is True, why


def test_warmup_rejected():
    closes = [100 + i for i in range(21)] + [100.0] * 5
    c5 = _mk(closes)
    ok, why = entry_pullback_deep_pass(c5, _atr_const(len(c5)), T0 + 10 * STEP, "BUY")
    assert ok is False and why == "PULLBACK_WARMUP", (ok, why)


def test_atr_na_rejected():
    closes = [100 + i for i in range(21)] + [100.0] * 5
    c5 = _mk(closes)
    atr = [None] * len(c5)  # нет ATR -> NA
    ok, why = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "BUY")
    assert ok is False and why == "PULLBACK_NA", (ok, why)


# ---- B4-RUN v2: require_medium (thr=0.25*ATR) ----
# medium pullback: pb между 0.25*ATR и 0.5*ATR -> проходит при thr=0.25, НО rejected при thr=0.5 (deep)


def test_medium_pullback_passes_with_require_medium():
    # ramp up to 120 (i=20, high=121), лёгкий откат до 119.5 (pb ~124 bps).
    # ATR=4: thr_medium=0.25*4/119.5*10000~83.7bps, thr_deep=0.5*4/119.5*10000~167.4bps.
    # pb 124 -> medium PASS (0.25), deep REJECT (0.5).
    closes = [100 + i for i in range(21)] + [119.5] * 9
    c5 = _mk(closes)
    atr = [None] * 14 + [4.0] * (len(c5) - 14)
    ok, why = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "BUY", thr_mult=0.25)
    assert ok is True, why
    ok_deep, why_deep = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "BUY", thr_mult=0.5)
    assert ok_deep is False and why_deep == "PULLBACK_SHALLOW", (ok_deep, why_deep)


def test_shallow_still_rejected_under_require_medium():
    # ramp up to 120 (high=121), минимальный откат до 120.5 (pb ~41 bps) < 83.7 bps -> shallow reject даже для medium
    closes = [100 + i for i in range(21)] + [120.5] * 9
    c5 = _mk(closes)
    atr = [None] * 14 + [4.0] * (len(c5) - 14)
    ok, why = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "BUY", thr_mult=0.25)
    assert ok is False and why == "PULLBACK_SHALLOW", (ok, why)


def test_short_medium_pullback_passes_with_require_medium():
    # ramp down to 100 (i=20, low=99), лёгкий отскок до 100.5 (pb ~151 bps).
    # thr_medium=0.25*4/100.5*10000~99.5bps, thr_deep~199bps -> medium pass, deep reject
    closes = [120 - i for i in range(21)] + [100.5] * 9
    c5 = _mk(closes)
    atr = [None] * 14 + [4.0] * (len(c5) - 14)
    ok, why = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "SELL", thr_mult=0.25)
    assert ok is True, why
    ok_deep, why_deep = entry_pullback_deep_pass(c5, atr, T0 + 25 * STEP, "SELL", thr_mult=0.5)
    assert ok_deep is False and why_deep == "PULLBACK_SHALLOW", (ok_deep, why_deep)


def test_gate_mapping_require_medium_thr():
    # _build_session_policy: entry_session_extended -> open/close 09:30/19:15
    from app.services.ensemble import _build_session_policy
    p = _build_session_policy({"entry_session": "main", "entry_session_extended": True})
    assert p is not None
    assert p.open_time == "09:30" and p.close_time == "19:15", (p.open_time, p.close_time)
    p2 = _build_session_policy({"entry_session": "main"})
    assert p2.open_time == "10:00" and p2.close_time == "18:45"
    p3 = _build_session_policy({"entry_session": "all"})
    assert p3 is None
