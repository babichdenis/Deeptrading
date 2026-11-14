"""Дневной фильтр BUY-входа: перепроданность дневки, волатильность дня и положительный импульс H1."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

D1_INTERVAL = 24
H1_SECONDS = 3600
H1_LOOKBACK = 3
M5_SECONDS = 300
M5_LOOKBACK = 12
SELL_H3_MAX = -0.001
SELL_M5_MAX = -0.0006
BUY_RSI_MAX = 45.0
BUY_ATR_MIN = 2.4286
MSK = timezone(timedelta(hours=3))


def msk_day(ts: datetime) -> tuple[int, int, int]:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(MSK)
    return (local.year, local.month, local.day)


def sma_rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """RSI по простым средним (rolling(period).mean()), как в research — не RSI Уайлдера."""
    if period <= 0 or len(closes) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = float(closes[i]) - float(closes[i - 1])
        gains.append(delta if delta > 0.0 else 0.0)
        losses.append(-delta if delta < 0.0 else 0.0)
    up = sum(gains[-period:]) / period
    down = sum(losses[-period:]) / period
    if down == 0.0:
        return 100.0 if up > 0.0 else 50.0
    return 100.0 - 100.0 / (1.0 + up / down)


def atr_pct(bars: Sequence, period: int = 14) -> float | None:
    """ATR(period) предыдущего дня в процентах от цены закрытия (parity с research)."""
    if period <= 0 or len(bars) < period + 1:
        return None
    close = float(bars[-1].close)
    if close <= 0.0:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        high = float(bars[i].high)
        low = float(bars[i].low)
        prev_close = float(bars[i - 1].close)
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return (sum(trs[-period:]) / period) / close * 100.0


def previous_day_features(daily_bars: Sequence, ref_ts: datetime, period: int = 14
                          ) -> tuple[float | None, float | None]:
    """d_rsi и d_atr строго по завершённым дням до ref_ts (без lookahead)."""
    day = msk_day(ref_ts)
    prior = [b for b in daily_bars if msk_day(b.ts) < day]
    if len(prior) < period + 1:
        return None, None
    closes = [float(b.close) for b in prior]
    return sma_rsi(closes, period), atr_pct(prior, period)


def h1_ret3(h1_bars: Sequence, ref_ts: datetime, lookback: int = H1_LOOKBACK,
            tf_seconds: int = H1_SECONDS) -> float | None:
    """Доходность за lookback часовых баров по последнему закрытому H1-бару."""
    if ref_ts.tzinfo is None:
        ref_ts = ref_ts.replace(tzinfo=timezone.utc)
    closed = []
    for bar in h1_bars:
        ts = bar.ts if bar.ts.tzinfo else bar.ts.replace(tzinfo=timezone.utc)
        if ts + timedelta(seconds=tf_seconds) <= ref_ts:
            closed.append(bar)
    if len(closed) < lookback + 1:
        return None
    current = float(closed[-1].close)
    base = float(closed[-1 - lookback].close)
    if base <= 0.0:
        return None
    return current / base - 1.0


@dataclass(frozen=True)
class D1Verdict:
    allowed: bool
    reason: str
    d_rsi: float | None = None
    d_atr: float | None = None
    h_ret3: float | None = None


def buy_verdict(d_rsi: float | None, d_atr: float | None, h_ret3: float | None,
                atr_min: float = BUY_ATR_MIN, rsi_max: float = BUY_RSI_MAX) -> D1Verdict:
    if d_rsi is None or d_atr is None or h_ret3 is None:
        return D1Verdict(True, "insufficient_data", d_rsi, d_atr, h_ret3)
    if d_rsi > rsi_max:
        return D1Verdict(False, f"d_rsi={d_rsi:.1f}>{rsi_max:g}", d_rsi, d_atr, h_ret3)
    if d_atr < atr_min:
        return D1Verdict(False, f"d_atr={d_atr:.2f}<{atr_min:.2f}", d_rsi, d_atr, h_ret3)
    if h_ret3 <= 0.0:
        return D1Verdict(False, f"h_ret3={h_ret3:+.4f}<=0", d_rsi, d_atr, h_ret3)
    return D1Verdict(True, "ok", d_rsi, d_atr, h_ret3)
def m5_ret12(m5_bars: Sequence, ref_ts: datetime, lookback: int = M5_LOOKBACK,
             tf_seconds: int = M5_SECONDS) -> float | None:
    """Доходность за lookback закрытых 5m-баров по образцу h1_ret3 (без lookahead)."""
    if ref_ts.tzinfo is None:
        ref_ts = ref_ts.replace(tzinfo=timezone.utc)
    closed = []
    for bar in m5_bars:
        ts = bar.ts if bar.ts.tzinfo else bar.ts.replace(tzinfo=timezone.utc)
        if ts + timedelta(seconds=tf_seconds) <= ref_ts:
            closed.append(bar)
    if len(closed) < lookback + 1:
        return None
    current = float(closed[-1].close)
    base = float(closed[-1 - lookback].close)
    if base <= 0.0:
        return None
    return current / base - 1.0


@dataclass(frozen=True)
class SellVerdict:
    allowed: bool
    reason: str
    h_ret3: float | None = None
    m5_ret12: float | None = None


def sell_verdict(h_ret3: float | None, m5: float | None,
                 h3_max: float = SELL_H3_MAX, m5_max: float = SELL_M5_MAX) -> SellVerdict:
    if h_ret3 is None or m5 is None:
        return SellVerdict(True, "insufficient_data", h_ret3, m5)
    if h_ret3 > h3_max:
        return SellVerdict(False, f"h_ret3={h_ret3:+.4f}>{h3_max:g}", h_ret3, m5)
    if m5 > m5_max:
        return SellVerdict(False, f"m5_ret12={m5:+.4f}>{m5_max:g}", h_ret3, m5)
    return SellVerdict(True, "ok", h_ret3, m5)
