'''Юнит-тесты индикаторов ema/rsi/macd (шаг 1 плана выходов, 2026-09-26).

RSI 70.46 — эталон по классическому примеру Уайлдера (первое значение на
20-точечном ряде). Плоский ряд даёт нейтральные 50 — это контракт,
на который опирается вето-логика (не блокировать и не форсировать).
'''
from __future__ import annotations

import pytest

from app.engine.indicators import ema, macd, rsi

WILDER = [
    44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
    45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
]


class TestEma:
    def test_constant_series(self):
        out = ema([10.0] * 15, 12)
        assert out[10] is None
        assert out[11] == pytest.approx(10.0)
        assert out[14] == pytest.approx(10.0)

    def test_first_valid_at_period_minus_one(self):
        out = ema(list(range(20)), 5)
        assert all(v is None for v in out[:4])
        assert out[4] == pytest.approx(2.0)

    def test_short_input_all_none(self):
        assert ema([1.0, 2.0], 5) == [None, None]

    def test_zero_period_all_none(self):
        assert ema([1.0, 2.0, 3.0], 0) == [None, None, None]

    def test_trend_lags_price(self):
        closes = [100.0 + i for i in range(50)]
        out = ema(closes, 12)
        assert out[-1] < closes[-1]
        assert out[-1] > out[-10]


class TestRsi:
    def test_wilder_reference(self):
        out = rsi(WILDER)
        assert out[14] == pytest.approx(70.46, abs=0.1)

    def test_flat_series_neutral_50(self):
        assert rsi([5.0] * 20)[14] == pytest.approx(50.0)

    def test_all_gains_is_100(self):
        out = rsi([float(i) for i in range(20)])
        assert out[14] == pytest.approx(100.0)

    def test_all_losses_is_0(self):
        out = rsi([float(-i) for i in range(20)])
        assert out[14] == pytest.approx(0.0)

    def test_short_input_all_none(self):
        assert rsi([1.0, 2.0, 3.0]) == [None, None, None]

    def test_first_valid_at_period(self):
        out = rsi(WILDER)
        assert all(v is None for v in out[:14])
        assert out[14] is not None

    def test_values_within_bounds(self):
        out = rsi(WILDER + WILDER)
        assert all(v is None or 0.0 <= v <= 100.0 for v in out)


class TestMacd:
    def test_constant_series_zero(self):
        m = macd([100.0] * 60)
        assert m['line'][25] == pytest.approx(0.0)
        assert m['hist'][33] == pytest.approx(0.0)

    def test_shapes_match_input(self):
        m = macd([100 + 0.5 * i for i in range(60)])
        assert len(m['line']) == len(m['signal']) == len(m['hist']) == 60

    def test_line_valid_from_slow_minus_one(self):
        m = macd([100 + 0.5 * i for i in range(60)])
        assert m['line'][24] is None
        assert m['line'][25] == pytest.approx(3.5)

    def test_signal_valid_after_signal_period(self):
        m = macd([100 + 0.5 * i for i in range(60)])
        assert m['signal'][32] is None
        assert m['signal'][33] is not None

    def test_hist_is_line_minus_signal(self):
        m = macd([100 + 0.5 * i for i in range(60)])
        for i in range(60):
            if m['hist'][i] is not None:
                assert m['hist'][i] == pytest.approx(m['line'][i] - m['signal'][i])

    def test_uptrend_hist_positive(self):
        # hist>0 требует разгона: на линейном тренде line==signal и hist=0
        assert macd([100.0 + 0.05 * i * i for i in range(120)])['hist'][-1] > 0

    def test_downtrend_hist_negative(self):
        assert macd([300.0 - 0.05 * i * i for i in range(120)])['hist'][-1] < 0

    def test_linear_trend_hist_zero(self):
        # контракт: линейный рост не даёт импульса (важно для вето-логики)
        m = macd([100.0 + 2.0 * i for i in range(120)])
        assert abs(m['hist'][-1]) < 1e-6

    def test_short_input_all_none(self):
        m = macd([1.0] * 10)
        assert m['line'] == [None] * 10
