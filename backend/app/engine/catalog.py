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
    "stochastic": StrategyCard(
        id="stochastic",
        name="Stochastic",
        family="reversal",
        wave=1,
        long_rule="%K<oversold & %K cross up %D",
        short_rule="%K>overbought & %K cross down %D",
        timeframes=("15min", "hour", "day"),
        status="AVAILABLE",
        params_schema={
            "k_period": {"type": "int", "default": 14, "min": 2, "max": 100},
            "d_period": {"type": "int", "default": 3, "min": 1, "max": 20},
            "oversold": {"type": "float", "default": 20, "min": 5, "max": 50},
            "overbought": {"type": "float", "default": 80, "min": 50, "max": 95},
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
    "volume_drop": StrategyCard(
        id="volume_drop",
        name="Volume on Drop (V4)",
        family="volume",
        wave=3,
        long_rule="—",
        short_rule="close<prev_close & vol_ratio>drop_ratio",
        timeframes=("5min",),
        status="AVAILABLE",
        params_schema={
            "ma_len": {"type": "int", "default": 20, "min": 5, "max": 200},
            "drop_ratio": {"type": "float", "default": 1.5, "min": 0.5, "max": 20},
        },
    ),
    "volume_climax": StrategyCard(
        id="volume_climax",
        name="Volume Climax (V3)",
        family="volume",
        wave=3,
        long_rule="climax_short (нижняя тень) → BUY",
        short_rule="climax_long (верхняя тень) → SELL",
        timeframes=("5min",),
        status="AVAILABLE",
        params_schema={
            "ma_len": {"type": "int", "default": 20, "min": 5, "max": 200},
            "climax_ratio": {"type": "float", "default": 3.0, "min": 1.0, "max": 20},
            "wick_frac": {"type": "float", "default": 0.5, "min": 0.05, "max": 1.0},
        },
    ),
    "volume_divergence": StrategyCard(
        id="volume_divergence",
        name="Volume Divergence (V2/V5)",
        family="volume",
        wave=3,
        long_rule="новый low при объёме ниже среднего → BUY (V5)",
        short_rule="новый high при объёме ниже среднего → SELL (V2)",
        timeframes=("5min",),
        status="AVAILABLE",
        params_schema={
            "div_n": {"type": "int", "default": 20, "min": 5, "max": 200},
        },
    ),
    "trend_up": StrategyCard(
        id="trend_up",
        name="Uptrend Breakout",
        family="trend",
        wave=3,
        long_rule="close>SMA(long) & EMA(fast)>EMA(slow) & close>max(high[N]) & vol>k·avg",
        short_rule="—",
        timeframes=("5min", "15min", "hour"),
        status="AVAILABLE",
        params_schema={
            "sma_long": {"type": "int", "default": 200, "min": 20, "max": 400},
            "ema_fast": {"type": "int", "default": 20, "min": 5, "max": 100},
            "ema_slow": {"type": "int", "default": 50, "min": 10, "max": 300},
            "donchian": {"type": "int", "default": 20, "min": 5, "max": 200},
            "vol_ma": {"type": "int", "default": 20, "min": 5, "max": 200},
            "vol_mult": {"type": "float", "default": 1.5, "min": 0.5, "max": 10},
        },
    ),
    "trend_down": StrategyCard(
        id="trend_down",
        name="Downtrend Breakdown",
        family="trend",
        wave=3,
        long_rule="—",
        short_rule="close<SMA(long) & EMA(fast)<EMA(slow) & close<min(low[N]) & vol>k·avg",
        timeframes=("5min", "15min", "hour"),
        status="AVAILABLE",
        params_schema={
            "sma_long": {"type": "int", "default": 200, "min": 20, "max": 400},
            "ema_fast": {"type": "int", "default": 20, "min": 5, "max": 100},
            "ema_slow": {"type": "int", "default": 50, "min": 10, "max": 300},
            "donchian": {"type": "int", "default": 20, "min": 5, "max": 200},
            "vol_ma": {"type": "int", "default": 20, "min": 5, "max": 200},
            "vol_mult": {"type": "float", "default": 1.5, "min": 0.5, "max": 10},
        },
    ),
    "range_reversion": StrategyCard(
        id="range_reversion",
        name="Range Mean Reversion",
        family="reversion",
        wave=3,
        long_rule="боковик & RSI<oversold & close<=нижняя BB → BUY",
        short_rule="боковик & RSI>overbought & close>=верхняя BB → SELL",
        timeframes=("5min", "15min", "hour"),
        status="AVAILABLE",
        params_schema={
            "lookback": {"type": "int", "default": 20, "min": 5, "max": 200},
            "range_pct": {"type": "float", "default": 6.0, "min": 0.5, "max": 50},
            "rsi_period": {"type": "int", "default": 14, "min": 2, "max": 100},
            "rsi_oversold": {"type": "float", "default": 30.0, "min": 5, "max": 50},
            "rsi_overbought": {"type": "float", "default": 70.0, "min": 50, "max": 95},
            "bb_period": {"type": "int", "default": 20, "min": 5, "max": 200},
            "bb_k": {"type": "float", "default": 2.0, "min": 0.5, "max": 4},
        },
    ),
    "long_ensemble": StrategyCard(
        id="long_ensemble", name="Uptrend Ensemble (4 groups)", family="regime", wave=4,
        long_rule="TrendUp+Breakout+Volume+VolStructure → кворум",
        short_rule="—", timeframes=("5min",), status="AVAILABLE",
        params_schema={
            "regime": {"type": "str", "default": "TREND_UP"},
            "require_breakout": {"type": "int", "default": 1, "min": 0, "max": 1},
            "atr_mult_trail": {"type": "float", "default": 2.5, "min": 0.5, "max": 6},
            "max_bars": {"type": "int", "default": 40, "min": 5, "max": 300},
        },
    ),
    "short_ensemble": StrategyCard(
        id="short_ensemble", name="Downtrend Ensemble (4 groups)", family="regime", wave=4,
        long_rule="—",
        short_rule="TrendDown+Breakdown+Volume+VolStructure → кворум",
        timeframes=("5min",), status="AVAILABLE",
        params_schema={
            "regime": {"type": "str", "default": "TREND_DOWN"},
            "require_breakdown": {"type": "int", "default": 1, "min": 0, "max": 1},
            "atr_mult_trail": {"type": "float", "default": 2.5, "min": 0.5, "max": 6},
            "max_bars": {"type": "int", "default": 40, "min": 5, "max": 300},
        },
    ),
    "range_ensemble": StrategyCard(
        id="range_ensemble", name="Range Ensemble (MR + scalp)", family="regime", wave=4,
        long_rule="MR от нижней BB / скальп вверх (не штиль)",
        short_rule="MR от верхней BB / скальп вниз",
        timeframes=("5min",), status="AVAILABLE",
        params_schema={
            "allow_scalping": {"type": "int", "default": 1, "min": 0, "max": 1},
        },
    ),
    "hv_ensemble": StrategyCard(
        id="hv_ensemble", name="High-Vol Ensemble", family="regime", wave=4,
        long_rule="сильный ап-пробой (тренд+пробой+объём)",
        short_rule="сильный даун-пробой",
        timeframes=("5min",), status="AVAILABLE",
        params_schema={
            "risk_per_trade": {"type": "float", "default": 0.005, "min": 0.0, "max": 0.05},
            "atr_mult_stop": {"type": "float", "default": 2.5, "min": 0.5, "max": 6},
        },
    ),
    "neutral_ensemble": StrategyCard(
        id="neutral_ensemble", name="Neutral Ensemble (strict)", family="regime", wave=4,
        long_rule="кворум 3 + обязательный breakout",
        short_rule="кворум 3 + обязательный breakdown",
        timeframes=("5min",), status="AVAILABLE",
        params_schema={
            "risk_per_trade": {"type": "float", "default": 0.0075, "min": 0.0, "max": 0.05},
            "atr_mult_stop": {"type": "float", "default": 1.75, "min": 0.5, "max": 6},
        },
    ),
    "momentum_1bar": StrategyCard(
        id="momentum_1bar", name="Momentum 1 Bar", family="momentum", wave=4,
        long_rule="close>open (бычья свеча) → BUY",
        short_rule="close<open (медвежья свеча) → SELL",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "min_body_pct": {"type": "float", "default": 0.0, "min": 0.0, "max": 5.0},
        },
    ),
}


def canonical_params_hash(params: dict) -> str:
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
