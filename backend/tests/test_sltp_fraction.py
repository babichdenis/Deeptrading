"""Golden-проверки SL/TP: доли против процентов и правильные стороны LONG/SHORT.

Случай из paper-контура 15.11.2026: в конфиге стояло sl_mode=fixed со
stop_pct=50.0 и target_pct=50.0 (проценты на месте долей). FixedSlTpPolicy
считает stop = fill*(1 ∓ stop_pct), поэтому получалось fill*51 и fill*(−49):
ASTR при 213.17 — SL 10873.71, TP −10447.29; SBER при 276.64 — SL −13552.42,
TP 14105.58. Стопы такой ширины не срабатывают, позиция остаётся без защиты.
"""
from __future__ import annotations

import pytest

from app.engine.exits import (AtrStopPolicy, FixedSlTpPolicy, PLAUSIBLE_LEVEL_SPAN,
                              is_fraction, level_implausible, level_problem, pct_problem)
from app.engine.models import Candle, Side
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# --- is_fraction: единая проверка «доля, а не проценты» ---------------------

@pytest.mark.parametrize("value", [0.01, 0.02, 0.5, 0.999])
def test_fraction_accepts_shares(value: float) -> None:
    assert is_fraction(value) is True


@pytest.mark.parametrize("value", [50.0, 10.0, 1.0, 0.0, -0.5, None, "abc"])
def test_fraction_rejects_percents_and_junk(value) -> None:
    assert is_fraction(value) is False


# --- дефект зафиксирован: проценты на месте доли дают неисполнимый стоп ----

def test_percent_as_share_reproduces_defect() -> None:
    pol = FixedSlTpPolicy(stop_pct=50.0, target_pct=50.0)
    plan = pol.plan_entry(Side.BUY, 276.58, [])
    assert plan.stop_loss == pytest.approx(-13552.42, rel=1e-5)
    assert plan.take_profit == pytest.approx(14105.58, rel=1e-5)
    assert plan.stop_loss < 0  # цена ниже нуля — стоп невозможен


# --- корректные доли: LONG и SHORT ----------------------------------------

def test_fixed_policy_long_correct() -> None:
    pol = FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02)
    plan = pol.plan_entry(Side.BUY, 276.58, [])
    assert plan.stop_loss == pytest.approx(273.8142, rel=1e-6)
    assert plan.take_profit == pytest.approx(282.1116, rel=1e-6)
    assert plan.stop_loss < 276.58 < plan.take_profit


def test_fixed_policy_short_correct() -> None:
    pol = FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02)
    plan = pol.plan_entry(Side.SELL, 213.21, [])
    assert plan.stop_loss == pytest.approx(215.3421, rel=1e-6)
    assert plan.take_profit == pytest.approx(208.9458, rel=1e-6)
    assert plan.take_profit < 213.21 < plan.stop_loss


# --- ATR-политика: стороны и величина стопа -------------------------------

def _bars(closes: list[float]) -> list[Candle]:
    t0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    return [Candle(ts=t0 + timedelta(minutes=5 * i), open=c, high=c + 0.1,
                   low=c - 0.1, close=c, volume=100) for i, c in enumerate(closes)]


def test_atr_policy_long_and_short_sides() -> None:
    bars = _bars([219.9 + 0.01 * i for i in range(30)])
    pol = AtrStopPolicy(period=14, multiplier=4.0, risk_reward=1.0)
    long_plan = pol.plan_entry(Side.BUY, 220.0, bars)
    short_plan = AtrStopPolicy(period=14, multiplier=4.0, risk_reward=1.0).plan_entry(
        Side.SELL, 220.0, bars)
    assert long_plan.stop_loss < 220.0 < long_plan.take_profit
    assert short_plan.take_profit < 220.0 < short_plan.stop_loss
    # дистанция стопа одинаковой величины в обе стороны
    assert (220.0 - long_plan.stop_loss) == pytest.approx(
        short_plan.stop_loss - 220.0, rel=1e-9)
    # на спокойном ряду стоп в рублях, а не в «процентах от цены в 50 раз»
    assert (220.0 - long_plan.stop_loss) < 220.0 * 0.1


