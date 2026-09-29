"""OsEngine-порты роботов — clean-room (Фаза D, пилот 5 роботов).

Источник семантики: клон ~/OsEngine → Robots/Trend/*.cs,
Robots/CounterTrend/*.cs (только чтение; лицензия OsEngine — EULA, код не
копируется, воспроизводится поведение). Индикаторы — порты из .indicators
(тех же скриптов Indicators/Scripts), поэтому роботы видят те же числа,
что оригинальный тестер.

Порты (событие CandleFinishedEvent воспроизведено 1:1): сначала закрытие
открытых позиций, затем гейт OnlyClosePosition, затем открытие — только
если позиций нет. Особенности оригинала сохранены:
- PriceChannelTrade: канал читается со сдвигом [-2] (значение предыдущего
  бара); бар, пробивший обе стороны, входа не даёт; закрытие с разворотом,
  пока открытых позиций < 3;
- SmaStochastic: стохастик читается парой (K[-2], K[-1]) — second/first;
- EnvelopTrend: вход стоп-заявками по полосам конвертов (HigherOrEqual /
  LowerOrEqual); после открытия (PositionOpeningSuccesEvent) стопы
  снимаются и ставится трейлинг-стоп: активация up*(1-Trail%) для лонга,
  down*(1+Trail%) для шорта; перевыставляется каждым баром;
- RsiContrtrend: контртренд с фильтром SMA, выход по обратному сигналу;
- RsiTrade: кроссовер уровней RSI, выход с реверсом в той же свече;
- StrategyBollinger: контртренд по полосам Боллинджера, выход по SMA,
  закрытие только при часе закрытия свечи ≤ 18.

Осознанные отличия порта:
- прогрев: в C# серии индикаторов содержат 0, и роботы могут шуметь на
  прогреве; здесь прогрев = None → робот ждёт валидных значений (гейты
  вида Values.Count < N покрыты этим);
- объём контрактный; ветки "Contract currency"/"Deposit percent" не
  портируются — в каркасе нет портфеля;
- EnvelopTrend: guard candles.Count + 5 < Values.Count опущен — ряды
  порта всегда одной длины со свечами;
- исполнение: TesterTab (bar-replay, TesterServer-стиль) — лимитные
  заявки исполняются немедленно по цене заявки (каркас для тестов
  сигнальной логики); стоп-заявки на вход и слоты выхода позиции
  (стоп/тейк с OCO, трейлинг = перезаряжаемый стоп-слот) живут между
  барами и срабатывают по касанию диапазоном следующего бара. Филл
  стоп-заявок — по цене активации: так делает тестер OsEngine
  (BuyAtStop для не-OsTrader ставит PriceOrder = priceRedLine,
  TryReloadStop перезаписывает priceOrder = priceActivate), поэтому
  slippage в этих заявках на результат не влияет. Трейлинг хранит
  ratchet (уровень не откатывается), fills помечают причину закрытия
  (Position.exit_reason). Входные заявки живут expires_bars баров
  (0 — бессрочно, NoLifeTime), stop-market входы — алиасы
  buy/sell_at_stop_market (в бар-реплее филл тот же — по активации).
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Sequence

from app.engine.models import Candle

from .indicators import _rsi_at_index, bollinger, envelops, price_channel, rsi, sma, stochastic


def _rsi_series_incremental(robot, candles: Sequence[Candle], length: int) -> list:
    """RSI-серия с хвостовым досчётом (значения бит-в-бит как у rsi()).

    rsi() считает всю серию (O(n·окно)); роботы звали её каждый бар → O(n²).
    Значение индекса i не зависит от будущего, поэтому на выросшем на 1 баре
    списке считаем только новый индекс. Кэш живёт на инстансе робота
    (сброс = новый робот, см. _build_robot/reset).
    """
    n = len(candles)
    cache = getattr(robot, "_rsi_tail", None)
    if cache is not None and cache[0] == n - 1:
        closes = [c.close for c in candles]
        series = cache[1]
        series.append(_rsi_at_index(closes, length, n - 1))
        robot._rsi_tail = (n, series)
        return series
    series = rsi(candles, length)
    robot._rsi_tail = (n, series)
    return series

__all__ = [
    "Regime",
    "Side",
    "Position",
    "StopOrder",
    "TesterTab",
    "PriceChannelTrade",
    "SmaStochastic",
    "EnvelopTrend",
    "RsiContrtrend",
    "RsiTrade",
    "StrategyBollinger",
    "CciTrade",
    "BbPowerTrade",
    "RviTrade",
    "MacdRevers",
    "MacdTrail",
    "BollingerRevers",
    "BollingerTrailing",
    "SmaTrendSample",
    "PriceChannelVolatility",
]


class Regime(enum.Enum):
    """BotTradeRegime (Entity/BotTradeRegime.cs)."""

    OFF = "Off"
    ON = "On"
    ONLY_LONG = "OnlyLong"
    ONLY_SHORT = "OnlyShort"
    ONLY_CLOSE_POSITION = "OnlyClosePosition"


class Side(enum.Enum):
    """Side (Entity/Side.cs)."""

    BUY = "Buy"
    SELL = "Sell"


@dataclass
class Position:
    """Позиция в объёме, нужном портам (Entity/Position.cs): сторона,
    объём, цена входа, состояние. Closing в bar-replay неоднозначен —
    закрытие в каркасе мгновенно переводит позицию в Closed. Слоты
    выхода — стоп и тейк: BotTabSimple хранит их на самой позиции и
    перезагружает каждым вызовом CloseAtStopMarket/CloseAtProfitMarket
    (TryReload: та же пара цен — no-op); трейлинг — тот же стоп-слот,
    перезаряжаемый роботом каждым баром. OCO: исполнение одного слота
    снимает второй."""

    side: Side
    volume: float
    entry_price: float
    state: str = "Open"
    stop: tuple[float, float] | None = None    # (activation, order_price)
    profit: tuple[float, float] | None = None  # (activation, order_price)
    exit_reason: str | None = None  # close / stop_close / profit_close / trail_close
    stop_is_trail: bool = False     # стоп-слот заряжен трейлингом (CloseAtTrailingStop*)


@dataclass
class StopOrder:
    """Отложенная стоп-заявка (BotTabSimple.BuyAtStop/SellAtStop).

    expires_bars — срок жизни (CancelStopOpenerByNewCandle в оригинале):
    0 = бессрочная (NoLifeTime), k > 0 = заявка живёт k баров после бара
    размещения и снимается в конце k-го, после шанса активации."""

    side: Side
    volume: float
    activation_price: float
    order_price: float
    expires_bars: int = 0
    created_bar: int = 0


@dataclass
class TesterTab:
    """Каркас исполнения в духе TesterServer (bar-replay).

    Лимитные заявки (buy/sell_at_limit, close_at_limit) исполняются
    немедленно по цене заявки. Отложенное живёт до следующего бара:
    process_intrabar(candle) вызывается каркасом перед событием закрытия
    бара и срабатывает, когда диапазон бара касается activation.

    Цена исполнения стоп-заявок — как в тестере OsEngine: BuyAtStop для
    не-OsTrader ставит PriceOrder = priceRedLine, TryReloadStop в тестере
    перезаписывает priceOrder = priceActivate, CloseAtTrailingStopMarket
    выставляет обе цены в activation. Поэтому и вход стоп-заявкой, и слоты
    выхода заполняются по цене активации, а переданный order_price на филл
    не влияет (хранится ради TryReload no-op и будущего реального режима).
    Трейлинг держит ratchet: CloseAtTrailingStop не опускает стоп лонга и
    не поднимает стоп шорта.

    Порядок обработки: слоты выхода открытых позиций (стоп, затем тейк;
    при касании обеих активаций в одном баре приоритет у стопа — CheckStop
    проверяется раньше CheckProfit в BotTabSimple), затем стоп-заявки на
    вход, пока позиций нет. Заполнение стопа на вход снимает остальные
    стопы и вызывает on_position_opened (аналог
    PositionOpeningSuccesEvent). fills — журнал исполнений; причина
    закрытия пишется в Position.exit_reason.
    """

    __test__ = False  # pytest: имя Test* не должно попадать в коллекцию

    positions: list[Position] = field(default_factory=list)
    pending_stops: list[StopOrder] = field(default_factory=list)
    fills: list[tuple[str, float]] = field(default_factory=list)
    on_position_opened: Callable[[Position], None] | None = None
    _bar_index: int = field(default=0, init=False, repr=False)

    @property
    def positions_open_all(self) -> list[Position]:
        """BotTabSimple.PositionsOpenAll — только открытые позиции."""
        return [p for p in self.positions if p.state == "Open"]

    # -- лимитные заявки: немедленное исполнение --

    def buy_at_limit(self, volume: float, price: float) -> None:
        self.positions.append(Position(Side.BUY, volume, price))
        self.fills.append(("open_long", price))

    def sell_at_limit(self, volume: float, price: float) -> None:
        self.positions.append(Position(Side.SELL, volume, price))
        self.fills.append(("open_short", price))

    def buy_at_market(self, volume: float, price: float) -> None:
        """BuyAtMarket: в бар-реплее маркет исполняется по переданной
        цене — каркас не моделирует очередь и проскальзывание."""
        self.buy_at_limit(volume, price)

    def sell_at_market(self, volume: float, price: float) -> None:
        """SellAtMarket: см. buy_at_market."""
        self.sell_at_limit(volume, price)

    def close_at_limit(self, position: Position, price: float, volume: float,
                       reason: str = "close") -> None:
        if position.state != "Open":
            return
        position.state = "Closed"
        position.stop = None
        position.profit = None
        position.exit_reason = reason
        self.fills.append(("close", price))

    # -- стоп-заявки и трейлинг: исполнение на следующем баре --

    def buy_at_stop(self, volume: float, price: float, activation_price: float,
                    activate_type: str = "HigherOrEqual",
                    expires_bars: int = 0) -> None:
        self.pending_stops.append(StopOrder(
            Side.BUY, volume, activation_price, price,
            expires_bars=expires_bars, created_bar=self._bar_index))

    def sell_at_stop(self, volume: float, price: float, activation_price: float,
                     activate_type: str = "LowerOrEqual",
                     expires_bars: int = 0) -> None:
        self.pending_stops.append(StopOrder(
            Side.SELL, volume, activation_price, price,
            expires_bars=expires_bars, created_bar=self._bar_index))

    def buy_at_stop_market(self, volume: float, activation_price: float,
                           expires_bars: int = 0) -> None:
        """BuyAtStopMarket: в бар-реплее исполняется как обычный стоп —
        по цене активации (тестерная семантика для обоих вариантов)."""
        self.buy_at_stop(volume, activation_price, activation_price,
                         expires_bars=expires_bars)

    def sell_at_stop_market(self, volume: float, activation_price: float,
                            expires_bars: int = 0) -> None:
        """SellAtStopMarket: см. buy_at_stop_market."""
        self.sell_at_stop(volume, activation_price, activation_price,
                          expires_bars=expires_bars)

    def cancel_stop_orders(self, side: Side | None = None) -> None:
        """CancelStopOrders: side=None — все заявки, иначе только заявки
        указанной стороны (BuyAtStopCancel/SellAtStopCancel в оригинале)."""
        if side is None:
            self.pending_stops.clear()
        else:
            self.pending_stops = [o for o in self.pending_stops if o.side is not side]

    # -- слоты выхода позиции (TryReloadStop / TryReloadProfit) --

    def close_at_stop_market(self, position: Position, activation_price: float,
                             order_price: float) -> None:
        """CloseAtStopMarket → TryReloadStop: пара цен уже активна —
        no-op, иначе перезапись стоп-слота. Филл слота — по activation
        (тестерная семантика), order_price хранится для no-op-сравнения."""
        new = (activation_price, order_price)
        if position.stop == new:
            return
        position.stop = new
        position.stop_is_trail = False

    def close_at_stop(self, position: Position, activation_price: float,
                      order_price: float) -> None:
        """CloseAtStop (лимитный стоп): в тестере OsEngine филл тот же —
        по activation (TryReloadStop перезаписывает priceOrder)."""
        self.close_at_stop_market(position, activation_price, order_price)

    def close_at_profit_market(self, position: Position, activation_price: float,
                               order_price: float) -> None:
        """CloseAtProfitMarket → TryReloadProfit: пара цен уже активна —
        no-op, иначе перезапись тейк-слота."""
        new = (activation_price, order_price)
        if position.profit == new:
            return
        position.profit = new

    def close_at_profit(self, position: Position, activation_price: float,
                        order_price: float) -> None:
        """CloseAtProfit (лимитный тейк): филл по activation, см. close_at_stop."""
        self.close_at_profit_market(position, activation_price, order_price)

    def close_at_stop_cancel(self, position: Position) -> None:
        """CloseAtStopCancel: снять стоп-слот, позицию не закрывать."""
        position.stop = None
        position.stop_is_trail = False

    def close_at_profit_cancel(self, position: Position) -> None:
        """CloseAtProfitCancel: снять тейк-слот, позицию не закрывать."""
        position.profit = None

    def close_at_trailing_stop(self, position: Position, activation_price: float,
                               order_price: float) -> None:
        """CloseAtTrailingStop: трейлинг с ratchet — стоп лонга двигается
        только вверх, стоп шорта только вниз. Гварды оригинала: при
        RedLine > activation (Buy) или RedLine < activation (Sell) новый
        уровень не применяется, остаётся лучший из уже стоящих."""
        current = position.stop
        if current is not None:
            if position.side is Side.BUY and current[0] > activation_price:
                return
            if position.side is Side.SELL and current[0] < activation_price:
                return
        self.close_at_stop_market(position, activation_price, order_price)
        position.stop_is_trail = True

    def _exit_slot_hits(self, pos: Position, candle: Candle) -> tuple[bool, bool]:
        """Касания стоп- и тейк-слотов диапазоном бара. Порядок проверки —
        как в BotTabSimple: CheckStop раньше CheckProfit, поэтому при
        касании обеих активаций в одном баре приоритет у стопа."""
        hit_stop = False
        hit_profit = False
        if pos.stop is not None:
            activation = pos.stop[0]
            if pos.side is Side.BUY:
                hit_stop = candle.low <= activation
            else:
                hit_stop = candle.high >= activation
        if pos.profit is not None:
            activation = pos.profit[0]
            if pos.side is Side.BUY:
                hit_profit = candle.high >= activation
            else:
                hit_profit = candle.low <= activation
        if hit_stop and hit_profit:
            hit_profit = False
        return hit_stop, hit_profit

    def process_intrabar(self, candle: Candle) -> None:
        """Отложенные заявки против диапазона закрывшегося бара: сначала
        слоты выхода открытых позиций (исполнение одного снимает второй —
        OCO), затем стоп-заявки на вход, пока позиций нет, затем — срок
        жизни входных заявок (0 = бессрочная). Филлы — по цене активации
        (тестер OsEngine: PriceOrder := priceRedLine / priceActivate),
        причина закрытия пишется в Position.exit_reason."""
        self._bar_index += 1
        for pos in list(self.positions_open_all):
            hit_stop, hit_profit = self._exit_slot_hits(pos, candle)
            if not (hit_stop or hit_profit):
                continue
            if hit_stop:
                reason = "trail_close" if pos.stop_is_trail else "stop_close"
                self.close_at_limit(pos, pos.stop[0], pos.volume, reason=reason)
            else:
                self.close_at_limit(pos, pos.profit[0], pos.volume,
                                    reason="profit_close")
        if not self.positions_open_all:
            for order in list(self.pending_stops):
                if order.side is Side.BUY:
                    hit = candle.high >= order.activation_price
                else:
                    hit = candle.low <= order.activation_price
                if not hit:
                    continue
                self.pending_stops.clear()
                # BuyAtStop в тестере ставит PriceOrder = priceRedLine.
                fill_price = order.activation_price
                if order.side is Side.BUY:
                    self.buy_at_limit(order.volume, fill_price)
                else:
                    self.sell_at_limit(order.volume, fill_price)
                if self.on_position_opened is not None:
                    self.on_position_opened(self.positions[-1])
                break
        # CancelStopOpenerByNewCandle: заявка живёт expires_bars баров и
        # снимается в конце k-го — после шанса активации на этом баре.
        if self.pending_stops:
            self.pending_stops = [
                o for o in self.pending_stops
                if o.expires_bars == 0
                or (self._bar_index - o.created_bar) < o.expires_bars
            ]


class _Robot:
    """Базовый класс порта робота: таб + событие закрытия свечи."""

    def __init__(self, tab: TesterTab) -> None:
        self.tab = tab

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        raise NotImplementedError


class PriceChannelTrade(_Robot):
    """Порт Robots/Trend/PriceChannelTrade.cs.

    Пробой канала PriceChannel: High > ChUp (значение предыдущего бара —
    сдвиг [-2], как в оригинале) → Buy; Low < ChDown → Sell. Бар,
    пробивший обе стороны, входа не даёт. Закрытие — по обратной стороне
    канала, с разворотом, пока открытых позиций < 3 и режим позволяет.
    """

    def __init__(self, tab: TesterTab, *, length_up: int = 21, length_down: int = 21,
                 slippage: float = 0.0, volume: float = 1.0,
                 regime: Regime = Regime.ON) -> None:
        super().__init__(tab)
        self.length_up = length_up
        self.length_down = length_down
        self.slippage = slippage
        self.volume = volume
        self.regime = regime

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self.regime is Regime.OFF or len(candles) < 2:
            return
        channel = price_channel(candles, self.length_up, self.length_down)
        ch_up = channel["up"][-2]
        ch_down = channel["down"][-2]
        if ch_up is None or ch_down is None:
            return
        last = candles[-1]
        close, high, low = last.close, last.high, last.low
        for pos in list(self.tab.positions_open_all):
            self._close_position(pos, close, high, low, ch_up, ch_down)
        if self.regime is Regime.ONLY_CLOSE_POSITION:
            return
        if not self.tab.positions_open_all:
            self._open_position(close, high, low, ch_up, ch_down)

    def _open_position(self, close: float, high: float, low: float,
                       ch_up: float, ch_down: float) -> None:
        if high > ch_up and low < ch_down:
            return
        if high > ch_up and self.regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self.volume, close + self.slippage)
        if low < ch_down and self.regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self.volume, close - self.slippage)

    def _close_position(self, pos: Position, close: float, high: float, low: float,
                        ch_up: float, ch_down: float) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY:
            if low < ch_down:
                self.tab.close_at_limit(pos, close - self.slippage, pos.volume)
                if (self.regime is not Regime.ONLY_LONG
                        and self.regime is not Regime.ONLY_CLOSE_POSITION
                        and len(self.tab.positions_open_all) < 3):
                    self.tab.sell_at_limit(self.volume, close - self.slippage)
        elif pos.side is Side.SELL:
            if high > ch_up:
                self.tab.close_at_limit(pos, close + self.slippage, pos.volume)
                if (self.regime is not Regime.ONLY_SHORT
                        and self.regime is not Regime.ONLY_CLOSE_POSITION
                        and len(self.tab.positions_open_all) < 3):
                    self.tab.buy_at_limit(self.volume, close + self.slippage)


class SmaStochastic(_Robot):
    """Порт Robots/Trend/SmaStochastic.cs.

    Buy: Close > Sma + Step и K пересёк Downline снизу вверх
    (K[-2] <= Downline <= K[-1]); Sell: Close < Sma - Step и K пересёк
    Upline сверху вниз (K[-2] >= Upline >= K[-1]). Exit Long:
    Close < Sma - Step; Exit Short: Close > Sma + Step. В оригинале SMA
    создаётся с дефолтной длиной Sma.cs (14), стохастик — 5/3/3.
    """

    def __init__(self, tab: TesterTab, *, sma_length: int = 14,
                 period1: int = 5, period2: int = 3, period3: int = 3,
                 step: float = 500.0, upline: float = 70.0,
                 downline: float = 30.0, slippage: float = 0.0,
                 volume: float = 1.0, regime: Regime = Regime.ON) -> None:
        super().__init__(tab)
        self.sma_length = sma_length
        self.period1 = period1
        self.period2 = period2
        self.period3 = period3
        self.step = step
        self.upline = upline
        self.downline = downline
        self.slippage = slippage
        self.volume = volume
        self.regime = regime

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self.regime is Regime.OFF or len(candles) < 2:
            return
        sma_series = sma(candles, self.sma_length)
        k_series = stochastic(candles, self.period1, self.period2, self.period3)["k"]
        first = k_series[-1]
        second = k_series[-2]
        last_sma = sma_series[-1]
        if first is None or second is None or last_sma is None:
            return
        close = candles[-1].close
        for pos in list(self.tab.positions_open_all):
            self._close_position(pos, close, last_sma)
        if self.regime is Regime.ONLY_CLOSE_POSITION:
            return
        if not self.tab.positions_open_all:
            self._open_position(close, first, second, last_sma)

    def _open_position(self, close: float, first: float, second: float,
                       last_sma: float) -> None:
        if (close > last_sma + self.step and second <= self.downline
                and first >= self.downline
                and self.regime is not Regime.ONLY_SHORT):
            self.tab.buy_at_limit(self.volume, close + self.slippage)
        if (close < last_sma - self.step and second >= self.upline
                and first <= self.upline
                and self.regime is not Regime.ONLY_LONG):
            self.tab.sell_at_limit(self.volume, close - self.slippage)

    def _close_position(self, pos: Position, close: float, last_sma: float) -> None:
        if pos.side is Side.BUY and close < last_sma - self.step:
            self.tab.close_at_limit(pos, close - self.slippage, pos.volume)
        elif pos.side is Side.SELL and close > last_sma + self.step:
            self.tab.close_at_limit(pos, close + self.slippage, pos.volume)


class EnvelopTrend(_Robot):
    """Порт Robots/Trend/EnvelopTrend.cs.

    Вход стоп-заявками по полосам Envelops: BuyAtStop по верхней
    (HigherOrEqual), SellAtStop по нижней (LowerOrEqual), цена заявки
    смещена на Slippage шагов цены. После открытия позиции стопы
    снимаются и ставится трейлинг-стоп: активация up*(1 - TrailStop%)
    для лонга и down*(1 + TrailStop%) для шорта; пока позиция открыта,
    трейлинг перевыставляется каждым баром (уровни — полосы последнего
    закрытого бара, как DataSeries.Last в оригинале).
    """

    def __init__(self, tab: TesterTab, *, length: int = 10,
                 deviation: float = 0.3, trail_stop: float = 0.1,
                 slippage_steps: int = 0, price_step: float = 1.0,
                 volume: float = 1.0, regime: Regime = Regime.ON) -> None:
        super().__init__(tab)
        self.length = length
        self.deviation = deviation
        self.trail_stop = trail_stop
        self.slippage_steps = slippage_steps
        self.price_step = price_step
        self.volume = volume
        self.regime = regime
        self._last_up: float | None = None
        self._last_down: float | None = None
        tab.on_position_opened = self._on_position_opened

    def _on_position_opened(self, pos: Position) -> None:
        # PositionOpeningSuccesEvent: снять стопы, поставить трейлинг
        self.tab.cancel_stop_orders()
        if self._last_up is not None and self._last_down is not None:
            self._arm_trailing(pos, self._last_up, self._last_down)

    def _arm_trailing(self, pos: Position, up: float, down: float) -> None:
        if pos.side is Side.BUY:
            activation = up - up * (self.trail_stop / 100.0)
            order_price = activation - self.price_step * self.slippage_steps
            self.tab.close_at_trailing_stop(pos, activation, order_price)
        elif pos.side is Side.SELL:
            activation = down + down * (self.trail_stop / 100.0)
            order_price = activation + self.price_step * self.slippage_steps
            self.tab.close_at_trailing_stop(pos, activation, order_price)

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self.regime is not Regime.ON or len(candles) < 2:
            return
        bands = envelops(candles, self.length, self.deviation)
        up = bands["up"][-1]
        down = bands["down"][-1]
        if up is None or down is None:
            return
        self._last_up = up
        self._last_down = down
        positions = self.tab.positions_open_all
        if not positions:
            slip = self.price_step * self.slippage_steps
            self.tab.cancel_stop_orders()
            self.tab.buy_at_stop(self.volume, up + slip, up, "HigherOrEqual")
            self.tab.sell_at_stop(self.volume, down - slip, down, "LowerOrEqual")
        else:
            pos = positions[0]
            if pos.state != "Open":
                return
            self._arm_trailing(pos, up, down)


class RsiContrtrend(_Robot):
    """Порт Robots/CounterTrend/RsiContrtrend.cs.

    Контртренд с фильтром тренда по SMA: Sell при Sma > Close и
    RSI > Upline; Buy при Sma < Close и RSI < Downline. Выход по
    обратному сигналу: Long закрывается при Close < Sma или
    RSI > Upline; Short — при Close > Sma или RSI < Downline.
    """

    def __init__(self, tab: TesterTab, *, sma_length: int = 50,
                 rsi_length: int = 20, upline: float = 65.0,
                 downline: float = 35.0, slippage: float = 0.0,
                 volume: float = 1.0, regime: Regime = Regime.ON) -> None:
        super().__init__(tab)
        self.sma_length = sma_length
        self.rsi_length = rsi_length
        self.upline = upline
        self.downline = downline
        self.slippage = slippage
        self.volume = volume
        self.regime = regime

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self.regime is Regime.OFF or len(candles) < 2:
            return
        sma_series = sma(candles, self.sma_length)
        rsi_series = _rsi_series_incremental(self, candles, self.rsi_length)
        last_sma = sma_series[-1]
        last_rsi = rsi_series[-1]
        if last_sma is None or last_rsi is None:
            return
        close = candles[-1].close
        for pos in list(self.tab.positions_open_all):
            self._close_position(pos, close, last_sma, last_rsi)
        if self.regime is Regime.ONLY_CLOSE_POSITION:
            return
        if not self.tab.positions_open_all:
            self._open_position(close, last_sma, last_rsi)

    def _open_position(self, close: float, last_sma: float, last_rsi: float) -> None:
        if (last_sma > close and last_rsi > self.upline
                and self.regime is not Regime.ONLY_LONG):
            self.tab.sell_at_limit(self.volume, close - self.slippage)
        if (last_sma < close and last_rsi < self.downline
                and self.regime is not Regime.ONLY_SHORT):
            self.tab.buy_at_limit(self.volume, close + self.slippage)

    def _close_position(self, pos: Position, close: float,
                        last_sma: float, last_rsi: float) -> None:
        if pos.side is Side.BUY:
            if close < last_sma or last_rsi > self.upline:
                self.tab.close_at_limit(pos, close - self.slippage, pos.volume)
        elif pos.side is Side.SELL:
            if close > last_sma or last_rsi < self.downline:
                self.tab.close_at_limit(pos, close + self.slippage, pos.volume)


class RsiTrade(_Robot):
    """Порт Robots/OnScriptIndicators/RsiTrade.cs.

    Кроссовер уровней RSI (без фильтра тренда). Вход: Buy при пересечении
    Downline снизу вверх (second < downline и first > downline), Sell при
    пересечении Upline сверху вниз (second > upline и first < upline) —
    лимитником по lastPrice ± slippage (в каркасе исполняется сразу).
    Выход по обратному пересечению с реверсом в той же свече: Long — при
    second >= upline и first <= upline, следом шорт (если режим не
    OnlyLong/OnlyClosePosition); Short — при second <= downline и
    first >= downline, следом лонг (если не OnlyShort/OnlyClosePosition).
    Как в оригинале, вход на баре закрытия позиции не выполняется: гейт
    входа смотрит на снимок позиций до закрытий — реверс ставит сама
    LogicClosePosition, иначе был бы двойной ордер. Гейт оригинала
    Values.Count < length + 5 покрыт прогревом rsi() (= None).
    """

    def __init__(self, tab: TesterTab, *, rsi_length: int = 20,
                 upline: float = 65.0, downline: float = 35.0,
                 slippage: float = 0.0, volume: float = 1.0,
                 regime: Regime = Regime.ON) -> None:
        super().__init__(tab)
        self.rsi_length = rsi_length
        self.upline = upline
        self.downline = downline
        self.slippage = slippage
        self.volume = volume
        self.regime = regime

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self.regime is Regime.OFF or len(candles) < 2:
            return
        rsi_series = _rsi_series_incremental(self, candles, self.rsi_length)
        first_rsi = rsi_series[-1]
        second_rsi = rsi_series[-2]
        if first_rsi is None or second_rsi is None:
            return
        close = candles[-1].close
        had_positions = bool(self.tab.positions_open_all)
        for pos in list(self.tab.positions_open_all):
            self._close_position(pos, close, first_rsi, second_rsi)
        if self.regime is Regime.ONLY_CLOSE_POSITION:
            return
        if not had_positions:
            self._open_position(close, first_rsi, second_rsi)

    def _open_position(self, close: float, first_rsi: float,
                       second_rsi: float) -> None:
        if (second_rsi < self.downline and first_rsi > self.downline
                and self.regime is not Regime.ONLY_SHORT):
            self.tab.buy_at_limit(self.volume, close + self.slippage)
        if (second_rsi > self.upline and first_rsi < self.upline
                and self.regime is not Regime.ONLY_LONG):
            self.tab.sell_at_limit(self.volume, close - self.slippage)

    def _close_position(self, pos: Position, close: float,
                        first_rsi: float, second_rsi: float) -> None:
        if pos.side is Side.BUY:
            if second_rsi >= self.upline and first_rsi <= self.upline:
                self.tab.close_at_limit(pos, close - self.slippage, pos.volume)
                if (self.regime is not Regime.ONLY_LONG
                        and self.regime is not Regime.ONLY_CLOSE_POSITION):
                    self.tab.sell_at_limit(self.volume, close - self.slippage)
        elif pos.side is Side.SELL:
            if second_rsi <= self.downline and first_rsi >= self.downline:
                self.tab.close_at_limit(pos, close + self.slippage, pos.volume)
                if (self.regime is not Regime.ONLY_SHORT
                        and self.regime is not Regime.ONLY_CLOSE_POSITION):
                    self.tab.buy_at_limit(self.volume, close + self.slippage)


# ==================== Wave B: CCI / BBPower / RVI / MACD / Bollinger =======
# Порты Robots/OnScriptIndicators (C#). Единый шаблон обработчика:
# Off → расчёт серий → гвард прогрева (None) → закрытие по снапшоту
# открытых позиций → OnlyClosePosition → вход при пустом снапшоте.
# Лимитники — close ± slippage (PriceStep = 1 в тестерном порту).

from app.engine.ose.indicators import (
    atr,
    bears_power,
    bollinger,
    bulls_power,
    cci,
    envelops,
    macd,
    price_channel,
    rvi,
    sma,
)


class CciTrade(_Robot):
    """Порт Robots/OnScriptIndicators/CciTrade.cs.

    Контртренд по CCI против горизонтальных линий (±100): CCI ниже нижней —
    BuyAtLimit, выше верхней — SellAtLimit. Выход — обратный пробой линии,
    сразу реверс-лимитник (не более 3 позиций — allPos.Count < 3).
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 cci_length: int = 20, up_line: float = 100.0,
                 down_line: float = -100.0, slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._cci_length = cci_length
        self._up = up_line
        self._down = down_line
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        last_cci = cci(candles, self._cci_length)[-1]
        if last_cci is None:
            return
        close = candles[-1].close
        buy_px = close + self._slip
        sell_px = close - self._slip
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, last_cci, buy_px, sell_px, len(open_positions))
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        self._open_position(last_cci, buy_px, sell_px)

    def _open_position(self, last_cci: float, buy_px: float, sell_px: float) -> None:
        if last_cci < self._down and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, buy_px)
        if last_cci > self._up and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, sell_px)

    def _close_position(self, pos: Position, last_cci: float, buy_px: float,
                        sell_px: float, n_positions: int) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY and last_cci > self._up:
            self.tab.close_at_limit(pos, sell_px, pos.volume)
            if (self._regime is not Regime.ONLY_LONG
                    and self._regime is not Regime.ONLY_CLOSE_POSITION
                    and n_positions < 3):
                self.tab.sell_at_limit(self._volume, sell_px)
        elif pos.side is Side.SELL and last_cci < self._down:
            self.tab.close_at_limit(pos, buy_px, pos.volume)
            if (self._regime is not Regime.ONLY_SHORT
                    and self._regime is not Regime.ONLY_CLOSE_POSITION
                    and n_positions < 3):
                self.tab.buy_at_limit(self._volume, buy_px)


