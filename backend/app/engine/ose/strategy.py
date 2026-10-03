"""Адаптеры OsEngine-роботов под контракт Strategy (голосование).

Каждый робот ведёт собственный TesterTab — изолированный бар-реплей, как
отдельный тестер в OsEngine, поэтому позиции роботов не мешают друг другу.
Голос робота — смена его позиции на закрытии бара:

- открыл long / реверс short→long  → BUY, kind="entry";
- открыл short / реверс long→short → SELL, kind="entry";
- закрыл long без реверса  → SELL, закрыл short без реверса → BUY
  (kind="exit": «выйти из стороны»; на флэте внешнего движка такая заявка
  не открывает противоположную позицию — ENG-012, audit 2026-09-29).

ENG-011 (audit 2026-09-29): робот исполняет свои сделки внутри TesterTab по
close бара, а внешний EngineRunner — по следующему open с комиссиями и
слиппеджем. Внутренняя и внешняя позиции расходятся по времени/цене входа —
это осознанный компромисс порта; единый execution owner — задача этапа C.
Сигналы внешнему движку при этом уже разделены на entry/exit intents (ENG-012).

OseAllStrategy собирает голоса шести роботов (пять пилотов + RsiTrade). При
quorum=1 (дефолт) вход даёт один голос; при равенстве (напр. 1 BUY против
1 SELL) сигнала нет. Адаптеры отдельных роботов (ose_price_channel, ...)
возвращают сигнал ровно по своему голосу — по ним видно, кто как торгует
по отдельности.

SmaStochastic: шаг тренд-фильтра задаётся в процентах от цены бара
(sma_stoch_step_pct, дефолт 1.0%), потому что дефолт оригинала 500 пунктов
не работает на акциях (бэктест 2026-09-28: 0 сделок на всех ТФ).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.engine.models import Candle, Side, Signal

from .robots import (
    BbPowerTrade,
    BollingerRevers,
    BollingerTrailing,
    CciTrade,
    EnvelopTrend,
    MacdRevers,
    MacdTrail,
    PriceChannelTrade,
    PriceChannelVolatility,
    RsiContrtrend,
    RsiTrade,
    RviTrade,
    SmaStochastic,
    SmaTrendSample,
    StrategyBollinger,
    TesterTab,
)
from .robots import (
    Side as RobotSide,
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
    "OseRsiTradeStrategy",
    "OseBollingerStrategy",
    "OseCciTradeStrategy",
    "OseBbPowerStrategy",
    "OseRviTradeStrategy",
    "OseMacdReversStrategy",
    "OseMacdTrailStrategy",
    "OseBollingerReversStrategy",
    "OseBollingerTrailingStrategy",
    "OseSmaTrendStrategy",
    "OsePcVolatilityStrategy",
]


@dataclass(frozen=True)
class OseAllParams:
    quorum: int = 1
    sma_stoch_step_pct: float = 1.0
    members: str = ""  # CSV имён роботов; пусто = все (_ROBOT_ORDER)
    er_length: int = 10  # EfficiencyRatio (Кауфман) — длина окна
    er_min: float = 0.0  # 0 = фильтр выключен; вход только при ER >= er_min


@dataclass(frozen=True)
class OseRobotParams:
    """Параметры конструктора робота (None = дефолт порта/оригинала).

    Имена совпадают с kwarg конструкторов robots.py; адаптер прокидывает только
    заданные (не-None) поля, остальное берут дефолты робота. Паритет с
    харнессом (bt_ose_sweep.py real --spec): один и тот же params-dict
    управляет и проектом, и харнессом.
    """

    length: int | None = None
    deviation: float | None = None
    trail_stop: float | None = None
    length_up: int | None = None
    length_down: int | None = None
    boll_length: int | None = None
    boll_deviation: float | None = None
    sma_length: int | None = None
    rsi_length: int | None = None
    upline: float | None = None
    downline: float | None = None


@dataclass(frozen=True)
class OseSmaStochParams:
    sma_stoch_step_pct: float = 1.0


_OSE_ROBOT_CLASSES = {
    "price_channel": PriceChannelTrade,
    "sma_stoch": SmaStochastic,
    "envelop_trend": EnvelopTrend,
    "rsi_contrtrend": RsiContrtrend,
    "rsi_trade": RsiTrade,
    "bollinger": StrategyBollinger,
    "cci_trade": CciTrade,
    "bb_power": BbPowerTrade,
    "rvi_trade": RviTrade,
    "macd_revers": MacdRevers,
    "macd_trail": MacdTrail,
    "bollinger_revers": BollingerRevers,
    "bollinger_trailing": BollingerTrailing,
    "sma_trend": SmaTrendSample,
    "pc_volatility": PriceChannelVolatility,
}


def _build_robot(name: str, params=None) -> tuple[TesterTab, object]:
    """Создать робота; из params прокидываются только поля, совпадающие с
    kwarg-ами конструктора (паритет параметров проект ↔ харнесс)."""
    cls = _OSE_ROBOT_CLASSES.get(name)
    if cls is None:
        raise ValueError(f"unknown ose robot: {name}")
    kwargs: dict = {}
    if params is not None:
        try:
            import dataclasses as _dc
            import inspect as _inspect
            sig = _inspect.signature(cls.__init__).parameters
            for f in _dc.fields(params):
                v = getattr(params, f.name, None)
                if v is not None and f.name in sig:
                    kwargs[f.name] = v
        except Exception:
            kwargs = {}
    tab = TesterTab()
    return tab, cls(tab, **kwargs)


def _open_side(tab: TesterTab) -> RobotSide | None:
    positions = tab.positions_open_all
    if not positions:
        return None
    return positions[-1].side


def _vote_from_fills(fills: Sequence[tuple[str, float]], start: int,
                     before: RobotSide | None) -> tuple[Side, str, str] | None:
    """Голос по журналу исполнений за бар: последнее открытие задаёт
    сторону (реверс закрывает и открывает в одном баре); только закрытие —
    голос против закрытой стороны.

    Возврат: (сторона, действие, kind). kind="entry" для открытий робота,
    kind="exit" для чистых закрытий (ENG-012, audit 2026-09-29): закрытие
    long — это намерение «выйти», а не «открыть short», и на флэте внешнего
    движка оно не должно открывать противоположную позицию."""
    for action, _price in reversed(fills[start:]):
        if action == "open_long":
            return Side.BUY, "open_long", "entry"
        if action == "open_short":
            return Side.SELL, "open_short", "entry"
    if len(fills) > start and before is not None:
        if before is RobotSide.BUY:
            return Side.SELL, "close_long", "exit"
        return Side.BUY, "close_short", "exit"
    return None


def _robot_vote(tab: TesterTab, robot, candles: Sequence[Candle],
                step_pct: float) -> tuple[Side, str, str] | None:
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
    "rsi_trade": 25,          # RSI 20 + гейт оригинала Values.Count < length+5
    "bollinger": 23,          # BB 21 + пара
    "cci_trade": 30,          # CCI 25 + SMA
    "bb_power": 30,           # Bulls/Bears + SMA 13
    "rvi_trade": 45,          # RVI 10 + сигнальная
    "macd_revers": 45,        # MACD 26+9
    "macd_trail": 45,         # MACD 26+9
    "bollinger_revers": 30,   # BB 21
    "bollinger_trailing": 30,  # BB 21
    "sma_trend": 30,          # SMA + конверты
    "pc_volatility": 30,      # канал 21 + ATR 14
}

# OseAll голосует всеми 15 роботами OnScriptIndicators (Wave A 6 + Wave B 9);
# каждый робот — один голос; членство ограничивается members, порог — quorum.
_ROBOT_ORDER = ("price_channel", "sma_stoch", "envelop_trend",
                "rsi_contrtrend", "bollinger", "rsi_trade",
                "cci_trade", "bb_power", "rvi_trade", "macd_revers", "macd_trail",
                "bollinger_revers", "bollinger_trailing", "sma_trend", "pc_volatility")


class OseAllStrategy:
    """Все 15 роботов голосуют; при quorum=1 вход даёт один голос."""

    strategy_id = "ose_all"
    version = "1.0.0"

    def __init__(self, params: OseAllParams | None = None):
        self.p = params or OseAllParams()
        self._order = tuple(m.strip() for m in (self.p.members or "").split(",") if m.strip()) \
            or _ROBOT_ORDER
        _unknown = [m for m in self._order if m not in _OSE_ROBOT_CLASSES]
        if _unknown:
            raise ValueError(f"unknown ose robot(s): {_unknown}")
        self._robots = {name: _build_robot(name) for name in self._order}
        self._last_votes = None
        self._last_skip: str | None = None

    def warmup_bars(self) -> int:
        return max(_WARMUP.get(name, 30) for name in self._order)

    def reset(self) -> None:
        """ENG-007: чистые роботы и TesterTab — повторный прогон детерминирован."""
        self._robots = {name: _build_robot(name) for name in self._order}
        self._last_votes = None
        self._last_skip = None

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if not candles:
            self._last_skip = "no_candles"
            return None
        buy_members: list[str] = []
        sell_members: list[str] = []
        details: list[str] = []
        buy_entry = False
        sell_entry = False
        for name in self._order:
            tab, robot = self._robots[name]
            vote = _robot_vote(tab, robot, candles, self.p.sma_stoch_step_pct)
            if vote is None:
                continue
            side, action, kind = vote
            details.append(f"{name}:{action}")
            if side is Side.BUY:
                buy_members.append(name)
                buy_entry = buy_entry or kind == "entry"
            else:
                sell_members.append(name)
                sell_entry = sell_entry or kind == "entry"
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
        _er = None
        if float(self.p.er_min) > 0.0:
            # ER-фильтр (Кауфман): «торгуй только в тренде». Считается по закрытым
            # барам, без look-ahead; 0 = выключен.
            from app.engine.indicatorhub import _efficiency_ratio
            _er_series = _efficiency_ratio(candles, int(self.p.er_length))
            _er = _er_series[-1] if _er_series else None
            if _er is None or float(_er) < float(self.p.er_min):
                self._last_skip = (f"er_filter(er={'-' if _er is None else round(_er, 3)}"
                                   f" < {self.p.er_min})")
                return None
        self._last_skip = None
        _reason = (f"ose_vote {side.value}: {len(buy_members)}B/{len(sell_members)}S "
                   f"({';'.join(details) or '-'})")
        # Карточка сделки как у ансамбля: meta.entry.reason + meta.quorum_event
        # (голоса, кто за/против) — читается фронтом в tradeDetailsHtml.
        _for = list(buy_members if side is Side.BUY else sell_members)
        _opp = list(sell_members if side is Side.BUY else buy_members)
        # ENG-012: если в сторону решения есть хотя бы одно ОТКРЫТИЕ робота —
        # это entry-intent; чистые закрытия (без открытий) — exit-intent.
        intent_kind = "entry" if (buy_entry if side is Side.BUY else sell_entry) else "exit"
        return Signal(
            strategy_id=self.strategy_id,
            side=side,
            time=candles[-1].ts,
            reason=_reason,
            kind=intent_kind,
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
                    "total_members": len(self._order),
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
                "er": (round(float(_er), 3) if _er is not None else None),
            },
        )


class _OseSingleRobot:
    """База адаптера одного робота: сигнал == голос робота."""

    strategy_id = ""
    robot_name = ""

    def __init__(self, params=None):
        self.p = params or OseRobotParams()
        self._tab, self._robot = _build_robot(self.robot_name, self.p)
        self._last_votes = None
        self._last_skip: str | None = None

    def warmup_bars(self) -> int:
        return _WARMUP[self.robot_name]

    def reset(self) -> None:
        """ENG-007: чистый TesterTab/робот — повторный прогон детерминирован."""
        self._tab, self._robot = _build_robot(self.robot_name, self.p)
        self._last_votes = None
        self._last_skip = None

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
        side, action, vote_kind = vote
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
            kind=vote_kind,
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


class OseRsiTradeStrategy(_OseSingleRobot):
    """Волна B: RsiTrade (OnScriptIndicators) — кроссовер RSI с реверсом."""

    strategy_id = "ose_rsi_trade"
    robot_name = "rsi_trade"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseBollingerStrategy(_OseSingleRobot):
    strategy_id = "ose_bollinger"
    robot_name = "bollinger"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseCciTradeStrategy(_OseSingleRobot):
    """Волна B: CciTrade (OnScriptIndicators) — CCI-кроссовер с реверсом."""

    strategy_id = "ose_cci_trade"
    robot_name = "cci_trade"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseBbPowerStrategy(_OseSingleRobot):
    """Волна B: BbPowerTrade (OnScriptIndicators) — Bulls/Bears Power + SMA."""

    strategy_id = "ose_bb_power"
    robot_name = "bb_power"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseRviTradeStrategy(_OseSingleRobot):
    """Волна B: RviTrade (OnScriptIndicators) — RVI-кроссовер."""

    strategy_id = "ose_rvi_trade"
    robot_name = "rvi_trade"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseMacdReversStrategy(_OseSingleRobot):
    """Волна B: MacdRevers (OnScriptIndicators) — MACD-реверс."""

    strategy_id = "ose_macd_revers"
    robot_name = "macd_revers"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseMacdTrailStrategy(_OseSingleRobot):
    """Волна B: MacdTrail (OnScriptIndicators) — MACD с трейлингом."""

    strategy_id = "ose_macd_trail"
    robot_name = "macd_trail"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseBollingerReversStrategy(_OseSingleRobot):
    """Волна B: BollingerRevers (OnScriptIndicators) — реверс от границ BB."""

    strategy_id = "ose_bollinger_revers"
    robot_name = "bollinger_revers"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseBollingerTrailingStrategy(_OseSingleRobot):
    """Волна B: BollingerTrailing (OnScriptIndicators) — BB с трейлингом."""

    strategy_id = "ose_bollinger_trailing"
    robot_name = "bollinger_trailing"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OseSmaTrendStrategy(_OseSingleRobot):
    """Волна B: SmaTrendSample (OnScriptIndicators) — SMA-тренд с конвертами."""

    strategy_id = "ose_sma_trend"
    robot_name = "sma_trend"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)


class OsePcVolatilityStrategy(_OseSingleRobot):
    """Волна B: PriceChannelVolatility (OnScriptIndicators) — канал + ATR-волатильность."""

    strategy_id = "ose_pc_volatility"
    robot_name = "pc_volatility"

    def __init__(self, params: OseRobotParams | None = None):
        super().__init__(params)
