"""Ensemble v2: 10 функций голосуют (M1 + M5), направление = большинство (кворум).

M1 (по 1м): micro_breakout (lookback=5 + фильтр перерастяжения), ema_signal, macd_signal, rsi_signal
M5 (по 5м): donchian, pullback, range, volume, bollinger, atr_breakout

Стратегия регистрируется с tf="1min" и сама ресемплит 5м внутри on_bar.
Направление = большинство: long_votes >= min_votes и > short_votes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.engine.models import Candle, Side, Signal


def _as_dict(p) -> dict:
    if p is None:
        return {}
    if isinstance(p, dict):
        return dict(p)
    try:
        return {k: getattr(p, k) for k in p.__dataclass_fields__}
    except Exception:
        return {}


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


def _atr_last(candles: Sequence[Candle], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None

    def _tr(c: Candle, prev: Candle) -> float:
        return max(float(c.high) - float(c.low),
                   abs(float(c.high) - float(prev.close)),
                   abs(float(c.low) - float(prev.close)))

    trs = [_tr(candles[i], candles[i - 1]) for i in range(len(candles) - period, len(candles))]
    return sum(trs) / len(trs)


def _macd_last(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    if len(closes) < slow + signal:
        return None, None
    a_f = 2 / (fast + 1)
    a_s = 2 / (slow + 1)
    a_g = 2 / (signal + 1)
    ef = es = float(closes[0])
    macd = []
    for v in closes:
        ef = a_f * float(v) + (1 - a_f) * ef
        es = a_s * float(v) + (1 - a_s) * es
        macd.append(ef - es)
    sig = macd[0]
    for v in macd:
        sig = a_g * v + (1 - a_g) * sig
    return macd[-1], sig


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
    return 100 - 100 / (1 + ag / al)


def _bb_last(closes: Sequence[float], n: int = 20, k: float = 2.0):
    if len(closes) < n:
        return None, None, None
    w = list(closes[-n:])
    mid = sum(w) / n
    sd = (sum((x - mid) ** 2 for x in w) / n) ** 0.5
    return mid - k * sd, mid, mid + k * sd


# ============================================================
# M1 функции
# ============================================================
def micro_breakout(candles: Sequence[Candle], lookback: int = 5, overext_mult: float = 3.0) -> Side | None:
    """Пробой за lookback баров + закрытие за уровнем + фильтр перерастяжения."""
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    if len(candles) < lookback + 2:
        return None
    prior_high = max(highs[-lookback - 1:-1])
    prior_low = min(lows[-lookback - 1:-1])
    close = closes[-1]
    atr = _atr_last(candles, 14)
    sma50 = _sma(closes, 50)
    if close > prior_high:
        if sma50 and atr:
            if close <= sma50 + overext_mult * atr:
                return Side.BUY
    if close < prior_low:
        if sma50 and atr:
            if close >= sma50 - overext_mult * atr:
                return Side.SELL
    return None


def ema_signal(candles: Sequence[Candle], buf: float = 0.001) -> Side | None:
    closes = [float(c.close) for c in candles]
    if len(closes) < 50:
        return None
    e20 = _ema_last(closes, 20)
    e50 = _ema_last(closes, 50)
    if e20 is None or e50 is None:
        return None
    if e20 > e50 * (1 + buf):
        return Side.BUY
    if e20 < e50 * (1 - buf):
        return Side.SELL
    return None


def macd_signal(candles: Sequence[Candle]) -> Side | None:
    closes = [float(c.close) for c in candles]
    m, s = _macd_last(closes)
    if m is None or s is None:
        return None
    if m > s:
        return Side.BUY
    if m < s:
        return Side.SELL
    return None


def rsi_signal(candles: Sequence[Candle]) -> Side | None:
    closes = [float(c.close) for c in candles]
    r = _rsi_last(closes, 14)
    if r is None:
        return None
    if r < 30:
        return Side.BUY
    if r > 70:
        return Side.SELL
    return None


# ============================================================
# M5 функции
# ============================================================
def donchian_m5(candles: Sequence[Candle], period: int = 20) -> Side | None:
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    if len(candles) < period + 2:
        return None
    ph = max(highs[-period - 1:-1])
    pl = min(lows[-period - 1:-1])
    close = float(candles[-1].close)
    if close > ph:
        return Side.BUY
    if close < pl:
        return Side.SELL
    return None


def pullback_m5(candles: Sequence[Candle]) -> Side | None:
    closes = [float(c.close) for c in candles]
    if len(closes) < 50:
        return None
    e20 = _ema_last(closes, 20)
    e50 = _ema_last(closes, 50)
    if e20 is None or e50 is None:
        return None
    close = closes[-1]
    if e20 > e50 * 1.001 and abs(close - e20) <= e20 * 0.002:
        return Side.BUY
    if e20 < e50 * 0.999 and abs(close - e20) <= e20 * 0.002:
        return Side.SELL
    return None


def range_breakout_m5(candles: Sequence[Candle], period: int = 20) -> Side | None:
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    if len(candles) < period + 2:
        return None
    rh = max(highs[-period - 1:-1])
    rl = min(lows[-period - 1:-1])
    close = float(candles[-1].close)
    if close > rh:
        return Side.BUY
    if close < rl:
        return Side.SELL
    return None


def volume_breakout_m5(candles: Sequence[Candle]) -> Side | None:
    closes = [float(c.close) for c in candles]
    vols = [float(c.volume or 0.0) for c in candles]
    if len(candles) < 22:
        return None
    vs = _sma(vols, 20)
    if not vs:
        return None
    close = closes[-1]
    prev = closes[-2]
    if vols[-1] > 1.5 * vs:
        if close > prev * 1.002:
            return Side.BUY
        if close < prev * 0.998:
            return Side.SELL
    return None


def bollinger_m5(candles: Sequence[Candle]) -> Side | None:
    closes = [float(c.close) for c in candles]
    lo, mid, up = _bb_last(closes, 20, 2.0)
    if lo is None or up is None:
        return None
    close = closes[-1]
    if close > up:
        return Side.BUY
    if close < lo:
        return Side.SELL
    return None


def atr_breakout_m5(candles: Sequence[Candle]) -> Side | None:
    closes = [float(c.close) for c in candles]
    if len(candles) < 22:
        return None
    atr = _atr_last(candles, 14)
    if not atr:
        return None
    close = closes[-1]
    prev = closes[-2]
    if abs(close - prev) > 1.5 * atr:
        return Side.BUY if close > prev else Side.SELL
    return None


M1_FUNCS = [("micro_breakout", micro_breakout), ("ema_signal", ema_signal),
            ("macd_signal", macd_signal), ("rsi_signal", rsi_signal)]
M5_FUNCS = [("donchian", donchian_m5), ("pullback", pullback_m5), ("range", range_breakout_m5),
            ("volume", volume_breakout_m5), ("bollinger", bollinger_m5), ("atr_breakout", atr_breakout_m5)]


# ============================================================
# Стратегия: голосование 10 функций
# ============================================================
class EnsembleVoteStrategy:
    """10 функций голосуют; направление = большинство (>= min_votes и больше противоположных)."""
    strategy_id = "ensemble_vote"
    version = "2.0.0"

    def __init__(self, params: dict | None = None):
        self.params = _as_dict(params)
        self.min_votes = int(self.params.get("min_votes", 3))
        self.lookback = int(self.params.get("lookback", 5))
        self.overext_mult = float(self.params.get("overext_mult", 3.0))

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        # 5м — ресемплим из 1м внутри
        from app.services.ensemble import resample
        c5 = resample(list(candles), 300)
        if len(c5) < 210:
            return None

        votes = []
        details: dict = {}
        for name, fn in M1_FUNCS:
            try:
                if name == "micro_breakout":
                    s = fn(candles, self.lookback, self.overext_mult)
                else:
                    s = fn(candles)
            except Exception:
                s = None
            if s:
                votes.append(s)
                details[f"m1_{name}"] = s.value
        for name, fn in M5_FUNCS:
            try:
                s = fn(c5)
            except Exception:
                s = None
            if s:
                votes.append(s)
                details[f"m5_{name}"] = s.value

        longs = sum(1 for v in votes if v == Side.BUY)
        shorts = sum(1 for v in votes if v == Side.SELL)
        feat = {"long_votes": longs, "short_votes": shorts, "total_votes": len(votes), **details}
        if longs >= self.min_votes and longs > shorts:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY,
                          time=candles[-1].ts, reason="ensemble_long", features=feat)
        if shorts >= self.min_votes and shorts > longs:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL,
                          time=candles[-1].ts, reason="ensemble_short", features=feat)
        return None


@dataclass(frozen=True)
class EnsembleVoteParams:
    min_votes: int = 3
    lookback: int = 5
    overext_mult: float = 3.0