class BbPowerTrade(_Robot):
    """Порт Robots/OnScriptIndicators/BbPowerTrade.cs.

    Elder Bulls/Bears Power (High/Low − SMA(Close)): сумма > Step —
    BuyAtLimit, < −Step — SellAtLimit; выход по обратному условию с
    реверс-лимитником (в оригинале без лимита на число позиций).
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 bulls_length: int = 13, bears_length: int = 13,
                 step: float = 100.0, slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._bulls_length = bulls_length
        self._bears_length = bears_length
        self._step = step
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        bulls = bulls_power(candles, self._bulls_length)
        bears = bears_power(candles, self._bears_length)
        if bulls[-1] is None or bears[-1] is None:
            return
        power = bulls[-1] + bears[-1]
        close = candles[-1].close
        buy_px = close + self._slip
        sell_px = close - self._slip
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, power, buy_px, sell_px)
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        self._open_position(power, buy_px, sell_px)

    def _open_position(self, power: float, buy_px: float, sell_px: float) -> None:
        if power > self._step and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, buy_px)
        if power < -self._step and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, sell_px)

    def _close_position(self, pos: Position, power: float, buy_px: float,
                        sell_px: float) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY and power < -self._step:
            self.tab.close_at_limit(pos, sell_px, pos.volume)
            if (self._regime is not Regime.ONLY_LONG
                    and self._regime is not Regime.ONLY_CLOSE_POSITION):
                self.tab.sell_at_limit(self._volume, sell_px)
        elif pos.side is Side.SELL and power > self._step:
            self.tab.close_at_limit(pos, buy_px, pos.volume)
            if (self._regime is not Regime.ONLY_SHORT
                    and self._regime is not Regime.ONLY_CLOSE_POSITION):
                self.tab.buy_at_limit(self._volume, buy_px)


class RviTrade(_Robot):
    """Порт Robots/OnScriptIndicators/RviTrade.cs.

    RVI против нуля: signal < 0 и rvi > signal → BuyAtLimit; signal > 0 и
    rvi < signal → SellAtLimit. Выход — обратное условие + реверс
    (≤3 позиций, шаблон CciTrade).
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 rvi_length: int = 5, slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._rvi_length = rvi_length
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        series = rvi(candles, self._rvi_length)
        last_rvi, last_sig = series["rvi"][-1], series["signal"][-1]
        if last_rvi is None or last_sig is None:
            return
        close = candles[-1].close
        buy_px = close + self._slip
        sell_px = close - self._slip
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, last_rvi, last_sig, buy_px, sell_px,
                                 len(open_positions))
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        if last_sig < 0 and last_rvi > last_sig \
                and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, buy_px)
        if last_sig > 0 and last_rvi < last_sig \
                and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, sell_px)

    def _close_position(self, pos: Position, last_rvi: float, last_sig: float,
                        buy_px: float, sell_px: float, n_positions: int) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY and last_sig > 0 and last_rvi < last_sig:
            self.tab.close_at_limit(pos, sell_px, pos.volume)
            if (self._regime is not Regime.ONLY_LONG
                    and self._regime is not Regime.ONLY_CLOSE_POSITION
                    and n_positions < 3):
                self.tab.sell_at_limit(self._volume, sell_px)
        elif pos.side is Side.SELL and last_sig < 0 and last_rvi > last_sig:
            self.tab.close_at_limit(pos, buy_px, pos.volume)
            if (self._regime is not Regime.ONLY_SHORT
                    and self._regime is not Regime.ONLY_CLOSE_POSITION
                    and n_positions < 3):
                self.tab.buy_at_limit(self._volume, buy_px)


