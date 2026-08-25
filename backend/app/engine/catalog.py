from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ParamSpec:
    type: str
    default: int | float | str
    min: float | None = None
    max: float | None = None


@dataclass(frozen=True)
class StrategyCard:
    id: str
    name: str
    family: str
    wave: int
    long_rule: str
    short_rule: str
    timeframes: tuple[str, ...]
    status: str = "DRAFT"
    params_schema: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timeframes"] = list(self.timeframes)
        return d


STRATEGY_CATALOG: dict[str, StrategyCard] = {
    "rsi_reversal": StrategyCard(
        id="rsi_reversal",
        name="RSI Reversal",
        family="reversal",
        wave=1,
        long_rule="RSI<oversold & RSI↑ & close>high[t-1]",
        short_rule="RSI>overbought & RSI↓ & close<low[t-1]",
        timeframes=("15min", "hour", "day"),
        status="AVAILABLE",
        params_schema={
            "period": {"type": "int", "default": 14, "min": 2, "max": 100},
            "oversold": {"type": "float", "default": 35, "min": 5, "max": 50},
            "overbought": {"type": "float", "default": 65, "min": 50, "max": 95},
        },
    ),
    "bollinger_reclaim": StrategyCard(
        id="bollinger_reclaim",
        name="Bollinger Reclaim",
        family="reversal",
        wave=1,
        long_rule="close[t-1]<lower band & close[t]>lower band",
        short_rule="close[t-1]>upper band & close[t]<upper band",
        timeframes=("15min", "hour", "day"),
        status="AVAILABLE",
        params_schema={
            "period": {"type": "int", "default": 20, "min": 5, "max": 200},
            "k": {"type": "float", "default": 2.0, "min": 0.5, "max": 4},
        },
    ),
    "pullback_ema": StrategyCard(
        id="pullback_ema",
        name="Pullback to EMA",
        family="pullback",
        wave=1,
        long_rule="EMA50↑ & price>EMA50 & low≤EMA20 & close>high[t-1]",
        short_rule="EMA50↓ & price<EMA50 & high≥EMA20 & close<low[t-1]",
        timeframes=("hour", "day"),
        status="AVAILABLE",
        params_schema={
            "trend_ema": {"type": "int", "default": 50, "min": 10, "max": 300},
            "pull_ema": {"type": "int", "default": 20, "min": 5, "max": 100},
        },
    ),
    "vwap_reclaim": StrategyCard(
        id="vwap_reclaim",
        name="VWAP Reclaim (intraday)",
        family="reversal",
        wave=1,
        long_rule="price<VWAP−k·σ → close>VWAP",
        short_rule="price>VWAP+k·σ → close<VWAP",
        timeframes=("5min", "15min", "hour"),
        status="AVAILABLE",
        params_schema={
            "k": {"type": "float", "default": 2.0, "min": 0.5, "max": 5},
        },
    ),
    "range_compression_breakout": StrategyCard(
        id="range_compression_breakout",
        name="Range Compression Breakout",
        family="breakout",
        wave=1,
        long_rule="ATR в низшем перцентиле N барсов → close>range high",
        short_rule="→ close<range low",
        timeframes=("15min", "hour", "day"),
        status="AVAILABLE",
        params_schema={
            "lookback": {"type": "int", "default": 20, "min": 5, "max": 100},
            "atr_period": {"type": "int", "default": 14, "min": 5, "max": 50},
            "pct": {"type": "float", "default": 25, "min": 5, "max": 50},
        },
    ),
    "macd_cross": StrategyCard(
        id="macd_cross",
        name="MACD Cross",
        family="momentum",
        wave=3,
        long_rule="MACD пересекает signal снизу вверх",
        short_rule="сверху вниз",
        timeframes=("15min", "hour", "day"),
        status="AVAILABLE",
        params_schema={
            "fast": {"type": "int", "default": 12, "min": 2, "max": 60},
            "slow": {"type": "int", "default": 26, "min": 5, "max": 120},
            "signal_period": {"type": "int", "default": 9, "min": 2, "max": 40},
        },
    ),
    "donchian_breakout": StrategyCard(
        id="donchian_breakout",
        name="Donchian Breakout",
        family="breakout",
        wave=3,
        long_rule="close > max(high[N])",
        short_rule="close < min(low[N])",
        timeframes=("15min", "hour", "day"),
        status="AVAILABLE",
        params_schema={
            "period": {"type": "int", "default": 20, "min": 5, "max": 200},
        },
    ),
}


def canonical_params_hash(params: dict) -> str:
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
