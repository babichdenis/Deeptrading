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
    RsiTrade,
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


# --- RsiTrade ----------------------------------------------------------------


def test_rsi_trade_crossover_short_then_reversal_cycle():
    # Флэт даёт RSI 100 (квирк оригинала); первое падение — кросс Upline
    # сверху вниз (100 → 20): шорт. Разгон вверх — кросс Upline вниз
    # (87.58 → 63.38): закрытие и реверс в лонг той же свечой; откат —
    # кросс Downline вверх (24.85 → 61.4): реверс в шорт; финальный
    # отскок — снова лонг. Реверс выполняет LogicClosePosition, вход на
    # баре закрытия позиции не дублируется (гейт по снимку позиций).
    candles = make_candles(
        [200.0] * 30
        + [190.0, 192.0, 186.0, 188.0, 198.0, 208.0, 214.0, 220.0, 224.0,
           216.0, 208.0, 200.0, 194.0, 188.0, 194.0, 202.0, 210.0]
    )
    tab = TesterTab()
    robot = RsiTrade(tab, rsi_length=5)
    feed(robot, candles)
    assert_fills(
        tab,
        [
            ("open_short", 192.0),
            ("close", 198.0),
            ("open_long", 198.0),
            ("close", 216.0),
            ("open_short", 216.0),
            ("close", 194.0),
            ("open_long", 194.0),
        ],
    )
    assert tab.positions_open_all[-1].side is Side.BUY


def test_rsi_trade_short_stays_open_without_downline_upcross():
    # Скачок вверх с флэта (100 → 80 → 64) — кросс Upline вниз, шорт по 206.
    # Монотонное падение держит RSI ниже Upline, но кросса Downline вверх
    # (second <= 35 и first >= 35) нет — шорт остаётся открытым.
    candles = make_candles(
        [200.0] * 30 + [210.0, 208.0, 206.0, 204.0, 202.0, 200.0, 198.0, 196.0]
    )
    tab = TesterTab()
    robot = RsiTrade(tab, rsi_length=5)
    feed(robot, candles)
    assert_fills(tab, [("open_short", 206.0)])
    assert tab.positions_open_all[0].side is Side.SELL


def test_rsi_trade_only_long_blocks_short_entry():
    candles = make_candles(
        [200.0] * 30 + [210.0, 208.0, 206.0, 204.0, 202.0, 200.0, 198.0, 196.0]
    )
    tab = TesterTab()
    robot = RsiTrade(tab, rsi_length=5, regime=Regime.ONLY_LONG)
    feed(robot, candles)
    assert tab.fills == []


def test_rsi_trade_only_close_position_closes_without_reversal():
    # Режим переключается на лету, как смена BotTradeRegime в OsEngine:
    # открытый шорт закрывается обратным кроссом Downline, реверса нет.
    closes = [200.0] * 30 + [190.0, 192.0, 186.0, 188.0, 198.0, 208.0, 214.0]
    candles = make_candles(closes)
    tab = TesterTab()
    robot = RsiTrade(tab, rsi_length=5)
    feed(robot, candles[:33])  # шорт по 192.0 (кросс Upline вниз)
    assert_fills(tab, [("open_short", 192.0)])
    robot.regime = Regime.ONLY_CLOSE_POSITION
    for i in range(34, len(candles) + 1):
        tab.process_intrabar(candles[i - 1])
        robot.on_candle_finished(candles[:i])
    assert_fills(tab, [("open_short", 192.0), ("close", 198.0)])
    assert tab.positions_open_all == []


def test_rsi_trade_slippage_shifts_entry_exit_and_reversal():
    candles = make_candles([200.0] * 30 + [190.0, 192.0, 186.0, 188.0, 198.0])
    tab = TesterTab()
    robot = RsiTrade(tab, rsi_length=5, slippage=0.5)
    feed(robot, candles)
    # Вход шорт по close - slippage; закрытие и реверс-лонг по close + slippage
    assert_fills(
        tab,
        [("open_short", 191.5), ("close", 198.5), ("open_long", 198.5)],
    )
    assert tab.positions_open_all[-1].side is Side.BUY


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
    # приоритет у стопа (CheckStop раньше CheckProfit), филл по activation
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 101.0, 121.0, 99.5, 110.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(100.0)
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
    # low 104 не задел бы старую активацию 100, но задел новую 105;
    # филл — по activation (тестер OsEngine игнорирует order_price)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 106.0, 107.0, 104.0, 105.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(105.0)


def test_exit_slot_short_stop_hits_on_high():
    tab = TesterTab()
    tab.sell_at_limit(1.0, 110.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 105.0, 105.1)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 104.0, 105.5, 103.0, 104.0))
    assert [a for a, _ in tab.fills] == ["open_short", "close"]
    assert tab.fills[-1][1] == pytest.approx(105.0)


