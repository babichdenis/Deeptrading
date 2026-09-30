"""IndicatorHub — декларативный кэш индикаторов поверх CandleHub.

Референс: docs/osengine/PORT_NOTES_CANDLEHUB.md.

КОНТУР ОТВЕТСТВЕННОСТИ
- CandleHub владеет свечами и СТРОИТ старшие ТФ из 1m. IndicatorHub не
  ресемплит: он считает только по закрытым барам того (figi, tf), который
  ему дали.
- Состояние индикатора — пара (figi, tf, name, params). RSI(SBER, 1m) и
  RSI(SBER, 5m) — разные индикаторы с разными рядами значений.
- Индикаторы создаются ТОЛЬКО по явной подписке. Никакого «включить все
  индикаторы на все ТФ»: требования объявляет потребитель (стратегия),
  прогрев считается под его требования (required_bars).

ДВЕ ФАЗЫ
- commit(closed) — единственная фаза, которая пишет состояние.
- preview(forming) — считает по закрытым барам + текущий forming-бар,
  возвращает значения наружу и НЕ трогает состояние. Формирующаяся свеча
  не имеет права загрязнять состояние индикатора.

WARMUP
- required_bars(params) — сколько ЗАКРЫТЫХ баров нужно индикатору, чтобы
  последнее значение перестало быть None. Зависит от параметров, а не от
  захардкоженного числа.
- warmup(figi, tf, candles) — полный пересчёт подписки по переданной
  истории: состояние сбрасывается и собирается заново, поэтому повторный
  прогрев на том же входе даёт тот же результат (детерминизм реплея).
- Окно пересчёта ограничено max_lookback. Для рекуррентных индикаторов
  (EMA/MACD/ADX) это КОНЕЧНОЕ окно (ENG-021, audit 2026-09-29): после
  выпадения старых баров значение может слегка дрейфовать относительно
  full-history варианта. Свойство осознанное и зафиксировано: для точного
  совпадения с full-history нужен настоящий incremental state (задача C-13).
  Маркер: IndicatorHub.finite_window == True. Каноническим считается
  последний бар окна: рекурсия к нему сходится, левое краевое значение
  окна может отличаться от бесконечной истории.

ФОРМА ТОЧКИ
- Индикатор — чистая функция от последовательности закрытых свечей,
  возвращающая dict[row_name, list[float | None]] той же длины.
- stateful=False — индикатор не тянет состояние (значение бара зависит
  только от его собственных окон); stateful=True — рекуррентный.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Sequence

from app.engine.models import Candle

Number = float | None
Rows = dict[str, list[Number]]


@dataclass(frozen=True)
class IndicatorKey:
    figi: str
    tf_seconds: int
    name: str
    params_hash: str


@dataclass(frozen=True)
class ParameterSpec:
    name: str
    type: type
    default: Any
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class IndicatorDefinition:
    name: str
    category: str
    parameters: tuple[ParameterSpec, ...]
    calculate: Callable[[Sequence[Candle], dict], Rows]
    warmup: Callable[[dict], int]
    primary: str = "value"
    dependencies: tuple[str, ...] = ()
    stateful: bool = True
    supports_preview: bool = True

    def resolve(self, params: dict | None = None) -> dict:
        if params:
            known = {spec.name for spec in self.parameters}
            unknown = sorted(set(params) - known)
            if unknown:
                raise ValueError(
                    f"{self.name}: unknown params {unknown}; known: {sorted(known)}"
                )
        out: dict[str, Any] = {}
        for spec in self.parameters:
            out[spec.name] = self._coerce(spec, (params or {}).get(spec.name, spec.default))
        return out

    def required_bars(self, params: dict | None = None) -> int:
        """Сколько закрытых баров нужно до первого не-None значения.

        Учитывает и зависимости: композиция (adx поверх atr) не может быть
        готова раньше своей части. У каждой зависимости своя схема параметров,
        поэтому делятся только параметры с совпадающими именами.
        """
        resolved = self.resolve(params)
        own = int(self.warmup(resolved))
        for dep_name in self.dependencies:
            dep = INDICATORS.get(dep_name)
            if dep is None:
                continue
            dep_params = {
                k: v for k, v in resolved.items()
                if k in {spec.name for spec in dep.parameters}
            }
            own = max(own, dep.required_bars(dep_params))
        return own

    @property
    def warmup_period(self) -> int:
        """required_bars на дефолтных параметрах (метаданные для UI/логов)."""
        return self.required_bars()

    def default_params(self) -> dict:
        return self.resolve()

    def _coerce(self, spec: ParameterSpec, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(f"{self.name}.{spec.name}: bool is not a number")
        if spec.type is int and isinstance(value, float):
            if not value.is_integer():
                raise ValueError(f"{self.name}.{spec.name}: {value!r} is not an int")
            value = int(value)
        elif spec.type is float and isinstance(value, int):
            value = float(value)
        if not isinstance(value, spec.type):
            raise ValueError(
                f"{self.name}.{spec.name}: expected {spec.type.__name__}, got {value!r}"
            )
        if spec.minimum is not None and value < spec.minimum:
            raise ValueError(f"{self.name}.{spec.name}: {value} < min {spec.minimum}")
        if spec.maximum is not None and value > spec.maximum:
            raise ValueError(f"{self.name}.{spec.name}: {value} > max {spec.maximum}")
        return value


def _spec(name: str, type_: type, default: Any, minimum=None, maximum=None) -> ParameterSpec:
    return ParameterSpec(name=name, type=type_, default=default, minimum=minimum, maximum=maximum)


def params_hash(params: dict) -> str:
    return ",".join(f"{k}={params[k]!r}" for k in sorted(params))


def resolve_params(name: str, params: dict | None = None) -> dict:
    definition = INDICATORS.get(name)
    if definition is None:
        raise KeyError(f"unknown indicator: {name!r}")
    return definition.resolve(params)


# --- индикаторы ---------------------------------------------------------


def _sma(values: Sequence[float], length: int) -> list[Number]:
    n = len(values)
    result: list[Number] = [None] * n
    if length <= 0 or n < length:
        return result
    for i in range(length - 1, n):
        result[i] = sum(values[i - length + 1 : i + 1]) / length
    return result


def _ema(values: Sequence[float], length: int) -> list[Number]:
    n = len(values)
    result: list[Number] = [None] * n
    if length <= 0 or n < length:
        return result
    alpha = 2.0 / (length + 1)
    result[length - 1] = sum(values[:length]) / length
    for i in range(length, n):
        prev = result[i - 1]
        result[i] = values[i] * alpha + prev * (1 - alpha)
    return result


def _true_range(candles: Sequence[Candle]) -> list[float]:
    n = len(candles)
    tr = [0.0] * n
    for i in range(n):
        high, low = candles[i].high, candles[i].low
        tr[i] = high - low
        if i > 0:
            pc = candles[i - 1].close
            tr[i] = max(tr[i], abs(high - pc), abs(low - pc))
    return tr


def _atr(candles: Sequence[Candle], length: int) -> list[Number]:
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n < length:
        return result
    tr = _true_range(candles)
    result[length - 1] = sum(tr[:length]) / length
    for i in range(length, n):
        result[i] = (result[i - 1] * (length - 1) + tr[i]) / length
    return result


def _efficiency_ratio(candles: Sequence[Candle], length: int) -> list[Number]:
    """EfficiencyRatio Кауфмана: |C[t]-C[t-n]| / Σ|ΔC| за n шагов (0..1).

    «Прямота» движения: низкое = шум/боковик, высокое = устойчивый тренд.
    Плоский ряд (знаменатель 0) → 0.0. Первый валидный индекс — i = length.
    """
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n < length + 1:
        return result
    for i in range(length, n):
        change = abs(candles[i].close - candles[i - length].close)
        volatility = 0.0
        for j in range(i - length + 1, i + 1):
            volatility += abs(candles[j].close - candles[j - 1].close)
        result[i] = (change / volatility) if volatility > 0 else 0.0
    return result


def _rsi(candles: Sequence[Candle], length: int) -> list[Number]:
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n < length + 1:
        return result
    gains = [0.0] * n
    losses = [0.0] * n
    for i in range(1, n):
        diff = candles[i].close - candles[i - 1].close
        if diff > 0:
            gains[i] = diff
        else:
            losses[i] = -diff
    avg_gain = sum(gains[1 : length + 1]) / length
    avg_loss = sum(losses[1 : length + 1]) / length
    result[length] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(length + 1, n):
        avg_gain = (avg_gain * (length - 1) + gains[i]) / length
        avg_loss = (avg_loss * (length - 1) + losses[i]) / length
        result[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return result


def _stochastic(candles: Sequence[Candle], k: int, d: int) -> Rows:
    n = len(candles)
    k_series: list[Number] = [None] * n
    d_series: list[Number] = [None] * n
    if k <= 0 or d <= 0 or n < k:
        return {"k": k_series, "d": d_series}
    for i in range(k - 1, n):
        window = candles[i - k + 1 : i + 1]
        hh = max(c.high for c in window)
        ll = min(c.low for c in window)
        k_series[i] = 50.0 if hh == ll else 100.0 * (candles[i].close - ll) / (hh - ll)
    for i in range(k + d - 2, n):
        values = [v for v in k_series[i - d + 1 : i + 1] if v is not None]
        if len(values) == d:
            d_series[i] = sum(values) / d
    return {"k": k_series, "d": d_series}


def _bollinger(candles: Sequence[Candle], length: int, mult: float) -> Rows:
    n = len(candles)
    up: list[Number] = [None] * n
    center: list[Number] = [None] * n
    down: list[Number] = [None] * n
    if length <= 0 or n < length:
        return {"up": up, "center": center, "down": down}
    for i in range(length - 1, n):
        window = [candles[j].close for j in range(i - length + 1, i + 1)]
        mean = sum(window) / length
        std = math.sqrt(sum((x - mean) ** 2 for x in window) / length)
        center[i] = mean
        up[i] = mean + mult * std
        down[i] = mean - mult * std
    return {"up": up, "center": center, "down": down}


def _macd(candles: Sequence[Candle], fast: int, slow: int, signal: int) -> Rows:
    n = len(candles)
    macd_line: list[Number] = [None] * n
    signal_line: list[Number] = [None] * n
    hist: list[Number] = [None] * n
    if min(fast, slow, signal) <= 0 or n < slow:
        return {"macd": macd_line, "signal": signal_line, "histogram": hist}
    closes = [c.close for c in candles]
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    for i in range(n):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
    valid = [i for i in range(n) if macd_line[i] is not None]
    if len(valid) >= signal:
        first = valid[0]
        signal_line[first + signal - 1] = sum(
            macd_line[i] for i in valid[:signal]
        ) / signal
        alpha = 2.0 / (signal + 1)
        for i in range(first + signal, n):
            if macd_line[i] is not None:
                signal_line[i] = macd_line[i] * alpha + signal_line[i - 1] * (1 - alpha)
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            hist[i] = macd_line[i] - signal_line[i]
    return {"macd": macd_line, "signal": signal_line, "histogram": hist}


def _adx(candles: Sequence[Candle], length: int) -> Rows:
    """Wilder ADX (+DI/-DI/ADX) — каноническая реализация (ENG-010, audit 2026-09-29).

    Отличие от прежней версии: directional movement и TR сглаживаются
    по Wilder (running sum c вычитанием prev/length), а не берутся за один
    бар; ADX — Wilder-сглаживание DX. Reference-вектор: см.
    tests/test_audit_2026_09_29.py::test_adx_wilder_reference_vector.
    """
    n = len(candles)
    plus_di: list[Number] = [None] * n
    minus_di: list[Number] = [None] * n
    adx_line: list[Number] = [None] * n
    if length <= 0 or n < length * 2:
        return {"+di": plus_di, "-di": minus_di, "adx": adx_line}
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
        tr[i] = max(
            candles[i].high - candles[i].low,
            abs(candles[i].high - candles[i - 1].close),
            abs(candles[i].low - candles[i - 1].close),
        )
    # Wilder seed: суммы за первые length значений (i = 1..length).
    smooth_tr = sum(tr[1:length + 1])
    smooth_plus = sum(plus_dm[1:length + 1])
    smooth_minus = sum(minus_dm[1:length + 1])

    def _di(p: float, m: float) -> tuple[float, float, float]:
        if smooth_tr <= 0:
            return 0.0, 0.0, 0.0
        pd = 100.0 * p / smooth_tr
        md = 100.0 * m / smooth_tr
        total = pd + md
        dx = 0.0 if total == 0 else 100.0 * abs(pd - md) / total
        return pd, md, dx

    dx: list[Number] = [None] * n
    first = length
    p, m, d = _di(smooth_plus, smooth_minus)
    plus_di[first], minus_di[first], dx[first] = p, m, d
    for i in range(first + 1, n):
        smooth_tr = smooth_tr - smooth_tr / length + tr[i]
        smooth_plus = smooth_plus - smooth_plus / length + plus_dm[i]
        smooth_minus = smooth_minus - smooth_minus / length + minus_dm[i]
        p, m, d = _di(smooth_plus, smooth_minus)
        plus_di[i], minus_di[i], dx[i] = p, m, d
    valid = [i for i in range(first, n) if dx[i] is not None]
    if len(valid) >= length:
        adx_start = valid[length - 1]
        adx_line[adx_start] = sum(dx[i] for i in valid[:length]) / length
        for i in range(adx_start + 1, n):
            if dx[i] is not None:
                adx_line[i] = (adx_line[i - 1] * (length - 1) + dx[i]) / length
    return {"+di": plus_di, "-di": minus_di, "adx": adx_line}


def _envelops(candles: Sequence[Candle], length: int, deviation: float) -> Rows:
    """Конверты (канон проекта): SMA ± deviation% от уровня (не от шага цены)."""
    n = len(candles)
    up: list[Number] = [None] * n
    center: list[Number] = [None] * n
    down: list[Number] = [None] * n
    if length <= 0:
        return {"up": up, "center": center, "down": down}
    sma_series = _sma([c.close for c in candles], length)
    for i in range(n):
        v = sma_series[i]
        if v is None:
            continue
        center[i] = v
        up[i] = v + v * deviation / 100.0
        down[i] = v - v * deviation / 100.0
    return {"up": up, "center": center, "down": down}


def _donchian(candles: Sequence[Candle], length: int) -> Rows:
    """Канал по ПРЕДЫДУЩИМ length барам (текущий бар в окно не входит)."""
    n = len(candles)
    upper: list[Number] = [None] * n
    lower: list[Number] = [None] * n
    if length <= 0 or n <= length:
        return {"upper": upper, "lower": lower}
    for i in range(length, n):
        window = candles[i - length : i]
        upper[i] = max(c.high for c in window)
        lower[i] = min(c.low for c in window)
    return {"upper": upper, "lower": lower}


def _returns(candles: Sequence[Candle], length: int) -> list[Number]:
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n <= length:
        return result
    for i in range(length, n):
        prev = candles[i - length].close
        if prev:
            result[i] = (candles[i].close - prev) / prev * 100.0
    return result


def _range_atr(candles: Sequence[Candle], length: int) -> list[Number]:
    n = len(candles)
    result: list[Number] = [None] * n
    atr_values = _atr(candles, length)
    for i in range(n):
        atr_value = atr_values[i]
        if atr_value:
            result[i] = (candles[i].high - candles[i].low) / atr_value
    return result


def _volume_sma(candles: Sequence[Candle], length: int) -> list[Number]:
    return _sma([c.volume for c in candles], length)


def _relative_volume(candles: Sequence[Candle], length: int) -> list[Number]:
    n = len(candles)
    result: list[Number] = [None] * n
    average = _volume_sma(candles, length)
    for i in range(n):
        if average[i]:
            result[i] = candles[i].volume / average[i]
    return result


def _rolling_high_low(candles: Sequence[Candle], length: int) -> Rows:
    n = len(candles)
    high: list[Number] = [None] * n
    low: list[Number] = [None] * n
    if length <= 0 or n <= length:
        return {"high": high, "low": low}
    for i in range(length, n):
        window = candles[i - length : i]
        high[i] = max(c.high for c in window)
        low[i] = min(c.low for c in window)
    return {"high": high, "low": low}


def _candle_body(candles: Sequence[Candle]) -> list[Number]:
    return [
        (c.close - c.open) / c.open * 100.0 if c.open else None for c in candles
    ]


def _wicks(candles: Sequence[Candle]) -> Rows:
    upper: list[Number] = []
    lower: list[Number] = []
    for c in candles:
        span = c.high - c.low
        if span <= 0:
            upper.append(None)
            lower.append(None)
            continue
        upper.append((c.high - max(c.open, c.close)) / span * 100.0)
        lower.append((min(c.open, c.close) - c.low) / span * 100.0)
    return {"upper": upper, "lower": lower}


def _range(candles: Sequence[Candle]) -> list[Number]:
    return [
        (c.high - c.low) / c.close * 100.0 if c.close else None for c in candles
    ]


def _vwap(candles: Sequence[Candle], length: int) -> list[Number]:
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n < length:
        return result
    typical = [((c.high + c.low + c.close) / 3.0) * c.volume for c in candles]
    volumes = [c.volume for c in candles]
    for i in range(length - 1, n):
        denominator = sum(volumes[i - length + 1 : i + 1])
        if denominator:
            result[i] = sum(typical[i - length + 1 : i + 1]) / denominator
    return result


def _williams_r(candles: Sequence[Candle], length: int) -> list[Number]:
    """Williams %R (OSE Scripts/WilliamsRange.cs, дефолт 14).

    -100 * (HH - C) / (HH - LL) за окно length, округление 2 знака.
    Первый валидный индекс — length (в C# на прогреве 0, здесь None —
    семантика канона проекта, как у rsi_trade_hub); HH == LL → 0.0.
    """
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n < length + 1:
        return result
    for i in range(length, n):
        window = candles[i - length + 1 : i + 1]
        hh = max(c.high for c in window)
        ll = min(c.low for c in window)
        if hh == ll:
            result[i] = 0.0
        else:
            result[i] = round(-100.0 * (hh - candles[i].close) / (hh - ll), 2)
    return result


def _momentum(candles: Sequence[Candle], length: int) -> list[Number]:
    """Momentum (OSE Scripts/Momentum.cs, дефолт 5, точка Close).

    C / C[length назад] * 100; без округления (в C# его нет).
    Первый валидный индекс — length; делитель 0 → 0.0 (как в C#).
    """
    n = len(candles)
    result: list[Number] = [None] * n
    if length <= 0 or n < length + 1:
        return result
    for i in range(length, n):
        divider = candles[i - length].close
        result[i] = candles[i].close / divider * 100.0 if divider != 0 else 0.0
    return result


def _parabolic_sar(candles: Sequence[Candle], af: float, max_af: float) -> Rows:
    """Parabolic SAR (Wilder; OSE Scripts/ParabolicSAR.cs, Af 0.02 / MaxAf 0.2).

    rows: sar — уровень стопа (round 6), trend — +1/-1 (направление).
    Реверс: SAR = предыдущий EP; ускорение растёт на новых экстремумах,
    кап на max_af; SAR не проходит через low/high двух предыдущих баров.
    """
    n = len(candles)
    sar: list[Number] = [None] * n
    trend_row: list[Number] = [None] * n
    if n < 2 or af <= 0 or max_af < af:
        return {"sar": sar, "trend": trend_row}
    up = candles[1].close >= candles[0].close
    ep = candles[0].high if up else candles[0].low
    cur = candles[0].low if up else candles[0].high
    accel = af
    for i in range(1, n):
        cur = cur + accel * (ep - cur)
        if up:
            lo1 = candles[i - 1].low
            cur = min(cur, lo1, candles[i - 2].low if i >= 2 else lo1)
            if cur > candles[i].low:
                up, cur, ep, accel = False, ep, candles[i].low, af
            elif candles[i].high > ep:
                ep, accel = candles[i].high, min(accel + af, max_af)
        else:
            hi1 = candles[i - 1].high
            cur = max(cur, hi1, candles[i - 2].high if i >= 2 else hi1)
            if cur < candles[i].high:
                up, cur, ep, accel = True, ep, candles[i].high, af
            elif candles[i].low < ep:
                ep, accel = candles[i].low, min(accel + af, max_af)
        sar[i] = round(cur, 6)
        trend_row[i] = 1.0 if up else -1.0
    return {"sar": sar, "trend": trend_row}


def _length(params: dict) -> int:
    return int(params["length"])


INDICATORS: dict[str, IndicatorDefinition] = {
    "sma": IndicatorDefinition(
        name="sma",
        category="trend",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: {"value": _sma([x.close for x in c], _length(p))},
        warmup=_length,
    ),
    "ema": IndicatorDefinition(
        name="ema",
        category="trend",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: {"value": _ema([x.close for x in c], _length(p))},
        warmup=_length,
    ),
    "vwap": IndicatorDefinition(
        name="vwap",
        category="trend",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: {"value": _vwap(c, _length(p))},
        warmup=_length,
    ),
    "adx": IndicatorDefinition(
        name="adx",
        category="trend",
        parameters=(_spec("length", int, 14, minimum=2),),
        calculate=lambda c, p: _adx(c, _length(p)),
        warmup=lambda p: 2 * _length(p),
        primary="adx",
        dependencies=("atr",),
    ),
    "macd": IndicatorDefinition(
        name="macd",
        category="trend",
        parameters=(
            _spec("fast", int, 12, minimum=1),
            _spec("slow", int, 26, minimum=1),
            _spec("signal", int, 9, minimum=1),
        ),
        calculate=lambda c, p: _macd(c, int(p["fast"]), int(p["slow"]), int(p["signal"])),
        warmup=lambda p: int(p["slow"]) + int(p["signal"]) - 1,
        primary="macd",
        dependencies=("ema",),
    ),
    "rsi": IndicatorDefinition(
        name="rsi",
        category="momentum",
        parameters=(_spec("length", int, 14, minimum=1),),
        calculate=lambda c, p: {"value": _rsi(c, _length(p))},
        warmup=lambda p: _length(p) + 1,
    ),
    "efficiency_ratio": IndicatorDefinition(
        name="efficiency_ratio",
        category="trend",
        parameters=(_spec("length", int, 10, minimum=1),),
        calculate=lambda c, p: {"value": _efficiency_ratio(c, _length(p))},
        warmup=lambda p: _length(p) + 1,
    ),
    "stochastic": IndicatorDefinition(
        name="stochastic",
        category="momentum",
        parameters=(
            _spec("k", int, 14, minimum=1),
            _spec("d", int, 3, minimum=1),
        ),
        calculate=lambda c, p: _stochastic(c, int(p["k"]), int(p["d"])),
        warmup=lambda p: int(p["k"]) + int(p["d"]) - 1,
        primary="k",
    ),
    "atr": IndicatorDefinition(
        name="atr",
        category="volatility",
        parameters=(_spec("length", int, 14, minimum=1),),
        calculate=lambda c, p: {"value": _atr(c, _length(p))},
        warmup=_length,
    ),
    "bollinger": IndicatorDefinition(
        name="bollinger",
        category="volatility",
        parameters=(
            _spec("length", int, 20, minimum=1),
            _spec("mult", float, 2.0, minimum=0.0),
        ),
        calculate=lambda c, p: _bollinger(c, _length(p), float(p["mult"])),
        warmup=_length,
        primary="center",
        dependencies=("sma",),
    ),
    "envelops": IndicatorDefinition(
        name="envelops",
        category="volatility",
        parameters=(
            _spec("length", int, 10, minimum=2),
            _spec("deviation", float, 0.3, minimum=0.01),
        ),
        calculate=lambda c, p: _envelops(c, _length(p), float(p["deviation"])),
        warmup=_length,
        primary="center",
        dependencies=("sma",),
    ),
    "donchian": IndicatorDefinition(
        name="donchian",
        category="price_structure",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: _donchian(c, _length(p)),
        warmup=lambda p: _length(p) + 1,
        primary="upper",
    ),
    "returns": IndicatorDefinition(
        name="returns",
        category="price_structure",
        parameters=(_spec("length", int, 1, minimum=1),),
        calculate=lambda c, p: {"value": _returns(c, _length(p))},
        warmup=lambda p: _length(p) + 1,
        stateful=False,
    ),
    "range_atr": IndicatorDefinition(
        name="range_atr",
        category="price_structure",
        parameters=(_spec("length", int, 14, minimum=1),),
        calculate=lambda c, p: {"value": _range_atr(c, _length(p))},
        warmup=_length,
        stateful=False,
        dependencies=("atr",),
    ),
    "volume_sma": IndicatorDefinition(
        name="volume_sma",
        category="volume",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: {"value": _volume_sma(c, _length(p))},
        warmup=_length,
    ),
    "relative_volume": IndicatorDefinition(
        name="relative_volume",
        category="volume",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: {"value": _relative_volume(c, _length(p))},
        warmup=_length,
        stateful=False,
        dependencies=("volume_sma",),
    ),
    "rolling_high_low": IndicatorDefinition(
        name="rolling_high_low",
        category="price_structure",
        parameters=(_spec("length", int, 20, minimum=1),),
        calculate=lambda c, p: _rolling_high_low(c, _length(p)),
        warmup=lambda p: _length(p) + 1,
        primary="high",
    ),
    "candle_body": IndicatorDefinition(
        name="candle_body",
        category="price_structure",
        parameters=(),
        calculate=lambda c, p: {"value": _candle_body(c)},
        warmup=lambda p: 1,
        stateful=False,
    ),
    "wicks": IndicatorDefinition(
        name="wicks",
        category="price_structure",
        parameters=(),
        calculate=lambda c, p: _wicks(c),
        warmup=lambda p: 1,
        stateful=False,
    ),
    "range": IndicatorDefinition(
        name="range",
        category="price_structure",
        parameters=(),
        calculate=lambda c, p: {"value": _range(c)},
        warmup=lambda p: 1,
        stateful=False,
    ),
    "williams_r": IndicatorDefinition(
        name="williams_r",
        category="momentum",
        parameters=(_spec("length", int, 14, minimum=1),),
        calculate=lambda c, p: {"value": _williams_r(c, _length(p))},
        warmup=lambda p: _length(p) + 1,
    ),
    "momentum": IndicatorDefinition(
        name="momentum",
        category="momentum",
        parameters=(_spec("length", int, 5, minimum=1),),
        calculate=lambda c, p: {"value": _momentum(c, _length(p))},
        warmup=lambda p: _length(p) + 1,
    ),
    "parabolic_sar": IndicatorDefinition(
        name="parabolic_sar",
        category="trend",
        parameters=(
            _spec("af", float, 0.02, minimum=0.01),
            _spec("max_af", float, 0.2, minimum=0.02),
        ),
        calculate=lambda c, p: _parabolic_sar(c, float(p["af"]), float(p["max_af"])),
        warmup=lambda p: 2,
        primary="sar",
    ),
}


UpdatedListener = Callable[[str, int, str], None]
PreviewListener = Callable[[str, int, dict[str, dict[str, Number]]], None]
ErrorListener = Callable[[str, int, str, str], None]
SubscribeListener = Callable[[IndicatorKey], None]


class IndicatorHub:
    """Кэш индикаторов в RAM по (figi, tf, name, params).

    Создаёт состояние ТОЛЬКО для явно подписанных комбинаций. События:
    on_updated (commit), on_preview (forming, без записи), on_error,
    on_subscribe (оркестратор навешивает TF-ряд и считает прогрев).
    """

    def __init__(self, max_lookback: int = 500):
        if max_lookback < 1:
            raise ValueError("max_lookback must be >= 1")
        self._max_lookback = max_lookback
        self._cache: dict[IndicatorKey, dict[str, dict[datetime, float]]] = {}
        self._params: dict[IndicatorKey, dict] = {}
        self.on_updated: list[UpdatedListener] = []
        self.on_preview: list[PreviewListener] = []
        self.on_error: list[ErrorListener] = []
        self.on_subscribe: list[SubscribeListener] = []

    @property
    def max_lookback(self) -> int:
        """Размер окна пересчёта (не меньше required_bars индикатора)."""
        return self._max_lookback

    @property
    def finite_window(self) -> bool:
        """ENG-021: рекуррентные индикаторы считаются по конечному окну
        max_lookback — маркер конечного окна (не full-history)."""
        return True

    # --- подписки --------------------------------------------------------
    def subscribe(
        self, figi: str, tf_seconds: int, name: str, params: dict | None = None
    ) -> IndicatorKey:
        key = self.key_for(figi, tf_seconds, name, params)
        if key not in self._params:
            self._params[key] = INDICATORS[name].resolve(params)
            for cb in list(self.on_subscribe):
                cb(key)
        return key

    def unsubscribe(
        self, figi: str, tf_seconds: int, name: str, params: dict | None = None
    ) -> bool:
        key = self.key_for(figi, tf_seconds, name, params)
        if key not in self._params:
            return False
        del self._params[key]
        self._cache.pop(key, None)
        return True

    def key_for(
        self, figi: str, tf_seconds: int, name: str, params: dict | None = None
    ) -> IndicatorKey:
        if tf_seconds < 60:
            raise ValueError(f"tf_seconds must be >= 60, got {tf_seconds}")
        definition = INDICATORS.get(name)
        if definition is None:
            raise KeyError(f"unknown indicator: {name!r}")
        resolved = definition.resolve(params)
        return IndicatorKey(figi, tf_seconds, name, params_hash(resolved))

    def is_subscribed(
        self, figi: str, tf_seconds: int, name: str, params: dict | None = None
    ) -> bool:
        return self.key_for(figi, tf_seconds, name, params) in self._params

    def subscriptions(
        self, figi: str | None = None, tf_seconds: int | None = None
    ) -> list[IndicatorKey]:
        return [
            key
            for key in self._params
            if (figi is None or key.figi == figi)
            and (tf_seconds is None or key.tf_seconds == tf_seconds)
        ]

    def timeframes(self, figi: str) -> list[int]:
        return sorted({key.tf_seconds for key in self._params if key.figi == figi})

    def required_bars(self, figi: str, tf_seconds: int) -> int:
        """Сколько закрытых баров нужно figi×tf, чтобы все подписки были готовы."""
        need = 0
        for key in self._params:
            if key.figi != figi or key.tf_seconds != tf_seconds:
                continue
            need = max(need, INDICATORS[key.name].required_bars(self._params[key]))
        return need

    # --- запись ----------------------------------------------------------
    def commit(self, figi: str, tf_seconds: int, candles: Sequence[Candle]) -> None:
        """Закрытый бар — единственная фаза, которая пишет состояние."""
        for key in self._keys_for(figi, tf_seconds):
            definition = INDICATORS[key.name]
            params = self._params[key]
            window = self._window(candles, definition.required_bars(params))
            try:
                result = definition.calculate(window, params)
            except Exception as exc:
                self._emit_error(figi, tf_seconds, key.name, str(exc))
                continue
            self._store(key, window, result)
            self._emit_updated(figi, tf_seconds, key.name)

    def update(
        self,
        figi: str,
        tf_seconds: int,
        candles: Sequence[Candle],
        *,
        is_closed: bool = True,
    ) -> None:
        """Совместимый вход: is_closed=True → commit, False → preview (без записи)."""
        if is_closed:
            self.commit(figi, tf_seconds, candles)
        else:
            self.preview(figi, tf_seconds, candles)

    def preview(
        self,
        figi: str,
        tf_seconds: int,
        closed: Sequence[Candle] = (),
        forming: Candle | None = None,
    ) -> dict[str, dict[str, Number]]:
        """Значения по forming-бару. Состояние НЕ меняется.

        Считает по closed + (forming, если он новее последнего закрытого).
        Ключ результата — имя индикатора; если на (figi, tf) один индикатор
        подписан с разными параметрами, к ключу добавляется params_hash.
        """
        keys = self._keys_for(figi, tf_seconds)
        if not keys:
            return {}
        series = list(closed)
        if forming is not None and (not series or forming.ts > series[-1].ts):
            series.append(forming)
        names: dict[str, int] = {}
        for key in keys:
            names[key.name] = names.get(key.name, 0) + 1
        out: dict[str, dict[str, Number]] = {}
        for key in keys:
            definition = INDICATORS[key.name]
            if not definition.supports_preview:
                continue
            params = self._params[key]
            window = self._window(series, definition.required_bars(params))
            if not window:
                continue
            try:
                result = definition.calculate(window, params)
            except Exception as exc:
                self._emit_error(figi, tf_seconds, key.name, str(exc))
                continue
            label = key.name
            if names[label] > 1:
                label = f"{label}:{key.params_hash}"
            out[label] = {row: values[-1] for row, values in result.items() if values}
        for cb in list(self.on_preview):
            cb(figi, tf_seconds, out)
        return out

    def warmup(self, figi: str, tf_seconds: int, candles: Sequence[Candle]) -> None:
        """Полный пересчёт подписок (figi, tf) по истории — с нуля.

        Состояние сбрасывается, поэтому повторный прогрев на том же входе
        даёт тот же результат: реплей и live считаются одинаково.
        """
        for key in self._keys_for(figi, tf_seconds):
            self._cache.pop(key, None)
        self.commit(figi, tf_seconds, candles)

    # --- чтение ----------------------------------------------------------
    def get(
        self,
        figi: str,
        tf_seconds: int,
        name: str,
        params: dict | None = None,
        row: str | None = None,
    ) -> float | None:
        definition = INDICATORS.get(name)
        if definition is None:
            return None
        return self.get_row(figi, tf_seconds, name, row or definition.primary, params)

    def get_row(
        self,
        figi: str,
        tf_seconds: int,
        name: str,
        row: str,
        params: dict | None = None,
    ) -> float | None:
        return self.get_at(figi, tf_seconds, name, params, row=row, ts=None)

    def get_at(
        self,
        figi: str,
        tf_seconds: int,
        name: str,
        params: dict | None = None,
        row: str | None = None,
        ts: datetime | None = None,
    ) -> float | None:
        """Значение строки индикатора: на последнем баре или на метке ts."""
        definition = INDICATORS.get(name)
        if definition is None:
            return None
        key = self.key_for(figi, tf_seconds, name, params)
        entry = self._cache.get(key)
        if not entry:
            return None
        series = entry.get(row or definition.primary)
        if not series:
            return None
        if ts is None:
            return series[max(series)]
        return series.get(ts)

    def get_series(
        self,
        figi: str,
        tf_seconds: int,
        name: str,
        params: dict | None = None,
        n: int | None = None,
        row: str | None = None,
    ) -> list[tuple[datetime, float]]:
        definition = INDICATORS.get(name)
        if definition is None:
            return []
        key = self.key_for(figi, tf_seconds, name, params)
        entry = self._cache.get(key)
        if not entry:
            return []
        series = entry.get(row or definition.primary)
        if not series:
            return []
        items = sorted(series.items())
        return items[-n:] if n else items

    def last_ts(self, figi: str, tf_seconds: int, name: str,
                params: dict | None = None) -> datetime | None:
        definition = INDICATORS.get(name)
        if definition is None:
            return None
        series = self.get_series(figi, tf_seconds, name, params)
        return series[-1][0] if series else None

    def is_ready(
        self, figi: str, tf_seconds: int, name: str, params: dict | None = None
    ) -> bool:
        return self.get(figi, tf_seconds, name, params) is not None

    def describe(
        self, figi: str | None = None, tf_seconds: int | None = None
    ) -> list[dict]:
        """Манифест готовности — для логов и UI (SUBSCRIPTION_READY)."""
        out: list[dict] = []
        for key in self.subscriptions(figi, tf_seconds):
            definition = INDICATORS[key.name]
            params = self._params[key]
            out.append(
                {
                    "figi": key.figi,
                    "tf_seconds": key.tf_seconds,
                    "indicator": key.name,
                    "params": dict(params),
                    "params_hash": key.params_hash,
                    "category": definition.category,
                    "stateful": definition.stateful,
                    "required_bars": definition.required_bars(params),
                    "ready": self.is_ready(key.figi, key.tf_seconds, key.name, params),
                    "last_ts": self.last_ts(key.figi, key.tf_seconds, key.name, params),
                }
            )
        return out

    # --- внутреннее ------------------------------------------------------
    def _keys_for(self, figi: str, tf_seconds: int) -> list[IndicatorKey]:
        return [
            key
            for key in self._params
            if key.figi == figi and key.tf_seconds == tf_seconds
        ]

    def _window(self, candles: Sequence[Candle], required: int) -> list[Candle]:
        size = max(self._max_lookback, required)
        return list(candles)[-size:]

    def _store(self, key: IndicatorKey, window: Sequence[Candle], result: Rows) -> None:
        entry = self._cache.setdefault(key, {})
        for stale in [r for r in entry if r not in result]:
            del entry[stale]
        if not window:
            return
        lo, hi = window[0].ts, window[-1].ts
        limit = max(self._max_lookback * 3, 64)
        for row_name, values in result.items():
            row = entry.setdefault(row_name, {})
            for ts in [t for t in row if lo <= t <= hi]:
                del row[ts]
            for candle, value in zip(window, values, strict=True):
                if value is not None:
                    row[candle.ts] = value
            if len(row) > limit:
                for ts in sorted(row)[: len(row) - limit]:
                    del row[ts]

    def _emit_updated(self, figi: str, tf: int, name: str) -> None:
        for cb in list(self.on_updated):
            cb(figi, tf, name)

    def _emit_error(self, figi: str, tf: int, name: str, error: str) -> None:
        for cb in list(self.on_error):
            cb(figi, tf, name, error)
