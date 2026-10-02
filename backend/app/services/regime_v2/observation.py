"""Regime v2 — RegimeObservation (C.3): контракт наблюдения.

Descriptive-состояние: оси + confidence-однозначность + измерения.
Не probability будущего события и не торговый сигнал.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class RegimeObservation:
    ts: datetime
    timeframe: int
    direction: str
    direction_strength: float
    trend_strength: float
    volatility: str
    volatility_percentile: float | None
    structure: str
    confidence: float
    reason_codes: tuple[str, ...] = ()
    session: str | None = None
    version: str = "v2.0"
    measurements: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["ts"] = self.ts.isoformat()
        out["reason_codes"] = list(self.reason_codes)
        return out