class MacdRevers(_Robot):
    """Порт Robots/OnScriptIndicators/MacdRevers.cs (индикатор MacdLine).

    signal < 0 и macd > signal → BuyAtLimit; signal > 0 и macd < signal →
    SellAtLimit. Выход — обратное условие + реверс-лимитник.
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 fast: int = 12, slow: int = 26, signal: int = 9,
                 slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._fast = fast
        self._slow = slow
        self._signal_len = signal
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        m = macd(candles, self._fast, self._slow, self._signal_len)
        up, down = m["macd"][-1], m["signal"][-1]
        if up is None or down is None:
            return
        close = candles[-1].close
        buy_px = close + self._slip
        sell_px = close - self._slip
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, up, down, buy_px, sell_px)
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        if down < 0 and up > down and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, buy_px)
        if down > 0 and up < down and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, sell_px)

    def _close_position(self, pos: Position, up: float, down: float,
                        buy_px: float, sell_px: float) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY and down > 0 and up < down:
            self.tab.close_at_limit(pos, sell_px, pos.volume)
            if (self._regime is not Regime.ONLY_LONG
                    and self._regime is not Regime.ONLY_CLOSE_POSITION):
                self.tab.sell_at_limit(self._volume, sell_px)
        elif pos.side is Side.SELL and down < 0 and up > down:
            self.tab.close_at_limit(pos, buy_px, pos.volume)
            if (self._regime is not Regime.ONLY_SHORT
                    and self._regime is not Regime.ONLY_CLOSE_POSITION):
                self.tab.buy_at_limit(self._volume, buy_px)


class MacdTrail(_Robot):
    """Порт Robots/OnScriptIndicators/MacdTrail.cs (индикатор MacdLine).

    Вход как у MacdRevers; выход — трейлинг-стоп на close ∓ trail_stop%
    от close, перезаряжаемый каждым баром (CloseAtTrailingStop).
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 fast: int = 12, slow: int = 26, signal: int = 9,
                 trail_stop: float = 0.7, slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._fast = fast
        self._slow = slow
        self._signal_len = signal
        self._trail_stop = trail_stop
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        m = macd(candles, self._fast, self._slow, self._signal_len)
        up, down = m["macd"][-1], m["signal"][-1]
        if up is None or down is None:
            return
        close = candles[-1].close
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, close)
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        if down < 0 and up > down and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, close + self._slip)
        if down > 0 and up < down and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, close - self._slip)

    def _close_position(self, pos: Position, close: float) -> None:
        if pos.state != "Open":
            return
        trail = close - close * self._trail_stop / 100
        if pos.side is Side.SELL:
            trail = close + close * self._trail_stop / 100
        self.tab.close_at_trailing_stop(pos, trail, trail)


