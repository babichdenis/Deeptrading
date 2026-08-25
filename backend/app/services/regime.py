"""Каузальный rule-based детектор режима рынка.

Режим на каждом баре считается ТОЛЬКО по закрытым барам (никакого look-ahead).
Режимы: RANGE, TREND_UP, TREND_DOWN, HIGH_VOLATILITY, NEUTRAL (прогрев/неизвестно).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.engine.models import Candle as EngineCandle


def _atr(candles: list[EngineCandle], period: int = 14) -> list[float]:
    out: list[float] = []
    for i in range(1, len(candles)):
        prev = candles[i - 1]
        cur = candles[i]
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        out.append(tr)
    # простое скользящее среднее TR
    sma: list[float] = []
    for i in range(len(out)):
        window = out[max(0, i - period + 1) : i + 1]
        sma.append(sum(window) / len(window))
    return sma


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _adx(candles: list[EngineCandle], period: int = 14) -> list[float]:
    """Простой ADX (Wilder-стиль, без сглаживания Wilder)."""
    n = len(candles)
    dx: list[float] = []
    trs: list[float] = []
    for i in range(1, n):
        prev, cur = candles[i - 1], candles[i]
        up = cur.high - prev.high
        down = prev.low - cur.low
        plus_dm = up if up > down and up > 0 else 0.0
        minus_dm = down if down > up and down > 0 else 0.0
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        trs.append(tr)
        if tr == 0:
            dx.append(0.0)
        else:
            di_plus = plus_dm / tr * 100
            di_minus = minus_dm / tr * 100
            s = di_plus + di_minus
            dx.append(abs(di_plus - di_minus) / s * 100 if s else 0.0)
    adx: list[float] = []
    for i in range(len(dx)):
        window = dx[max(0, i - period + 1) : i + 1]
        adx.append(sum(window) / len(window))
    return adx


def _percentile_rank(values: list[float], window: int) -> list[float]:
    """Процентиль последнего значения в историческом окне (0..100)."""
    out: list[float] = []
    for i in range(len(values)):
        window_vals = values[max(0, i - window) : i + 1]
        if len(window_vals) < 5:
            out.append(50.0)
            continue
        cur = values[i]
        below = sum(1 for v in window_vals if v < cur)
        out.append(below / len(window_vals) * 100)
    return out


class RegimeDetector:
    def __init__(
        self,
        slope_threshold: float = 0.002,   # |наклон EMA50| за 5 баров, выше = тренд
        adx_threshold: float = 18.0,      # ниже = боковик
        atr_percentile_threshold: float = 90.0,
        range_mult: float = 2.5,          # (high-low)/close > mult*ATR% → high vol
        atr_period: int = 14,
        ema_fast: int = 20,
        ema_slow: int = 50,
        window: int = 200,
    ):
        self.slope_threshold = slope_threshold
        self.adx_threshold = adx_threshold
        self.atr_percentile_threshold = atr_percentile_threshold
        self.range_mult = range_mult
        self.atr_period = atr_period
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.window = window

    def compute(self, candles: list[EngineCandle]) -> list[dict]:
        """Режим на каждом баре (по закрытию бара i)."""
        if len(candles) < max(self.ema_slow, self.atr_period) + 5:
            return []
        closes = [c.close for c in candles]
        ema_f = _ema(closes, self.ema_fast)
        ema_s = _ema(closes, self.ema_slow)
        atr_pct = [a / max(c.close, 1e-9) * 100 for a, c in zip(_atr(candles, self.atr_period), candles[1:])]
        atr_pct = [None] * 1 + atr_pct  # выравнивание по индексу баров
        atr_pct = [v if v is not None else atr_pct[1] for v in atr_pct]
        atr_perc = _percentile_rank(atr_pct, self.window)
        adx = [None] * 1 + _adx(candles, self.atr_period)
        adx = [v if v is not None else adx[1] for v in adx]
        vols = [c.volume for c in candles]
        vol_ratio: list[float] = []
        for i in range(len(vols)):
            window = vols[max(0, i - 50) : i]
            mean = sum(window) / len(window) if window else 1.0
            vol_ratio.append(vols[i] / max(mean, 1e-9))

        out: list[dict] = []
        warmup = self.ema_slow + self.atr_period
        for i in range(len(candles)):
            if i < warmup or i < 2:
                out.append({"ts": candles[i].ts, "state": "NEUTRAL", "features": None,
                            "reason": "warmup"})
                continue
            slope = (ema_s[i] - ema_s[i - 5]) / max(ema_s[i - 5], 1e-9)
            range_pct = (candles[i].high - candles[i].low) / max(candles[i].close, 1e-9) * 100
            a_pct = atr_pct[i]
            a_perc = atr_perc[i]
            a_adx = adx[i]
            v_ratio = vol_ratio[i]
            state: str
            reason: str
            if a_perc >= self.atr_percentile_threshold or range_pct > self.range_mult * a_pct:
                state, reason = "HIGH_VOLATILITY", "atr_percentile_high_or_range_wide"
            elif slope > self.slope_threshold and ema_f[i] > ema_s[i] and a_adx >= self.adx_threshold:
                state, reason = "TREND_UP", "ema_up_adx_high"
            elif slope < -self.slope_threshold and ema_f[i] < ema_s[i] and a_adx >= self.adx_threshold:
                state, reason = "TREND_DOWN", "ema_down_adx_high"
            elif abs(slope) < self.slope_threshold and a_adx < self.adx_threshold:
                state, reason = "RANGE", "flat_ema_low_adx"
            else:
                state, reason = "NEUTRAL", "mixed"
            out.append({
                "ts": candles[i].ts,
                "state": state,
                "reason": reason,
                "features": {
                    "atr_pct": round(a_pct, 3),
                    "atr_percentile": round(a_perc, 1),
                    "ema_slope": round(slope * 100, 3),
                    "adx": round(a_adx, 1),
                    "volume_ratio": round(v_ratio, 2),
                },
            })
        return out


def regime_at(regime_bars: list[dict], ts: datetime) -> dict | None:
    """Режим для момента ts: режим последнего ЗАКРЫТОГО бара детектора."""
    if not regime_bars:
        return None
    best = None
    for r in regime_bars:
        if r["ts"] <= ts:
            best = r
        else:
            break
    return best


def regime_timeline(regime_bars: list[dict]) -> list[dict]:
    """Сжатая лента: {from, to, state} — только моменты смены режима."""
    if not regime_bars:
        return []
    out: list[dict] = []
    last_state = None
    for i, r in enumerate(regime_bars):
        if r["state"] != last_state:
            if out:
                out[-1]["to"] = r["ts"]
            out.append({"from": r["ts"], "to": None, "state": r["state"],
                        "reason": r["reason"], "features": r["features"]})
            last_state = r["state"]
    if out:
        out[-1]["to"] = regime_bars[-1]["ts"]
    return out
