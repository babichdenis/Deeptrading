"""OsEngine-порты индикаторов — clean-room (Фаза D, пилот 5 роботов).

Источник семантики: клон ~/OsEngine → Indicators/Scripts/*.cs (только чтение;
лицензия OsEngine — EULA, код не копируется, воспроизводится поведение).
Ряды той же длины, что входные свечи; прогрев — None (в C# серии там 0).
Отличия от наших app/engine/indicators.py (EMA, RSI-Wilder с нейтралью 50)
сознательные: цель — поведенческая совместимость с тестером OsEngine, чтобы
порты роботов (ose/robots.py) видели те же числа, что оригинал.

Quirks оригинала, портированные как есть (каждый закрыт тестом):
- Sma: ScriptSpells.Summ суммирует окно [start+1, end] → окно [i-len+1, i],
  первый валидный индекс i = len (не len-1); свеча 0 в первое окно не входит.
- Rsi: сглаживание MovingAverageHard — сид при valueInd==len из суммы
  len+1 точек / len, дальше EMA с a = 2/(2*len); окно пересчитывается с
  20-барным запасом → первый валидный индекс i = len+21. Если avgHigh==0
  ИЛИ avgLow==0 (флэт или чистый тренд) → 100 (quirk: не 50 и не 0).
  Значения round(..., 2).
- Stochastic: k=0 в под-прогреве (i < P2+P3+3 или нулевые средние);
  средние всегда делятся на length (GetAverage); D = среднее НЕокруглённого
  K; оба ряда round(..., 2).
- Bollinger: делитель len-1 при len>30, иначе len; полосы round(..., 6);
  центр = SMA без округления.
- Envelops: полосы = SMA ± dev% от SMA, round(..., 6).
- PriceChannel: up = max High по [i-lenUp+1, i] (первый валид i=lenUp+1),
  down = min Low по [i-lenDown+1, i] — сдвиг окна как у Sma.
- ATR: Wilder по TR; сид = SMA первых len TR на i = len-1; в percent-режиме
  деление на OPEN предыдущей свечи (TR/(Open[i-1]/100)); round 9.
- CCI: первый валид i = len+1; md == 0 → 0.0; round 5.
- MACD: вложенные Ema с нулевым прогревом; сигнальная EMA по macd-ряду
  вместе с нулями, в серии всё с i = max(fast, slow).
- RVI: свёртки 1-2-2-1; нулевые суммы → 0.0; сигнал = 0.0 до period+6.
- Bulls/Bears Power: SMA(Close) по Charts-MovingAverage (первый валид
  i = len+1, НЕ как Scripts/Sma); Bulls = High-SMA, Bears = Low-SMA.
"""
from __future__ import annotations

import math
from typing import Sequence

from app.engine.models import Candle

__all__ = [
    "sma",
    "rsi",
    "stochastic",
    "bollinger",
    "envelops",
    "price_channel",
    "atr",
    "cci",
    "macd",
    "rvi",
    "bulls_power",
    "bears_power",
]


def _point(candle: Candle, point: str) -> float:
    key = point.lower()
    if key == "close":
        return candle.close
    if key == "open":
        return candle.open
    if key == "high":
        return candle.high
    if key == "low":
        return candle.low
    if key == "median":
        return (candle.high + candle.low) / 2
    if key == "typical":
        return (candle.high + candle.low + candle.close) / 3
    raise ValueError(f"unknown candle point: {point!r}")


def _window_max(candles: Sequence[Candle], index: int, length: int, attr: str) -> float:
    return max(getattr(candles[j], attr) for j in range(index - length + 1, index + 1))


def _window_min(candles: Sequence[Candle], index: int, length: int, attr: str) -> float:
    return min(getattr(candles[j], attr) for j in range(index - length + 1, index + 1))


def sma(candles: Sequence[Candle], length: int, point: str = "close") -> list[float | None]:
    """SMA (Scripts/Sma.cs через ScriptSpells.Summ): окно [i-length+1, i].

    Первый валидный индекс — i = length (quirk Summ: свеча 0 не входит в
    первое окно). Прогрев — None (в C# серии там 0).
    """
    result: list[float | None] = [None] * len(candles)
    if length <= 0 or len(candles) <= length:
        return result
    values = [_point(c, point) for c in candles]
    for i in range(length, len(candles)):
        result[i] = sum(values[i - length + 1 : i + 1]) / length
    return result


