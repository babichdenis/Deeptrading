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

Ускорение (30.09): функции принимают owner=None и держат на нём хвостовой кэш
(`owner._ind_cache`): при росте окна на бары досчитываются только новые
индексы, значения бит-в-бит совпадают с полным расчётом (оконные суммы
считаются заново в том же порядке; рекурсивные серии — пошагово тем же
уравнением). Кэш инвалидируется сам, если окно уменьшилось или стык баров
не совпал (перезапуск/смена серии). Без owner (owner=None) — прежний
полный расчёт, поведение не меняется.
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


# --- Хвостовой кэш (owner._ind_cache) -----------------------------------------


def _entry(owner, key):
    """Запись кэша для ключа (имя индикатора + параметры) на объекте-владельце."""
    if owner is None:
        return None
    try:
        cache = getattr(owner, "_ind_cache", None)
        if cache is None:
            cache = {}
            owner._ind_cache = cache
        e = cache.get(key)
        if e is None:
            e = {"n": 0, "last_ts": None, "last_close": None}
            cache[key] = e
        return e
    except Exception:
        return None


def _shared(e, candles) -> bool:
    """Кэш продолжает ту же серию: длина не уменьшилась и стык баров совпал."""
    n0 = e.get("n", 0)
    if n0 <= 0 or n0 > len(candles):
        return False
    return (candles[n0 - 1].ts == e.get("last_ts")
            and candles[n0 - 1].close == e.get("last_close"))


def _stamp(e, candles, n: int) -> None:
    if e is None:
        return
    e["n"] = n
    if n:
        e["last_ts"] = candles[n - 1].ts
        e["last_close"] = candles[n - 1].close


def sma(
    candles: Sequence[Candle],
    length: int,
    point: str = "close",
    owner=None,
) -> list[float | None]:
    """SMA (Scripts/Sma.cs через ScriptSpells.Summ): окно [i-length+1, i].

    Первый валидный индекс — i = length (quirk Summ: свеча 0 не входит в
    первое окно). Прогрев — None (в C# серии там 0).
    """
    n = len(candles)
    e = _entry(owner, ("sma", length, point))
    if e is not None and _shared(e, candles):
        result = e["series"]
        start = e["n"]
    else:
        result = [None] * n
        start = 0
        if e is not None:
            e["series"] = result
    if len(result) < n:
        result.extend([None] * (n - len(result)))
    if length > 0:
        for i in range(max(start, length), n):
            result[i] = sum(_point(candles[j], point) for j in range(i - length + 1, i + 1)) / length
    _stamp(e, candles, n)
    return result