def test_exit_slot_profit_hits_without_stop_touch():
    tab = TesterTab()
    tab.sell_at_limit(1.0, 110.0)
    pos = tab.positions[-1]
    tab.close_at_profit_market(pos, 100.0, 100.1)
    # low 99.5 <= активация 100.0 — тейк шорта исполнен по activation
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 105.0, 105.0, 99.5, 101.0))
    assert [a for a, _ in tab.fills] == ["open_short", "close"]
    assert tab.fills[-1][1] == pytest.approx(100.0)
    assert pos.stop is None and pos.profit is None  # OCO симметричен


def test_exit_slot_long_profit_hits_on_high():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_profit_market(pos, 120.0, 119.9)
    # high 120.5 >= активация 120.0 — тейк лонга исполнен по activation
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 115.0, 120.5, 114.0, 119.0))
    assert [a for a, _ in tab.fills] == ["open_long", "close"]
    assert tab.fills[-1][1] == pytest.approx(120.0)
    assert pos.stop is None and pos.profit is None


def test_exit_slot_gap_through_stop_fills_at_activation():
    tab = TesterTab()
    tab.sell_at_limit(1.0, 110.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 105.0, 105.1)
    # Гэп вверх сквозь активацию (open 106 > 105): тестер OsEngine
    # исполняет стоп-слот по цене активации (priceOrder := priceActivate
    # в TryReloadStop), а не по open и не по order_price.
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 106.0, 106.5, 105.2, 106.0))
    assert [a for a, _ in tab.fills] == ["open_short", "close"]
    assert tab.fills[-1][1] == pytest.approx(105.0)


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
    assert tab.fills[-1][1] == pytest.approx(98.0)


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
    # Вход стоп-заявкой филится по цене активации (в тестере OsEngine
    # BuyAtStop ставит PriceOrder = priceRedLine), а не по priceLimit.
    assert seen[0].entry_price == pytest.approx(99.0)


def test_exit_slot_entry_stop_fills_at_activation():
    # В тестере OsEngine вход стоп-заявкой исполняется по priceRedLine
    # (BuyAtStop: PriceOrder = priceRedLine для не-OsTrader), не по priceLimit.
    tab = TesterTab()
    tab.buy_at_stop(1.0, 100.5, activation_price=100.0)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 99.5, 100.2, 99.0, 100.2))
    assert tab.fills == [("open_long", 100.0)]


def test_exit_slot_trailing_ratchet_keeps_better_stop():
    # Long: стоп двигается только вверх (CloseAtTrailingStop guard:
    # RedLine > activation → старый уровень сохраняется).
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_trailing_stop(pos, 95.0, 94.9)
    tab.close_at_trailing_stop(pos, 94.0, 93.9)  # назад нельзя — ratchet
    assert pos.stop == (95.0, 94.9)
    tab.close_at_trailing_stop(pos, 98.0, 97.9)  # вверх можно
    assert pos.stop == (98.0, 97.9)

    # Short: стоп двигается только вниз.
    tab2 = TesterTab()
    tab2.sell_at_limit(1.0, 110.0)
    pos2 = tab2.positions[-1]
    tab2.close_at_trailing_stop(pos2, 105.0, 105.1)
    tab2.close_at_trailing_stop(pos2, 106.0, 106.1)  # назад нельзя
    assert pos2.stop == (105.0, 105.1)
    tab2.close_at_trailing_stop(pos2, 103.0, 103.1)  # вниз можно
    assert pos2.stop == (103.0, 103.1)


def test_exit_slot_exit_reasons_tagged():
    tab = TesterTab()

    tab.buy_at_limit(1.0, 90.0)
    stop_pos = tab.positions[-1]
    tab.close_at_stop_market(stop_pos, 100.0, 99.9)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 101.0, 101.0, 99.5, 100.0))
    assert stop_pos.exit_reason == "stop_close"

    tab.buy_at_limit(1.0, 90.0)
    profit_pos = tab.positions[-1]
    tab.close_at_profit_market(profit_pos, 120.0, 119.9)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=10), 115.0, 121.0, 114.0, 120.0))
    assert profit_pos.exit_reason == "profit_close"

    tab.buy_at_limit(1.0, 90.0)
    trail_pos = tab.positions[-1]
    tab.close_at_trailing_stop(trail_pos, 95.0, 94.9)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=11), 96.0, 96.0, 94.5, 95.0))
    assert trail_pos.exit_reason == "trail_close"

    tab.buy_at_limit(1.0, 90.0)
    signal_pos = tab.positions[-1]
    tab.close_at_limit(signal_pos, 91.0, signal_pos.volume)
    assert signal_pos.exit_reason == "close"