def rsi(candles: Sequence[Candle], length: int = 14) -> list[float | None]:
    """RSI (Scripts/Rsi.cs): MovingAverageHard, окно с 20-барным запасом.

    Первый валидный индекс — i = length+21. Флэт и чистый тренд
    (avgHigh==0 или avgLow==0) дают 100.0 — quirk оригинала. round(..., 2).
    """
    closes = [c.close for c in candles]
    result: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        value = _rsi_at_index(closes, length, i)
        if value is not None:
            result[i] = value
    return result


def _rsi_at_index(closes: Sequence[float], length: int, index: int) -> float | None:
    if length <= 0:
        raise ValueError("length must be positive")
    if index - length - 1 <= 0:
        return None
    start_index = 1
    if index > length:
        start_index = index - length - 20
        if start_index - 1 < 0:
            start_index = index - length + 1
    change_high: list[float] = []
    change_low: list[float] = []
    avg_high: list[float] = []
    avg_low: list[float] = []
    for i in range(start_index, index + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            change_high.append(diff)
            change_low.append(0.0)
        else:
            change_high.append(0.0)
            change_low.append(-diff)
        value_index = len(change_high) - 1
        _moving_average_hard(change_high, avg_high, length, value_index)
        _moving_average_hard(change_low, avg_low, length, value_index)
    if not avg_high:
        return None
    average_high = avg_high[-1]
    average_low = avg_low[-1]
    if average_high != 0.0 and average_low != 0.0:
        value = 100.0 * (1.0 - average_low / (average_low + average_high))
    else:
        value = 100.0
    return round(value, 2)


def _moving_average_hard(
    values: Sequence[float],
    moving: list[float],
    length: int,
    index: int,
) -> None:
    """MovingAverageHard (Scripts/Rsi.cs): сид и EMA с a = 2/(2*length).

    При index == length сид = сумма точек [0..length] (length+1 штук!) / length;
    при index > length — EMA от предыдущего значения. Заполняет moving по
    индексам, как оригинал.
    """
    if index == length:
        total = 0.0
        i = index
        while i > index - 1 - length:
            total += values[i]
            i -= 1
        while len(moving) <= index:
            moving.append(0.0)
        moving[index] = total / length
    elif index > length:
        alpha = 2.0 / (length * 2.0)
        previous = moving[index - 1]
        while len(moving) <= index:
            moving.append(0.0)
        moving[index] = previous + alpha * (values[index] - previous)


def stochastic(
    candles: Sequence[Candle],
    period1: int = 5,
    period2: int = 3,
    period3: int = 3,
) -> dict[str, list[float | None]]:
    """Стохастик (Scripts/Stochastic.cs): {"k": K, "d": сглаженный K}.

    Ряды пишутся с индекса max(P1, P2, P3)+1. K = 100 * tM1/tM2, где tM —
    средние (GetAverage, всегда /length) от T1 = Close - min(Low, P1-окно)
    и T2 = max(High) - min(Low) того же окна; в под-прогреве
    (i < P2+P3+3 или нулевые tM) K = 0. D = среднее неокруглённого K по P3.
    Оба ряда round(..., 2).
    """
    n = len(candles)
    k_series: list[float | None] = [None] * n
    d_series: list[float | None] = [None] * n
    if n == 0:
        return {"k": k_series, "d": d_series}
    t1 = [0.0] * n
    t2 = [0.0] * n
    k = [0.0] * n
    for i in range(max(period1, period2, period3) + 1, n):
        if i - period1 + 1 <= 0:
            t1[i] = 0.0
            t2[i] = 0.0
        else:
            lowest = _window_min(candles, i, period1, "low")
            highest = _window_max(candles, i, period1, "high")
            t1[i] = candles[i].close - lowest
            t2[i] = highest - lowest
        t_m1 = _average_last(t1[: i + 1], period2)
        t_m2 = _average_last(t2[: i + 1], period2)
        if i < period2 + period3 + 3 or t_m2 == 0.0 or t_m1 == 0.0:
            k[i] = 0.0
        else:
            k[i] = 100.0 * t_m1 / t_m2
        k_mean = _average_last(k[: i + 1], period3)
        k_series[i] = round(k[i], 2)
        d_series[i] = round(k_mean, 2)
    return {"k": k_series, "d": d_series}


def _average_last(values: Sequence[float], length: int) -> float:
    """GetAverage (Scripts/Stochastic.cs): сумма последних min(length, len)
    точек, делённая на length (без компенсации короткого окна).
    """
    if length <= 0:
        raise ValueError("length must be positive")
    window = values[-length:]
    return sum(window) / length


def bollinger(
    candles: Sequence[Candle],
    length: int = 21,
    deviation: float = 2.0,
) -> dict[str, list[float | None]]:
    """Bollinger (Scripts/Bollinger.cs): {"up", "center", "down"}.

    Первый валидный индекс — i = length+1. Стандартное отклонение по окну
    [i-length+1, i] с делителем length-1 при length>30, иначе length
    (quirk оригинала). Полосы round(..., 6), центр = SMA без округления.
    """
    n = len(candles)
    up: list[float | None] = [None] * n
    center: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    sma_series = sma(candles, length)
    divisor = length - 1 if length > 30 else length
    for i in range(length + 1, n):
        value_sma = sma_series[i]
        if value_sma is None or value_sma == 0.0:
            continue
        squared = sum(
            (candles[j].close - value_sma) ** 2
            for j in range(i - length + 1, i + 1)
        )
        std = math.sqrt(squared / divisor)
        center[i] = value_sma
        up[i] = round(value_sma + std * deviation, 6)
        down[i] = round(value_sma - std * deviation, 6)
    return {"up": up, "center": center, "down": down}


def envelops(
    candles: Sequence[Candle],
    length: int = 21,
    deviation: float = 2.0,
) -> dict[str, list[float | None]]:
    """Envelops (Scripts/Envelops.cs): {"up", "center", "down"}.

    Первый валидный индекс — i = length+1. Полосы = SMA ± deviation%
    от SMA (процент от уровня, не от цены шага), round(..., 6).
    """
    n = len(candles)
    up: list[float | None] = [None] * n
    center: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    sma_series = sma(candles, length)
    for i in range(length + 1, n):
        value_sma = sma_series[i]
        if value_sma is None:
            continue
        center[i] = value_sma
        up[i] = round(value_sma + value_sma * deviation / 100.0, 6)
        down[i] = round(value_sma - value_sma * deviation / 100.0, 6)
    return {"up": up, "center": center, "down": down}


def price_channel(
    candles: Sequence[Candle],
    length_up: int = 21,
    length_down: int = 21,
) -> dict[str, list[float | None]]:
    """PriceChannel (Scripts/PriceChannel.cs): {"up", "down"}.

    up = max High по окну [i-length_up+1, i] — первый валидный i =
    length_up+1; down = min Low по [i-length_down+1, i] — первый валидный
    i = length_down+1 (сдвиг окна как у Sma). Несовпадающие длины допустимы.
    """
    n = len(candles)
    up: list[float | None] = [None] * n
    down: list[float | None] = [None] * n
    for i in range(n):
        if i - length_up > 0:
            up[i] = _window_max(candles, i, length_up, "high")
        if i - length_down > 0:
            down[i] = _window_min(candles, i, length_down, "low")
    return {"up": up, "down": down}


# --- Волна B: ATR / CCI / MACD / RVI / Bulls & Bears Power ---------------------


def _ema_script(values: Sequence[float], length: int) -> list[float]:
    """EMA в семантике Scripts/Ema.cs по подготовленному ряду.

    0 при i < length; сид SMA на i == length (окно [i-length+1, i]);
    дальше EMA с a = round(2/(length+1), 8); каждая запись round(..., 8).
    """
    if length <= 0:
        raise ValueError("length must be positive")
    out = [0.0] * len(values)
    alpha = round(2.0 / (length + 1), 8)
    for i in range(len(values)):
        if i == length:
            out[i] = round(sum(values[i - length + 1 : i + 1]) / length, 8)
        elif i > length:
            out[i] = round(out[i - 1] + alpha * (values[i] - out[i - 1]), 8)
    return out


def atr(
    candles: Sequence[Candle],
    length: int = 14,
    mode: str = "absolute",
) -> list[float | None]:
    """ATR (Scripts/ATR.cs): сглаживание Wilder по True Range.

    TR[0] = 0, TR[i] = max(|H-L|, |C[i-1]-H|, |C[i-1]-L|); в percent-режиме
    (quirk) деление на OPEN предыдущей свечи: TR/(Open[i-1]/100).
    Сид — SMA первых len TR на i = len-1 (MovingAverageWild; при нулевой
    сумме сид 0), дальше Wilder: (prev*(len-1) + round(TR,9)) / len,
    round(..., 9). Первый валид — i = length-1; прогрев — None (в C# 0).
    """
    n = len(candles)
    result: list[float | None] = [None] * n
    if length <= 0:
        raise ValueError("length must be positive")
    if n == 0:
        return result
    tr = [0.0] * n
    for i in range(1, n):
        value = max(
            abs(candles[i].high - candles[i].low),
            abs(candles[i - 1].close - candles[i].high),
            abs(candles[i - 1].close - candles[i].low),
        )
        if mode.lower() == "percent" and value != 0 and candles[i - 1].open != 0:
            value = value / (candles[i - 1].open / 100.0)
        tr[i] = value
    moving = 0.0
    for i in range(n):
        if i + 1 < length:
            continue
        if i + 1 == length:
            total = sum(tr[i - length + 1 : i + 1])
            moving = total / length if total != 0 else 0.0
        else:
            moving = round((moving * (length - 1) + round(tr[i], 9)) / length, 9)
        result[i] = round(moving, 9)
    return result


def cci(
    candles: Sequence[Candle],
    length: int = 20,
    point: str = "typical",
) -> list[float | None]:
    """CCI (Scripts/CCI.cs): отклонение точки от средней окна.

    Первый валид — i = length+1 (guard index-length <= 0). ma и md — по окну
    [i-length+1, i] заданной точки (по умолчанию Typical = (H+L+C)/3);
    md == 0 → 0.0 (quirk: в серии C# остаётся ноль).
    CCI = (pt - ma) / (md * 0.015 / len), round(..., 5).
    """
    n = len(candles)
    result: list[float | None] = [None] * n
    if length <= 0:
        raise ValueError("length must be positive")
    for i in range(length + 1, n):
        points = [_point(candles[j], point) for j in range(i - length + 1, i + 1)]
        ma = sum(points) / length
        md = sum(abs(ma - p) for p in points)
        if md == 0:
            result[i] = 0.0
            continue
        result[i] = round((points[-1] - ma) / (md * 0.015 / length), 5)
    return result


def macd(
    candles: Sequence[Candle],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> dict[str, list[float | None]]:
    """MACD (Scripts/MACD.cs): {"macd", "signal", "histogram"}.

    macd = Ema(fast) - Ema(slow) (вложенные Ema, Scripts/Ema.cs; нули в
    прогреве C#), пишется с i = max(fast, slow), до того None.
    Сигнальная — EMA по macd-ряду ВМЕСТЕ с нулевым прогревом: сид SMA на
    i == signal (окно [i-signal+1, i]), дальше EMA с a = round(2/(signal+1),
    8); в серии — тоже с i = max(fast, slow) (OnProcess раньше не запускается,
    гистограмма = macd - signal пишется вместе с macd).
    """
    n = len(candles)
    out_macd: list[float | None] = [None] * n
    out_signal: list[float | None] = [None] * n
    out_hist: list[float | None] = [None] * n
    if min(fast, slow, signal) <= 0:
        raise ValueError("fast, slow, signal must be positive")
    if n == 0:
        return {"macd": out_macd, "signal": out_signal, "histogram": out_hist}
    closes = [c.close for c in candles]
    ema_fast = _ema_script(closes, fast)
    ema_slow = _ema_script(closes, slow)
    first = max(fast, slow)
    macd_full = [0.0] * n
    for i in range(first, n):
        macd_full[i] = ema_fast[i] - ema_slow[i]
    signal_full = [0.0] * n
    alpha = round(2.0 / (signal + 1), 8)
    for i in range(n):
        if i == signal:
            signal_full[i] = round(sum(macd_full[i - signal + 1 : i + 1]) / signal, 8)
        elif i > signal:
            signal_full[i] = round(
                signal_full[i - 1] + alpha * (macd_full[i] - signal_full[i - 1]), 8
            )
    for i in range(first, n):
        out_macd[i] = macd_full[i]
        out_signal[i] = signal_full[i]
        out_hist[i] = macd_full[i] - signal_full[i]
    return {"macd": out_macd, "signal": out_signal, "histogram": out_hist}


def rvi(
    candles: Sequence[Candle],
    period: int = 5,
) -> dict[str, list[float | None]]:
    """RVI (Scripts/RVI.cs): {"rvi", "signal"}.

    Числитель/знаменатель — свёртки 1-2-2-1 от (C-O) и (H-L) (нули при
    i <= 3); rvi = Σ(num)/Σ(den) по окну [i-period+1, i], нулевые суммы →
    0.0, round(..., 2); пишется с i = period+1. Сигнальная =
    (rvi + 2*rvi[i-1] + 2*rvi[i-2] + rvi[i-3]) / 6: до i = period+6
    пишется 0.0 (else-ветка GetValueSecond), дальше round(..., 2).
    """
    n = len(candles)
    one: list[float | None] = [None] * n
    two: list[float | None] = [None] * n
    if period <= 0:
        raise ValueError("period must be positive")
    if n == 0:
        return {"rvi": one, "signal": two}
    move = [0.0] * n
    rng = [0.0] * n
    for i in range(4, n):
        move[i] = (
            (candles[i].close - candles[i].open)
            + 2 * (candles[i - 1].close - candles[i - 1].open)
            + 2 * (candles[i - 2].close - candles[i - 2].open)
            + (candles[i - 3].close - candles[i - 3].open)
        )
        rng[i] = (
            (candles[i].high - candles[i].low)
            + 2 * (candles[i - 1].high - candles[i - 1].low)
            + 2 * (candles[i - 2].high - candles[i - 2].low)
            + (candles[i - 3].high - candles[i - 3].low)
        )
    rvi_full = [0.0] * n
    for i in range(period + 1, n):
        sum_ma = sum(move[i - period + 1 : i + 1])
        sum_ra = sum(rng[i - period + 1 : i + 1])
        if sum_ma == 0 or sum_ra == 0:
            rvi_full[i] = 0.0
        else:
            rvi_full[i] = round(sum_ma / sum_ra, 2)
        one[i] = rvi_full[i]
        if i >= period + 6:
            two[i] = round(
                (
                    rvi_full[i]
                    + 2 * rvi_full[i - 1]
                    + 2 * rvi_full[i - 2]
                    + rvi_full[i - 3]
                )
                / 6,
                2,
            )
        else:
            two[i] = 0.0
    return {"rvi": one, "signal": two}


def _sma_charts(candles: Sequence[Candle], length: int) -> list[float | None]:
    """SMA в семантике Charts/CandleChart/Indicators/MovingAverage
    (GetValueSimple — основа Bulls/Bears Power): первый валид — i = length+1
    (guard index - length <= 0), окно [i-length+1, i] по Close, round(..., 8).
    НЕ то же, что sma() выше (Scripts/Sma.cs через Summ) — граница прогрева
    сдвинута на бар.
    """
    n = len(candles)
    out: list[float | None] = [None] * n
    if length <= 0:
        raise ValueError("length must be positive")
    for i in range(length + 1, n):
        out[i] = round(
            sum(candles[j].close for j in range(i - length + 1, i + 1)) / length, 8
        )
    return out


def _power(candles: Sequence[Candle], period: int, attr: str) -> list[float | None]:
    """Общая часть Bulls/Bears Power: точка - SMA(Close, period).

    Guards как в GetValue: i < period или SMA == 0 → 0 в C# (прогрев — None).
    """
    n = len(candles)
    result: list[float | None] = [None] * n
    if period <= 0:
        raise ValueError("period must be positive")
    sma_close = _sma_charts(candles, period)
    for i in range(period + 1, n):
        ma = sma_close[i]
        result[i] = 0.0 if ma == 0.0 else getattr(candles[i], attr) - ma
    return result


def bulls_power(candles: Sequence[Candle], period: int = 13) -> list[float | None]:
    """BullsPower (Charts/.../BullsPower.cs): High - SMA(Close, period).

    De jure тип Exponential, de facto обёртка создаёт MovingAverage(false)
    с дефолтами Simple/Close (quirk оригинала). Первый валид — i = period+1.
    """
    return _power(candles, period, "high")


def bears_power(candles: Sequence[Candle], period: int = 13) -> list[float | None]:
    """BearsPower (Charts/.../BearsPower.cs): Low - SMA(Close, period).

    Гварды те же, что у BullsPower.
    """
    return _power(candles, period, "low")
