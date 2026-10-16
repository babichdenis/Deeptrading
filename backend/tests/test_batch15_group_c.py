"""B3 синтетические тесты BATCH-15 group C (B11–B15).

Герметично: синтетические минуты через a7.build_ticker_data, без сети/БД/app.*.
Проверяют фиксированные правила по каждому кандидату: long/short/no_signal,
неполные окна -> insufficient_context (не no_signal), причность (будущее не
двигает принятый сигнал), регистрация spec hashes, ATR boundary.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import batch15_common as b15  # noqa: E402
import batch15_group_c as gc  # noqa: E402
import day_strategies_core as a7  # noqa: E402

TICKER = "B15T"
WARM = date(2026, 6, 15)  # Monday
DAY = date(2026, 6, 16)  # Tuesday


def _minute(ts, price, spread=0.005, volume=10.0):
    return a7.MinuteBar(ts, price, price + spread, price - spread, price, volume)


def _flat_day(day, price=100.0, spread=0.005, volume=10.0, missing=()):
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    while tick <= end:
        if a7.msk_mod(tick) not in missing:
            bars.append(_minute(tick, price, spread, volume))
        tick += timedelta(minutes=1)
    return bars


def _bars_ohlc(day, overrides, base_price=100.0):
    """Полный день: каждый 10-мин бар из overrides получает OHLC-свечные минуты."""
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    while tick <= end:
        mod = a7.msk_mod(tick)
        lab_mod = mod - (mod % 10)
        if lab_mod in overrides:
            o, h, l, c = overrides[lab_mod]
            seg = mod - lab_mod
            if seg == 0:
                price = o
            else:
                span = (c - o) / 9.0
                price = o + span * seg
            hi = max(o, h)
            lo = min(o, l)
            bars.append(a7.MinuteBar(tick, price, hi, lo, price, 10.0))
        else:
            bars.append(_minute(tick, base_price))
        tick += timedelta(minutes=1)
    return bars


def _tdata(day_bars, *, warmup_days=2):
    issues = []
    bars = []
    for i in range(warmup_days):
        wd = WARM - timedelta(days=i)
        if wd.weekday() < 5:
            bars.extend(_flat_day(wd, price=100.0))
    bars.extend(day_bars)
    return a7.build_ticker_data(TICKER, bars, WARM - timedelta(days=warmup_days), issues)


def _mod(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


# ---------------------------------------------------------------- B11

def _b11_overrides(o10=100.0, c1150=101.0, c1350=102.0):
    return {
        _mod("10:00"): (o10, o10 + 0.5, o10 - 0.5, o10),
        _mod("11:50"): (100.5, 101.5, 100.0, c1150),
        _mod("13:50"): (101.5, 102.5, 101.0, c1350),
    }


def test_b11_rising_both_positive_long():
    tdata = _tdata(_bars_ohlc(DAY, _b11_overrides(100.0, 101.0, 102.0)))
    ev = gc.evaluate("B11", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "long", ev
    assert ev["candidate_id"] == "B11"
    assert ev["spec_hash"] == gc.SPECS["B11"]["spec_hash"]
    assert ev["gates"]["r1"] > 0 and ev["gates"]["r2"] > 0


def test_b11_falling_both_negative_short():
    tdata = _tdata(_bars_ohlc(DAY, _b11_overrides(100.0, 99.0, 98.0)))
    ev = gc.evaluate("B11", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "short", ev


def test_b11_mixed_signs_no_signal():
    tdata = _tdata(_bars_ohlc(DAY, _b11_overrides(100.0, 99.0, 100.5)))
    ev = gc.evaluate("B11", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


# ---------------------------------------------------------------- B12

def test_b12_reversal_long():
    ovr = _b11_overrides(100.0, 101.0, 100.0)
    ovr[_mod("11:50")] = (100.5, 101.5, 100.0, 101.0)
    ovr[_mod("13:50")] = (101.0, 101.2, 99.8, 100.4)
    tdata = _tdata(_bars_ohlc(DAY, ovr))
    ev = gc.evaluate("B12", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "long", ev
    r1 = ev["gates"]["r1"]
    r2 = ev["gates"]["r2"]
    assert r1 > 0 and r2 < 0 and abs(r2) > abs(r1) / 2.0


def test_b12_reversal_short():
    ovr = _b11_overrides(100.0, 99.0, 100.0)
    ovr[_mod("11:50")] = (100.5, 100.8, 99.0, 99.2)
    ovr[_mod("13:50")] = (99.2, 100.5, 99.0, 100.0)
    tdata = _tdata(_bars_ohlc(DAY, ovr))
    ev = gc.evaluate("B12", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "short", ev


def test_b12_weak_reversal_no_signal():
    ovr = _b11_overrides(100.0, 101.0, 100.9)
    ovr[_mod("11:50")] = (100.5, 101.5, 100.0, 101.0)
    ovr[_mod("13:50")] = (101.0, 101.1, 100.5, 100.9)
    tdata = _tdata(_bars_ohlc(DAY, ovr))
    ev = gc.evaluate("B12", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


# ---------------------------------------------------------------- B13

def _b13_overrides(h1130=101.0, l1130=99.5, h1140=100.8, l1140=99.6, c1150=101.5):
    return {
        _mod("11:30"): (100.0, h1130, l1130, 100.5),
        _mod("11:40"): (100.5, h1140, l1140, 100.6),
        _mod("11:50"): (100.6, max(100.6, c1150), 100.4, c1150),
    }


def test_b13_inside_break_long():
    tdata = _tdata(_bars_ohlc(DAY, _b13_overrides()))
    ev = gc.evaluate("B13", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "long", ev
    assert ev["gates"]["inside"] is True


def test_b13_inside_break_short():
    tdata = _tdata(_bars_ohlc(DAY, _b13_overrides(c1150=99.0)))
    ev = gc.evaluate("B13", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "short", ev


def test_b13_not_inside_no_signal():
    tdata = _tdata(_bars_ohlc(DAY, _b13_overrides(h1140=101.5)))
    ev = gc.evaluate("B13", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


def test_b13_inside_but_no_break_no_signal():
    tdata = _tdata(_bars_ohlc(DAY, _b13_overrides(c1150=100.5)))
    ev = gc.evaluate("B13", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


# ---------------------------------------------------------------- B14

def _b14_overrides(b1130=(99.0, 100.5, 98.9, 100.2),
                   b1140=(100.2, 101.0, 100.1, 100.8),
                   b1150=(100.8, 101.5, 100.7, 101.2)):
    return {
        _mod("11:30"): b1130,
        _mod("11:40"): b1140,
        _mod("11:50"): b1150,
    }


def test_b14_three_bearish_falling_long():
    o = _b14_overrides((101.0, 101.1, 99.9, 100.2),
                       (100.2, 100.3, 99.0, 99.4),
                       (99.4, 99.5, 98.5, 98.8))
    tdata = _tdata(_bars_ohlc(DAY, o))
    ev = gc.evaluate("B14", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "long", ev


def test_b14_three_bullish_growing_short():
    tdata = _tdata(_bars_ohlc(DAY, _b14_overrides()))
    ev = gc.evaluate("B14", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "short", ev


def test_b14_mixed_bars_no_signal():
    o = _b14_overrides((100.0, 100.5, 99.5, 100.2),
                       (100.2, 100.3, 99.0, 99.4),
                       (99.4, 99.5, 98.5, 98.8))
    tdata = _tdata(_bars_ohlc(DAY, o))
    ev = gc.evaluate("B14", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


# ---------------------------------------------------------------- B15

def _b15_overrides():
    o = {}
    for lab in ("11:00", "11:10", "11:20", "11:30", "11:40", "11:50"):
        m = _mod(lab)
        o[m] = (100.0, 101.0, 99.0, 100.5)
    for lab in ("12:00", "12:10", "12:20", "12:30", "12:40", "12:50"):
        m = _mod(lab)
        o[m] = (100.5, 100.7, 100.3, 100.6)
    o[_mod("13:50")] = (101.5, 102.5, 101.4, 102.0)
    return o


def test_b15_compression_break_long():
    tdata = _tdata(_bars_ohlc(DAY, _b15_overrides()))
    ev = gc.evaluate("B15", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "long", ev
    assert ev["gates"]["range12"] < 0.5 * ev["gates"]["range11"]


def test_b15_compression_break_short():
    o = _b15_overrides()
    o[_mod("13:50")] = (100.5, 100.8, 99.5, 99.8)
    tdata = _tdata(_bars_ohlc(DAY, o))
    ev = gc.evaluate("B15", tdata, DAY)
    assert ev["type"] == "signal", ev
    assert ev["side"] == "short", ev


def test_b15_no_compression_no_signal():
    o = _b15_overrides()
    for lab in ("12:00", "12:10", "12:20", "12:30", "12:40", "12:50"):
        m = _mod(lab)
        o[m] = (100.5, 102.0, 98.5, 100.6)
    tdata = _tdata(_bars_ohlc(DAY, o))
    ev = gc.evaluate("B15", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


def test_b15_compression_no_break_no_signal():
    o = _b15_overrides()
    o[_mod("13:50")] = (100.5, 100.7, 99.9, 100.5)
    tdata = _tdata(_bars_ohlc(DAY, o))
    ev = gc.evaluate("B15", tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev


# ---------------------------------------------------------------- shared

def test_incomplete_window_insufficient_all():
    o = _b11_overrides()
    missing_mods = {_mod("13:00"), _mod("13:01")}
    bars = _bars_ohlc(DAY, o)
    bars = [b for b in bars if a7.msk_mod(b.ts) not in missing_mods]
    tdata = _tdata(bars)
    for cid in ("B11", "B12", "B15"):
        ev = gc.evaluate(cid, tdata, DAY)
        assert ev["type"] == "insufficient_context", (cid, ev)
        assert ev["reason"] == "incomplete_window", (cid, ev)
        assert ev["type"] != "skip" and ev.get("reason") != "no_signal"


def test_atr_warmup_insufficient_no_warmup():
    o = _b11_overrides()
    bars = _bars_ohlc(DAY, o)
    issues = []
    tdata = a7.build_ticker_data(TICKER, bars, DAY, issues)
    for cid in ("B13", "B14"):
        ev = gc.evaluate(cid, tdata, DAY)
        assert ev["type"] == "insufficient_context", (cid, ev)
        assert ev["reason"] == "atr_warmup_insufficient", (cid, ev)


def test_full_window_14implies_atr_warmup_for_24bars():
    o = _b11_overrides(100.0, 101.0, 102.0)
    bars = _bars_ohlc(DAY, o)
    issues = []
    tdata = a7.build_ticker_data(TICKER, bars, DAY, issues)
    for cid in ("B11", "B12", "B15"):
        ev = gc.evaluate(cid, tdata, DAY)
        assert not (ev["type"] == "insufficient_context"
                    and ev.get("reason") == "atr_warmup_insufficient"), (cid, ev)
    assert gc.evaluate("B11", tdata, DAY)["type"] == "signal"
    assert gc.evaluate("B15", tdata, DAY)["type"] == "signal"
    assert gc.evaluate("B12", tdata, DAY)["type"] == "skip"


def test_causality_future_bars_do_not_change_signal():
    o = _b11_overrides(100.0, 101.0, 102.0)
    tdata1 = _tdata(_bars_ohlc(DAY, o))
    ev1 = gc.evaluate("B11", tdata1, DAY)
    assert ev1["type"] == "signal", ev1

    bars = _bars_ohlc(DAY, o)
    bars = [b for b in bars if a7.msk_mod(b.ts) < 840]
    tick = a7.label_ts(DAY, "14:01")
    end = a7.label_ts(DAY, "18:44")
    while tick <= end:
        bars.append(_minute(tick, 500.0 + 0.1 * a7.msk_mod(tick), spread=2.0))
        tick += timedelta(minutes=1)
    for wd in (WARM, WARM - timedelta(days=3)):
        if wd.weekday() < 5:
            bars[0:0] = _flat_day(wd, price=100.0)
    tdata2 = a7.build_ticker_data(TICKER, bars, WARM - timedelta(days=3), [])
    ev2 = gc.evaluate("B11", tdata2, DAY)
    assert ev2 == ev1


def test_weekend_skip():
    tdata = _tdata(_bars_ohlc(DAY, _b11_overrides()))
    for cid in ("B11", "B12", "B13", "B14", "B15"):
        ev = gc.evaluate(cid, tdata, date(2026, 6, 14))  # Saturday
        assert ev["type"] == "skip", (cid, ev)
        assert ev["reason"] == "weekend_excluded", (cid, ev)


def test_spec_hashes_stable_and_unique():
    hashes = [gc.SPECS[cid]["spec_hash"] for cid in gc.CANDIDATE_IDS]
    assert len(set(hashes)) == 5, f"дублирующиеся spec_hash: {hashes}"
    assert all(len(h) == 64 for h in hashes)
    assert gc.SPEC_HASHES["B11"] == gc.SPECS["B11"]["spec_hash"]


def test_unknown_candidate_raises():
    tdata = _tdata(_bars_ohlc(DAY, _b11_overrides()))
    try:
        gc.evaluate("B99", tdata, DAY)
        raise AssertionError("unreachable")
    except ValueError as exc:
        assert "B99" in str(exc)
