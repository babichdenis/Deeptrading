"""B3 независимая интеграционная приёмка C3 (WORKLINE-2H, фиксированный контракт).

Герметично: синтетические минуты через a7.build_ticker_data, без сети/БД/app.*.
Проверяет зафиксированный контракт C3_TREND60_V1 (не повторяет market replay B2):
- решение в 11:00 по шести полным 10min-барам 10:00..10:50 (close6 vs mean six);
- неполный час -> insufficient_context, никогда no_signal;
- ATR = a7.atr_before(10:50), решающий бар исключён; warmup < 14 TR -> insufficient;
- вход next-open ровно 11:00 MSK (UTC-aware поля события);
- costs/SL/TP через существующий A24 executor (общее ядро research_execution);
- изменение будущего не двигает уже принятый сигнал;
- регистрация C3 в A24 (spec hash, мультипликаторы из a7).
"""
from __future__ import annotations

import sys
from datetime import date, timedelta, timezone

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import day_candidate_execution as a24  # noqa: E402
import day_strategies_core as a7  # noqa: E402
import intraday_trend_candidate as c3  # noqa: E402

ABS_TOL = 1e-9
TICKER = "C3T"
WARM = date(2026, 6, 15)  # Monday
DAY = date(2026, 6, 16)  # Tuesday
COSTS = a7.CostParams(commission_bps=5.0, slippage_bps=2.0)


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


def _trend_day(day, closes, *, spread=0.005, volume=10.0, missing=()):
    """Шесть полных 10min-баров с заданными close; после 11:00 flat на closes[-1]."""
    bars = []
    tick = a7.label_ts(day, "10:00")
    end = a7.label_ts(day, "18:44")
    while tick <= end:
        mod = a7.msk_mod(tick)
        if mod in missing:
            tick += timedelta(minutes=1)
            continue
        if mod >= 660:
            price = closes[-1]
        else:
            j = (mod - 600) // 10
            seg = (mod - 600) % 10
            prev = closes[j - 1] if j > 0 else closes[0]
            price = prev + (closes[j] - prev) * (seg + 1) / 10
        bars.append(_minute(tick, price, spread, volume))
        tick += timedelta(minutes=1)
    return bars


def _warm_and_trend(closes, *, missing=()):
    issues = []
    bars = _flat_day(WARM, price=100.0)
    bars.extend(_trend_day(DAY, closes, missing=missing))
    return a7.build_ticker_data(TICKER, bars, WARM, issues)


LONG_CLOSES = [100.0, 100.0, 100.0, 100.0, 100.0, 110.0]
SHORT_CLOSES = [110.0, 110.0, 110.0, 110.0, 110.0, 100.0]
FLAT_CLOSES = [100.0, 100.0, 100.0, 100.0, 100.0, 100.0]


def _expect_signal(ev, side):
    assert ev["type"] == "signal", ev
    assert ev["candidate_id"] == "C3", ev
    assert ev["ticker"] == TICKER, ev
    assert ev["day"] == DAY.isoformat(), ev
    assert ev["side"] == side, ev
    assert ev["spec_hash"] == c3.TREND_SPEC_HASH, ev