class BollingerRevers(_Robot):
    """Порт Robots/OnScriptIndicators/BollingerRevers.cs.

    Пробой полосы Боллинджера: close > up → BuyAtLimit, close < down →
    SellAtLimit. Выход — возврат за противоположную полосу + реверс.
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 boll_length: int = 21, boll_deviation: float = 2.0,
                 slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._boll_length = boll_length
        self._boll_deviation = boll_deviation
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        b = bollinger(candles, self._boll_length, self._boll_deviation)
        up, down = b["up"][-1], b["down"][-1]
        if up is None or down is None:
            return
        close = candles[-1].close
        buy_px = close + self._slip
        sell_px = close - self._slip
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, close, up, down, buy_px, sell_px)
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        if close > up and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, buy_px)
        if close < down and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, sell_px)

    def _close_position(self, pos: Position, close: float, up: float, down: float,
                        buy_px: float, sell_px: float) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY and close < down:
            self.tab.close_at_limit(pos, sell_px, pos.volume)
            if (self._regime is not Regime.ONLY_LONG
                    and self._regime is not Regime.ONLY_CLOSE_POSITION):
                self.tab.sell_at_limit(self._volume, sell_px)
        elif pos.side is Side.SELL and close > up:
            self.tab.close_at_limit(pos, buy_px, pos.volume)
            if (self._regime is not Regime.ONLY_SHORT
                    and self._regime is not Regime.ONLY_CLOSE_POSITION):
                self.tab.buy_at_limit(self._volume, buy_px)


class BollingerTrailing(_Robot):
    """Порт Robots/OnScriptIndicators/BollingerTrailing.cs.

    Вход — пробой полосы (как BollingerRevers); выход — трейлинг-стоп
    на противоположной полосе, перезаряжаемый каждым баром.
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 boll_length: int = 21, boll_deviation: float = 2.0,
                 slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._boll_length = boll_length
        self._boll_deviation = boll_deviation
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        b = bollinger(candles, self._boll_length, self._boll_deviation)
        up, down = b["up"][-1], b["down"][-1]
        if up is None or down is None:
            return
        close = candles[-1].close
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            self._close_position(pos, up, down)
        if self._regime is Regime.ONLY_CLOSE_POSITION or open_positions:
            return
        if close > up and self._regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self._volume, close + self._slip)
        if close < down and self._regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self._volume, close - self._slip)

    def _close_position(self, pos: Position, up: float, down: float) -> None:
        if pos.state != "Open":
            return
        if pos.side is Side.BUY:
            self.tab.close_at_trailing_stop(pos, down, down)
        else:
            self.tab.close_at_trailing_stop(pos, up, up)


