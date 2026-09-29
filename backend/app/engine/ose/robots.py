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

from .indicators import bollinger, envelops, price_channel, rsi, sma, stochastic

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
        rsi_series = rsi(candles, self.rsi_length)
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
        rsi_series = rsi(candles, self.rsi_length)
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
