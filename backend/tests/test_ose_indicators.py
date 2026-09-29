"""Тесты OsEngine-портов индикаторов (app/engine/ose/indicators.py).

Инварианты сняты с поведения ~/OsEngine → Indicators/Scripts/*.cs:
окна, индексы первого валидного значения, делители, округление и
зафиксированные quirks (см. докстринги модуля indicators.py).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app.engine.models import Candle
from app.engine.ose.indicators import (
    atr,
    bears_power,
    bollinger,
    bulls_power,
    cci,
    envelops,
    macd,
    price_channel,
    rsi,
    rvi,
    sma,
    stochastic,
)

_T0 = datetime(2026, 1, 5, 10, 0)


def make_candles(closes, *, high=None, low=None):
    candles = []
    for i, close in enumerate(closes):
        h = close if high is None else high(i)
        low_value = close if low is None else low(i)
        candles.append(
            Candle(
                ts=_T0 + timedelta(minutes=i),
                open=close,
                high=h,
                low=low_value,
                close=close,
                volume=100.0,
            )
        )
    return candles


# --- Sma ----------------------------------------------------------------------


def test_sma_window_and_first_valid_index():
    candles = make_candles([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    result = sma(candles, 3)
    # quirk ScriptSpells.Summ: окно [i-len+1, i] → первый валид i=len, свеча 0 не входит
    assert result[:3] == [None, None, None]
    assert result[3] == pytest.approx((2.0 + 3.0 + 4.0) / 3.0)
    assert result[4] == pytest.approx((3.0 + 4.0 + 5.0) / 3.0)
    assert result[5] == pytest.approx((4.0 + 5.0 + 6.0) / 3.0)


def test_sma_point_high():
    candles = make_candles([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], high=lambda i: i + 10.0)
    result = sma(candles, 3, point="high")
    assert result[3] == pytest.approx((11.0 + 12.0 + 13.0) / 3.0)


def test_sma_constant_series():
    candles = make_candles([50.0] * 25)
    result = sma(candles, 10)
    assert result[:10] == [None] * 10
    assert all(v == 50.0 for v in result[10:])


# --- Rsi ----------------------------------------------------------------------


def test_rsi_first_valid_index_is_len_plus_21():
    rising = make_candles([float(i) for i in range(80)])
    result = rsi(rising, 14)
    first = next(i for i, v in enumerate(result) if v is not None)
    assert first == 14 + 21


def test_rsi_pure_rise_pegs_at_100():
    rising = make_candles([float(i) for i in range(80)])
    result = rsi(rising, 14)
    values = [v for v in result if v is not None]
    assert values and all(v == 100.0 for v in values)


def test_rsi_pure_fall_pegs_at_100_too():
    # quirk оригинала: avgHigh == 0 → тоже 100 (не 0)
    falling = make_candles([float(100 - i) for i in range(80)])
    result = rsi(falling, 14)
    values = [v for v in result if v is not None]
    assert values and all(v == 100.0 for v in values)


def test_rsi_mixed_market_inside_bounds():
    closes = []
    price = 100.0
    for i in range(80):
        price += 1.0 if i % 2 == 0 else -0.7
        closes.append(price)
    result = rsi(make_candles(closes), 14)
    values = [v for v in result if v is not None]
    assert values
    assert all(0.0 < v < 100.0 for v in values)


# --- Stochastic ---------------------------------------------------------------


def test_stochastic_gapless_rising():
    candles = make_candles([float(i) for i in range(20)])
    result = stochastic(candles, 5, 3, 3)
    k = result["k"]
    d = result["d"]
    assert k[:6] == [None] * 6  # ряд пишется с i > max(P1, P2, P3)
    assert k[6] == 0.0 and k[7] == 0.0 and k[8] == 0.0  # под-прогрев i < P2+P3+3
    assert k[9] == 100.0  # безфитильный рост: T1 == T2
    assert d[9] == pytest.approx(33.33)  # (0 + 0 + 100) / 3
    assert d[11] == 100.0
    assert k[19] == 100.0


def test_stochastic_flat_is_zero():
    candles = make_candles([10.0] * 20)
    result = stochastic(candles, 5, 3, 3)
    values = [v for v in result["k"] if v is not None]
    assert values and all(v == 0.0 for v in values)


# --- Bollinger ----------------------------------------------------------------


def test_bollinger_len_le_30_divides_by_len():
    candles = make_candles([float(i) for i in range(1, 13)])
    result = bollinger(candles, 5, 2.0)
    up, down, center = result["up"], result["down"], result["center"]
    assert up[:6] == [None] * 6  # первый валид i = len+1
    sma_val = (3.0 + 4.0 + 5.0 + 6.0 + 7.0) / 5.0  # окно [2..6]
    var = sum((v - sma_val) ** 2 for v in (3.0, 4.0, 5.0, 6.0, 7.0)) / 5.0
    std = math.sqrt(var)
    assert center[6] == pytest.approx(sma_val)
    assert up[6] == pytest.approx(round(sma_val + 2.0 * std, 6))
    assert down[6] == pytest.approx(round(sma_val - 2.0 * std, 6))


def test_bollinger_len_gt_30_divides_by_len_minus_1():
    n = 60
    closes = [float(i) for i in range(1, n + 1)]
    candles = make_candles(closes)
    length = 31
    result = bollinger(candles, length, 2.0)
    assert result["up"][length] is None  # первый валид i = len+1
    assert result["up"][length + 1] is not None
    i = 45
    sma_val = sum(closes[i - length + 1 : i + 1]) / length
    var = sum(
        (closes[j] - sma_val) ** 2 for j in range(i - length + 1, i + 1)
    ) / (length - 1)
    std = math.sqrt(var)
    assert result["center"][i] == pytest.approx(sma_val)
    assert result["up"][i] == pytest.approx(round(sma_val + 2.0 * std, 6))
    assert result["down"][i] == pytest.approx(round(sma_val - 2.0 * std, 6))


def test_bollinger_flat_bands_collapse():
    candles = make_candles([50.0] * 30)
    result = bollinger(candles, 10, 2.0)
    assert result["up"][11] == pytest.approx(50.0)
    assert result["down"][11] == pytest.approx(50.0)
    assert result["center"][11] == 50.0


# --- Envelops -----------------------------------------------------------------


def test_envelops_percent_bands():
    candles = make_candles([100.0] * 30)
    result = envelops(candles, 10, 0.3)
    assert result["up"][10] is None  # первый валид i = len+1
    assert result["up"][11] == pytest.approx(100.3)
    assert result["center"][11] == 100.0
    assert result["down"][11] == pytest.approx(99.7)


# --- PriceChannel -------------------------------------------------------------


def test_price_channel_windows():
    candles = make_candles(
        [float(i) for i in range(10)],
        high=lambda i: i + 1.0,
        low=lambda i: i - 1.0,
    )
    result = price_channel(candles, 3, 3)
    assert result["up"][:4] == [None] * 4  # первый валид i = lenUp+1
    assert result["up"][4] == 5.0  # max High по окну [2..4]
    assert result["down"][4] == 1.0  # min Low по окну [2..4]
    assert result["up"][9] == 10.0
    assert result["down"][9] == 6.0


# --- ATR -----------------------------------------------------------------------


def test_atr_first_valid_is_len_minus_1_with_wilder_seed():
    candles = make_candles([10.0 + i for i in range(30)])
    result = atr(candles, 14)
    assert result[:13] == [None] * 13  # первый валид i = length-1
    # без фитилей: TR[i] = 1 при i >= 1, TR[0] = 0 → сид = 13/14
    assert result[13] == pytest.approx(round(13.0 / 14.0, 9))
    # Wilder: (prev*13 + 1)/14
    assert result[14] == pytest.approx(round((13.0 / 14.0 * 13 + 1) / 14, 9))


def test_atr_flat_is_zero_after_warmup():
    candles = make_candles([50.0] * 30)
    result = atr(candles, 14)
    assert result[:13] == [None] * 13
    assert all(v == 0.0 for v in result[13:])


def test_atr_percent_mode_divides_by_prev_open():
    candles = make_candles([10.0 + i for i in range(20)])
    result = atr(candles, 14, mode="percent")
    # TR[i] = 1 / (Open[i-1]/100) = 100/(9+i) при i >= 1 (Open = Close)
    expected_seed = round(sum(100.0 / (9 + j) for j in range(1, 14)) / 14.0, 9)
    assert result[13] == pytest.approx(expected_seed)


# --- CCI -----------------------------------------------------------------------


def test_cci_first_valid_and_flat_md_zero_is_zero():
    candles = make_candles([100.0] * 12, high=lambda i: 101.0, low=lambda i: 99.0)
    result = cci(candles, 5)
    assert result[:6] == [None] * 6  # первый валид i = len+1
    # typical = (101+99+100)/3 = 100 = ma → md == 0 → пишется 0.0 (quirk C#)
    assert result[6] == 0.0
    assert all(v == 0.0 for v in result[6:])


def test_cci_value_matches_formula():
    candles = make_candles(
        [float(i) for i in range(12)],
        high=lambda i: float(i),
        low=lambda i: float(i),
    )
    result = cci(candles, 5)
    # окно [2..6]: pts 2..6, ma = 4, md = 6 → (6-4)/(6*0.015/5)
    assert result[6] == pytest.approx(round(2.0 / (6.0 * 0.015 / 5.0), 5))
    assert result[5] is None


# --- Macd ----------------------------------------------------------------------


def _ema_ref(values, length):
    out = [0.0] * len(values)
    for i in range(len(values)):
        if i == length:
            out[i] = sum(values[i - length + 1 : i + 1]) / length
        elif i > length:
            a = round(2.0 / (length + 1), 8)
            out[i] = out[i - 1] + a * (values[i] - out[i - 1])
    return out


def test_macd_warmup_and_first_values():
    closes = [float(i) for i in range(60)]
    candles = make_candles(closes)
    result = macd(candles, 12, 26, 9)
    assert result["macd"][:26] == [None] * 26  # пишется с max(fast, slow)
    assert result["signal"][:26] == [None] * 26
    fast, slow = _ema_ref(closes, 12), _ema_ref(closes, 26)
    macd26 = fast[26] - slow[26]
    assert result["macd"][26] == pytest.approx(macd26)
    # сигнал: EMA по macd-ряду с нулевым прогревом → на 26: 0 + a*(macd - 0)
    a = round(2.0 / 10.0, 8)
    assert result["signal"][26] == pytest.approx(round(a * macd26, 8))
    assert result["histogram"][26] == pytest.approx(macd26 - result["signal"][26])


def test_macd_flat_is_zero():
    candles = make_candles([100.0] * 40)
    result = macd(candles, 12, 26, 9)
    assert result["macd"][26] == pytest.approx(0.0)
    assert result["signal"][30] == pytest.approx(0.0)
    assert result["histogram"][30] == pytest.approx(0.0)


# --- Rvi -----------------------------------------------------------------------


def test_rvi_constant_body_ratio_and_signal():
    candles = [
        Candle(
            ts=_T0 + timedelta(minutes=i),
            open=10.0 + i,
            high=12.0 + i,
            low=9.0 + i,
            close=11.0 + i,
            volume=100.0,
        )
        for i in range(20)
    ]
    result = rvi(candles, 5)
    assert result["rvi"][:6] == [None] * 6  # пишется с i = period+1
    assert result["rvi"][6] == pytest.approx(0.33)  # 18/54 (окно с нулями)
    assert result["rvi"][10] == pytest.approx(0.33)  # 30/90
    assert result["signal"][:6] == [None] * 6
    assert result["signal"][6] == 0.0  # GetValueSecond → 0 до period+6
    assert result["signal"][11] == pytest.approx(0.33)


def test_rvi_doji_is_zero():
    candles = make_candles([50.0] * 20)  # open == close == high == low
    result = rvi(candles, 5)
    values = [v for v in result["rvi"] if v is not None]
    assert values and all(v == 0.0 for v in values)


# --- Bulls / Bears Power -------------------------------------------------------


def test_bulls_bears_power_first_valid_is_period_plus_1():
    candles = make_candles([100.0] * 20, high=lambda i: 102.0, low=lambda i: 98.0)
    bulls = bulls_power(candles, 13)
    bears = bears_power(candles, 13)
    # Charts-MovingAverage: первый валид i = len+1 = 14 (не 13, как у sma())
    assert bulls[:14] == [None] * 14
    assert bears[:14] == [None] * 14
    assert bulls[14] == pytest.approx(2.0)  # High - SMA(Close) = 102 - 100
    assert bears[14] == pytest.approx(-2.0)  # Low - SMA(Close) = 98 - 100


def test_bulls_power_tracks_high_minus_sma():
    closes = [100.0 + i for i in range(25)]
    candles = make_candles(
        closes, high=lambda i: closes[i] + 3.0, low=lambda i: closes[i] - 3.0
    )
    bulls = bulls_power(candles, 5)
    i = 20
    sma_val = round(sum(closes[i - 4 : i + 1]) / 5.0, 8)
    assert bulls[i] == pytest.approx(closes[i] + 3.0 - sma_val)
