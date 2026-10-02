"""Regime v2 — пакет нового контракта (Stage C).

C.1 measurements: нормализованные измерения осей поверх канонических
индикаторов `app/engine/indicatorhub.py` (Wilder ATR/ADX, EMA, Kaufman ER).
В runtime не подключён: используется harness'ом осей до golden-паритета.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Any, Sequence

from app.engine.indicatorhub import _adx, _atr, _efficiency_ratio, _ema, _range_atr
from app.engine.models import Candle


@dataclass(frozen=True)
class RegimeV2Params:
    window: int = 12
    atr_period: int = 14
    ema_slow: int = 50
    ema_fast: int = 20
    percentile_window: int = 200
    slope_bars: int = 5


def rolling_percentile(values: Sequence[float | None], window: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i, v in enumerate(values):
        if v is None:
            continue
        trailing = [x for x in values[max(0, i - window):i + 1] if x is not None]
        if len(trailing) < 5:
            out[i] = 50.0
            continue
        below = sum(1 for x in trailing if x < v)
        out[i] = below / len(trailing) * 100.0
    return out


def directional_consistency(bars: Sequence[Candle], window: int) -> list[float | None]:
    n = len(bars)
    out: list[float | None] = [None] * n
    if window <= 0:
        return out
    for i in range(window, n):
        ups = sum(1 for k in range(i - window + 1, i + 1) if bars[k].close >= bars[k - 1].close)
        out[i] = ups / window
    return out


def realized_vol_pct(bars: Sequence[Candle], window: int) -> list[float | None]:
    n = len(bars)
    out: list[float | None] = [None] * n
    if window <= 0:
        return out
    closes = [float(c.close) for c in bars]
    for i in range(window, n):
        rets = [(closes[k] - closes[k - 1]) / max(closes[k - 1], 1e-9)
                for k in range(i - window + 1, i + 1)]
        mean = sum(rets) / window
        var = sum((r - mean) ** 2 for r in rets) / window
        out[i] = sqrt(var) * 100.0
    return out


def compute_measurements(bars: Sequence[Candle],
                         params: RegimeV2Params | None = None) -> list[dict[str, Any]]:
    p = params or RegimeV2Params()
    n = len(bars)
    closes = [float(c.close) for c in bars]
    atr = _atr(bars, p.atr_period)
    atr_pct: list[float | None] = [
        None if a is None else (a / max(closes[i], 1e-9) * 100.0)
        for i, a in enumerate(atr)
    ]
    atr_perc = rolling_percentile(atr_pct, p.percentile_window)
    ema_s = _ema(closes, p.ema_slow)
    er = _efficiency_ratio(bars, p.window)
    cons = directional_consistency(bars, p.window)
    rv = realized_vol_pct(bars, p.window)
    rng = _range_atr(bars, p.atr_period)
    adx_rows = _adx(bars, p.atr_period)
    di_p, di_m, adx = adx_rows["+di"], adx_rows["-di"], adx_rows["adx"]

    rows: list[dict[str, Any]] = []
    for i in range(n):
        a = atr[i]
        drift_atr = (closes[i] - closes[i - p.window]) / a if i >= p.window and a else None
        slope_atr = None
        if (i >= p.slope_bars and a and ema_s[i] is not None
                and ema_s[i - p.slope_bars] is not None):
            slope_atr = (ema_s[i] - ema_s[i - p.slope_bars]) / a
        dist_ema_atr = (closes[i] - ema_s[i]) / a if ema_s[i] is not None and a else None
        di_spread = (di_p[i] - di_m[i]) if di_p[i] is not None and di_m[i] is not None else None
        rv_atr = (rv[i] / atr_pct[i]) if rv[i] is not None and atr_pct[i] else None
        rows.append({
            "ts": bars[i].ts,
            "close": closes[i],
            "atr": a,
            "atr_pct": atr_pct[i],
            "atr_percentile": atr_perc[i],
            "drift_atr": drift_atr,
            "slope_atr": slope_atr,
            "dist_ema_atr": dist_ema_atr,
            "er": er[i],
            "consistency": cons[i],
            "di_spread": di_spread,
            "adx": adx[i],
            "rv_pct": rv[i],
            "rv_atr": rv_atr,
            "range_atr": rng[i],
        })
    for r in rows:
        rng_v, rv_v = r["range_atr"], r["rv_atr"]
        if rng_v is not None and rv_v is not None:
            r["vol_persistence"] = 0.6 * rng_v + 0.4 * rv_v
        elif rng_v is not None:
            r["vol_persistence"] = rng_v
        else:
            r["vol_persistence"] = None
    vol_perc = rolling_percentile([r["vol_persistence"] for r in rows], p.percentile_window)
    for r, vp in zip(rows, vol_perc, strict=True):
        r["vol_percentile"] = vp
    return rows
