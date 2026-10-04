"""Активный провайдер режима: legacy (RegimeDetector) или v2 (RegimeProvider).

Выбор — в конфиг-файле `backend/configs/regime.json` (env только секреты):
    {"provider": "legacy" | "v2", "v2_window": 12}
Отсутствие/битый файл → legacy. Возвращает тот же контракт, что
`app.services.regime.detect_regime` — (states, timeline, bars).
V2-строки: {ts, state=label, reason=raw, features=observation}.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

CONFIG_PATH = Path(__file__).resolve().parents[3] / "configs" / "regime.json"
DEFAULT_PROVIDER = "legacy"
DEFAULT_WINDOW = 12
PROVIDERS = ("legacy", "v2")


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if not isinstance(data, dict):
        data = {}
    provider = str(data.get("provider") or DEFAULT_PROVIDER).lower()
    if provider not in PROVIDERS:
        provider = DEFAULT_PROVIDER
    try:
        window = int(data.get("v2_window")) if data.get("v2_window") is not None else DEFAULT_WINDOW
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW
    return {"provider": provider, "v2_window": max(2, window)}


def provider_name() -> str:
    return load_config()["provider"]


def is_v2() -> bool:
    return provider_name() == "v2"


def _infer_tf_sec(bars: Sequence[Any]) -> int:
    try:
        d = (bars[1].ts - bars[0].ts).total_seconds()
        return max(60, int(d))
    except Exception:
        return 300


def v2_states(bars: Sequence[Any], tf_sec: int) -> list[dict]:
    from app.services.regime_v2.provider import RegimeProvider
    cfg = load_config()
    out = RegimeProvider(provider="v2", v2_window=cfg["v2_window"]).run(list(bars), int(tf_sec))
    return [{"ts": o["ts"], "state": o["label"], "reason": o.get("raw") or "",
             "features": o.get("observation")} for o in out]


def detect_regime_active(bars: list, tf_sec: int | None = None, **legacy_kw) -> tuple:
    """detect_regime с выбором провайдера. tf_sec нужен v2 (иначе выводим из баров)."""
    if not is_v2():
        from app.services.regime import detect_regime
        return detect_regime(bars, **legacy_kw)
    from app.services.regime import regime_timeline
    states = v2_states(bars, tf_sec or _infer_tf_sec(bars))
    return states, regime_timeline(states), list(bars)


def compute_regime_active(candles_1m: list, tf_seconds: int = 3600, **legacy_kw) -> tuple:
    """compute_regime с выбором провайдера (1m → ресемпл → detect)."""
    if not is_v2():
        from app.services.regime import compute_regime
        return compute_regime(candles_1m, tf_seconds, **legacy_kw)
    from app.services.ensemble import resample
    bars = resample(list(candles_1m), tf_seconds)
    return detect_regime_active(bars, tf_seconds)
