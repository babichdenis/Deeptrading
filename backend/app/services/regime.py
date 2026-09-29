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
        slope_threshold: float = 0.0002,  # |наклон EMA20| за 5 баров (в направлении дрейфа)
        adx_threshold: float = 19.0,      # ниже = боковик (оставлено для обратной совместимости)
        atr_percentile_threshold: float = 78.0,  # калибровано
        range_mult: float = 2.75,         # (high-low)/close > mult*ATR% → high vol (калибровано)
        drift_pct: float = 0.5,           # |дрейф close за drift_bars| → порог «есть движение»
        drift_bars: int = 6,              # окно дрейфа (6 часов)
        drift_strong_pct: float = 1.0,    # дрейф, при котором консистентность можно ослабить
        cons_pct: float = 0.71,           # доля баров в направлении дрейфа за cons_bars → тренд
        cons_relax_pct: float = 0.66,     # порог консистентности при сильном дрейфе (4 из 6)
        cons_bars: int = 6,
        atr_period: int = 14,
        ema_slope: int = 20,              # EMA для скорости (быстрая, отзывчивая)
        ema_fast: int = 20,
        ema_slow: int = 50,
        window: int = 200,
    ):
        self.slope_threshold = slope_threshold
        self.adx_threshold = adx_threshold
        self.atr_percentile_threshold = atr_percentile_threshold
        self.range_mult = range_mult
        self.drift_pct = drift_pct
        self.drift_bars = drift_bars
        self.drift_strong_pct = drift_strong_pct
        self.cons_pct = cons_pct
        self.cons_relax_pct = cons_relax_pct
        self.cons_bars = cons_bars
        self.atr_period = atr_period
        self.ema_slope = ema_slope
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.window = window

    def compute(self, candles: list[EngineCandle]) -> list[dict]:
        """Режим на каждом баре (по закрытию бара i)."""
        if len(candles) < max(self.ema_slow, self.atr_period) + 5:
            return []
        # Нормализуем числовые поля к float (свечи из БД приходят как Decimal).
        candles = [
            EngineCandle(ts=c.ts, open=float(c.open), high=float(c.high),
                         low=float(c.low), close=float(c.close),
                         volume=float(c.volume or 0))
            for c in candles
        ]
        closes = [c.close for c in candles]
        ema_f = _ema(closes, self.ema_fast)
        ema_s = _ema(closes, self.ema_slow)
        ema_sp = _ema(closes, self.ema_slope)
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
            slope = (ema_sp[i] - ema_sp[i - 5]) / max(ema_sp[i - 5], 1e-9)
            range_pct = (candles[i].high - candles[i].low) / max(candles[i].close, 1e-9) * 100
            a_pct = atr_pct[i]
            a_perc = atr_perc[i]
            a_adx = adx[i]
            v_ratio = vol_ratio[i]
            # дрейф за drift_bars (накопленное движение, в %)
            _db = self.drift_bars
            dr = (closes[i] - closes[max(0, i - _db)]) / max(closes[max(0, i - _db)], 1e-9) * 100
            # консистентность: доля баров за cons_bars в направлении дрейфа
            _cb = self.cons_bars
            _win = candles[max(0, i - _cb) : i + 1]
            _n = len(_win) - 1 if len(_win) > 1 else 1
            _ups = sum(1 for k in range(1, len(_win)) if _win[k].close >= _win[k - 1].close)
            cons = _ups / _n if _n else 0.5
            # «пила»: доля меньшинства баров не слишком мала (движение и туда, и сюда)
            _minority = min(_ups, _n - _ups) / _n if _n else 0.5
            state: str
            reason: str
            _driftok = dr >= self.drift_pct
            _consok = cons >= self.cons_pct
            # сильный дрейф ослабляет требование консистентности (≥cons_relax)
            _consrelax = cons >= self.cons_relax_pct and dr >= self.drift_strong_pct
            if a_perc >= self.atr_percentile_threshold or range_pct > self.range_mult * a_pct:
                state, reason = "HIGH_VOLATILITY", "atr_percentile_high_or_range_wide"
            elif slope >= 0 and _driftok and (_consok or _consrelax) and a_adx >= self.adx_threshold:
                state, reason = "TREND_UP", f"drift_up_cons{cons:.2f}"
            elif slope <= 0 and dr <= -self.drift_pct and \
                    (cons <= 1 - self.cons_pct or (cons <= 1 - self.cons_relax_pct and dr <= -self.drift_strong_pct)) \
                    and a_adx >= self.adx_threshold:
                state, reason = "TREND_DOWN", f"drift_dn_cons{cons:.2f}"
            elif abs(dr) < self.drift_pct:
                # дрейф за окно слишком мал — боковик, даже если ADX/наклон что-то «видят»
                state, reason = "RANGE", "low_drift"
            elif _minority >= 0.34:
                # движение в обе стороны примерно поровну — пила, тренда нет
                state, reason = "RANGE", "low_consistency"
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
                    "drift_pct": round(dr, 3),
                    "consistency": round(cons, 2),
                },
            })
        return out


# Индекс для regime_at: id списка -> (len, first, last, [ts]).
# Валидация по identity крайних элементов исключает ABA при reuse id().
_regime_ts_index: dict = {}


def regime_at(regime_bars: list[dict], ts: datetime) -> dict | None:
    """Режим для момента ts: режим последнего бара с ts <= query.

    Индекс по (id, len, first, last): внутри одного вызова строится один раз
    O(R), дальше O(log n). Контракт: строки упорядочены по ts (все продюсеры
    выдают ordered) — тогда эквивалентно линейному скану бит-в-бит.
    """
    if not regime_bars:
        return None
    import bisect as _bisect
    key = id(regime_bars)
    ent = _regime_ts_index.get(key)
    n = len(regime_bars)
    first = regime_bars[0]
    last = regime_bars[-1]
    if ent is None or ent[0] != n or ent[1] is not first or ent[2] is not last:
        ent = (n, first, last, [r.get("ts") for r in regime_bars])
        _regime_ts_index[key] = ent
        if len(_regime_ts_index) > 64:
            _regime_ts_index.pop(next(iter(_regime_ts_index)))
    i = _bisect.bisect_right(ent[3], ts) - 1
    if i < 0:
        return None
    return regime_bars[i]


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



def detect_regime(bars: list, **detector_kwargs) -> tuple:
    """Единый пересчёт режима по ГОТОВЫМ барам (ресемплированному ряду).

    Единственная точка, где индикаторный детектор считается для бота:
    compute_ensemble (бэктест), стратегия, runtime-фолбэк и heatmap зовут её.
    Возвращает (states, timeline, bars).
    """
    det = RegimeDetector(**detector_kwargs)
    states = det.compute(list(bars)) or []
    if states:
        tl = regime_timeline(states)
    else:
        tl = []
    return states, tl, list(bars)


def compute_regime(candles_1m: list, tf_seconds: int = 3600, **detector_kwargs) -> tuple:
    """Единый пересчёт режима из 1м-свечей: ресемпл на ТФ -> detect_regime.

    Возвращает (states, timeline, bars). tf_seconds=3600 — H1 (режимный ТФ по умолчанию).
    Для тяжёлых вызовов можно ресемплировать заранее cached_resample и звать detect_regime.
    """
    from app.services.ensemble import resample  # lazy-импорт: избегаем цикла ensemble<->regime
    bars = resample(list(candles_1m), tf_seconds)
    return detect_regime(bars, **detector_kwargs)
