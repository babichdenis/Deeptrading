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
    "ensemble_vote": StrategyCard(
        id="ensemble_vote", name="Ensemble Vote (10 функций)", family="ensemble", wave=4,
        long_rule=">=min_votes функций за BUY и больше, чем за SELL",
        short_rule=">=min_votes функций за SELL и больше, чем за BUY",
        timeframes=("1min",), status="AVAILABLE",
        params_schema={
            "min_votes": {"type": "int", "default": 3, "min": 1, "max": 10},
            "lookback": {"type": "int", "default": 5, "min": 1, "max": 20},
            "overext_mult": {"type": "float", "default": 3.0, "min": 0.5, "max": 8},
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
    "ose_all": StrategyCard(
        id="ose_all", name="OSEngine All Robots (15 votes)", family="ose", wave=5,
        long_rule="голос любого робота за лонг (quorum, дефолт 1)",
        short_rule="голос любого робота за шорт",
        timeframes=("1min", "5min", "10min"), status="AVAILABLE",
        params_schema={
            "quorum": {"type": "int", "default": 1, "min": 1, "max": 15},
            "sma_stoch_step_pct": {"type": "float", "default": 1.0, "min": 0.0, "max": 10.0},
            "members": {"type": "str", "default": "",
                        "desc": "CSV роботов (пусто = все 15); напр. price_channel,rsi_trade"},
            "er_length": {"type": "int", "default": 10, "min": 1, "max": 100},
            "er_min": {"type": "float", "default": 0.0, "min": 0.0, "max": 1.0},
        },
    ),
    "ose_price_channel": StrategyCard(
        id="ose_price_channel", name="OSEngine Price Channel", family="ose", wave=5,
        long_rule="high > канал вверх[−2] (пробой)",
        short_rule="low < канал вниз[−2] (пробой)",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "length_up": {"type": "int", "default": 21, "min": 2, "max": 200},
            "length_down": {"type": "int", "default": 21, "min": 2, "max": 200},
        },
    ),
    "ose_sma_stoch": StrategyCard(
        id="ose_sma_stoch", name="OSEngine SMA Stochastic", family="ose", wave=5,
        long_rule="close>SMA+step & %K пересёк downline снизу вверх",
        short_rule="close<SMA−step & %K пересёк upline сверху вниз",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "sma_stoch_step_pct": {"type": "float", "default": 1.0, "min": 0.0, "max": 10.0},
        },
    ),
    "ose_envelop_trend": StrategyCard(
        id="ose_envelop_trend", name="OSEngine Envelop Trend", family="ose", wave=5,
        long_rule="стоп-заявка по верхней полосе конвертов + трейлинг",
        short_rule="стоп-заявка по нижней полосе конвертов + трейлинг",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "length": {"type": "int", "default": 10, "min": 2, "max": 100},
            "deviation": {"type": "float", "default": 0.3, "min": 0.05, "max": 5.0},
            "trail_stop": {"type": "float", "default": 0.1, "min": 0.01, "max": 5.0},
        },
    ),
    "ose_rsi_contrtrend": StrategyCard(
        id="ose_rsi_contrtrend", name="OSEngine RSI Contrtrend", family="ose", wave=5,
        long_rule="Sma<Close & RSI<downline (контртренд)",
        short_rule="Sma>Close & RSI>upline (контртренд)",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "sma_length": {"type": "int", "default": 50, "min": 5, "max": 200},
            "rsi_length": {"type": "int", "default": 20, "min": 5, "max": 100},
            "upline": {"type": "float", "default": 65.0, "min": 50.0, "max": 95.0},
            "downline": {"type": "float", "default": 35.0, "min": 5.0, "max": 50.0},
        },
    ),
    "ose_rsi_trade": StrategyCard(
        id="ose_rsi_trade", name="OSEngine RSI Trade", family="ose", wave=5,
        long_rule="RSI пересёк downline снизу вверх",
        short_rule="RSI пересёк upline сверху вниз",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "rsi_length": {"type": "int", "default": 20, "min": 5, "max": 100},
            "upline": {"type": "float", "default": 65.0, "min": 50.0, "max": 95.0},
            "downline": {"type": "float", "default": 35.0, "min": 5.0, "max": 50.0},
        },
    ),
    "ose_bollinger": StrategyCard(
        id="ose_bollinger", name="OSEngine Bollinger", family="ose", wave=5,
        long_rule="close < нижней полосы BB (контртренд)",
        short_rule="close > верхней полосы BB (контртренд)",
        timeframes=("1min", "5min"), status="AVAILABLE",
        params_schema={
            "boll_length": {"type": "int", "default": 21, "min": 5, "max": 100},
            "boll_deviation": {"type": "float", "default": 2.0, "min": 0.5, "max": 5.0},
            "sma_length": {"type": "int", "default": 15, "min": 2, "max": 100},
        },
    ),
    "rsi_trade_hub": StrategyCard(
        id="rsi_trade_hub", name="RSI Trade (hub-канон)", family="momentum", wave=5,
        long_rule="Wilder-RSI пересёк downline снизу вверх (канон IndicatorHub)",
        short_rule="Wilder-RSI пересёк upline сверху вниз",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "rsi_length": {"type": "int", "default": 20, "min": 5, "max": 100},
            "upline": {"type": "float", "default": 65.0, "min": 50.0, "max": 95.0},
            "downline": {"type": "float", "default": 35.0, "min": 5.0, "max": 50.0},
        },
    ),
    "rsi_mtf_hub": StrategyCard(
        id="rsi_mtf_hub", name="RSI MTF (1h режим → 10m вход)", family="momentum", wave=5,
        long_rule="RSI_h ≥ 50+gap и RSI_10m пересёк downline снизу вверх",
        short_rule="RSI_h ≤ 50−gap и RSI_10m пересёк upline сверху вниз",
        timeframes=("10min", "15min"), status="AVAILABLE",
        params_schema={
            "rsi_length": {"type": "int", "default": 20, "min": 5, "max": 100},
            "upline": {"type": "float", "default": 65.0, "min": 50.0, "max": 95.0},
            "downline": {"type": "float", "default": 35.0, "min": 5.0, "max": 50.0},
            "bias_length": {"type": "int", "default": 20, "min": 5, "max": 100},
            "bias_tf_min": {"type": "int", "default": 60, "min": 10, "max": 240},
            "bias_gap": {"type": "float", "default": 2.0, "min": 0.0, "max": 20.0},
        },
    ),
    "envelop_trend_hub": StrategyCard(
        id="envelop_trend_hub", name="Envelop Trend (hub-канон)", family="trend", wave=5,
        long_rule="касание верхнего конверта (канон IndicatorHub) + трейлинг",
        short_rule="касание нижнего конверта + трейлинг",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "length": {"type": "int", "default": 10, "min": 2, "max": 100},
            "deviation": {"type": "float", "default": 0.3, "min": 0.05, "max": 5.0},
            "trail_stop": {"type": "float", "default": 0.1, "min": 0.0, "max": 5.0},
        },
    ),
    "canon_ensemble": StrategyCard(
        id="canon_ensemble", name="Канон-ансамбль (кворум)", family="ensemble", wave=5,
        long_rule=">= quorum канонных членов за BUY и больше противоположных",
        short_rule=">= quorum членов за SELL",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "members": {"type": "str", "default": "rsi_trade_hub",
                        "desc": "CSV strategy_id через запятую"},
            "quorum": {"type": "int", "default": 1, "min": 1, "max": 9},
        },
    ),
    "williams_range_hub": StrategyCard(
        id="williams_range_hub", name="Williams %R Trade (hub-канон)", family="momentum", wave=6,
        long_rule="%R пересёк downline (-80) сверху вниз (OsEngine WilliamsRangeTrade)",
        short_rule="%R пересёк upline (-20) снизу вверх",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "wr_length": {"type": "int", "default": 14, "min": 2, "max": 100},
            "upline": {"type": "float", "default": -20.0, "min": -50.0, "max": -5.0},
            "downline": {"type": "float", "default": -80.0, "min": -95.0, "max": -50.0},
        },
    ),
    "momentum_macd_hub": StrategyCard(
        id="momentum_macd_hub", name="Momentum + MACD (hub-канон)", family="momentum", wave=6,
        long_rule="MACD > signal и Momentum(Close, len) > 100 — переход условия в True",
        short_rule="MACD < signal и Momentum < 100 — переход условия в True",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "momentum_length": {"type": "int", "default": 5, "min": 1, "max": 50},
            "macd_fast": {"type": "int", "default": 12, "min": 2, "max": 50},
            "macd_slow": {"type": "int", "default": 26, "min": 3, "max": 100},
            "macd_signal": {"type": "int", "default": 9, "min": 1, "max": 50},
        },
    ),
    "parabolic_sar_hub": StrategyCard(
        id="parabolic_sar_hub", name="Parabolic SAR Trade (hub-канон)", family="trend", wave=6,
        long_rule="SAR-тренд перевернулся вверх (цена выше SAR; OsEngine ParabolicSarTrade)",
        short_rule="SAR-тренд перевернулся вниз (цена ниже SAR)",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "af": {"type": "float", "default": 0.02, "min": 0.01, "max": 0.1},
            "max_af": {"type": "float", "default": 0.2, "min": 0.05, "max": 0.5},
        },
    ),
    "price_channel_hub": StrategyCard(
        id="price_channel_hub", name="Price Channel Trade (hub-канон)", family="trend", wave=6,
        long_rule="High пробил верх канала (L=21, сдвиг [-2]); предыдущий бар не пробивал (OsEngine PriceChannelTrade)",
        short_rule="Low пробил низ канала; предыдущий бар не пробивал",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "length_up": {"type": "int", "default": 21, "min": 2, "max": 200},
            "length_down": {"type": "int", "default": 21, "min": 2, "max": 200},
        },
    ),
    "parabolic_bollinger_hub": StrategyCard(
        id="parabolic_bollinger_hub", name="Parabolic Bollinger (hub-канон)", family="trend", wave=6,
        long_rule="Крест касания верхней BB-границы при параболике P строго внутри полос (OsEngine ParabolicBollinger)",
        short_rule="Крест касания нижней BB-границы при P внутри полос",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "bb_length": {"type": "int", "default": 28, "min": 5, "max": 200},
            "deviation": {"type": "float", "default": 2.0, "min": 0.5, "max": 5.0},
            "averaging": {"type": "int", "default": 15, "min": 2, "max": 100},
            "vol_mult": {"type": "float", "default": 0.2, "min": 0.01, "max": 2.0},
        },
    ),
    "parabolic_price_channel_hub": StrategyCard(
        id="parabolic_price_channel_hub", name="Parabolic Price Channel (hub-канон)", family="trend", wave=6,
        long_rule="Крест касания верхней границы канала при параболике P строго внутри (OsEngine ParabolicPriceChannel)",
        short_rule="Крест касания нижней границы канала при P внутри",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "length_up": {"type": "int", "default": 21, "min": 2, "max": 200},
            "length_down": {"type": "int", "default": 21, "min": 2, "max": 200},
            "averaging": {"type": "int", "default": 15, "min": 2, "max": 100},
            "vol_mult": {"type": "float", "default": 0.1, "min": 0.01, "max": 2.0},
        },
    ),
    "break_lr_channel_hub": StrategyCard(
        id="break_lr_channel_hub", name="Break Linear Regression Channel (hub-канон)", family="trend", wave=6,
        long_rule="Close пробил верх LRC (period 50, dev 1.0); опц. фильтры SMA — позиция/наклон (OsEngine BreakLinearRegressionChannel)",
        short_rule="Close пробил низ LRC; выход — стоп на противоположной границе уровня предыдущего бара",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "lr_period": {"type": "int", "default": 50, "min": 5, "max": 300},
            "up_deviation": {"type": "float", "default": 1.0, "min": 0.1, "max": 5.0},
            "down_deviation": {"type": "float", "default": 1.0, "min": 0.1, "max": 5.0},
            "sma_length": {"type": "int", "default": 100, "min": 2, "max": 300},
            "sma_position_filter": {"type": "bool", "default": False},
            "sma_slope_filter": {"type": "bool", "default": False},
        },
    ),
    "bill_williams_hub": StrategyCard(
        id="bill_williams_hub", name="Bill Williams (hub-канон)", family="trend", wave=6,
        long_rule="Close строго выше Lips/Teeth/Jaw (SSMA 3/10/40, сдвиги 3/5/8) и выше последнего верхнего фрактала (OsEngine StrategyBillWilliams)",
        short_rule="Close ниже всех линий Alligator и ниже нижнего фрактала; выход — против Teeth",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "jaw_length": {"type": "int", "default": 40, "min": 5, "max": 200},
            "teeth_length": {"type": "int", "default": 10, "min": 3, "max": 100},
            "lips_length": {"type": "int", "default": 3, "min": 2, "max": 50},
            "jaw_shift": {"type": "int", "default": 8, "min": 0, "max": 50},
            "teeth_shift": {"type": "int", "default": 5, "min": 0, "max": 50},
            "lips_shift": {"type": "int", "default": 3, "min": 0, "max": 50},
        },
    ),
    "two_timeframes_hub": StrategyCard(
        id="two_timeframes_hub", name="Two Time Frames (hub-канон)", family="trend", wave=6,
        long_rule="Close > верх PriceChannel пред. бара И close старшего ТФ > SMA (long-only, OsEngine TwoTimeFramesBot)",
        short_rule="— long-only; выход: Close < низ PriceChannel",
        timeframes=("5min", "15min"), status="AVAILABLE",
        params_schema={
            "pc_length": {"type": "int", "default": 20, "min": 2, "max": 200},
            "sma_length": {"type": "int", "default": 30, "min": 1, "max": 200},
            "big_tf_min": {"type": "int", "default": 60, "min": 10, "max": 1440},
        },
    ),
}


def canonical_params_hash(params: dict) -> str:
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
