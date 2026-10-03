"""Конфиг Signal Lab: JSON → dataclass → канонический config_hash.

config_hash — ключ идемпотентности lab_experiment_runs: перезапуск того же
прогона не создаёт дублей, смена состава/периода/кода создаёт новый run_id.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs" / "signal_lab"


@dataclass
class LabConfig:
    run_key: str
    stage: str = "discovery"
    dataset_key: str = "moex_1m"
    period_from: str = ""
    period_to: str = ""
    warmup_from: str = ""
    universe: list[str] = field(default_factory=list)
    tfs: list[str] = field(default_factory=lambda: ["1min", "5min", "10min"])
    engines: list[str] = field(default_factory=list)   # после раскрытия "*"
    engines_raw: str = "*"
    engine_params: dict = field(default_factory=dict)
    engine_tfs: dict = field(default_factory=dict)  # движок → ограниченный список ТФ
    window: int | None = 400
    costs: dict = field(default_factory=lambda: {
        "commission_rate": 0.0005, "slippage_bps": 2.0, "qty": 1})
    horizons: list[int] = field(default_factory=lambda: [1, 3, 6, 12, 24])
    timeout_bars: int = 24
    atr_period: int = 14
    exit_profiles: list[dict] = field(default_factory=lambda: [
        {"code": "sl1_tp2", "sl_atr": 1.0, "tp_atr": 2.0},
        {"code": "sl1.5_tp3", "sl_atr": 1.5, "tp_atr": 3.0},
        {"code": "sl2_tp4", "sl_atr": 2.0, "tp_atr": 4.0},
        {"code": "sl3_tp6", "sl_atr": 3.0, "tp_atr": 6.0},
    ])
    trailing_profiles: list[dict] = field(default_factory=list)
    sessions: list[str] = field(default_factory=lambda: ["morning", "day", "evening"])
    overnight: bool = False
    raw: dict = field(default_factory=dict)

    @property
    def tf_seconds(self) -> dict[str, int]:
        from app.marketdata.resampler import PERIOD_MIN
        return {tf: PERIOD_MIN[tf] * 60 for tf in self.tfs}


def all_engines() -> list[str]:
    from app.engine.strategies import STRATEGY_REGISTRY
    return sorted(STRATEGY_REGISTRY.keys())


def load_config(path: str | Path) -> LabConfig:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cfg = LabConfig(
        run_key=str(data.get("run_key") or Path(path).stem),
        stage=str(data.get("stage") or "discovery"),
        dataset_key=str(data.get("dataset_key") or "moex_1m"),
        period_from=str(data.get("period_from") or ""),
        period_to=str(data.get("period_to") or ""),
        warmup_from=str(data.get("warmup_from") or ""),
        universe=[str(t).upper() for t in (data.get("universe") or [])],
        tfs=list(data.get("tfs") or ["1min", "5min", "10min"]),
        engines_raw=data.get("engines", "*"),
        engine_params=dict(data.get("engine_params") or {}),
        engine_tfs=dict(data.get("engine_tfs") or {}),
        window=(None if data.get("window", 400) is None else int(data.get("window", 400))),
        costs=dict(data.get("costs") or {}),
        horizons=[int(h) for h in (data.get("horizons") or [1, 3, 6, 12, 24])],
        timeout_bars=int(data.get("timeout_bars", 24)),
        atr_period=int(data.get("atr_period", 14)),
        exit_profiles=list(data.get("exit_profiles") or LabConfig().exit_profiles),
        trailing_profiles=list(data.get("trailing_profiles") or []),
        sessions=list(data.get("sessions") or ["morning", "day", "evening"]),
        overnight=bool(data.get("overnight", False)),
        raw=data,
    )
    raw_eng = cfg.engines_raw
    if raw_eng == "*" or raw_eng is None:
        cfg.engines = all_engines()
    elif isinstance(raw_eng, list):
        cfg.engines = [str(e) for e in raw_eng]
    else:
        cfg.engines = [str(raw_eng)]
    return cfg


def canonical_payload(cfg: LabConfig, code_version: str = "") -> dict:
    """Всё, что влияет на результат. code_version входит → смена кода = новый run."""
    return {
        "run_key": cfg.run_key,
        "stage": cfg.stage,
        "dataset_key": cfg.dataset_key,
        "period_from": cfg.period_from,
        "period_to": cfg.period_to,
        "warmup_from": cfg.warmup_from,
        "universe": cfg.universe,
        "tfs": cfg.tfs,
        "engines": cfg.engines,
        "engine_params": cfg.engine_params,
        "engine_tfs": cfg.engine_tfs,
        "window": cfg.window,
        "costs": cfg.costs,
        "horizons": cfg.horizons,
        "timeout_bars": cfg.timeout_bars,
        "atr_period": cfg.atr_period,
        "exit_profiles": cfg.exit_profiles,
        "trailing_profiles": cfg.trailing_profiles,
        "sessions": cfg.sessions,
        "overnight": cfg.overnight,
        "code_version": code_version,
    }


def config_hash(cfg: LabConfig, code_version: str = "") -> str:
    payload = canonical_payload(cfg, code_version)
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
