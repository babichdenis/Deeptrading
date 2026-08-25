from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from app.engine.models import Candle, Signal, Side
from app.engine.wave1 import (
    BollingerReclaimParams,
    BollingerReclaimStrategy,
    PullbackEmaParams,
    PullbackEmaStrategy,
    RsiReversalParams,
    RsiReversalStrategy,
    SqueezeBreakoutParams,
    SqueezeBreakoutStrategy,
    VwapReclaimParams,
    VwapReclaimStrategy,
)


@dataclass(frozen=True)
class MacdCrossParams:
    fast: int = 12
    slow: int = 26
    signal_period: int = 9


class MacdCrossStrategy:
    strategy_id = "macd_cross"
    version = "1.0.0"

    def __init__(self, params: MacdCrossParams | None = None):
        self.params = params or MacdCrossParams()
        p = self.params
        self._kf = 2 / (p.fast + 1)
        self._ks = 2 / (p.slow + 1)
        self._kg = 2 / (p.signal_period + 1)
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._ema_signal: float | None = None
        self._prev_hist: float | None = None
        self._count = 0

    def warmup_bars(self) -> int:
        return self.params.slow + self.params.signal_period

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        close = candles[-1].close
        self._count += 1

        if self._ema_fast is None:
            self._ema_fast = close
            self._ema_slow = close
        else:
            self._ema_fast = close * self._kf + self._ema_fast * (1 - self._kf)
            self._ema_slow = close * self._ks + self._ema_slow * (1 - self._ks)

        macd = self._ema_fast - self._ema_slow
        if self._count >= self.params.slow:
            if self._ema_signal is None:
                self._ema_signal = macd
            else:
                self._ema_signal = macd * self._kg + self._ema_signal * (1 - self._kg)

        hist = None if self._ema_signal is None else macd - self._ema_signal

        signal_out: Signal | None = None
        if self._prev_hist is not None and hist is not None:
            features = {"macd": macd, "signal": self._ema_signal, "hist": hist}
            if self._prev_hist <= 0 < hist:
                signal_out = Signal(
                    strategy_id=self.strategy_id,
                    side=Side.BUY,
                    time=candles[-1].ts,
                    reason="bullish_cross",
                    features=features,
                )
            elif self._prev_hist >= 0 > hist:
                signal_out = Signal(
                    strategy_id=self.strategy_id,
                    side=Side.SELL,
                    time=candles[-1].ts,
                    reason="bearish_cross",
                    features=features,
                )

        if hist is not None:
            self._prev_hist = hist
        return signal_out


@dataclass(frozen=True)
class DonchianParams:
    period: int = 20


class DonchianBreakoutStrategy:
    strategy_id = "donchian_breakout"
    version = "1.0.0"

    def __init__(self, params: DonchianParams | None = None):
        self.params = params or DonchianParams()

    def warmup_bars(self) -> int:
        return self.params.period + 1

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        n = len(candles)
        period = self.params.period
        if n < period + 1:
            return None
        window = candles[n - period - 1 : n - 1]
        highest = max(b.high for b in window)
        lowest = min(b.low for b in window)
        last = candles[-1]
        features = {"donchian_high": highest, "donchian_low": lowest}
        if last.close > highest:
            return Signal(
                strategy_id=self.strategy_id,
                side=Side.BUY,
                time=last.ts,
                reason="breakout_up",
                features=features,
            )
        if last.close < lowest:
            return Signal(
                strategy_id=self.strategy_id,
                side=Side.SELL,
                time=last.ts,
                reason="breakdown",
                features=features,
            )
        return None


STRATEGY_REGISTRY: dict[str, type] = {
    "macd_cross": MacdCrossStrategy,
    "donchian_breakout": DonchianBreakoutStrategy,
    "rsi_reversal": RsiReversalStrategy,
    "bollinger_reclaim": BollingerReclaimStrategy,
    "pullback_ema": PullbackEmaStrategy,
    "vwap_reclaim": VwapReclaimStrategy,
    "range_compression_breakout": SqueezeBreakoutStrategy,
}

_PARAMS_BY_STRATEGY: dict[str, type] = {
    "macd_cross": MacdCrossParams,
    "donchian_breakout": DonchianParams,
    "rsi_reversal": RsiReversalParams,
    "bollinger_reclaim": BollingerReclaimParams,
    "pullback_ema": PullbackEmaParams,
    "vwap_reclaim": VwapReclaimParams,
    "range_compression_breakout": SqueezeBreakoutParams,
}


class ParamValidationError(ValueError):
    pass


def validate_params(strategy_id: str, params: dict | None) -> dict:
    from app.engine.catalog import STRATEGY_CATALOG

    card = STRATEGY_CATALOG.get(strategy_id)
    if card is None:
        raise ParamValidationError(f"unknown strategy: {strategy_id}")
    incoming = dict(params or {})
    validated: dict = {}
    for key, spec in card.params_schema.items():
        value = incoming.pop(key, spec.get("default"))
        if value is None:
            raise ParamValidationError(f"missing param {key}")
        expected = spec["type"]
        try:
            if expected == "int":
                value = int(value)
            elif expected == "float":
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise ParamValidationError(f"param {key}: wrong type, expected {expected}")
        if "min" in spec and value < spec["min"]:
            raise ParamValidationError(f"param {key}={value} < min {spec['min']}")
        if "max" in spec and value > spec["max"]:
            raise ParamValidationError(f"param {key}={value} > max {spec['max']}")
        validated[key] = value
    if incoming:
        raise ParamValidationError(f"unknown params: {sorted(incoming)}")
    return validated


def build_strategy(strategy_id: str, params: dict | None):
    cls = STRATEGY_REGISTRY.get(strategy_id)
    if cls is None:
        raise ParamValidationError(f"unknown strategy: {strategy_id}")
    validated = validate_params(strategy_id, params)
    params_cls = _PARAMS_BY_STRATEGY[strategy_id]
    return cls(params_cls(**validated))
