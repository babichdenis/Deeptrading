"""Regime-Filtered Simple Strategies (2026).

Три простые стратегии, управляемые режимом (ADX + Efficiency Ratio + ATR ratio):
  1. TrendFollowing — только TRENDING/TRANSITIONING
  2. MeanReversion  — только CHOPPY
  3. Breakout       — TRANSITIONING/TRENDING (сжатие BB + пробой)
Плюс простой ExitManager (SL/TP/трейлинг/time/regime).

HIGH_VOLATILITY — входы блокируются.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

from app.engine.models import Candle, Side, Signal


# ============================================================
# Индикаторы
# ============================================================
def _sma(vals: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _ema_last(vals: Sequence[float], n: int) -> float | None:
    if not vals or n <= 0:
        return None
    m = min(len(vals), max(n * 5, n + 5))
    seg = vals[-m:]
    a = 2 / (n + 1)
    e = float(seg[0])
    for v in seg[1:]:
        e = a * float(v) + (1 - a) * e
    return e


def _tr(c: Candle, prev: Candle) -> float:
    return max(float(c.high) - float(c.low),
               abs(float(c.high) - float(prev.close)),
               abs(float(c.low) - float(prev.close)))


def _atr_last(candles: Sequence[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    trs = [_tr(candles[i], candles[i - 1]) for i in range(len(candles) - period, len(candles))]
    return sum(trs) / len(trs)


def _atr_series(candles: Sequence[Candle], period: int = 14) -> list[float]:
    if len(candles) < 2:
        return []
    trs = [_tr(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    out = []
    for i in range(len(trs)):
        w = trs[max(0, i - period + 1): i + 1]
        out.append(sum(w) / len(w))
    return out


def _adx_last(candles: Sequence[Candle], period: int = 14):
    n = len(candles)
    if n < period + 2:
        return None, None, None
    cs = candles[-2 * period - 1:] if n > 2 * period + 1 else candles
    dx, di_p, di_m = [], [], []
    for i in range(1, len(cs)):
        prev, cur = cs[i - 1], cs[i]
        up = float(cur.high) - float(prev.high)
        down = float(prev.low) - float(cur.low)
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        tr = _tr(cur, prev)
        if tr <= 0:
            dx.append(0.0); di_p.append(0.0); di_m.append(0.0)
            continue
        dp = plus_dm / tr * 100
        dm = minus_dm / tr * 100
        s = dp + dm
        dx.append(abs(dp - dm) / s * 100 if s else 0.0)
        di_p.append(dp); di_m.append(dm)
    if not dx:
        return None, None, None
    w = dx[-period:]
    wp = di_p[-period:]
    wm = di_m[-period:]
    return sum(w) / len(w), sum(wp) / len(wp), sum(wm) / len(wm)


def _rsi_last(closes: Sequence[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    seg = closes[-(period * 3 + 1):] if len(closes) > period * 3 + 1 else closes
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = seg[i] - seg[i - 1]
        gains += d if d > 0 else 0.0
        losses += -d if d < 0 else 0.0
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(seg)):
        d = seg[i] - seg[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0.0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0.0)) / period
    if al <= 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _bb_last(closes: Sequence[float], n: int = 20, k: float = 2.0):
    if len(closes) < n:
        return None, None, None
    w = list(closes[-n:])
    mid = sum(w) / n
    sd = (sum((x - mid) ** 2 for x in w) / n) ** 0.5
    return mid - k * sd, mid, mid + k * sd


def _highest(vals: Sequence[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return max(vals[-n:])


def _lowest(vals: Sequence[float], n: int) -> float | None:
    if len(vals) < n:
        return None
    return min(vals[-n:])


# ============================================================
# Режим-детектор (ADX + Efficiency Ratio + ATR ratio)
# ============================================================
@dataclass(frozen=True)
class RegimeResult:
    regime: str  # TRENDING, CHOPPY, HIGH_VOLATILITY, TRANSITIONING
    adx: float | None
    efficiency_ratio: float
    atr_ratio: float
    risk_multiplier: float


def detect_regime(candles: Sequence[Candle]) -> RegimeResult:
    closes = [float(c.close) for c in candles]
    adx, _, _ = _adx_last(candles, 14)

    er_period = 10
    if len(closes) > er_period:
        net_change = abs(closes[-1] - closes[-er_period - 1])
        total_change = sum(abs(closes[i] - closes[i - 1])
                           for i in range(len(closes) - er_period, len(closes)))
        efficiency_ratio = net_change / total_change if total_change > 0 else 0.0
    else:
        efficiency_ratio = 0.0

    atr = _atr_last(candles, 14)
    atr_sma50 = None
    if len(candles) >= 65:
        # только хвост (O(64)) — иначе O(N) на каждый бар = O(N²) на прогон
        seg = candles[-64:]
        atrs = _atr_series(seg, 14)
        tail = atrs[-50:]
        if tail:
            atr_sma50 = sum(tail) / len(tail)
    if atr and atr_sma50 and atr_sma50 > 0:
        atr_ratio = atr / atr_sma50
    else:
        atr_ratio = 1.0

    if atr_ratio > 1.8:
        regime, risk_multiplier = "HIGH_VOLATILITY", 0.0
    elif adx is not None and adx > 25 and efficiency_ratio > 0.7:
        regime, risk_multiplier = "TRENDING", 1.0
    elif adx is not None and adx < 20 and efficiency_ratio < 0.4:
        regime, risk_multiplier = "CHOPPY", 0.7
    else:
        regime, risk_multiplier = "TRANSITIONING", 0.5

    return RegimeResult(regime, adx, efficiency_ratio, atr_ratio, risk_multiplier)


# ============================================================
# Exit-менеджер
# ============================================================
class ExitReason(Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    SIGNAL_REVERSAL = "signal_reversal"
    TIME_EXIT = "time_exit"
    REGIME_CHANGE = "regime_change"


@dataclass
class ExitDecision:
    exit: bool
    reason: ExitReason | None
    price: float | None
    fraction: float = 1.0


@dataclass
class Position:
    side: Side
    entry_price: float
    entry_bar: int
    entry_time: object
    strategy_id: str
    stop_loss: float
    take_profit: float | None
    trailing_stop: float | None
    bars_in_trade: int = 0
    highest_price: float | None = None
    lowest_price: float | None = None
    tp1_hit: bool = False
    entry_regime: str = ""


class ExitManager:
    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.default_atr_mult_stop = float(self.params.get("default_atr_mult_stop", 2.0))
        self.default_atr_mult_trail = float(self.params.get("default_atr_mult_trail", 2.5))
        self.default_risk_reward = float(self.params.get("default_risk_reward", 2.0))
        self.max_bars_in_trade = int(self.params.get("max_bars_in_trade", 40))
        self.enable_trailing = bool(self.params.get("enable_trailing", True))
        self.enable_time_exit = bool(self.params.get("enable_time_exit", True))
        self.enable_regime_exit = bool(self.params.get("enable_regime_exit", True))

    def create_position(self, signal: Signal, entry_price: float, entry_bar: int,
                        atr: float | None = None, regime: str = "") -> Position:
        side = signal.side
        stop_distance = atr * self.default_atr_mult_stop if atr is not None else entry_price * 0.02
        if side == Side.BUY:
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + stop_distance * self.default_risk_reward if self.default_risk_reward > 0 else None
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - stop_distance * self.default_risk_reward if self.default_risk_reward > 0 else None
        trailing_stop = None
        if self.enable_trailing and signal.reason in ("trend_long", "trend_short", "breakout_long", "breakout_short"):
            trailing_stop = stop_loss
        return Position(
            side=side, entry_price=entry_price, entry_bar=entry_bar, entry_time=signal.time,
            strategy_id=signal.strategy_id, stop_loss=stop_loss, take_profit=take_profit,
            trailing_stop=trailing_stop,
            highest_price=entry_price if side == Side.BUY else None,
            lowest_price=entry_price if side == Side.SELL else None,
            entry_regime=regime,
        )

    def check_exit(self, position: Position, candle: Candle, bar_index: int,
                   current_regime: RegimeResult, candles: Sequence[Candle]) -> ExitDecision:
        close = float(candle.close)
        high = float(candle.high)
        low = float(candle.low)
        position.bars_in_trade += 1

        if position.side == Side.BUY:
            if position.highest_price is None or high > position.highest_price:
                position.highest_price = high
        else:
            if position.lowest_price is None or low < position.lowest_price:
                position.lowest_price = low

        # 1) SL по High/Low
        if position.side == Side.BUY:
            if low <= position.stop_loss:
                return ExitDecision(True, ExitReason.STOP_LOSS, position.stop_loss)
        else:
            if high >= position.stop_loss:
                return ExitDecision(True, ExitReason.STOP_LOSS, position.stop_loss)

        # 2) TP
        if position.take_profit is not None:
            if position.side == Side.BUY:
                if high >= position.take_profit:
                    return ExitDecision(True, ExitReason.TAKE_PROFIT, position.take_profit)
            else:
                if low <= position.take_profit:
                    return ExitDecision(True, ExitReason.TAKE_PROFIT, position.take_profit)

        # 3) Trailing
        if self.enable_trailing and position.trailing_stop is not None:
            atr = _atr_last(candles, 14)
            if atr is not None:
                if position.side == Side.BUY and position.highest_price is not None:
                    nt = position.highest_price - atr * self.default_atr_mult_trail
                    if nt > position.trailing_stop:
                        position.trailing_stop = nt
                    if low <= position.trailing_stop:
                        return ExitDecision(True, ExitReason.TRAILING_STOP, position.trailing_stop)
                elif position.side == Side.SELL and position.lowest_price is not None:
                    nt = position.lowest_price + atr * self.default_atr_mult_trail
                    if position.trailing_stop is None or nt < position.trailing_stop:
                        position.trailing_stop = nt
                    if high >= position.trailing_stop:
                        return ExitDecision(True, ExitReason.TRAILING_STOP, position.trailing_stop)

        # 4) Time exit
        if self.enable_time_exit and position.bars_in_trade >= self.max_bars_in_trade:
            return ExitDecision(True, ExitReason.TIME_EXIT, close)

        # 5) Regime change
        if self.enable_regime_exit:
            if current_regime.regime == "HIGH_VOLATILITY" and position.entry_regime != "HIGH_VOLATILITY":
                return ExitDecision(True, ExitReason.REGIME_CHANGE, close)
            if position.entry_regime == "TRENDING" and current_regime.regime == "CHOPPY":
                return ExitDecision(True, ExitReason.REGIME_CHANGE, close)

        return ExitDecision(False, None, None)


# ============================================================
# Стратегии
# ============================================================
class TrendFollowingStrategy:
    strategy_id = "trend_following"
    version = "1.0.0"

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.sma_fast = int(self.params.get("sma_fast", 50))
        self.sma_slow = int(self.params.get("sma_slow", 200))
        self.sma_exit = int(self.params.get("sma_exit", 20))
        self.adx_min = float(self.params.get("adx_min", 25.0))

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle], regime: RegimeResult) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        if regime.regime == "HIGH_VOLATILITY":
            return None
        if regime.regime == "TRANSITIONING" and regime.risk_multiplier < 0.6:
            return None
        closes = [float(c.close) for c in candles]
        sma_fast = _sma(closes, self.sma_fast)
        sma_slow = _sma(closes, self.sma_slow)
        adx, _, _ = _adx_last(candles, 14)
        if sma_fast is None or sma_slow is None or adx is None:
            return None
        close = closes[-1]
        feat = {"regime": regime.regime, "risk_multiplier": regime.risk_multiplier,
                "adx": adx, "efficiency_ratio": regime.efficiency_ratio}
        if close > sma_fast > sma_slow and adx > self.adx_min:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=candles[-1].ts,
                          reason="trend_long", features=feat)
        if close < sma_fast < sma_slow and adx > self.adx_min:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=candles[-1].ts,
                          reason="trend_short", features=feat)
        return None

    def check_exit_signal(self, candles: Sequence[Candle], position: Position) -> ExitDecision | None:
        closes = [float(c.close) for c in candles]
        sma_exit = _sma(closes, self.sma_exit)
        if sma_exit is None:
            return None
        close = closes[-1]
        if position.side == Side.BUY and close < sma_exit:
            return ExitDecision(True, ExitReason.SIGNAL_REVERSAL, close)
        if position.side == Side.SELL and close > sma_exit:
            return ExitDecision(True, ExitReason.SIGNAL_REVERSAL, close)
        return None


class MeanReversionStrategy:
    strategy_id = "mean_reversion"
    version = "1.0.0"

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.rsi_oversold = float(self.params.get("rsi_oversold", 30.0))
        self.rsi_overbought = float(self.params.get("rsi_overbought", 70.0))
        self.bb_period = int(self.params.get("bb_period", 20))
        self.bb_std = float(self.params.get("bb_std", 2.0))
        self.exit_bb_mid = bool(self.params.get("exit_bb_mid", True))

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle], regime: RegimeResult) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        if regime.regime != "CHOPPY":
            return None
        closes = [float(c.close) for c in candles]
        rsi = _rsi_last(closes, self.rsi_period)
        lower, mid, upper = _bb_last(closes, self.bb_period, self.bb_std)
        if rsi is None or lower is None or upper is None:
            return None
        close = closes[-1]
        feat = {"regime": regime.regime, "risk_multiplier": regime.risk_multiplier, "rsi": rsi}
        if rsi < self.rsi_oversold and close < lower:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=candles[-1].ts,
                          reason="mr_long", features=feat)
        if rsi > self.rsi_overbought and close > upper:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=candles[-1].ts,
                          reason="mr_short", features=feat)
        return None

    def check_exit_signal(self, candles: Sequence[Candle], position: Position) -> ExitDecision | None:
        closes = [float(c.close) for c in candles]
        _, mid, _ = _bb_last(closes, self.bb_period, self.bb_std)
        if mid is None:
            return None
        close = closes[-1]
        if self.exit_bb_mid:
            if position.side == Side.BUY and close >= mid:
                return ExitDecision(True, ExitReason.SIGNAL_REVERSAL, close)
            if position.side == Side.SELL and close <= mid:
                return ExitDecision(True, ExitReason.SIGNAL_REVERSAL, close)
        return None


class BreakoutStrategy:
    strategy_id = "breakout"
    version = "1.0.0"

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.bb_period = int(self.params.get("bb_period", 20))
        self.bb_std = float(self.params.get("bb_std", 2.0))
        self.bb_width_sma = int(self.params.get("bb_width_sma", 60))
        self.breakout_period = int(self.params.get("breakout_period", 20))
        self.atr_mult_stop = float(self.params.get("atr_mult_stop", 2.0))

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle], regime: RegimeResult) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        if regime.regime not in ("TRANSITIONING", "TRENDING"):
            return None
        closes = [float(c.close) for c in candles]
        highs = [float(c.high) for c in candles]
        lows = [float(c.low) for c in candles]
        lower, mid, upper = _bb_last(closes, self.bb_period, self.bb_std)
        if lower is None or upper is None or mid is None or mid == 0:
            return None
        bb_width = (upper - lower) / mid
        if len(candles) < self.bb_width_sma + self.bb_period:
            return None
        bb_widths = []
        for i in range(self.bb_width_sma):
            idx = len(candles) - self.bb_width_sma + i
            sl = closes[idx:idx + self.bb_period]
            if len(sl) == self.bb_period:
                l, m, u = _bb_last(sl, self.bb_period, self.bb_std)
                if l is not None and u is not None and m is not None and m > 0:
                    bb_widths.append((u - l) / m)
        if len(bb_widths) < self.bb_width_sma:
            return None
        bb_width_sma = sum(bb_widths) / len(bb_widths)
        if bb_width >= 0.9 * bb_width_sma:
            return None
        highest_high = _highest(highs, self.breakout_period)
        lowest_low = _lowest(lows, self.breakout_period)
        if highest_high is None or lowest_low is None:
            return None
        close = closes[-1]
        feat = {"regime": regime.regime, "risk_multiplier": regime.risk_multiplier,
                "bb_width": bb_width, "bb_width_sma": bb_width_sma}
        if close > highest_high:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=candles[-1].ts,
                          reason="breakout_long", features=feat)
        if close < lowest_low:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=candles[-1].ts,
                          reason="breakout_short", features=feat)
        return None

    def check_exit_signal(self, candles: Sequence[Candle], position: Position) -> ExitDecision | None:
        return None


def create_strategies_by_regime(regime: str, params: dict | None = None) -> list:
    params = params or {}
    out = []
    if regime in ("TRENDING", "TRANSITIONING"):
        out.append(TrendFollowingStrategy(params))
        out.append(BreakoutStrategy(params))
    elif regime == "CHOPPY":
        out.append(MeanReversionStrategy(params))
    return out


class RegimeFilteredStrategies:
    """Единая точка входа: детект режима -> стратегии -> сигналы; + exits."""

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.exit_manager = ExitManager(params)
        self.trend_strategy = TrendFollowingStrategy(params)
        self.mr_strategy = MeanReversionStrategy(params)
        self.breakout_strategy = BreakoutStrategy(params)

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle]) -> list[Signal]:
        if len(candles) < self.warmup_bars():
            return []
        regime = detect_regime(candles)
        if regime.regime == "HIGH_VOLATILITY":
            return []
        out = []
        for s in (self.trend_strategy, self.mr_strategy, self.breakout_strategy):
            sig = s.on_bar(candles, regime)
            if sig:
                out.append(sig)
        return out