def rsi(candles: Sequence[Candle], length: int = 14, owner=None) -> list[float | None]:
    """RSI (Scripts/Rsi.cs): MovingAverageHard, окно с 20-барным запасом.

    Первый валидный индекс — i = length+21. Флэт и чистый тренд
    (avgHigh==0 или avgLow==0) дают 100.0 — quirk оригинала. round(..., 2).
    """
    n = len(candles)
    e = _entry(owner, ("rsi", length))
    if e is not None and _shared(e, candles):
        closes = e["closes"]
        result = e["series"]
        start = e["n"]
    else:
        closes = [c.close for c in candles]
        result = [None] * n
        start = 0
        if e is not None:
            e["closes"] = closes
            e["series"] = result
    if len(closes) < n:
        closes.extend(c.close for c in candles[len(closes):n])
    if len(result) < n:
        result.extend([None] * (n - len(result)))
    for i in range(start, n):
        value = _rsi_at_index(closes, length, i)
        if value is not None:
            result[i] = value
    _stamp(e, candles, n)
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
    owner=None,
) -> dict[str, list[float | None]]:
    """Стохастик (Scripts/Stochastic.cs): {"k": K, "d": сглаженный K}.

    Ряды пишутся с индекса max(P1, P2, P3)+1. K = 100 * tM1/tM2, где tM —
    средние (GetAverage, всегда /length) от T1 = Close - min(Low, P1-окно)
    и T2 = max(High) - min(Low) того же окна; в под-прогреве
    (i < P2+P3+3 или нулевые tM) K = 0. D = среднее неокруглённого K по P3.
    Оба ряда round(..., 2).
    """
    n = len(candles)
    e = _entry(owner, ("stochastic", period1, period2, period3))
    if e is not None and _shared(e, candles):
        k_series, d_series = e["k"], e["d"]
        t1, t2, kk = e["t1"], e["t2"], e["kk"]
        start = e["n"]
    else:
        k_series = [None] * n
        d_series = [None] * n
        t1 = [0.0] * n
        t2 = [0.0] * n
        kk = [0.0] * n
        start = 0
        if e is not None:
            e.update({"k": k_series, "d": d_series, "t1": t1, "t2": t2, "kk": kk})
    if len(k_series) < n:
        k_series.extend([None] * (n - len(k_series)))
        d_series.extend([None] * (n - len(d_series)))
        t1.extend([0.0] * (n - len(t1)))
        t2.extend([0.0] * (n - len(t2)))
        kk.extend([0.0] * (n - len(kk)))
    if n:
        p_min = max(period1, period2, period3) + 1
        for i in range(max(start, p_min), n):
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
                kk[i] = 0.0
            else:
                kk[i] = 100.0 * t_m1 / t_m2
            k_mean = _average_last(kk[: i + 1], period3)
            k_series[i] = round(kk[i], 2)
            d_series[i] = round(k_mean, 2)
    _stamp(e, candles, n)
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
    owner=None,
) -> dict[str, list[float | None]]:
    """Bollinger (Scripts/Bollinger.cs): {"up", "center", "down"}.

    Первый валидный индекс — i = length+1. Стандартное отклонение по окну
    [i-length+1, i] с делителем length-1 при length>30, иначе length
    (quirk оригинала). Полосы round(..., 6), центр = SMA без округления.
    """
    n = len(candles)
    e = _entry(owner, ("bollinger", length, deviation))
    if e is not None and _shared(e, candles):
        up, center, down = e["up"], e["center"], e["down"]
        start = e["n"]
    else:
        up = [None] * n
        center = [None] * n
        down = [None] * n
        start = 0
        if e is not None:
            e.update({"up": up, "center": center, "down": down})
    if len(up) < n:
        up.extend([None] * (n - len(up)))
        center.extend([None] * (n - len(center)))
        down.extend([None] * (n - len(down)))
    divisor = length - 1 if length > 30 else length
    for i in range(max(start, length + 1), n):
        # value_sma = sma()[i] (Scripts/Sma: окно [i-length+1, i]) — считаем локально,
        # порядок суммирования как в sma(), значения бит-в-бит.
        value_sma = sum(candles[j].close for j in range(i - length + 1, i + 1)) / length
        if value_sma == 0.0:
            continue
        # dx * dx вместо (x) ** 2: libm pow не обязан быть бит-в-бит
        # одинаковым между платформами, а умножение IEEE-детерминировано.
        # Порядок суммирования прежний — слева направо, значения не меняются.
        squared = 0.0
        for j in range(i - length + 1, i + 1):
            dx = candles[j].close - value_sma
            squared += dx * dx
        std = math.sqrt(squared / divisor)
        center[i] = value_sma
        up[i] = round(value_sma + std * deviation, 6)
        down[i] = round(value_sma - std * deviation, 6)
    _stamp(e, candles, n)
    return {"up": up, "center": center, "down": down}


def envelops(
    candles: Sequence[Candle],
    length: int = 21,
    deviation: float = 2.0,
    owner=None,
) -> dict[str, list[float | None]]:
    """Envelops (Scripts/Envelops.cs): {"up", "center", "down"}.

    Первый валидный индекс — i = length+1. Полосы = SMA ± deviation%
    от SMA (процент от уровня, не от цены шага), round(..., 6).
    """
    n = len(candles)
    e = _entry(owner, ("envelops", length, deviation))
    if e is not None and _shared(e, candles):
        up, center, down = e["up"], e["center"], e["down"]
        start = e["n"]
    else:
        up = [None] * n
        center = [None] * n
        down = [None] * n
        start = 0
        if e is not None:
            e.update({"up": up, "center": center, "down": down})
    if len(up) < n:
        up.extend([None] * (n - len(up)))
        center.extend([None] * (n - len(center)))
        down.extend([None] * (n - len(down)))
    for i in range(max(start, length + 1), n):
        value_sma = sum(candles[j].close for j in range(i - length + 1, i + 1)) / length
        center[i] = value_sma
        up[i] = round(value_sma + value_sma * deviation / 100.0, 6)
        down[i] = round(value_sma - value_sma * deviation / 100.0, 6)
    _stamp(e, candles, n)
    return {"up": up, "center": center, "down": down}


