"""Read-only калибровочная диагностика канонического RegimeDetector.

Не production-код: чистые функции для scripts/regime_calibration.py и тестов.
БД не трогает, RegimeDetector/RegimeState/runtime/ensemble не меняет.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from math import sqrt
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.engine.sessions import SESSION_WINDOWS
from app.services.regime import RegimeDetector

_MSK = ZoneInfo("Europe/Moscow")

DEFAULT_QUANTILES: tuple[int, ...] = (1, 5, 10, 25, 50, 75, 90, 95, 99)
FEATURE_KEYS: tuple[str, ...] = (
    "atr_pct", "atr_percentile", "ema_slope", "adx",
    "volume_ratio", "drift_pct", "consistency", "range_ratio",
)
TREND_CONJUNCTS: tuple[str, ...] = ("slope", "drift", "cons", "adx")
MARGIN_KEYS: tuple[str, ...] = (
    "range_drift_margin", "range_cons_margin", "range_flat_margin",
    "hv_atr_margin", "hv_range_margin",
)


def percentile(values: Sequence[float], q: float) -> float:
    """Линейная интерполяция (method='linear', как numpy.percentile)."""
    xs = sorted(float(v) for v in values)
    if not xs:
        return float("nan")
    if len(xs) == 1:
        return xs[0]
    pos = (q / 100.0) * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def percentiles(values: Sequence[float], qs: Sequence[int] = DEFAULT_QUANTILES) -> dict[str, float]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {}
    return {f"p{q:02d}": percentile(vals, q) for q in qs}


def session_of(ts: datetime) -> str:
    msk = ts.astimezone(_MSK)
    if msk.weekday() >= 5:
        return "weekend"
    mins = msk.hour * 60 + msk.minute
    for name in ("morning", "day", "evening"):
        a, b = SESSION_WINDOWS[name]
        if a <= mins < b:
            return name
    if 18 * 60 + 45 <= mins < 19 * 60 + 5:
        return "clearing"
    return "other"


def bar_range_ratio(high: float, low: float, close: float, atr_pct: float | None) -> float | None:
    if atr_pct is None or atr_pct <= 0:
        return None
    px = max(float(close), 1e-9)
    rng_pct = (float(high) - float(low)) / px * 100.0
    return rng_pct / float(atr_pct)


def trend_conditions(features: Mapping[str, Any], det: RegimeDetector) -> dict[str, Any]:
    """Конъюнкты обеих трендовых ветвей на округлённых features (как отдаёт detector)."""
    slope = float(features["ema_slope"]) / 100.0
    dr = float(features["drift_pct"])
    cons = float(features["consistency"])
    adx = float(features["adx"])
    adx_ok = adx >= det.adx_threshold
    up = {
        "slope": slope >= 0.0,
        "drift": dr >= det.drift_pct,
        "cons": cons >= det.cons_pct or (cons >= det.cons_relax_pct and dr >= det.drift_strong_pct),
        "adx": adx_ok,
    }
    dn = {
        "slope": slope <= 0.0,
        "drift": dr <= -det.drift_pct,
        "cons": cons <= 1.0 - det.cons_pct or (cons <= 1.0 - det.cons_relax_pct and dr <= -det.drift_strong_pct),
        "adx": adx_ok,
    }
    return {
        "up": up, "dn": dn,
        "up_failed": [k for k in TREND_CONJUNCTS if not up[k]],
        "dn_failed": [k for k in TREND_CONJUNCTS if not dn[k]],
        "up_pass": all(up.values()),
        "dn_pass": all(dn.values()),
    }


def classify_mixed(features: Mapping[str, Any], range_ratio_value: float | None,
                   det: RegimeDetector | None = None) -> dict[str, Any]:
    """Причины попадания observation в NEUTRAL/mixed (дерево regime.py:172-189).

    Точность ограничена округлением features в detector (slope 3 знака, drift 3,
    consistency 2, ADX 1); пограничные строки считаются отдельно.
    """
    det = det or RegimeDetector()
    t = trend_conditions(features, det)
    up_sig = t["up"]["slope"] and t["up"]["drift"]
    dn_sig = t["dn"]["slope"] and t["dn"]["drift"]
    blockers_up = [k for k in ("cons", "adx") if not t["up"][k]]
    blockers_dn = [k for k in ("cons", "adx") if not t["dn"][k]]
    if up_sig and not dn_sig:
        primary, blockers = "up", blockers_up
        code = "up_" + "_".join(blockers) if blockers else "up_inconsistent"
    elif dn_sig and not up_sig:
        primary, blockers = "down", blockers_dn
        code = "down_" + "_".join(blockers) if blockers else "down_inconsistent"
    elif up_sig and dn_sig:
        primary, blockers = "both", []
        code = "both_directions"
    else:
        primary, blockers = "none", []
        dr = float(features["drift_pct"])
        slope = float(features["ema_slope"]) / 100.0
        if abs(dr) < det.drift_pct:
            code = "low_drift_edge"
        elif dr >= det.drift_pct and slope < 0.0:
            code = "conflict_drift_up_slope_dn"
        elif dr <= -det.drift_pct and slope > 0.0:
            code = "conflict_drift_dn_slope_up"
        else:
            code = "no_direction"

    cons = float(features["consistency"])
    adx = float(features["adx"])
    a_perc = float(features["atr_percentile"])
    slope = float(features["ema_slope"]) / 100.0
    dr = float(features["drift_pct"])
    minority = min(cons, 1.0 - cons)
    flat_slope_ok = abs(slope) < det.slope_threshold
    flat_adx_ok = adx < det.adx_threshold
    if flat_adx_ok:
        flat_margin = abs(slope) - det.slope_threshold
    else:
        flat_margin = adx - det.adx_threshold
    return {
        "code": code,
        "primary": primary,
        "blockers": blockers,
        "failed_up": t["up_failed"],
        "failed_dn": t["dn_failed"],
        "trend_up_pass": t["up_pass"],
        "trend_dn_pass": t["dn_pass"],
        "range_drift_margin": abs(dr) - det.drift_pct,
        "range_cons_margin": 0.34 - minority,
        "range_flat_slope_ok": flat_slope_ok,
        "range_flat_margin": flat_margin,
        "hv_atr_margin": a_perc - det.atr_percentile_threshold,
        "hv_range_margin": (None if range_ratio_value is None
                            else float(range_ratio_value) - det.range_mult),
    }


def summarize_mixed(diags: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    codes = Counter(str(d["code"]) for d in diags)
    primary = Counter(str(d["primary"]) for d in diags)
    failed_up: Counter = Counter()
    failed_dn: Counter = Counter()
    for d in diags:
        failed_up.update(d["failed_up"])
        failed_dn.update(d["failed_dn"])
    margins: dict[str, dict[str, float]] = {}
    for key in MARGIN_KEYS:
        vals = [d[key] for d in diags if d.get(key) is not None]
        if vals:
            margins[key] = percentiles(vals)
    return {
        "n": len(diags),
        "by_code": dict(sorted(codes.items(), key=lambda kv: (-kv[1], kv[0]))),
        "by_primary": dict(sorted(primary.items())),
        "failed_up_conjuncts": dict(sorted(failed_up.items(), key=lambda kv: (-kv[1], kv[0]))),
        "failed_dn_conjuncts": dict(sorted(failed_dn.items(), key=lambda kv: (-kv[1], kv[0]))),
        "margins": margins,
    }


def transition_stats(states: Sequence[str]) -> dict[str, Any]:
    runs: list[int] = []
    run_states: list[str] = []
    prev: str | None = None
    n = 0
    for s in states:
        if s == prev:
            n += 1
        else:
            if prev is not None:
                runs.append(n)
                run_states.append(prev)
            prev = s
            n = 1
    if prev is not None:
        runs.append(n)
        run_states.append(prev)
    by_state: dict[str, dict[str, float]] = {}
    for st in sorted(set(run_states)):
        lens = [r for r, s in zip(runs, run_states, strict=True) if s == st]
        by_state[st] = {
            "runs": len(lens),
            "mean": sum(lens) / len(lens),
            "median": percentile(lens, 50),
            "total_bars": sum(lens),
        }
    return {
        "observations": len(states),
        "transitions": max(len(runs) - 1, 0),
        "runs": len(runs),
        "mean_run": (sum(runs) / len(runs)) if runs else 0.0,
        "median_run": percentile(runs, 50) if runs else 0.0,
        "max_run": max(runs) if runs else 0,
        "singleton_runs": sum(1 for r in runs if r == 1),
        "singleton_share": (sum(1 for r in runs if r == 1) / len(runs)) if runs else 0.0,
        "by_state": by_state,
    }


FORWARD_HORIZONS: tuple[int, ...] = (12, 24, 48)
FORWARD_KEYS: tuple[str, ...] = (
    "ret_pct", "ret_atr", "mfe_atr", "mae_atr", "er", "cons", "rv_pct",
    "rv_ratio", "dir_eff", "range_atr", "range_norm",
)


def forward_outcome(closes: Sequence[float], highs: Sequence[float], lows: Sequence[float],
                    atr_pct: Sequence[float | None], i: int, horizon: int) -> dict[str, float] | None:
    """Исходы баров i+1..i+horizon относительно close бара i (строго без look-ahead).

    Все *_atr нормированы на ATR-в-цене бара i (close[i] * atr_pct[i] / 100).
    range_norm = range_atr / sqrt(horizon) — масштабно-инвариантная мера расширения
    диапазона (≈1 при нормальной волатильности), dir_eff = (close_end-close_start)/range.
    None, если исход не помещается в данные или ATR бара i недоступен.
    """
    n = len(closes)
    if i < 0 or horizon <= 0 or i + horizon >= n:
        return None
    a = atr_pct[i]
    if a is None or a <= 0:
        return None
    c0 = float(closes[i])
    atr_price = c0 * float(a) / 100.0
    if atr_price <= 0:
        return None
    end = i + horizon
    c1 = float(closes[end])
    up = max(float(h) for h in highs[i + 1:end + 1])
    dn = min(float(x) for x in lows[i + 1:end + 1])
    total = sum(abs(float(closes[k]) - float(closes[k - 1])) for k in range(i + 1, end + 1))
    ups = sum(1 for k in range(i + 1, end + 1) if float(closes[k]) >= float(closes[k - 1]))
    rets = [(float(closes[k]) - float(closes[k - 1])) / max(float(closes[k - 1]), 1e-9)
            for k in range(i + 1, end + 1)]
    mean = sum(rets) / horizon
    var = sum((r - mean) ** 2 for r in rets) / horizon
    rv = sqrt(var) * 100.0
    rng = up - dn
    return {
        "ret_pct": (c1 - c0) / c0 * 100.0,
        "ret_atr": (c1 - c0) / atr_price,
        "mfe_atr": (up - c0) / atr_price,
        "mae_atr": (dn - c0) / atr_price,
        "er": abs(c1 - c0) / total if total > 0 else 0.0,
        "cons": ups / horizon,
        "rv_pct": rv,
        "rv_ratio": rv / float(a),
        "dir_eff": (c1 - c0) / rng if rng > 0 else 0.0,
        "range_atr": rng / atr_price,
        "range_norm": (rng / atr_price) / sqrt(horizon),
    }


def future_class(out: Mapping[str, float] | None) -> str:
    """Временная (диагностическая) разметка форвардного поведения.

    Масштабно-инвариантные правила: расширение диапазона — range/√h ≥ 1.5,
    направленный тренд — direction efficiency ≥ 0.5 при ER ≥ 0.4, боковик — ER ≤ 0.35.
    Пороги — рабочая гипотеза для матрицы качества, НЕ production-классификатор
    и НЕ калибровка.
    """
    if out is None:
        return "NO_DATA"
    if out["range_norm"] >= 1.5:
        return "FUT_HIGH_VOL"
    if out["dir_eff"] >= 0.5 and out["er"] >= 0.4:
        return "FUT_UP"
    if out["dir_eff"] <= -0.5 and out["er"] >= 0.4:
        return "FUT_DOWN"
    if out["er"] <= 0.35:
        return "FUT_RANGE"
    return "FUT_TRANSITION"


def rankdata(values: Sequence[float]) -> list[float]:
    """Средние ранги (1-based) с учётом связей — как scipy.stats.rankdata('average')."""
    n = len(values)
    order = sorted(range(n), key=lambda k: values[k])
    ranks = [0.0] * n
    k = 0
    while k < n:
        j = k
        while j + 1 < n and values[order[j + 1]] == values[order[k]]:
            j += 1
        avg = (k + j) / 2.0 + 1.0
        for m in range(k, j + 1):
            ranks[order[m]] = avg
        k = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    n = len(xs)
    if n < 3 or n != len(ys):
        return None
    rx = rankdata(xs)
    ry = rankdata(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    dx = sqrt(sum((a - mx) ** 2 for a in rx))
    dy = sqrt(sum((b - my) ** 2 for b in ry))
    if dx <= 0 or dy <= 0:
        return None
    return num / (dx * dy)
