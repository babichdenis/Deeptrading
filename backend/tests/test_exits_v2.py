'''Юнит-тесты шага 2 плана выходов (2026-09-26): wick_tol в intrabar_exit,
breakeven_stop, early_abort_exit.'''
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.engine.exits import breakeven_stop, early_abort_exit, intrabar_exit
from app.engine.models import Candle, PositionState, Side


def bar(open_, high, low, close, i=0):
    return Candle(ts=datetime(2026, 9, 26) + timedelta(minutes=i),
                  open=open_, high=high, low=low, close=close)


class TestIntrabarExitWick:
    def test_wick_tol_zero_keeps_touch_exit(self):
        # дефолт: любой хвост до стопа закрывает (старое поведение)
        b = bar(101.0, 101.5, 99.4, 100.5)
        assert intrabar_exit(b, PositionState.LONG, 100.0, None) == (100.0, 'stop_loss')

    def test_small_wick_forgiven(self):
        b = bar(101.0, 101.5, 99.8, 100.5)  # прокол 0.2 <= tol 0.5, close выше стопа
        assert intrabar_exit(b, PositionState.LONG, 100.0, None, wick_tol=0.5) == (None, '')

    def test_exact_tol_boundary_forgiven(self):
        b = bar(101.0, 101.5, 99.5, 100.5)  # прокол ровно 0.5 == tol
        assert intrabar_exit(b, PositionState.LONG, 100.0, None, wick_tol=0.5) == (None, '')

    def test_deep_wick_not_forgiven(self):
        b = bar(101.0, 101.5, 99.0, 100.5)  # прокол 1.0 > tol 0.5
        assert intrabar_exit(b, PositionState.LONG, 100.0, None, wick_tol=0.5) == (100.0, 'stop_loss')

    def test_close_beyond_stop_not_forgiven(self):
        b = bar(101.0, 101.5, 99.8, 99.9)  # закрылся за стопом
        # touch-семантика (не close_based): выход по УРОВНЮ стопа, не по close
        assert intrabar_exit(b, PositionState.LONG, 100.0, None, wick_tol=0.5) == (100.0, 'stop_loss')

    def test_gap_open_beyond_stop_not_forgiven(self):
        b = bar(99.5, 101.5, 99.0, 100.5)  # гэп открытия через стоп
        assert intrabar_exit(b, PositionState.LONG, 100.0, None, wick_tol=0.5) == (99.5, 'stop_loss')

    def test_short_small_wick_forgiven(self):
        b = bar(99.0, 100.2, 98.5, 99.5)  # high проколол стоп 100 на 0.2, close ниже
        assert intrabar_exit(b, PositionState.SHORT, 100.0, None, wick_tol=0.5) == (None, '')

    def test_short_deep_wick_not_forgiven(self):
        b = bar(99.0, 101.2, 98.5, 99.5)
        assert intrabar_exit(b, PositionState.SHORT, 100.0, None, wick_tol=0.5) == (100.0, 'stop_loss')

    def test_short_close_beyond_stop_not_forgiven(self):
        b = bar(99.0, 100.2, 98.5, 100.5)
        # touch-семантика: выход по УРОВНЮ стопа, не по close
        assert intrabar_exit(b, PositionState.SHORT, 100.0, None, wick_tol=0.5) == (100.0, 'stop_loss')

    def test_tp_unaffected(self):
        b = bar(99.0, 101.0, 98.5, 100.8)
        assert intrabar_exit(b, PositionState.LONG, 98.0, 101.0, wick_tol=0.5) == (101.0, 'target')

    def test_close_based_ignores_wick_tol(self):
        b = bar(101.0, 101.5, 99.8, 100.5)
        assert intrabar_exit(b, PositionState.LONG, 100.0, None, close_based=True, wick_tol=0.5) == (None, '')
        b2 = bar(101.0, 101.5, 99.8, 99.9)
        assert intrabar_exit(b2, PositionState.LONG, 100.0, None, close_based=True, wick_tol=0.5) == (99.9, 'stop_loss')