def price_channel(
    candles: Sequence[Candle],
    length_up: int = 21,
    length_down: int = 21,
    owner=None,
) -> dict[str, list[float | None]]:
    """PriceChannel (Scripts/PriceChannel.cs): {"up", "down"}.

    up = max High по окну [i-length_up+1, i] — первый валидный i =
    length_up+1; down = min Low по [i-length_down+1, i] — первый валидный
    i = length_down+1 (сдвиг окна как у Sma). Несовпадающие длины допустимы.
    """
    n = len(candles)
    e = _entry(owner, ("price_channel", length_up, length_down))
    if e is not None and _shared(e, candles):
        up, down = e["up"], e["down"]
        start = e["n"]
    else:
        up = [None] * n
        down = [None] * n
        start = 0
        if e is not None:
            e.update({"up": up, "down": down})
    if len(up) < n:
        up.extend([None] * (n - len(up)))
        down.extend([None] * (n - len(down)))
    for i in range(start, n):
        if i - length_up > 0 and up[i] is None:
            up[i] = _window_max(candles, i, length_up, "high")
        if i - length_down > 0 and down[i] is None:
            down[i] = _window_min(candles, i, length_down, "low")
    _stamp(e, candles, n)
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
    owner=None,
) -> list[float | None]:
    """ATR (Scripts/ATR.cs): сглаживание Wilder по True Range.

    TR[0] = 0, TR[i] = max(|H-L|, |C[i-1]-H|, |C[i-1]-L|); в percent-режиме
    (quirk) деление на OPEN предыдущей свечи: TR/(Open[i-1]/100).
    Сид — SMA первых len TR на i = len-1 (MovingAverageWild; при нулевой
    сумме сид 0), дальше Wilder: (prev*(len-1) + round(TR,9)) / len,
    round(..., 9). Первый валид — i = length-1; прогрев — None (в C# 0).
    """
    n = len(candles)
    if length <= 0:
        raise ValueError("length must be positive")
    e = _entry(owner, ("atr", length, mode))
    if e is not None and _shared(e, candles):
        result, tr = e["res"], e["tr"]
        moving = e["moving"]
        start = e["n"]
    else:
        result = [None] * n
        tr = [0.0] * n
        moving = 0.0
        start = 0
        if e is not None:
            e.update({"res": result, "tr": tr, "moving": 0.0})
    if len(result) < n:
        result.extend([None] * (n - len(result)))
        tr.extend([0.0] * (n - len(tr)))
    for i in range(max(1, start), n):
        value = max(
            abs(candles[i].high - candles[i].low),
            abs(candles[i - 1].close - candles[i].high),
            abs(candles[i - 1].close - candles[i].low),
        )
        if mode.lower() == "percent" and value != 0 and candles[i - 1].open != 0:
            value = value / (candles[i - 1].open / 100.0)
        tr[i] = value
    for i in range(max(start, length - 1), n):
        if i + 1 == length:
            total = sum(tr[i - length + 1 : i + 1])
            moving = total / length if total != 0 else 0.0
        else:
            moving = round((moving * (length - 1) + round(tr[i], 9)) / length, 9)
        result[i] = round(moving, 9)
    if e is not None:
        e["moving"] = moving
    _stamp(e, candles, n)
    return result


