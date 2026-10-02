"""Regime v2 — агрегаты валидации осей (C.2).

Условные статистики измерения против forward-исхода:
Spearman, знаковое соответствие, бакеты. Без БД и без порогов продакшена.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from app.services.regime_calibration import percentile, spearman


def sign_match(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys, strict=True) if x != 0 and y != 0]
    if not pairs:
        return None
    return sum(1 for x, y in pairs if (x > 0) == (y > 0)) / len(pairs)


def pair_metrics(rows: Sequence[Mapping[str, Any]], key: str, outcome: str) -> dict[str, Any]:
    xs = [float(r[key]) for r in rows if r.get(key) is not None and r.get(outcome) is not None]
    ys = [float(r[outcome]) for r in rows if r.get(key) is not None and r.get(outcome) is not None]
    if not xs:
        return {"n": 0, "spearman": None, "sign_match": None, "p50_outcome": None}
    return {
        "n": len(xs),
        "spearman": spearman(xs, ys),
        "sign_match": sign_match(xs, ys),
        "p50_outcome": percentile(ys, 50),
    }


def bucket_table(rows: Sequence[Mapping[str, Any]], key: str, outcome: str,
                 edges: Sequence[float]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    bounds = [float("-inf")] + list(edges) + [float("inf")]
    for lo, hi in zip(bounds[:-1], bounds[1:], strict=True):
        vals = [float(r[outcome]) for r in rows
                if r.get(key) is not None and r.get(outcome) is not None
                and lo <= float(r[key]) < hi]
        if not vals:
            continue
        out.append({
            "lo": None if lo == float("-inf") else lo,
            "hi": None if hi == float("inf") else hi,
            "n": len(vals),
            "p25": percentile(vals, 25),
            "p50": percentile(vals, 50),
            "p75": percentile(vals, 75),
            "mean": sum(vals) / len(vals),
        })
    return out