# --- level_problem: свойства уровня, а не его ширина ----------------------

@pytest.mark.parametrize("value", [float("nan"), float("inf"), -5.0, 0.0, "abc", "12x"])
def test_level_rejects_non_positive_or_nonfinite(value: object) -> None:
    assert level_problem(value, "stop", Side.BUY, 100.0) is not None


def test_level_accepts_numeric_string() -> None:
    """Число в виде строки — не ошибка единиц: 95 как стоп для LONG валиден."""
    assert level_problem("95", "stop", Side.BUY, 100.0) is None


@pytest.mark.parametrize("side,stop,target", [
    (Side.BUY, 95.0, 110.0),
    (Side.SELL, 105.0, 90.0),
])
def test_level_accepts_correct_sides(side: Side, stop: float, target: float) -> None:
    assert level_problem(stop, "stop", side, 100.0) is None
    assert level_problem(target, "target", side, 100.0) is None


@pytest.mark.parametrize("side,stop,target", [
    (Side.BUY, 105.0, 90.0),   # перепутаны стороны — ровно дефект ASTR/SBER
    (Side.SELL, 95.0, 110.0),
])
def test_level_rejects_inverted_sides(side: Side, stop: float, target: float) -> None:
    assert level_problem(stop, "stop", side, 100.0) is not None
    assert level_problem(target, "target", side, 100.0) is not None


def test_wide_but_valid_stop_is_not_dropped() -> None:
    """Широкий уровень не повод его отбрасывать: прежний порог «50% от входа»
    отсекал именно такие и мог оставить позицию без защиты."""
    assert level_problem(20.0, "stop", Side.BUY, 100.0) is None
    assert level_problem(500.0, "target", Side.BUY, 100.0) is None


def test_level_problem_with_percent_scale_defect() -> None:
    """Те же значения, что дал дефект: 213.17 -> старый стоп 10873.71."""
    assert level_problem(10873.71, "stop", Side.BUY, 213.17) is not None
    assert level_problem(-13552.42, "stop", Side.BUY, 276.64) is not None


# --- масштаб уровня: сторона бывает верной, а дистанция — абсурдной -------

@pytest.mark.parametrize("ticker,side,entry,bad_sl", [
    ("ASTR", Side.SELL, 213.17, 10873.71),
    ("AFLT", Side.SELL, 31.89, 1626.90),
])
def test_percent_scale_defect_passes_side_check_but_fails_scale(
        ticker: str, side: Side, entry: float, bad_sl: float) -> None:
    """Дефект 15.11.2026 на SELL: стоп ВЫШЕ входа, то есть сторона формально
    правильная, и проверка стороны его пропускает. Ловит его проверка масштаба."""
    assert level_problem(bad_sl, "stop", side, entry) is None       # сторона верна
    assert level_implausible(bad_sl, entry) is True                 # но 50x цены


@pytest.mark.parametrize("ticker,side,entry,bad_sl", [
    ("SBER", Side.BUY, 276.64, -13552.42),
    ("MTSS", Side.BUY, 186.43, -9133.11),
])
def test_negative_stop_caught_by_positivity_check(
        ticker: str, side: Side, entry: float, bad_sl: float) -> None:
    """На BUY тот же дефект даёт отрицательный стоп — его ловит уже проверка
    положительности, до проверки масштаба дело не доходит."""
    assert level_problem(bad_sl, "stop", side, entry) is not None
    assert level_implausible(bad_sl, entry) is True


@pytest.mark.parametrize("entry,level", [
    (100.0, 60.0),    # стоп в 40% ниже — широкий, но в пределах порога
    (100.0, 140.0),   # тейк в 40% выше
])
def test_scale_allows_wide_levels(entry: float, level: float) -> None:
    assert level_implausible(level, entry) is False


@pytest.mark.parametrize("entry,level", [
    (100.0, 20.0),    # 80% — за порогом
    (100.0, 500.0),   # 400% — тем более
])
def test_scale_rejects_too_far_levels(entry: float, level: float) -> None:
    assert level_implausible(level, entry) is True


def test_scale_threshold_is_half_of_entry() -> None:
    assert level_implausible(100.0 - 49.0, 100.0) is False
    assert level_implausible(100.0 - 51.0, 100.0) is True
    assert PLAUSIBLE_LEVEL_SPAN == 0.5