def cci(
    candles: Sequence[Candle],
    length: int = 20,
    point: str = "typical",
    owner=None,
) -> list[float | None]:
    """CCI (Scripts/CCI.cs): отклонение точки от средней окна.

    Первый валид — i = length+1 (guard index-length <= 0). ma и md — по окну
    [i-length+1, i] заданной точки (по умолчанию Typical = (H+L+C)/3);
    md == 0 → 0.0 (quirk: в серии C# остаётся ноль).
    CCI = (pt - ma) / (md * 0.015 / len), round(..., 5).
    """
    n = len(candles)
    if length <= 0:
        raise ValueError("length must be positive")
    e = _entry(owner, ("cci", length, point))
    if e is not None and _shared(e, candles):
        result = e["series"]
        start = e["n"]
    else:
        result = [None] * n
        start = 0
        if e is not None:
            e["series"] = result
    if len(result) < n:
        result.extend([None] * (n - len(result)))
    for i in range(max(start, length + 1), n):
        points = [_point(candles[j], point) for j in range(i - length + 1, i + 1)]
        ma = sum(points) / length
        md = sum(abs(ma - p) for p in points)
        if md == 0:
            result[i] = 0.0
            continue
        result[i] = round((points[-1] - ma) / (md * 0.015 / length), 5)
    _stamp(e, candles, n)
    return result