class SmaTrendSample(_Robot):
    """Порт Robots/OnScriptIndicators/SmaTrendSample.cs.

    Трендовый: close > SMA и close > верхней Envelops → BuyAtLimit;
    close < SMA и close < нижней → SellAtLimit. Одна позиция: лимитный
    выход при пробое противоположного конверта, пока тренд держится —
    трейлинг-стоп на EntryPrice ∓ base_stop_percent% (перезарядка каждый бар).
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 sma_length: int = 50, env_length: int = 30,
                 env_deviation: float = 1.0, base_stop_percent: float = 0.3,
                 slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._sma_length = sma_length
        self._env_length = env_length
        self._env_deviation = env_deviation
        self._stop_pct = base_stop_percent
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        last_sma = sma(candles, self._sma_length, "close")[-1]
        env = envelops(candles, self._env_length, self._env_deviation)
        up, down = env["up"][-1], env["down"][-1]
        if last_sma is None or up is None or down is None:
            return
        if last_sma == 0 or up == 0 or down == 0:
            return
        close = candles[-1].close
        open_positions = list(self.tab.positions_open_all)
        if open_positions:
            self._close_position(open_positions[0], close, last_sma, up, down)
            return
        if self._regime is Regime.ONLY_CLOSE_POSITION:
            return
        if (close > last_sma and close > up
                and self._regime is not Regime.ONLY_SHORT):
            self.tab.buy_at_limit(self._volume, close + self._slip)
        if (close < last_sma and close < down
                and self._regime is not Regime.ONLY_LONG):
            self.tab.sell_at_limit(self._volume, close - self._slip)

    def _close_position(self, pos: Position, close: float, last_sma: float,
                        up: float, down: float) -> None:
        if pos.state != "Open":
            return
        stop = pos.entry_price - pos.entry_price * self._stop_pct / 100
        if pos.side is Side.SELL:
            stop = pos.entry_price + pos.entry_price * self._stop_pct / 100
        if pos.side is Side.BUY:
            if close < down:
                self.tab.close_at_limit(pos, close, pos.volume)
            if close > last_sma:
                self.tab.close_at_trailing_stop(pos, stop, stop)
        else:
            if close > up:
                self.tab.close_at_limit(pos, close, pos.volume)
            if close < last_sma:
                self.tab.close_at_trailing_stop(pos, stop, stop)


class PriceChannelVolatility(_Robot):
    """Порт Robots/OnScriptIndicators/PriceChannelVolatility.cs.

    Отложенные стоп-заявки за границами канала с отступом kof_atr*ATR:
    без позиций — заявки в обе стороны (volume); одна позиция — снятие
    и перезарядка стопа в её сторону (volume2). Выход — трейлинг-стоп на
    противоположной границе канала (перезарядка каждым баром). В порту
    обе ветки входа снимают старые заявки — каркас копит pending, в
    оригинале заявки переразмещаются каждый бар.
    """

    def __init__(self, tab: TesterTab, *, regime: str = "On", volume: float = 1.0,
                 volume2: float = 1.0, length_up: int = 20, length_down: int = 20,
                 atr_length: int = 14, kof_atr: float = 0.5,
                 slippage: int = 0) -> None:
        super().__init__(tab)
        self._regime = Regime(regime)
        self._volume = volume
        self._volume2 = volume2
        self._length_up = length_up
        self._length_down = length_down
        self._atr_length = atr_length
        self._kof_atr = kof_atr
        self._slip = slippage

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self._regime is Regime.OFF:
            return
        pc = price_channel(candles, self._length_up, self._length_down)
        up, down = pc["up"][-1], pc["down"][-1]
        a = atr(candles, self._atr_length)[-1]
        if up is None or down is None or a is None:
            return
        open_positions = list(self.tab.positions_open_all)
        for pos in open_positions:
            if pos.state != "Open":
                continue
            if pos.side is Side.BUY:
                self.tab.close_at_trailing_stop(pos, down, down - self._slip)
            else:
                self.tab.close_at_trailing_stop(pos, up, up + self._slip)
        if self._regime is Regime.ONLY_CLOSE_POSITION:
            return
        if not open_positions:
            self.tab.cancel_stop_orders()
            pe_up = up + a * self._kof_atr
            pe_down = down - a * self._kof_atr
            if self._regime is not Regime.ONLY_SHORT:
                self.tab.buy_at_stop(self._volume, pe_up + self._slip, pe_up)
            if self._regime is not Regime.ONLY_LONG:
                self.tab.sell_at_stop(self._volume, pe_down - self._slip, pe_down)
        elif len(open_positions) == 1:
            self.tab.cancel_stop_orders()
            pos = open_positions[0]
            if pos.side is Side.BUY:
                pe = up + a * self._kof_atr
                self.tab.buy_at_stop(self._volume2, pe + self._slip, pe)
            else:
                pe = down - a * self._kof_atr
                self.tab.sell_at_stop(self._volume2, pe - self._slip, pe)


class StrategyBollinger(_Robot):
    """Порт Robots/CounterTrend/StrategyBollinger.cs.

    Контртренд: Sell при Close > верхней полосы Боллинджера; Buy при
    Close < нижней. Выход по SMA: Long — при Close > Sma, Short — при
    Close < Sma. Закрытие выполняется только если час закрытия последней
    свечи ≤ 18 (гейт оригинала); открытие без ограничения по часу.
    """

    def __init__(self, tab: TesterTab, *, boll_length: int = 21,
                 boll_deviation: float = 2.0, sma_length: int = 15,
                 slippage: float = 0.0, volume: float = 1.0,
                 regime: Regime = Regime.ON) -> None:
        super().__init__(tab)
        self.boll_length = boll_length
        self.boll_deviation = boll_deviation
        self.sma_length = sma_length
        self.slippage = slippage
        self.volume = volume
        self.regime = regime

    def on_candle_finished(self, candles: Sequence[Candle]) -> None:
        if self.regime is Regime.OFF or len(candles) < 2:
            return
        bands = bollinger(candles, self.boll_length, self.boll_deviation)
        boll_up = bands["up"][-1]
        boll_down = bands["down"][-1]
        sma_series = sma(candles, self.sma_length)
        last_sma = sma_series[-1]
        if boll_up is None or boll_down is None or last_sma is None:
            return
        last = candles[-1]
        close = last.close
        open_positions = self.tab.positions_open_all
        if open_positions and last.ts.hour <= 18:
            for pos in list(open_positions):
                self._close_position(pos, close, last_sma)
        if self.regime is Regime.ONLY_CLOSE_POSITION:
            return
        if not self.tab.positions_open_all:
            self._open_position(close, boll_up, boll_down)

    def _open_position(self, close: float, boll_up: float, boll_down: float) -> None:
        if boll_up == 0 or boll_down == 0:  # гейт оригинала
            return
        if close > boll_up and self.regime is not Regime.ONLY_LONG:
            self.tab.sell_at_limit(self.volume, close - self.slippage)
        if close < boll_down and self.regime is not Regime.ONLY_SHORT:
            self.tab.buy_at_limit(self.volume, close + self.slippage)

    def _close_position(self, pos: Position, close: float, last_sma: float) -> None:
        if pos.state == "Closing":  # гейт оригинала; в каркасеClosed не доходит
            return
        if pos.side is Side.BUY and close > last_sma:
            self.tab.close_at_limit(pos, close - self.slippage, pos.volume)
        elif pos.side is Side.SELL and close < last_sma:
            self.tab.close_at_limit(pos, close + self.slippage, pos.volume)
