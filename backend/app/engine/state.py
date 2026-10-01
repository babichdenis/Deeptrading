"""Контракт состояния движка (State Snapshot) — Этап B, см. docs/roadmap/WARMUP_STATE_AUDIT.md.

Снимок = состояние ПЕРЕД первым входящим баром `as_of`. Строит его cold calculator
(история → индикаторы → стратегия), восстанавливают runtime/replay; эквивалентность
cold == warm проверяется сравнением fingerprint. Пока здесь — КОНТРАКТ и сериализация;
сбор/восстановление состояния индикаторов и стратегий — следующий шаг.

Не pickle(engine): только явные, JSON-safe снимки по слоям.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class IndicatorStateSnapshot:
    """Состояние одного индикатора на (figi, tf): скаляры + компактные кольца."""
    indicator: str
    params_hash: str
    state: dict[str, Any]


@dataclass(frozen=True)
class StrategyStateSnapshot:
    """Состояние стратегии (light-скаляры канона; OSE-портам — отдельный тяжёлый контракт)."""
    strategy_id: str
    version: str
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineStateSnapshot:
    """Версионированный снимок вычислительного графа до бара as_of."""
    instrument: str
    timeframe: str
    as_of: datetime
    data_version: str = ""
    resampler_version: str = ""
    indicator_version: str = ""
    strategy_version: str = ""
    indicators: tuple[IndicatorStateSnapshot, ...] = ()
    strategy: StrategyStateSnapshot | None = None

    def _payload(self) -> dict:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self._payload(), ensure_ascii=False, sort_keys=True, default=str)

    @classmethod
    def from_json(cls, raw: str) -> "EngineStateSnapshot":
        d = json.loads(raw)
        d["as_of"] = datetime.fromisoformat(str(d["as_of"]))
        d["indicators"] = tuple(IndicatorStateSnapshot(**i) for i in d.get("indicators") or [])
        st = d.get("strategy")
        d["strategy"] = StrategyStateSnapshot(**st) if st else None
        return cls(**d)

    def fingerprint(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()
