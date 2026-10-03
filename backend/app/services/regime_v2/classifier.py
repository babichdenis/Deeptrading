"""Regime v2 — rule-based классификатор (C.3): measurements → RegimeObservation → legacy.

Оси (по решениям владельца 2026-10-02 и OOS-валидации Stage C.2):
- direction — моментум только на H1 (h≈6–12 в валидации); на 5m — FLAT, measurement
  сохраняется как диагностика;
- trend_strength — descriptive (ER + consistency + |slope|/ATR), не предиктивная ось;
- volatility — персистентность range/RV (vol_persistence), ATR-percentile — diagnostic;
- structure — derived/descriptive RANGE / TREND / TRANSITION;
- confidence — однозначность соответствия measurements классификации, НЕ вероятность.

Hysteresis здесь нет. Runtime не подключён: provider по умолчанию legacy.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.services.regime_v2.observation import RegimeObservation

DIRECTION_KEYS = ("drift_atr", "slope_atr", "di_spread")


@dataclass(frozen=True)
class RegimeV2ClassifierParams:
    direction_tfs: tuple[int, ...] = (3600,)
    direction_k: float = 1.0
    min_dir_agreement: float = 2 / 3
    min_direction_strength: float = 0.15
    trend_k_slope: float = 1.0
    trend_strong: float = 0.4
    trend_weak: float = 0.25
    vol_low: float = 25.0
    vol_normal: float = 75.0
    vol_high: float = 90.0
    vol_conf_span: float = 10.0
    c_unknown: float = 0.25
    version: str = "v2.0"


def _clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _vol_bucket(percentile: float | None, p: RegimeV2ClassifierParams) -> str:
    if percentile is None:
        return "UNKNOWN"
    if percentile < p.vol_low:
        return "LOW"
    if percentile < p.vol_normal:
        return "NORMAL"
    if percentile < p.vol_high:
        return "HIGH"
    return "EXTREME"


def _direction(m: Mapping[str, Any], tf_sec: int,
               p: RegimeV2ClassifierParams) -> tuple[str, float, float, str | None]:
    votes: list[int] = []
    for key in DIRECTION_KEYS:
        v = m.get(key)
        if v is None:
            continue
        if v > 0:
            votes.append(1)
        elif v < 0:
            votes.append(-1)
    agreement = (max(votes.count(1), votes.count(-1)) / len(votes)) if votes else 0.5
    side = "UP" if votes.count(1) > votes.count(-1) else (
        "DOWN" if votes.count(-1) > votes.count(1) else "FLAT")
    if tf_sec not in p.direction_tfs:
        return "FLAT", 0.0, agreement, None
    if side == "FLAT" or agreement < p.min_dir_agreement:
        return "FLAT", 0.0, agreement, ("disagreement" if votes else None)
    mags = [abs(float(m[k])) for k in ("drift_atr", "slope_atr") if m.get(k) is not None]
    strength = _clip01((sum(mags) / len(mags)) / p.direction_k) if mags else 0.0
    if strength < p.min_direction_strength:
        return "FLAT", 0.0, agreement, "weak"
    return side, strength, agreement, None


def _trend_strength(m: Mapping[str, Any], p: RegimeV2ClassifierParams) -> float:
    er = m.get("er")
    slope = m.get("slope_atr")
    parts: list[float] = []
    if er is not None:
        parts.append(0.6 * float(er))
    if slope is not None:
        parts.append(0.4 * _clip01(abs(float(slope)) / p.trend_k_slope))
    if not parts:
        return 0.0
    scale = 0.6 * (1 if er is not None else 0) + 0.4 * (1 if slope is not None else 0)
    return _clip01(sum(parts) / scale)


def _vol_confidence(percentile: float | None, p: RegimeV2ClassifierParams) -> float:
    if percentile is None:
        return 0.0
    dist = min(abs(percentile - b) for b in (p.vol_low, p.vol_normal, p.vol_high))
    return _clip01(dist / p.vol_conf_span)


def _bands(trend_strength: float, p: RegimeV2ClassifierParams) -> str:
    if trend_strength >= p.trend_strong:
        return "TRENDING"
    if trend_strength <= p.trend_weak:
        return "RANGE"
    return "TRANSITION"


def classify_row(ts: datetime, tf_sec: int, m: Mapping[str, Any],
                 session: str | None = None,
                 params: RegimeV2ClassifierParams | None = None,
                 prev_structure: str | None = None) -> RegimeObservation:
    p = params or RegimeV2ClassifierParams()
    reasons: list[str] = []
    if m.get("atr_pct") is None:
        return RegimeObservation(
            ts=ts, timeframe=tf_sec, direction="FLAT", direction_strength=0.0,
            trend_strength=0.0, volatility="UNKNOWN", volatility_percentile=None,
            structure="TRANSITION", confidence=0.0, reason_codes=("warmup",),
            session=session, version=p.version,
            measurements=dict(m),
        )
    direction, dir_strength, dir_agreement, dir_note = _direction(m, tf_sec, p)
    if dir_note:
        reasons.append(f"direction_{dir_note}")
    trend_strength = _trend_strength(m, p)
    vp = m.get("vol_percentile")
    volatility = _vol_bucket(vp, p)
    if volatility == "UNKNOWN":
        reasons.append("vol_unknown")
    elif volatility == "EXTREME":
        reasons.append("vol_extreme")

    if prev_structure == "TRENDING":
        structure = "TRENDING" if trend_strength >= p.trend_weak else _bands(trend_strength, p)
    elif prev_structure == "RANGE":
        structure = "RANGE" if trend_strength <= p.trend_strong else _bands(trend_strength, p)
    else:
        structure = _bands(trend_strength, p)

    c_dir = dir_agreement
    c_vol = _vol_confidence(vp, p)
    if structure == "TRENDING":
        c_struct = trend_strength
    elif structure == "RANGE":
        c_struct = 1.0 - trend_strength
    else:
        c_struct = 0.5
        reasons.append("structure_transition")
    confidence = (c_dir + c_vol + c_struct) / 3.0
    return RegimeObservation(
        ts=ts, timeframe=tf_sec, direction=direction, direction_strength=dir_strength,
        trend_strength=trend_strength, volatility=volatility, volatility_percentile=vp,
        structure=structure, confidence=confidence, reason_codes=tuple(reasons),
        session=session, version=p.version, measurements=dict(m),
    )


def derive_legacy(obs: RegimeObservation,
                  params: RegimeV2ClassifierParams | None = None) -> str:
    """Контракт LegacyRegimeAdapter (утверждён владельцем 2026-10-02).

    warmup/low-confidence → NEUTRAL; volatility EXTREME → HIGH_VOLATILITY
    (явный volatility-mapping, не приоритет ради parity); structure RANGE → RANGE;
    TRENDING+UP/DOWN → TREND_*; TRENDING+FLAT и TRANSITION → NEUTRAL.
    """
    p = params or RegimeV2ClassifierParams()
    if "warmup" in obs.reason_codes or obs.confidence < p.c_unknown:
        return "NEUTRAL"
    if obs.volatility == "EXTREME":
        return "HIGH_VOLATILITY"
    if obs.structure == "RANGE":
        return "RANGE"
    if obs.structure == "TRENDING":
        if obs.direction == "UP":
            return "TREND_UP"
        if obs.direction == "DOWN":
            return "TREND_DOWN"
        return "NEUTRAL"
    return "NEUTRAL"
