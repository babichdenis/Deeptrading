"""Regime v2 — RegimeProvider (C.6): источник метки legacy|v2.

Default — `legacy` (production-поведение не меняется). V2 подключается только
для analytics/comparison/signal attribution/research, НЕ как trading-gate:
в runtime/ensemble этот провайдер не вызывается.
"""
from __future__ import annotations

from typing import Any, Sequence

from app.engine.models import Candle
from app.services.regime import RegimeDetector
from app.services.regime_calibration import session_of
from app.services.regime_v2.classifier import RegimeV2ClassifierParams
from app.services.regime_v2.hysteresis import HysteresisParams, RegimeV2State
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

DEFAULT_PROVIDER = "legacy"
PROVIDERS = ("legacy", "v2")


class RegimeProvider:
    def __init__(self, provider: str = DEFAULT_PROVIDER, *,
                 v2_window: int = 12,
                 hyst_params: HysteresisParams | None = None,
                 classifier_params: RegimeV2ClassifierParams | None = None):
        if provider not in PROVIDERS:
            raise ValueError(f"неизвестный regime_provider={provider!r}; допустимо {PROVIDERS}")
        self.provider = provider
        self.v2_window = v2_window
        self.hp = hyst_params or HysteresisParams()
        self.cp = classifier_params or RegimeV2ClassifierParams()

    def run(self, bars: Sequence[Candle], tf_sec: int) -> list[dict[str, Any]]:
        if self.provider == "legacy":
            rows = RegimeDetector().compute(list(bars))
            return [{"ts": r["ts"], "label": r["state"], "raw": r["state"],
                     "reason": r["reason"]} for r in rows]
        ms = compute_measurements(bars, RegimeV2Params(window=self.v2_window))
        state = RegimeV2State(self.cp, self.hp)
        out: list[dict[str, Any]] = []
        for m in ms:
            obs, raw, label = state.update(m["ts"], tf_sec, m, session_of(m["ts"]))
            out.append({"ts": m["ts"], "label": label, "raw": raw,
                        "observation": obs.to_dict()})
        return out