def test_c3_rising_hour_signal_long():
    tdata = _warm_and_trend(LONG_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    _expect_signal(ev, "long")
    assert ev["gates"]["decision_close"] == 110.0
    assert ev["gates"]["hour_mean"] == 101.66666666666667
    assert ev["atr"] > 0
    assert ev["decision_label"] == "10:50"


def test_c3_falling_hour_signal_short():
    tdata = _warm_and_trend(SHORT_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    _expect_signal(ev, "short")
    assert ev["gates"]["decision_close"] == 100.0


def test_c3_flat_hour_no_signal_skip():
    tdata = _warm_and_trend(FLAT_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    assert ev["type"] == "skip", ev
    assert ev["reason"] == "no_signal", ev
    assert ev["detail"]  # известное равенство в пределах FLOAT_EPS


def test_c3_incomplete_hour_is_insufficient_not_no_signal():
    issues = []
    bars = _flat_day(WARM, price=100.0)
    tick = a7.label_ts(DAY, "10:00")
    trunc = a7.label_ts(DAY, "10:19")
    while tick <= trunc:
        bars.append(_minute(tick, 100.0))
        tick += timedelta(minutes=1)
    tdata = a7.build_ticker_data(TICKER, bars, WARM, issues)
    ev = c3.evaluate_trend_day(tdata, DAY)
    assert ev["type"] == "insufficient_context", ev
    assert ev["reason"] == "incomplete_hour", ev
    assert ev["type"] != "skip" and ev.get("reason") != "no_signal", ev


def test_c3_bar_not_full_is_insufficient():
    tdata = _warm_and_trend(LONG_CLOSES, missing=(612,))
    ev = c3.evaluate_trend_day(tdata, DAY)
    assert ev["type"] == "insufficient_context", ev
    assert ev["reason"] == "incomplete_hour", ev


def test_c3_atr_warmup_insufficient_without_warmup():
    issues = []
    bars = _trend_day(DAY, LONG_CLOSES)
    tdata = a7.build_ticker_data(TICKER, bars, DAY, issues)
    ev = c3.evaluate_trend_day(tdata, DAY)
    assert ev["type"] == "insufficient_context", ev
    assert ev["reason"] == "atr_warmup_insufficient", ev


def test_c3_signal_utc_timestamps_and_next_open_minute():
    tdata = _warm_and_trend(LONG_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    ie = a7.label_ts(DAY, "11:00")
    assert ev["observed_until"] == ie.astimezone(timezone.utc).isoformat()
    assert ev["intended_entry_time"] == ev["decision_close_time"]
    assert ev["intended_entry_time"] == ie.isoformat()
    assert ev["last_bar_open_time"] == a7.label_ts(DAY, "10:50").isoformat()
    assert ev["atr"] == a7.atr_before(tdata, a7.label_ts(DAY, "10:50"))[0]
    assert ev["atr"] > 0


def test_c3_exec_filled_next_open_sl_tp_via_existing_executor():
    tdata = _warm_and_trend(LONG_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    rec = a24.execute_candidate_signal(ev, tdata, COSTS)
    assert rec["status"] == "FILLED", rec
    assert rec["exit_reason"] == "time_exit_18_30", rec
    ie = a7.label_ts(DAY, "11:00")
    assert rec["entry_ts"] == ie.isoformat(), rec
    assert rec["entry_base"] == 110.0, rec
    sl = rec["entry_base"] - 2.0 * ev["atr"]
    tp = rec["entry_base"] + 3.0 * ev["atr"]
    assert rec["sl_level"] == sl, rec
    assert rec["tp_level"] == tp, rec
    assert rec["gross"] != ""
    assert abs(rec["net"] - (rec["gross"] - rec["costs_total"])) <= ABS_TOL, rec
    assert rec["candidate_id"] == "C3", rec
    assert rec["spec_hash"] == c3.TREND_SPEC_HASH, rec


def test_c3_exec_short_direction_accounting():
    tdata = _warm_and_trend(SHORT_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    rec = a24.execute_candidate_signal(ev, tdata, COSTS)
    assert rec["status"] == "FILLED", rec
    assert rec["side"] == "short", rec
    assert rec["sl_level"] == rec["entry_base"] + 2.0 * ev["atr"], rec
    assert rec["tp_level"] == rec["entry_base"] - 3.0 * ev["atr"], rec
    assert abs(rec["net"] - (rec["gross"] - rec["costs_total"])) <= ABS_TOL, rec


def test_c3_exec_missing_entry_minute_is_skip_not_search():
    tdata = _warm_and_trend(LONG_CLOSES, missing=(660,))
    ev = c3.evaluate_trend_day(tdata, DAY)
    assert ev["type"] == "signal", ev
    rec = a24.execute_candidate_signal(ev, tdata, COSTS)
    assert rec["status"] == "SKIP", rec
    assert rec["incomplete_reason"] == "missing_entry", rec


def test_c3_exec_path_gap_after_entry_is_incomplete():
    tdata = _warm_and_trend(LONG_CLOSES, missing=(780,))
    ev = c3.evaluate_trend_day(tdata, DAY)
    assert ev["type"] == "signal", ev
    rec = a24.execute_candidate_signal(ev, tdata, COSTS)
    assert rec["status"] == "INCOMPLETE", rec
    assert rec["incomplete_reason"] == "path_gap_after_entry", rec
    assert rec["net"] == "" and rec["entry_base"] != ""


def test_c3_exec_rejects_foreign_spec_and_unknown_candidate():
    tdata = _warm_and_trend(LONG_CLOSES)
    ev = c3.evaluate_trend_day(tdata, DAY)
    bad = dict(ev, spec_hash="0" * 64)
    rec = a24.execute_candidate_signal(bad, tdata, COSTS)
    assert rec["status"] == "REJECT", rec
    assert rec["incomplete_reason"] == "spec_hash_mismatch", rec
    wrong_cid = dict(ev, candidate_id="C9", spec_hash="0" * 64)
    rec2 = a24.execute_candidate_signal(wrong_cid, tdata, COSTS)
    assert rec2["status"] == "REJECT", rec2
    assert rec2["incomplete_reason"] == "unknown_candidate", rec2


def test_c3_future_bars_do_not_change_accepted_signal():
    base = _warm_and_trend(LONG_CLOSES)
    ev1 = c3.evaluate_trend_day(base, DAY)
    assert ev1["type"] == "signal", ev1
    issues = []
    bars = _flat_day(WARM, price=100.0)
    bars.extend(_trend_day(DAY, LONG_CLOSES))
    tick = a7.label_ts(DAY, "11:00")
    end = a7.label_ts(DAY, "18:44")
    while tick <= end:  # будущее дня СТРОГО после минуты входа 11:00 резко уходит вверх
        if a7.msk_mod(tick) == 660:
            bars.append(_minute(tick, 110.0))
        else:
            bars.append(_minute(tick, 500.0 + 0.1 * a7.msk_mod(tick), spread=2.0))
        tick += timedelta(minutes=1)
    future = a7.build_ticker_data(TICKER, bars, WARM, issues)
    ev2 = c3.evaluate_trend_day(future, DAY)
    assert ev2 == ev1
    rec1 = a24.execute_candidate_signal(ev1, base, COSTS)
    rec2 = a24.execute_candidate_signal(ev1, future, COSTS)
    for field in ("entry_ts", "entry_base", "atr", "sl_level", "tp_level",
                  "side", "status"):
        assert rec1[field] == rec2[field], field


def test_c3_registration_pin_in_executor():
    assert a24.CANDIDATE_SPEC_HASHES["C3"] == c3.TREND_SPEC_HASH
    assert c3.CANDIDATE_ID == "C3"
    assert c3.SL_ATR_MULT == a7.SL_ATR_MULT == 2.0
    assert c3.TP_ATR_MULT == a7.TP_ATR_MULT == 3.0
    assert "C3" in a24.CANDIDATE_SPEC_HASHES
    assert c3.spec_hash() == c3.TREND_SPEC_HASH