def test_tester_tab_market_entries_open_at_price():
    tab = TesterTab()
    tab.buy_at_market(1.0, 100.0)
    tab.sell_at_market(1.0, 110.0)
    assert [a for a, _ in tab.fills] == ["open_long", "open_short"]
    assert tab.fills[0][1] == pytest.approx(100.0)
    assert tab.fills[1][1] == pytest.approx(110.0)
    assert [p.side for p in tab.positions_open_all] == [Side.BUY, Side.SELL]


def test_cancel_stop_orders_by_side():
    tab = TesterTab()
    tab.buy_at_stop(1.0, 100.0, activation_price=100.0)
    tab.sell_at_stop(1.0, 90.0, activation_price=90.0)
    tab.cancel_stop_orders(Side.BUY)
    assert [o.side for o in tab.pending_stops] == [Side.SELL]
    tab.cancel_stop_orders()
    assert tab.pending_stops == []


def test_close_at_stop_cancel_removes_slot_without_closing():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_stop_market(pos, 100.0, 99.9)
    tab.close_at_stop_cancel(pos)
    assert pos.stop is None
    assert pos.state == "Open"
    # Бар задевает бывшую активацию — позиция не закрывается
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 101.0, 101.0, 99.5, 100.0))
    assert [a for a, _ in tab.fills] == ["open_long"]


def test_close_at_stop_and_profit_aliases_use_activation_fill():
    tab = TesterTab()
    tab.buy_at_limit(1.0, 90.0)
    pos = tab.positions[-1]
    tab.close_at_stop(pos, 100.0, 99.9)     # лимитный вариант — тот же слот
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 100.0, 100.5, 99.5, 100.0))
    assert tab.fills[-1][1] == pytest.approx(100.0)

    tab.buy_at_limit(1.0, 90.0)
    pos2 = tab.positions[-1]
    tab.close_at_profit(pos2, 120.0, 119.9)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=10), 119.0, 120.5, 118.0, 120.0))
    assert tab.fills[-1][1] == pytest.approx(120.0)


def test_entry_stop_expires_after_one_bar():
    # expires_bars=1: заявка живёт один следующий бар; если он её не задел —
    # снимается в конце бара (CancelStopOpenerByNewCandle: ExpiresBars <= 1).
    tab = TesterTab()
    tab.buy_at_stop(1.0, 100.0, activation_price=100.0, expires_bars=1)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 95.0, 99.0, 94.0, 96.0))
    assert tab.pending_stops == []


def test_entry_stop_expires_fires_on_first_bar():
    # Тот же срок, но бар задел активацию — заявка срабатывает.
    tab = TesterTab()
    tab.buy_at_stop(1.0, 100.0, activation_price=100.0, expires_bars=1)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 99.0, 100.5, 98.5, 100.0))
    assert tab.fills == [("open_long", 100.0)]
    assert tab.pending_stops == []


def test_entry_stop_expires_bars_two_lives_two_bars():
    tab = TesterTab()
    tab.buy_at_stop(1.0, 100.0, activation_price=100.0, expires_bars=2)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 95.0, 99.0, 94.0, 96.0))
    assert len(tab.pending_stops) == 1  # первый бар прожит, заявка ждёт
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=10), 95.0, 99.5, 94.0, 96.0))
    assert tab.pending_stops == []      # второй бар — снята (после шанса)


def test_entry_stop_no_lifetime_survives():
    # Дефолт (expires_bars=0) — бессрочная, как NoLifeTime.
    tab = TesterTab()
    tab.buy_at_stop(1.0, 100.0, activation_price=100.0)
    for i in range(5):
        tab.process_intrabar(_candle(_T0 + timedelta(minutes=9 + i), 95.0, 99.0, 94.0, 96.0))
    assert len(tab.pending_stops) == 1
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=20), 99.0, 100.5, 98.5, 100.0))
    assert tab.fills == [("open_long", 100.0)]


def test_buy_stop_market_fills_at_activation():
    tab = TesterTab()
    tab.buy_at_stop_market(1.0, 100.0)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 99.0, 100.5, 98.5, 100.0))
    assert tab.fills == [("open_long", 100.0)]


def test_sell_stop_market_fills_at_activation():
    tab = TesterTab()
    tab.sell_at_stop_market(1.0, 95.0)
    tab.process_intrabar(_candle(_T0 + timedelta(minutes=9), 96.0, 96.5, 94.5, 95.0))
    assert tab.fills == [("open_short", 95.0)]