def test_scale_rejects_nan_and_unknown_entry() -> None:
    assert level_implausible(float("nan"), 100.0) is True
    assert level_implausible(95.0, float("nan")) is True
    assert level_implausible(95.0, 0.0) is True
    assert level_implausible(None, 100.0) is False


def test_live_config_levels_pass_span_rule() -> None:
    cases = (
        ("SBER", 276.64, 0.2498, 1),
        ("ASTR", 213.17, 0.1716, 1),
        ("VTBR", 52.26, 0.0287, 1),
        ("MTSS", 185.28, 0.2632, 1),
    )
    for ticker, entry, atr, mult in cases:
        dist = atr * mult
        planned = (
            ("BUY", "stop", entry - dist),
            ("BUY", "target", entry + dist),
            ("SELL", "stop", entry + dist),
            ("SELL", "target", entry - dist),
        )
        for side, kind, level in planned:
            problem = level_problem(level, kind, side, entry)
            assert problem is None, (ticker, side, kind, level, problem)
            assert level_implausible(level, entry) is False, (ticker, side, kind, level)


def test_fixed_pct_config_levels_pass_span_rule() -> None:
    for entry in (276.64, 213.17, 52.26, 185.28):
        for pct in (0.01, 0.02):
            stop = entry * (1 - pct)
            target = entry * (1 + pct)
            assert level_implausible(stop, entry) is False, (entry, pct)
            assert level_implausible(target, entry) is False, (entry, pct)


def test_exactly_half_entry_distance_is_allowed() -> None:
    entry = 100.0
    assert level_implausible(entry * 0.5, entry) is False
    assert level_implausible(entry * 1.5, entry) is False
    assert level_implausible(entry * 0.49, entry) is True
    assert level_implausible(entry * 1.51, entry) is True


def test_pct_problem_blocks_percent_scale() -> None:
    assert pct_problem("stop_pct", 50.0) is not None
    assert pct_problem("stop_pct", 2.0) is not None
    assert pct_problem("stop_pct", 1.0) is not None
    assert pct_problem("stop_pct", 0.0) is not None
    assert pct_problem("stop_pct", -0.5) is not None
    assert pct_problem("stop_pct", float("nan")) is not None
    assert pct_problem("stop_pct", float("inf")) is not None
    assert pct_problem("stop_pct", "abc") is not None
    assert pct_problem("stop_pct", None) is not None


def test_pct_problem_accepts_live_values() -> None:
    assert pct_problem("stop_pct", 0.01) is None
    assert pct_problem("target_pct", 0.02) is None
    assert pct_problem("stop_pct", "0.015") is None
    assert pct_problem("stop_pct", 0.2) is None

def test_atr_plan_records_atr_multiplier_and_policy() -> None:
    closes = [100.0] * 30
    t0 = datetime(2026, 11, 14, 10, 0, tzinfo=UTC)
    bars = [Candle(ts=t0 + timedelta(minutes=5 * i), open=100.0, high=101.0,
                   low=99.0, close=100.0, volume=100) for i in range(30)]
    pol = AtrStopPolicy(period=14, multiplier=4.0, risk_reward=1.0)
    plan = pol.plan_entry(Side.BUY, 100.0, bars)
    assert plan.atr is not None
    assert plan.atr == pytest.approx(2.0, rel=1e-6)
    assert plan.atr_period == 14
    assert plan.atr_multiplier == 4.0
    assert plan.policy_id == "atr_stop"
    assert plan.atr_as_of == bars[-1].ts
    assert abs(plan.stop_loss - 100.0) / plan.atr == pytest.approx(4.0, rel=1e-6)
    assert closes  # серия из плоских баров не нужна, но фиксирует сценарий


def test_fixed_plan_records_policy_without_atr() -> None:
    pol = FixedSlTpPolicy(stop_pct=0.01, target_pct=0.02)
    plan = pol.plan_entry(Side.BUY, 100.0, [])
    assert plan.policy_id == "fixed_sl_tp"
    assert plan.atr is None
    assert plan.atr_multiplier is None