class TestBreakevenStop:
    @staticmethod
    def bars_close(close):
        return [bar(100.0, 100.5, 99.5, close)]

    def test_not_triggered_keeps_current(self):
        s = breakeven_stop(Side.BUY, 100.0, 98.0, self.bars_close(101.0), risk=2.0, trigger_r=1.0)
        assert s == pytest.approx(98.0)

    def test_triggered_moves_to_entry(self):
        s = breakeven_stop(Side.BUY, 100.0, 98.0, self.bars_close(102.0), risk=2.0, trigger_r=1.0)
        assert s == pytest.approx(100.0)

    def test_ratchet_never_backwards(self):
        s = breakeven_stop(Side.BUY, 100.0, 100.3, self.bars_close(102.0), risk=2.0)
        assert s == pytest.approx(100.3)  # кандидат 100.0 хуже текущего — остаётся 100.3

    def test_offset_pct(self):
        s = breakeven_stop(Side.BUY, 100.0, 98.0, self.bars_close(102.0), risk=2.0, offset_pct=0.001)
        assert s == pytest.approx(100.1)

    def test_no_current_stop(self):
        s = breakeven_stop(Side.BUY, 100.0, None, self.bars_close(102.0), risk=2.0)
        assert s == pytest.approx(100.0)

    def test_short_triggered(self):
        s = breakeven_stop(Side.SELL, 100.0, 102.0, self.bars_close(98.0), risk=2.0)
        assert s == pytest.approx(100.0)

    def test_short_not_triggered(self):
        s = breakeven_stop(Side.SELL, 100.0, 102.0, self.bars_close(99.5), risk=2.0)
        assert s == pytest.approx(102.0)

    def test_short_ratchet(self):
        s = breakeven_stop(Side.SELL, 100.0, 99.7, self.bars_close(98.0), risk=2.0)
        assert s == pytest.approx(99.7)

    def test_zero_risk_silent(self):
        assert breakeven_stop(Side.BUY, 100.0, 98.0, self.bars_close(102.0), risk=0.0) == 98.0


class TestEarlyAbort:
    def test_long_abort_on_level_touch(self):
        b = bar(101.0, 101.2, 99.0, 100.0)  # level = 100 - 0.5*2 = 99.0
        assert early_abort_exit(b, Side.BUY, 100.0, risk=2.0, bars_held=1, max_bars=3) == (99.0, 'early_abort')

    def test_long_gap_open(self):
        b = bar(98.5, 99.5, 98.0, 99.0)
        assert early_abort_exit(b, Side.BUY, 100.0, risk=2.0, bars_held=1, max_bars=3) == (98.5, 'early_abort')

    def test_no_abort_above_level(self):
        b = bar(101.0, 101.2, 99.5, 100.8)
        assert early_abort_exit(b, Side.BUY, 100.0, risk=2.0, bars_held=1, max_bars=3) == (None, '')

    def test_silent_after_max_bars(self):
        b = bar(101.0, 101.2, 99.0, 100.0)
        assert early_abort_exit(b, Side.BUY, 100.0, risk=2.0, bars_held=3, max_bars=3) == (None, '')

    def test_active_on_last_window_bar(self):
        b = bar(101.0, 101.2, 99.0, 100.0)
        assert early_abort_exit(b, Side.BUY, 100.0, risk=2.0, bars_held=2, max_bars=3)[0] == 99.0

    def test_short_abort(self):
        b = bar(99.0, 101.0, 98.5, 100.2)  # level = 101.0
        assert early_abort_exit(b, Side.SELL, 100.0, risk=2.0, bars_held=0, max_bars=3) == (101.0, 'early_abort')

    def test_zero_risk_silent(self):
        b = bar(99.0, 101.0, 98.5, 100.0)
        assert early_abort_exit(b, Side.BUY, 100.0, risk=0.0, bars_held=0, max_bars=3) == (None, '')


from app.engine.exits import partial_take_exit  # noqa: E402
from app.engine.models import Side as _Side  # noqa: E402,F811


class TestPartialTakeExit:
    """Юнит-тесты partial_take_exit (шаг 5, 2026-09-26): R-уровень,
    touch-семантика, гэп открытия за уровнем — заполнение по open."""

    def test_long_touch(self):
        b = bar(100.0, 101.2, 99.9, 101.0)
        assert partial_take_exit(b, _Side.BUY, 100.0, risk=1.0, partial_r=1.0) == 101.0

    def test_long_gap_open(self):
        b = bar(101.6, 101.8, 100.9, 101.4)
        assert partial_take_exit(b, _Side.BUY, 100.0, risk=1.0, partial_r=1.0) == 101.6

    def test_long_no_touch(self):
        b = bar(100.0, 100.9, 99.9, 100.5)
        assert partial_take_exit(b, _Side.BUY, 100.0, risk=1.0, partial_r=1.0) is None

    def test_short_touch(self):
        b = bar(100.0, 100.1, 98.9, 99.2)
        assert partial_take_exit(b, _Side.SELL, 100.0, risk=1.0, partial_r=1.0) == 99.0

    def test_disabled_when_partial_r_zero(self):
        b = bar(100.0, 102.0, 98.0, 101.0)
        assert partial_take_exit(b, _Side.BUY, 100.0, risk=1.0, partial_r=0.0) is None
