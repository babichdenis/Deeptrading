#!/usr/bin/env python3
"""Config Presets («ветки» конфигураций) — см. docs/CONFIG_PRESETS.md.

Единый JSON-пресет (harness + runtime) → harness-спека / нагрузка /bot/mode
(+ сайдкар для аналитики: полные настройки тегами).

  .venv/bin/python scripts/preset.py tags    configs/presets/mtf-rsi-v1.json
  .venv/bin/python scripts/preset.py to-spec configs/presets/mtf-rsi-v1.json
  .venv/bin/python scripts/preset.py replay  configs/presets/mtf-rsi-v1.json --start
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.services.preset_tags import sanitize, save_sidecar, tags  # noqa: E402

from bt_ose_sweep import ROBOTS  # noqa: E402


def to_spec(p: dict) -> dict:
    h = p.get("harness") or {}
    return {
        "experiment": {"name": p.get("preset", {}).get("id", "preset")},
        "robots": h.get("robots") or [],
        "timeframe": h.get("timeframe", "10min"),
        "universe": h.get("universe") or [],
        "period": h.get("period") or [],
        "costs": h.get("costs") or {},
        "execution": {"jobs": 3, "cache": True},
        "filters": h.get("filters") or {"min_trades": 0, "min_net_pnl": -10**9},
        "exits": h.get("exits") or ["x07"],
        "wf": h.get("wf") or {},
        "outputs": {"dir": "reports/experiments"},
    }


def replay_payload(p: dict) -> tuple[dict, str]:
    pr = p.get("preset") or {}
    h = p.get("harness") or {}
    t = (p.get("targets") or {}).get("replay") or {}
    robots = h.get("robots") or []
    engine = t.get("engine") or (robots[0].get("robot") if robots else "")
    if engine in ROBOTS:
        engine = f"ose_{engine}"
    interval = t.get("interval") or h.get("timeframe") or "10min"
    params = (robots[0].get("params") if robots else {}) or {}
    period = h.get("period") or ["", ""]
    d0 = period[0] if len(period) > 0 else ""
    d1 = period[1] if len(period) > 1 else ""
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    test_name = sanitize(f"{pr.get('id', 'preset')} {ts}")
    payload = {
        "mode": "test",
        "test_name": test_name,
        "replay_start": f"{d0}T04:00:00Z" if d0 else "",
        "replay_end": f"{d1}T21:59:00Z" if d1 else "",
        "replay_pace": t.get("pace", "fast"),
        "test_engine": engine,
        "test_interval": interval,
        "test_params": params,
        "preset": p,  # runtime-блок применится в apply_test_overrides (TEST_PRESET)
    }
    return payload, test_name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("tags", "to-spec", "replay"):
        s = sub.add_parser(name)
        s.add_argument("file")
        if name == "to-spec":
            s.add_argument("--out", default="")
        if name == "replay":
            s.add_argument("--start", action="store_true")
            s.add_argument("--url", default="http://localhost:8000")
    a = ap.parse_args()
    p = json.loads(Path(a.file).read_text(encoding="utf-8"))
    if a.cmd == "tags":
        for t in tags(p):
            print(" •", t)
        return 0
    if a.cmd == "to-spec":
        spec = to_spec(p)
        out = Path(a.out) if a.out else ROOT / "configs" / "ose_runs" / f"preset_{spec['experiment']['name']}.json"
        out.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {out}")
        return 0
    payload, test_name = replay_payload(p)
    sc = save_sidecar(test_name, p, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"сайдкар: {sc}")
    if a.start:
        req = urllib.request.Request(
            f"{a.url}/api/v1/bot/mode", data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            print("started:", resp.read().decode("utf-8")[:250])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
