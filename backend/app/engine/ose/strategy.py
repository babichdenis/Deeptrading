"""Адаптеры OsEngine-роботов под контракт Strategy (голосование).

Каждый робот ведёт собственный TesterTab — изолированный бар-реплей, как
отдельный тестер в OsEngine, поэтому позиции роботов не мешают друг другу.
Голос робота — смена его позиции на закрытии бара:

- открыл long / реверс short→long  → BUY;
- открыл short / реверс long→short → SELL;
- закрыл long без реверса  → SELL, закрыл short без реверса → BUY
  (голос «выйти из стороны»).

OseAllStrategy собирает голоса пяти роботов. При quorum=1 (дефолт) вход
даёт один голос; при равенстве (напр. 1 BUY против 1 SELL) сигнала нет.
Адаптеры отдельных роботов (ose_price_channel, ...) возвращают сигнал
ровно по своему голосу — по ним видно, кто как торгует по отдельности.

SmaStochastic: шаг тренд-фильтра задаётся в процентах от цены бара
(sma_stoch_step_pct, дефолт 1.0%), потому что дефолт оригинала 500 пунктов
не работает на акциях (бэктест 2026-09-28: 0 сделок на всех ТФ).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.engine.models import Candle, Signal, Side

from .robots import (
    EnvelopTrend,
    PriceChannelTrade,
    RsiContrtrend,
    Side as RobotSide,
    SmaStochastic,
    StrategyBollinger,
    TesterTab,
)

__all__ = [
    "OseAllParams",
    "OseAllStrategy",
    "OseRobotParams",
    "OseSmaStochParams",
    "OsePriceChannelStrategy",
    "OseSmaStochStrategy",
    "OseEnvelopTrendStrategy",
    "OseRsiContrtrendStrategy",
    "OseBollingerStrategy",
]


@dataclass(frozen=True)
class OseAllParams:
    quorum: int = 1
    sma_stoch_step_pct: float = 1.0


@dataclass(frozen=True)
class OseRobotParams:
    """Пустой набор: поведение задают дефолты порта робота."""


@dataclass(frozen=True)
class OseSmaStochParams:
    sma_stoch_step_pct: float = 1.0


def _build_robot(name: str) -> tuple[TesterTab, object]:
    tab = TesterTab()
    if name == "price_channel":
        return tab, PriceChannelTrade(tab)
    if name == "sma_stoch":
        return tab, SmaStochastic(tab)
    if name == "envelop_trend":
        return tab, EnvelopTrend(tab)
    if name == "rsi_contrtrend":
        return tab, RsiContrtrend(tab)
    if name == "bollinger":
        return tab, StrategyBollinger(tab)
    raise ValueError(f"unknown ose robot: {name}")


def _open_side(tab: TesterTab) -> RobotSide | None:
    positions = tab.positions_open_all
    if not positions:
        return None
    return positions[-1].side


def _vote_from_fills(fills: Sequence[tuple[str, float]], start: int,
                     before: RobotSide | None) -> tuple[Side, str] | None:
    """Голос по журналу исполнений за бар: последнее открытие задаёт
    сторону (реверс закрывает и открывает в одном баре); только закрытие —
    голос против закрытой стороны. Возврат: (сторона, действие)."""
    for action, _price in reversed(fills[start:]):
        if action == "open_long":
            return Side.BUY, "open_long"
        if action == "open_short":
            return Side.SELL, "open_short"
    if len(fills) > start and before is not None:
        if before is RobotSide.BUY:
            return Side.SELL, "close_long"
        return Side.BUY, "close_short"
    return None


def _robot_vote(tab: TesterTab, robot, candles: Sequence[Candle],
                step_pct: float) -> tuple[Side, str] | None:
    """Прогон робота на закрытии бара (intrabar → on_candle_finished);
    голос — смена его позиции за этот бар."""
    before = _open_side(tab)
    fills_before = len(tab.fills)
    tab.process_intrabar(candles[-1])
    if hasattr(robot, "step"):
        robot.step = candles[-1].close * (step_pct / 100.0)
    robot.on_candle_finished(candles)
    return _vote_from_fills(tab.fills, fills_before, before)


def _decide(buy_n: int, sell_n: int, quorum: int) -> Side | None:
    if buy_n >= quorum and buy_n > sell_n:
        return Side.BUY
    if sell_n >= quorum and sell_n > buy_n:
        return Side.SELL
    return None


_WARMUP = {
    "price_channel": 23,      # канал 21 + сдвиг [-2]
    "sma_stoch": 22,          # стохастик 5/3/3, пара K[-2]/K[-1]
    "envelop_trend": 12,      # конверты 10
    "rsi_contrtrend": 52,     # SMA 50 + пара
    "bollinger": 23,          # BB 21 + пара
}

_ROBOT_ORDER = ("price_channel", "sma_stoch", "envelop_trend",
                "rsi_contrtrend", "bollinger")


class OseAllStrategy:
    """Все 5 роботов голосуют; при quorum=1 вход даёт один голос."""

    strategy_id = "ose_all"
    version = "1.0.0"

    def __init__(self, params: OseAllParams | None = None):
        self.p = params or OseAllParams()
        self._robots = {name: _build_robot(name) for name in _ROBOT_ORDER}
        self._last_votes = None
        self._last_skip: str | None = None

    def warmup_bars(self) -> int:
        return max(_WARMUP.values())

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if not candles:
            self._last_skip = "no_candles"
            return None
        buy_members: list[str] = []
        sell_members: list[str] = []
        details: list[str] = []
        for name in _ROBOT_ORDER:
            tab, robot = self._robots[name]
            vote = _robot_vote(tab, robot, candles, self.p.sma_stoch_step_pct)
            if vote is None:
                continue
            side, action = vote
            details.append(f"{name}:{action}")
            if side is Side.BUY:
                buy_members.append(name)
            else:
                sell_members.append(name)
        self._last_votes = {
            "ts": candles[-1].ts.isoformat(),
            "buy": len(buy_members),
            "sell": len(sell_members),
            "buy_members": buy_members,
            "sell_members": sell_members,
        }
        side = _decide(len(buy_members), len(sell_members), self.p.quorum)
        if side is None:
            self._last_skip = (f"vote_skip({len(buy_members)}B/{len(sell_members)}S "
                               f"quorum={self.p.quorum})")
            return None
        self._last_skip = None
        _reason = (f"ose_vote {side.value}: {len(buy_members)}B/{len(sell_members)}S "
                   f"({';'.join(details) or '-'})")
        # Карточка сделки как у ансамбля: meta.entry.reason + meta.quorum_event
        # (голоса, кто за/против) — читается фронтом в tradeDetailsHtml.
        _for = list(buy_members if side is Side.BUY else sell_members)
        _opp = list(sell_members if side is Side.BUY else buy_members)
        return Signal(
            strategy_id=self.strategy_id,
            side=side,
            time=candles[-1].ts,
            reason=_reason,
            features={
                "entry": {
                    "reason": _reason[:120],  # колонка entry_reason VARCHAR(128)
                    "quorum": self.p.quorum,
                    "buy_votes": len(buy_members),
                    "sell_votes": len(sell_members),
                    "vote_detail": ",".join(details),
                },
                "quorum_event": {
                    "ts": candles[-1].ts.isoformat(),
                    "side": side.value,
                    "votes": len(buy_members) + len(sell_members),
                    "buy_votes": len(buy_members),
                    "sell_votes": len(sell_members),
                    "members_for": _for,
                    "opposition": _opp,
                    "total_members": len(_ROBOT_ORDER),
                    "quorum_k": self.p.quorum,
                    "reason": f"{len(buy_members)}B/{len(sell_members)}S quorum={self.p.quorum}",
                },
                # Плоские ключи — обратная совместимость (статус/логи/старые выборки).
                "quorum": self.p.quorum,
                "buy_votes": len(buy_members),
                "sell_votes": len(sell_members),
                "buy_members": ",".join(buy_members),
                "sell_members": ",".join(sell_members),
                "vote_detail": ",".join(details),
            },
        )


class _OseSingleRobot:
    """База адаптера одного робота: сигнал == голос робота."""

    strategy_id = ""
    robot_name = ""

    def __init__(self, params=None):
        self.p = params or OseRobotParams()
        self._tab, self._robot = _build_robot(self.robot_name)
        self._last_votes = None
        self._last_skip: str | None = None

    def warmup_bars(self) -> int:
        return _WARMUP[self.robot_name]

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if not candles:
            self._last_skip = "no_candles"
            return None
        step_pct = float(getattr(self.p, "sma_stoch_step_pct", 0.0) or 0.0)
        vote = _robot_vote(self._tab, self._robot, candles, step_pct)
        if vote is None:
            self._last_votes = {
                "ts": candles[-1].ts.isoformat(),
                "buy": 0, "sell": 0,
                "buy_members": [], "sell_members": [],
            }
            self._last_skip = "no_vote"
            return None
        side, action = vote
        is_buy = side is Side.BUY
        self._last_votes = {
            "ts": candles[-1].ts.isoformat(),
            "buy": 1 if is_buy else 0,
            "sell": 0 if is_buy else 1,
            "buy_members": [self.robot_name] if is_buy else [],
            "sell_members": [] if is_buy else [self.robot_name],
        }
        self._last_skip = None
        _reason = f"ose_vote {side.value} ({self.robot_name}: {action})"
        # Карточка сделки как у ансамбля: meta.entry.reason + meta.quorum_event.
        return Signal(
            strategy_id=self.strategy_id,
            side=side,
            time=candles[-1].ts,
            reason=_reason,
            features={
                "entry": {
                    "reason": _reason[:120],  # колонка entry_reason VARCHAR(128)
                    "buy_votes": 1 if is_buy else 0,
                    "sell_votes": 0 if is_buy else 1,
                    "vote_detail": f"{self.robot_name}:{action}",
                },
                "quorum_event": {
                    "ts": candles[-1].ts.isoformat(),
                    "side": side.value,
                    "votes": 1,
                    "buy_votes": 1 if is_buy else 0,
                    "sell_votes": 0 if is_buy else 1,
                    "members_for": [self.robot_name],
                    "opposition": [],
                    "total_members": 1,
                    "quorum_k": 1,
                    "reason": f"{self.robot_name}: {action}",
                },
                # Плоские ключи — обратная совместимость.
                "buy_votes": 1 if is_buy else 0,
                "sell_votes": 0 if is_buy else 1,
                "vote_detail": f"{self.robot_name}:{action}",
            },
        )


class OsePriceChannelStrategy(_OseSingleRobot):
    strategy_id = "ose_price_channel"
    robot_name = "price_channel"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseSmaStochStrategy(_OseSingleRobot):
    strategy_id = "ose_sma_stoch"
    robot_name = "sma_stoch"

    def __init__(self, params: OseSmaStochParams | None = None):
        super().__init__(params or OseSmaStochParams())


class OseEnvelopTrendStrategy(_OseSingleRobot):
    strategy_id = "ose_envelop_trend"
    robot_name = "envelop_trend"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseRsiContrtrendStrategy(_OseSingleRobot):
    strategy_id = "ose_rsi_contrtrend"
    robot_name = "rsi_contrtrend"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseBollingerStrategy(_OseSingleRobot):
    strategy_id = "ose_bollinger"
    robot_name = "bollinger"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)
