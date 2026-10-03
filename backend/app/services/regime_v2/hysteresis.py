"""Regime v2 — stateful hysteresis (C.5).

`state(t) = f(features(t), state(t−1))`: sticky-структура по двойным порогам
(вход 0.4 / выход 0.25) + подтверждение смены метки (confirm_bars) и минимальная
длительность (min_tenure). Формирующийся бар — через clone(), состояние не мутируется.

Контракт parity: batch/incremental обязаны совпадать для КАЖДОГО префикса
(а не только итогового массива) — см. tests/test_regime_v2_hysteresis.py.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from app.services.regime_v2.classifier import (
    RegimeV2ClassifierParams,
    classify_row,
    derive_legacy,
)
from app.services.regime_v2.observation import RegimeObservation


@dataclass(frozen=True)
class HysteresisParams:
    """Дефолт — reference candidate `mild (2/1)` (решение владельца 2026-10-02).

    `default (2/3)` и `strong (3/5)` доступны явно как benchmark-варианты.
    """
    min_tenure: int = 1
    confirm_bars: int = 2


class LabelHysteresis:
    """Смена дискретной метки только после confirm_bars подряд и при tenure ≥ min_tenure."""

    def __init__(self, params: HysteresisParams | None = None, current: str | None = None):
        self.p = params or HysteresisParams()
        self.current = current
        self.tenure = 1 if current is not None else 0
        self.pending: str | None = None
        self.pending_count = 0

    def snapshot(self) -> dict:
        return {"current": self.current, "tenure": self.tenure,
                "pending": self.pending, "pending_count": self.pending_count}

    def restore(self, snap: dict) -> LabelHysteresis:
        self.current = snap["current"]
        self.tenure = snap["tenure"]
        self.pending = snap["pending"]
        self.pending_count = snap["pending_count"]
        return self

    def clone(self) -> LabelHysteresis:
        return LabelHysteresis(self.p).restore(self.snapshot())

    def update(self, candidate: str) -> str:
        if self.current is None:
            self.current = candidate
            self.tenure = 1
            return self.current
        if candidate == self.current:
            self.tenure += 1
            self.pending = None
            self.pending_count = 0
            return self.current
        if candidate == self.pending:
            self.pending_count += 1
        else:
            self.pending = candidate
            self.pending_count = 1
        if (self.pending_count >= self.p.confirm_bars
                and self.tenure >= self.p.min_tenure):
            self.current = candidate
            self.tenure = 1
            self.pending = None
            self.pending_count = 0
        else:
            self.tenure += 1
        return self.current


class RegimeV2State:
    """Персистентное состояние Regime v2 для одной TF-серии."""

    def __init__(self, classifier_params: RegimeV2ClassifierParams | None = None,
                 hyst_params: HysteresisParams | None = None,
                 prev_structure: str | None = None):
        self.cp = classifier_params or RegimeV2ClassifierParams()
        self._hyst = LabelHysteresis(hyst_params)
        self._prev_structure = prev_structure
        self.n = 0
        self.rows: list[dict[str, Any]] = []

    def snapshot(self) -> dict:
        return {"prev_structure": self._prev_structure, "n": self.n,
                "hyst": self._hyst.snapshot(), "rows": list(self.rows)}

    def restore(self, snap: dict) -> RegimeV2State:
        self._prev_structure = snap["prev_structure"]
        self.n = snap["n"]
        self._hyst.restore(snap["hyst"])
        self.rows = list(snap["rows"])
        return self

    def clone(self) -> RegimeV2State:
        state = RegimeV2State(self.cp).restore(self.snapshot())
        return state

    def update(self, ts: datetime, tf_sec: int, m: Mapping[str, Any],
               session: str | None = None) -> tuple[RegimeObservation, str, str]:
        obs = classify_row(ts, tf_sec, m, session, self.cp,
                           prev_structure=self._prev_structure)
        self._prev_structure = obs.structure
        raw = derive_legacy(obs, self.cp)
        label = self._hyst.update(raw)
        self.rows.append({"ts": ts, "obs": obs, "structure": obs.structure,
                          "legacy_raw": raw, "legacy": label})
        self.n += 1
        return obs, raw, label

    def evaluate(self, ts: datetime, tf_sec: int, m: Mapping[str, Any],
                 session: str | None = None) -> tuple[RegimeObservation, str, str]:
        return self.clone().update(ts, tf_sec, m, session)
