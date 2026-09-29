"""Тесты метрик прогона OsEngine-портов (app/engine/ose/metrics.py).

Метрики в пунктах: сделки — FIFO (close-fill ↔ старейшая открытая
позиция), эквити — mark-to-market на close бара, max_dd — по эквити на
закрытиях (первый бар задаёт пик), in_market — «есть открытая позиция на
закрытии бара» (внутрибарный выход делает бар «вне рынка»). Числа в
сценариях посчитаны руками; интеграция — реальный порт PriceChannelTrade
на серии из test_ose_robots.py (там сигнальная логика уже проверена:
fill'ы open_long@110 → close@95 → open_short@95).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import pytest

from app.engine.models import Candle
from app.engine.ose.metrics import MetricsTracker, run_robot_with_metrics
from app.engine.ose.robots import PriceChannelTrade, TesterTab

_T0 = datetime(2026, 2, 2, 10, 0)


def _candle(offset_min: int, close: float, *, high=None, low=None) -> Candle:
    return Candle(
        ts=_T0 + timedelta(minutes=offset_min),
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=100.0,
    )


# --- пустой прогон -----------------------------------------------------------


def test_empty_run_flat_market():
    tab = TesterTab()
    tr = MetricsTracker(tab)
    for i in range(5):
        tr.on_bar(_candle(i, 100.0))
    r = tr.report()
    assert r["bars"] == 5
    assert r["trades"] == 0 and r["wins"] == 0 and r["losses"] == 0
    assert r["win_pct"] == 0.0
    assert r["profit_factor"] == 0.0  # прибылей нет — не inf
    assert r["realized"] == 0.0 and r["unrealized"] == 0.0
    assert r["pnl"] == 0.0 and r["avg_trade"] == 0.0
    assert r["max_dd"] == 0.0
    assert r["in_market_pct"] == 0.0
    assert r["buy_hold"] == 0.0
    assert r["equity_curve"] == [0.0] * 5


# --- одна сделка: лонг +10 ----------------------------------------------------


def test_single_winning_long_trade():
    tab = TesterTab()
    tr = MetricsTracker(tab)
    tr.on_bar(_candle(0, 100.0))                 # флэт
    tab.buy_at_limit(1.0, 100.0)
    tr.on_bar(_candle(1, 105.0))                 # в позиции: эквити 5
    pos = tab.positions[0]
    tab.close_at_limit(pos, 110.0, pos.volume)
    tr.on_bar(_candle(2, 110.0))                 # реализовано +10
    r = tr.report()
    assert r["trades"] == 1 and r["wins"] == 1
    assert r["win_pct"] == 100.0
    assert math.isinf(r["profit_factor"])        # лоссов нет
    assert r["realized"] == pytest.approx(10.0)
    assert r["unrealized"] == 0.0
    assert r["pnl"] == pytest.approx(10.0)
    assert r["avg_trade"] == pytest.approx(10.0)
    # в рынке только бар 1 (закрытие до close бара 2)
    assert r["in_market_pct"] == pytest.approx(100.0 / 3)
    assert r["buy_hold"] == pytest.approx(10.0)


# --- три сделки: PF, win%, средний трейд --------------------------------------


def test_two_wins_one_loss_pf_winrate_avg():
    tab = TesterTab()
    tr = MetricsTracker(tab)
    tr.on_bar(_candle(0, 100.0))                 # флэт
    tab.buy_at_limit(1.0, 100.0)
    tr.on_bar(_candle(1, 101.0))                 # лонг 100
    p1 = tab.positions[0]
    tab.close_at_limit(p1, 110.0, p1.volume)
    tr.on_bar(_candle(2, 110.0))                 # +10
    tab.sell_at_limit(1.0, 110.0)
    tr.on_bar(_candle(3, 109.0))                 # шорт 110
    p2 = tab.positions[1]
    tab.close_at_limit(p2, 106.0, p2.volume)
    tr.on_bar(_candle(4, 106.0))                 # +4
    tab.buy_at_limit(1.0, 106.0)
    tr.on_bar(_candle(5, 105.0))                 # лонг 106
    p3 = tab.positions[2]
    tab.close_at_limit(p3, 102.0, p3.volume)
    tr.on_bar(_candle(6, 102.0))                 # -4
    r = tr.report()
    assert r["trades"] == 3 and r["wins"] == 2 and r["losses"] == 1
    assert r["win_pct"] == pytest.approx(200.0 / 3)
    assert r["profit_factor"] == pytest.approx(14.0 / 4.0)
    assert r["realized"] == pytest.approx(10.0)
    assert r["unrealized"] == 0.0
    assert r["pnl"] == pytest.approx(10.0)
    assert r["avg_trade"] == pytest.approx(10.0 / 3)
    # в рынке бары 1, 3, 5 (открытие до close бара); бары закрытия — вне
    assert r["in_market_pct"] == pytest.approx(300.0 / 7)
    assert r["buy_hold"] == pytest.approx(2.0)   # 102 - 100
    # эквити: [0, 1, 10, 11, 14, 13, 10] → dd = 14 - 10 = 4 (пик 14 на баре 4)
    assert r["max_dd"] == pytest.approx(4.0)
    assert len(r["equity_curve"]) == 7


# --- просадка по mark-to-market эквити ----------------------------------------


def test_max_drawdown_mark_to_market():
    tab = TesterTab()
    tr = MetricsTracker(tab)
    tr.on_bar(_candle(0, 100.0))
    tab.buy_at_limit(1.0, 100.0)
    tr.on_bar(_candle(1, 105.0))                 # eq 5, peak 5
    tr.on_bar(_candle(2, 95.0))                  # eq -5, dd 10
    tr.on_bar(_candle(3, 115.0))                 # eq 15, peak 15
    tr.on_bar(_candle(4, 110.0))                 # eq 10, dd остаётся 10
    r = tr.report()
    assert r["max_dd"] == pytest.approx(10.0)
    assert r["unrealized"] == pytest.approx(10.0)
    assert r["realized"] == 0.0
    assert r["pnl"] == pytest.approx(10.0)       # pnl = realized + unrealized
    assert r["trades"] == 0                      # позиция ещё открыта
    assert r["in_market_pct"] == pytest.approx(80.0)  # бары 1..4 из 5


def test_max_dd_recomputes_from_equity_curve():
    # инвариант: max_dd совпадает с пересчётом peak-to-trough по кривой
    tab = TesterTab()
    tr = MetricsTracker(tab)
    tr.on_bar(_candle(0, 100.0))
    tab.buy_at_limit(1.0, 100.0)
    for i, close in enumerate([103.0, 97.0, 99.0, 92.0, 104.0], start=1):
        tr.on_bar(_candle(i, close))
    r = tr.report()
    curve = r["equity_curve"]
    assert len(curve) == r["bars"]
    peak = curve[0]
    dd = 0.0
    for v in curve[1:]:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    assert r["max_dd"] == pytest.approx(dd)


# --- выход стоп-слотом внутри бара --------------------------------------------


def test_stop_slot_exit_recorded_as_trade():
    tab = TesterTab()
    tr = MetricsTracker(tab)
    tr.on_bar(_candle(0, 100.0))
    tab.buy_at_limit(1.0, 100.0)
    pos = tab.positions[0]
    tab.close_at_stop_market(pos, 95.0, 94.9)    # стоп-слот: активация 95
    c = _candle(1, 96.0, high=97.0, low=94.0)    # лоу 94 ≤ 95 — стоп сработал
    tab.process_intrabar(c)                      # исполнение по order_price
    tr.on_bar(c)
    r = tr.report()
    assert r["trades"] == 1
    assert r["realized"] == pytest.approx(-5.1)  # 94.9 - 100
    assert r["pnl"] == pytest.approx(-5.1)
    assert r["max_dd"] == pytest.approx(5.1)
    assert r["in_market_pct"] == 0.0             # выход внутри бара → бар вне рынка


# --- интеграция: реальный порт на серии с известными fill'ами ------------------


def test_price_channel_run_metrics_match_hand_count():
    # серия из test_Price_channel_closes_and_reverses: вход 110 (лонг),
    # пробой вниз → close 95 и реверс в шорт 95 (открыт на конце)
    closes = [100.0] * 45 + [110.0, 95.0]
    highs = [100.0] * 45 + [112.0, 99.0]
    lows = [100.0] * 45 + [109.0, 94.0]
    candles = [_candle(i, closes[i], high=highs[i], low=lows[i]) for i in range(47)]
    robot = PriceChannelTrade(TesterTab())
    r = run_robot_with_metrics(robot, candles)
    assert r["bars"] == 47
    assert r["trades"] == 1                      # лонг закрыт, шорт открыт
    assert r["realized"] == pytest.approx(-15.0)  # 95 - 110
    assert r["unrealized"] == pytest.approx(0.0)  # шорт 95 при close 95
    assert r["pnl"] == pytest.approx(-15.0)
    assert r["win_pct"] == 0.0
    assert r["profit_factor"] == 0.0             # gross_win = 0
    assert r["avg_trade"] == pytest.approx(-15.0)
    assert r["max_dd"] == pytest.approx(15.0)     # peak 0 → eq -15
    assert r["buy_hold"] == pytest.approx(-5.0)   # 95 - 100
    # в рынке бары входа (лонг) и реверса (шорт)
    assert r["in_market_pct"] == pytest.approx(100.0 * 2 / 47)
    assert len(r["equity_curve"]) == 47
