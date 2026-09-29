"""Тесты OsEngine-портов роботов (app/engine/ose/robots.py).

Инварианты сняты с поведения ~/OsEngine → Robots/**/*.cs: порядок
«закрытие → открытие» внутри CandleFinishedEvent, чтение индикаторов
(сдвиг [-2] у PriceChannelTrade, пара K[-2]/K[-1] у SmaStochastic),
режимы BotTradeRegime, реверс PriceChannelTrade при < 3 позиций, гейт
«час ≤ 18» у StrategyBollinger, стоп-заявки и трейлинг EnvelopTrend.
Индикаторная математика покрыта test_ose_indicators.py и здесь
используется как данность; каркас исполнения — TesterTab (bar-replay):
лимитные заявки исполняются сразу, стоп-заявки и трейлинг — по касанию
диапазоном следующего бара (feed() вызывает process_intrabar перед
событием, как тестер OsEngine перед CandleFinishedEvent).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.engine.models import Candle
from app.engine.ose.robots import (
    EnvelopTrend,
    PriceChannelTrade,
    Regime,
    RsiContrtrend,
    Side,
    SmaStochastic,
    StrategyBollinger,
    TesterTab,
)

_T0 = datetime(2026, 1, 5, 10, 0)


def make_candles(closes, *, high=None, low=None, start=None):
    start = start or _T0
    candles = []
    for i, close in enumerate(closes):
        h = close if high is None else high(i)
        low_value = close if low is None else low(i)
        candles.append(
            Candle(
                ts=start + timedelta(minutes=i),
                open=close,
                high=h,
                low=low_value,
                close=close,
                volume=100.0,
            )
        )
    return candles


def feed(robot, candles):
    """Прогоняет робота по префиксам ряда, как тестер: перед каждым
    CandleFinishedEvent каркас обрабатывает отложенные заявки диапазоном
    только что закрывшегося бара."""
    tab = robot.tab
    for i in range(2, len(candles) + 1):
        prefix = candles[:i]
        tab.process_intrabar(prefix[-1])
        robot.on_candle_finished(prefix)


def assert_fills(tab, expected):
    assert len(tab.fills) == len(expected)
    for (action, price), (exp_action, exp_price) in zip(tab.fills, expected):
        assert action == exp_action
        assert price == pytest.approx(exp_price)


# --- PriceChannelTrade -------------------------------------------------------


def test_price_channel_breakout_opens_long_with_slippage():
    candles = make_candles(
        [100.0] * 45 + [110.0],
        high=lambda i: 100.0 if i < 45 else 112.0,
        low=lambda i: 100.0 if i < 45 else 109.0,
    )
    tab = TesterTab()
    robot = PriceChannelTrade(tab, slippage=0.5)
    feed(robot, candles[:45])
    assert tab.fills == []
    robot.on_candle_finished(candles)
    assert len(tab.positions_open_all) == 1
    pos = tab.positions_open_all[0]
    assert pos.side is Side.BUY
    assert pos.entry_price == pytest.approx(110.5)  # close + slippage


def test_price_channel_ambiguous_bar_skips_entry():
    # Бар пробил обе стороны канала — входа нет (гейт оригинала)
    candles = make_candles(
        [100.0] * 45 + [110.0],
        high=lambda i: 100.0 if i < 45 else 112.0,
        low=lambda i: 100.0 if i < 45 else 95.0,
    )
    tab = TesterTab()
    robot = PriceChannelTrade(tab)
    feed(robot, candles)
    assert tab.fills == []
    assert tab.positions_open_all == []


def test_price_channel_closes_and_reverses_on_opposite_break():
    candles = make_candles(
        [100.0] * 45 + [110.0, 95.0],
        high=lambda i: 100.0 if i < 45 else (112.0 if i == 45 else 99.0),
        low=lambda i: 100.0 if i < 45 else (109.0 if i == 45 else 94.0),
    )
    tab = TesterTab()
    robot = PriceChannelTrade(tab)
    feed(robot, candles)
    # Лонг открыт по пробою вверх, закрыт по пробою вниз, реверс в шорт
    assert_fills(tab, [("open_long", 110.0), ("close", 95.0), ("open_short", 95.0)])
    open_positions = tab.positions_open_all
    assert len(open_positions) == 1
    assert open_positions[0].side is Side.SELL


def test_price_channel_only_long_blocks_short_entry():
    candles = make_candles(
        [100.0] * 45 + [90.0],
        high=lambda i: 100.0 if i < 45 else 91.0,
        low=lambda i: 100.0 if i < 45 else 88.0,
    )
    tab = TesterTab()
    robot = PriceChannelTrade(tab, regime=Regime.ONLY_LONG)
    feed(robot, candles)
    assert tab.fills == []


def test_price_channel_regime_off_is_noop():
    candles = make_candles(
        [100.0] * 45 + [110.0],
        high=lambda i: 100.0 if i < 45 else 112.0,
        low=lambda i: 100.0 if i < 45 else 109.0,
    )
    tab = TesterTab()
    robot = PriceChannelTrade(tab, regime=Regime.OFF)
    feed(robot, candles)
    assert tab.fills == []
    assert tab.positions_open_all == []


# --- SmaStochastic -----------------------------------------------------------


def test_sma_stochastic_enters_long_on_k_cross_up_and_exits_below_sma():
    # Флэт 100, всплеск до 300 (K: 0 -> 100, close > SMA), возврат к 100
    candles = make_candles([100.0] * 25 + [300.0, 100.0])
    tab = TesterTab()
    # upline=101 отключает шорт (K не превышает 100); step=0 — тренд-фильтр
    robot = SmaStochastic(tab, step=0.0, downline=30.0, upline=101.0)
    feed(robot, candles)
    assert_fills(tab, [("open_long", 300.0), ("close", 100.0)])
    assert tab.positions_open_all == []


def test_sma_stochastic_enters_short_on_k_cross_down():
    # Рост 100->200, затем спад: K пересекает 70 сверху вниз при close < SMA5
    candles = make_candles(
        [100.0] * 20
        + [110.0, 120.0, 130.0, 140.0, 150.0, 160.0, 170.0, 180.0, 190.0, 200.0]
        + [190.0, 180.0]
    )
    tab = TesterTab()
    # downline=-1 отключает лонг (K не бывает < 0); sma_length=5 — быстрый фильтр
    robot = SmaStochastic(tab, sma_length=5, step=0.0, upline=70.0, downline=-1.0)
    feed(robot, candles)
    assert_fills(tab, [("open_short", 180.0)])


# --- RsiContrtrend -----------------------------------------------------------


def test_rsi_contrtrend_opens_short_above_upline_and_closes_on_reversal():
    # Флэт (RSI-квирк: флэт/чистый тренд = 100), падение, откат:
    # шорт при sma > close и RSI 100 > 65; закрытие при RSI < 35
    candles = make_candles(
        [100.0] * 53 + [90.0, 80.0, 70.0, 80.0, 90.0, 100.0, 110.0]
    )
    tab = TesterTab()
    robot = RsiContrtrend(tab)  # дефолты: sma 50, rsi 20, upline 65, downline 35
    feed(robot, candles)
    assert_fills(tab, [("open_short", 90.0), ("close", 80.0)])
    assert tab.positions_open_all == []


def test_rsi_contrtrend_opens_long_below_downline():
    # Квирк RSI оригинала: флэт даёт 100 (avgHigh==0 → 100), поэтому на
    # первом баре падения close 190 под SMA(5) 198 при RSI 100 > 65
    # открывается шорт — так же ведёт себя C#-оригинал; +2 отскок роняет
    # RSI до 20 < 35 — шорт закрыт. Дальше чередование (-10/+2) держит
    # RSI(5) < 35, финальный отскок +4 поднимает close над SMA(5) — бай;
    # выход при close < SMA.
    candles = make_candles(
        [200.0] * 30
        + [190.0, 192.0, 182.0, 184.0, 174.0, 176.0, 166.0, 168.0,
           158.0, 160.0, 150.0, 152.0, 142.0, 144.0, 148.0, 140.0]
    )
    tab = TesterTab()
    robot = RsiContrtrend(tab, sma_length=5, rsi_length=5)
    feed(robot, candles)
    assert_fills(
        tab,
        [
            ("open_short", 190.0),
            ("close", 192.0),
            ("open_long", 148.0),
            ("close", 140.0),
        ],
    )
    assert tab.positions_open_all == []


def test_rsi_contrtrend_only_long_blocks_short():
    candles = make_candles(
        [100.0] * 53 + [90.0, 80.0, 70.0, 80.0, 90.0, 100.0, 110.0]
    )
    tab = TesterTab()
    robot = RsiContrtrend(tab, regime=Regime.ONLY_LONG)
    feed(robot, candles)
    assert tab.fills == []


# --- StrategyBollinger -------------------------------------------------------


def test_strategy_bollinger_short_on_upper_band_then_long_on_lower():
    # Флэт 100, всплеск 130 (close > верхняя полоса) -> шорт;
    # обвал 60 (close < нижняя полоса): шорт закрывается по SMA, открывается лонг;
    # откат к 100 (close > SMA) -> лонг закрывается
    candles = make_candles([100.0] * 30 + [130.0, 60.0, 100.0])
    tab = TesterTab()
    robot = StrategyBollinger(tab)
    feed(robot, candles)
    assert_fills(
        tab,
        [("open_short", 130.0), ("close", 60.0), ("open_long", 60.0), ("close", 100.0)],
    )
    assert tab.positions_open_all == []


def test_strategy_bollinger_skips_closing_after_18h():
    # Час закрытия свечи 19 > 18 — закрытие блокируется, шорт остаётся открытым
    candles = make_candles(
        [100.0] * 30 + [130.0, 60.0],
        start=datetime(2026, 1, 5, 19, 0),
    )
    tab = TesterTab()
    robot = StrategyBollinger(tab)
    feed(robot, candles)
    assert_fills(tab, [("open_short", 130.0)])
    open_positions = tab.positions_open_all
    assert len(open_positions) == 1
    assert open_positions[0].side is Side.SELL


def test_strategy_bollinger_only_long_blocks_short():
    candles = make_candles([100.0] * 30 + [130.0])
    tab = TesterTab()
    robot = StrategyBollinger(tab, regime=Regime.ONLY_LONG)
    feed(robot, candles)
    assert tab.fills == []


# --- EnvelopTrend ------------------------------------------------------------


def test_envelop_trend_arms_entry_stops_while_flat():
    # Флэт 100: полосы 100.3/99.7 (len 10, dev 0.3%); стопы перевыставляются
    candles = make_candles([100.0] * 20)
    tab = TesterTab()
    robot = EnvelopTrend(tab)
    feed(robot, candles)
    armed = [(o.side, o.activation_price, o.order_price) for o in tab.pending_stops]
    assert len(armed) == 2
    assert armed[0][0] is Side.BUY
    assert armed[0][1] == pytest.approx(100.3)
    assert armed[0][2] == pytest.approx(100.3)
    assert armed[1][0] is Side.SELL
    assert armed[1][1] == pytest.approx(99.7)
    assert armed[1][2] == pytest.approx(99.7)
    assert tab.fills == []


def test_envelop_trend_buy_stop_fills_and_trailing_stop_closes():
    # Бар 20 пробивает верхнюю полосу (high 101 >= 100.3) — лонг по 100.3.
    # Трейлинг перевыставляется каждым баром (как в оригинале), поэтому к
    # бару 21 активация считается от полос бара 20: SMA(10) = 100.1,
    # up = 100.1*1.003 = 100.4003 → активация 100.4003*(1-0.1%);
    # бар 21 low 100 задевает
    candles = make_candles(
        [100.0] * 20 + [101.0, 100.0],
        high=lambda i: 100.0 if i < 20 else (101.0 if i == 20 else 100.5),
        low=lambda i: 100.0 if i < 20 else (100.5 if i == 20 else 100.0),
    )
    tab = TesterTab()
    robot = EnvelopTrend(tab)
    feed(robot, candles)
    assert_fills(tab, [("open_long", 100.3), ("close", 100.1 * 1.003 * 0.999)])
    assert tab.positions_open_all == []
    closed = tab.positions[-1]
    assert closed.stop is None  # OCO: закрытие снимает стоп-слот
    assert closed.profit is None
    assert len(tab.pending_stops) == 2  # после закрытия стопы перевыставлены


def test_envelop_trend_sell_stop_fills_and_trailing_stop_closes():
    # Бар 20 пробивает нижнюю полосу (low 99 <= 99.7) — шорт по 99.7.
    # Трейлинг перевыставляется каждым баром: к бару 21 активация считается
    # от полос бара 20: SMA(10) = 99.9, down = 99.9*0.997 = 99.6003
    # → активация 99.6003*(1+0.1%); бар 21 high 100 задевает
    candles = make_candles(
        [100.0] * 20 + [99.0, 100.0],
        high=lambda i: 100.0 if i < 20 else (99.5 if i == 20 else 100.0),
        low=lambda i: 100.0 if i < 20 else (99.0 if i == 20 else 100.0),
    )
    tab = TesterTab()
    robot = EnvelopTrend(tab)
    feed(robot, candles)
    assert_fills(tab, [("open_short", 99.7), ("close", 99.9 * 0.997 * 1.001)])
    assert tab.positions_open_all == []


def test_envelop_trend_regime_off_is_noop():
    candles = make_candles([100.0] * 20 + [101.0])
    tab = TesterTab()
    robot = EnvelopTrend(tab, regime=Regime.OFF)
    feed(robot, candles)
    assert tab.pending_stops == []
    assert tab.fills == []


# --- Каркас исполнения -------------------------------------------------------


def test_tester_tab_close_at_limit_ignores_closed_position():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 100.0)
    pos = tab.positions[-1]
    tab.close_at_limit(pos, 99.0, pos.volume)
    assert tab.positions_open_all == []
    tab.close_at_limit(pos, 98.0, pos.volume)  # повторное закрытие — игнор
    assert len(tab.fills) == 2
    assert [action for action, _ in tab.fills] == ["open_long", "close"]


# --- Слоты выхода позиции (стоп/тейк, OCO, TryReload) ------------------------


def _candle(ts, o, h, l, c):
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=100.0)


def test_exit_slot_stop_hits_and_clears_profit_oco():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 100.0, 99.9)
    tab.close_at_profit_market(pos, 120.0, 119.9)
    # Бар задел обе активации (low 99.5 <= 100, high 121 >= 120):
    # приоритет у стопа (CheckStop раньше CheckProfit), закрытие по 99.9
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 101.0, 121.0, 99.5, 110.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(99.9)
    assert pos.stop is None  # OCO: исполнение стопа сняло тейк
    assert pos.profit is None


def test_exit_slot_reload_overrides_prices():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 100.0, 99.9)
    tab.close_at_stop_market(pos, 100.0, 99.9)  # та же пара — TryReload no-op
    tab.close_at_stop_market(pos, 105.0, 104.9)  # новая пара — перезапись слота
    assert pos.stop == (105.0, 104.9)
    # low 104 не задел бы старую активацию 100, но задел новую 105
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 106.0, 107.0, 104.0, 105.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(104.9)


def test_exit_slot_short_stop_hits_on_high():
    tab = TesterTab()
    tab.sell_at_limit(1.0, 110.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 105.0, 105.1)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 104.0, 105.5, 103.0, 104.0))
    assert [a for a, _ in tab.fills] == ["open_short", "close"]
    assert tab.fills[-1][1] == pytest.approx(105.1)


def test_exit_slot_profit_hits_without_stop_touch():
    tab = TesterTab()
    tab.sell_at_limit(1.0, 110.0)
    pos = tab.positions[-1]
    tab.close_at_profit_market(pos, 100.0, 100.1)
    # low 99.5 <= активация 100.0 — тейк шорта исполнен, стоп-слота нет
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 105.0, 105.0, 99.5, 101.0))
    assert [a for a, _ in tab.fills] == ["open_short", "close"]
    assert tab.fills[-1][1] == pytest.approx(100.1)
    assert pos.stop is None and pos.profit is None  # OCO симметричен


def test_exit_slot_long_profit_hits_on_high():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_profit_market(pos, 120.0, 119.9)
    # high 120.5 >= активация 120.0 — тейк лонга исполнен по order_price
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 115.0, 120.5, 114.0, 119.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(119.9)
    assert pos.stop is None and pos.profit is None


def test_exit_slot_gap_through_stop_fills_at_order_price():
    tab = TesterTab()
    tab.sell_at_limit(1.0, 110.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 105.0, 105.1)
    # Гэп вверх сквозь активацию (open 106 > 105): исполнение — маркет,
    # по order_price 105.1, а не по open/activation
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 106.0, 106.5, 105.2, 106.0))
    assert [a for a, _ in tab.fills] == ["open_short", "close"]
    assert tab.fills[-1][1] == pytest.approx(105.1)


def test_exit_slot_trailing_migrates_stop_each_bar():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    # Робот перезаряжает трейлинг каждым баром: активация ползёт вверх
    tab.close_at_trailing_stop(pos, 95.0, 94.9)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 96.0, 97.0, 95.5, 96.5))
    assert [a for a, _ in tab.fills] == ["open_long"]  # low 95.5 не задел 95.0
    tab.close_at_trailing_stop(pos, 98.0, 97.9)  # перезарядка новым баром
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=10), 98.5, 99.0, 97.5, 98.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(97.9)


def test_exit_slot_entry_stops_wait_while_position_open():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    tab.buy_at_stop(1.0, 100.0, activation_price=100.0)
    # Позиция открыта — стоп на вход не рассматривается, даже если бар его задел
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 95.0, 101.0, 94.0, 100.0))
    assert [a for a, _ in tab.fills] == ["open_long"]
    assert len(tab.pending_stops) == 1
    tab.close_at_limit(tab.positions[-1], 99.0, 1.0)
    # Позиций нет — стоп на вход срабатывает на следующем баре
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=10), 100.0, 101.0, 99.5, 100.5))
    assert [a for a, _ in tab.fills] == ["open_long", "close", "open_long"]


def test_exit_slot_entry_stop_fires_callback_with_position():
    seen: list = []
    tab = TesterTab()
    tab.on_position_opened = seen.append  # аналог PositionOpeningSuccesEvent
    tab.buy_at_stop(1.0, 100.0, activation_price=99.0)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 98.0, 99.5, 97.0, 99.0))
    assert len(seen) == 1
    assert seen[0] is tab.positions[-1]
    assert seen[0].side is Side.BUY
    assert seen[0].entry_price == pytest.approx(100.0)
