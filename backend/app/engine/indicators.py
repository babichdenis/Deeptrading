from __future__ import annotations

from typing import Sequence

from app.engine.models import Candle


def atr(bars: Sequence[Candle], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return result
    trs: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            trs.append(bar.high - bar.low)
        else:
            prev_close = bars[i - 1].close
            trs.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prev_close),
                    abs(bar.low - prev_close),
                )
            )
    value = sum(trs[:period]) / period
    result[period - 1] = value
    for i in range(period, len(bars)):
        value = (value * (period - 1) + trs[i]) / period
        result[i] = value
    return result


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Экспоненциальное скользящее среднее с SMA-инициализацией.

    Первое валидное значение — индекс period-1 (простое среднее первых period
    точек), дальше сглаживание alpha = 2/(period+1).
    """
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    result[period - 1] = value
    for i in range(period, len(values)):
        value = alpha * (values[i] - value) + value
        result[i] = value
    return result


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_gain == 0.0 and avg_loss == 0.0:
        return 50.0  # плоский рынок — нейтрально (важно для вето-логики)
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """RSI по Уайлдеру (сглаживание средних), ряд значений той же длины.

    Первое валидное значение — индекс period (нужно period изменений цены).
    Ровный ряд без движения даёт 50.0 — нейтрально, не блокирует и не форсирует.
    """
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(diff if diff > 0.0 else 0.0)
        losses.append(-diff if diff < 0.0 else 0.0)
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        result[i] = _rsi_value(avg_gain, avg_loss)
    return result


def macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """MACD: {"line", "signal", "hist"} — ряды длины входа.

    line = EMA(fast) - EMA(slow), валиден с индекса slow-1.
    signal = EMA(signal) по валидной части line.
    hist = line - signal. Вето-контур: hist > 0 два бара подряд = тренд жив.
    """
    n = len(closes)
    empty: list[float | None] = [None] * n
    if fast <= 0 or slow <= 0 or n < slow:
        return {"line": list(empty), "signal": list(empty), "hist": list(empty)}
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    line: list[float | None] = [None] * n
    start = slow - 1
    for i in range(start, n):
        line[i] = (ema_fast[i] or 0.0) - (ema_slow[i] or 0.0)
    valid = [v for v in line if v is not None]
    sig_vals = ema(valid, signal)
    sig: list[float | None] = [None] * n
    for j, v in enumerate(sig_vals):
        if v is not None:
            sig[start + j] = v
    hist: list[float | None] = [None] * n
    for i in range(n):
        if line[i] is not None and sig[i] is not None:
            hist[i] = line[i] - sig[i]
    return {"line": line, "signal": sig, "hist": hist}