def macd(
    candles: Sequence[Candle],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    owner=None,
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
    if min(fast, slow, signal) <= 0:
        raise ValueError("fast, slow, signal must be positive")
    e = _entry(owner, ("macd", fast, slow, signal))
    if e is not None and _shared(e, candles):
        out_macd, out_signal, out_hist = e["macd"], e["sig"], e["hist"]
        macd_full = e["macd_full"]
        ef, es, sg = e["ef"], e["es"], e["sg"]
        start = e["n"]
    else:
        out_macd = [None] * n
        out_signal = [None] * n
        out_hist = [None] * n
        macd_full = [0.0] * n
        ef = es = sg = 0.0
        start = 0
        if e is not None:
            e.update({"macd": out_macd, "sig": out_signal, "hist": out_hist,
                      "macd_full": macd_full, "ef": 0.0, "es": 0.0, "sg": 0.0})
    if len(out_macd) < n:
        out_macd.extend([None] * (n - len(out_macd)))
        out_signal.extend([None] * (n - len(out_signal)))
        out_hist.extend([None] * (n - len(out_hist)))
        macd_full.extend([0.0] * (n - len(macd_full)))
    first = max(fast, slow)
    alpha_f = round(2.0 / (fast + 1), 8)
    alpha_s = round(2.0 / (slow + 1), 8)
    alpha_sig = round(2.0 / (signal + 1), 8)
    for i in range(start, n):
        close = candles[i].close
        if i == fast:
            ef = round(sum(candles[j].close for j in range(i - fast + 1, i + 1)) / fast, 8)
        elif i > fast:
            ef = round(ef + alpha_f * (close - ef), 8)
        else:
            ef = 0.0
        if i == slow:
            es = round(sum(candles[j].close for j in range(i - slow + 1, i + 1)) / slow, 8)
        elif i > slow:
            es = round(es + alpha_s * (close - es), 8)
        else:
            es = 0.0
        macd_full[i] = ef - es if i >= first else 0.0
        if i == signal:
            sg = round(sum(macd_full[i - signal + 1 : i + 1]) / signal, 8)
        elif i > signal:
            sg = round(sg + alpha_sig * (macd_full[i] - sg), 8)
        else:
            sg = 0.0
        if i >= first:
            out_macd[i] = macd_full[i]
            out_signal[i] = sg
            out_hist[i] = macd_full[i] - sg
    if e is not None:
        e.update({"ef": ef, "es": es, "sg": sg})
    _stamp(e, candles, n)
    return {"macd": out_macd, "signal": out_signal, "histogram": out_hist}


def rvi(
    candles: Sequence[Candle],
    period: int = 5,
    owner=None,
) -> dict[str, list[float | None]]:
    """RVI (Scripts/RVI.cs): {"rvi", "signal"}.

    Числитель/знаменатель — свёртки 1-2-2-1 от (C-O) и (H-L) (нули при
    i <= 3); rvi = Σ(num)/Σ(den) по окну [i-period+1, i], нулевые суммы →
    0.0, round(..., 2); пишется с i = period+1. Сигнальная =
    (rvi + 2*rvi[i-1] + 2*rvi[i-2] + rvi[i-3]) / 6: до i = period+6
    пишется 0.0 (else-ветка GetValueSecond), дальше round(..., 2).
    """
    n = len(candles)
    if period <= 0:
        raise ValueError("period must be positive")
    e = _entry(owner, ("rvi", period))
    if e is not None and _shared(e, candles):
        one, two = e["rvi"], e["sig"]
        move, rng, rvi_full = e["move"], e["rng"], e["full"]
        start = e["n"]
    else:
        one = [None] * n
        two = [None] * n
        move = [0.0] * n
        rng = [0.0] * n
        rvi_full = [0.0] * n
        start = 0
        if e is not None:
            e.update({"rvi": one, "sig": two, "move": move, "rng": rng, "full": rvi_full})
    if len(one) < n:
        one.extend([None] * (n - len(one)))
        two.extend([None] * (n - len(two)))
        move.extend([0.0] * (n - len(move)))
        rng.extend([0.0] * (n - len(rng)))
        rvi_full.extend([0.0] * (n - len(rvi_full)))
    for i in range(max(start, 4), n):
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
    for i in range(max(start, period + 1), n):
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
    _stamp(e, candles, n)
    return {"rvi": one, "signal": two}


def bulls_power(candles: Sequence[Candle], length: int = 13) -> list[float | None]:
    """BullsPower (Charts/CandleChart/Indicators/BullsPower.cs):
    High − SMA(Close) в семантике MovingAverage (см. _sma_charts).

    Первый валид — i = length+1, до прогрева None.
    """
    base = _sma_charts(candles, length)
    out: list[float | None] = []
    for i, c in enumerate(candles):
        v = base[i]
        out.append(None if v is None else round(c.high - v, 8))
    return out


def bears_power(candles: Sequence[Candle], length: int = 13) -> list[float | None]:
    """BearsPower (Charts/CandleChart/Indicators/BearsPower.cs):
    Low − SMA(Close), семантика та же, что у bulls_power.
    """
    base = _sma_charts(candles, length)
    out: list[float | None] = []
    for i, c in enumerate(candles):
        v = base[i]
        out.append(None if v is None else round(c.low - v, 8))
    return out


def _sma_charts(
    candles: Sequence[Candle],
    length: int,
    owner=None,
) -> list[float | None]:
    """SMA в семантике Charts/CandleChart/Indicators/MovingAverage
    (GetValueSimple — основа Bulls/Bears Power): первый валид — i = length+1
    (guard index - length <= 0), окно [i-length+1, i] по Close, round(..., 8).
    НЕ то же, что sma() выше (Scripts/Sma.cs через Summ) — граница прогрева
    сдвинута на бар.
    """
    n = len(candles)
    if length <= 0:
        raise ValueError("length must be positive")
    e = _entry(owner, ("sma_charts", length))
    if e is not None and _shared(e, candles):
        out = e["series"]
        start = e["n"]
    else:
        out = [None] * n
        start = 0
        if e is not None:
            e["series"] = out
    if len(out) < n:
        out.extend([None] * (n - len(out)))
    for i in range(max(start, length + 1), n):
        out[i] = round(
            sum(candles[j].close for j in range(i - length + 1, i + 1)) / length, 8
        )
    _stamp(e, candles, n)
    return out


def _power(
    candles: Sequence[Candle],
    period: int,
    attr: str,
    owner=None,
) -> list[float | None]:
    """Общая часть Bulls/Bears Power: точка - SMA(Close, period).

    Guards как в GetValue: i < period или SMA == 0 → 0 в C# (прогрев — None).
    """
    n = len(candles)
    if period <= 0:
        raise ValueError("period must be positive")
    sma_close = _sma_charts(candles, period, owner)
    result: list[float | None] = [None] * n
    for i in range(period + 1, n):
        ma = sma_close[i]
        result[i] = 0.0 if ma == 0.0 else getattr(candles[i], attr) - ma
    return result


def bulls_power(candles: Sequence[Candle], period: int = 13, owner=None) -> list[float | None]:  # noqa: F811
    """BullsPower (Charts/.../BullsPower.cs): High - SMA(Close, period).

    De jure тип Exponential, de facto обёртка создаёт MovingAverage(false)
    с дефолтами Simple/Close (quirk оригинала). Первый валид — i = period+1.
    """
    return _power(candles, period, "high", owner)


def bears_power(candles: Sequence[Candle], period: int = 13, owner=None) -> list[float | None]:  # noqa: F811
    """BearsPower (Charts/.../BearsPower.cs): Low - SMA(Close, period).

    Гварды те же, что у BullsPower.
    """
    return _power(candles, period, "low", owner)
