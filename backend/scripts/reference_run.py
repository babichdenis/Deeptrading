#!/usr/bin/env python3
"""REFERENCE RUN — эталонный прогон канонического движка (fingerprint).

REF-001: фиксированные dataset/стратегия/исполнение → EngineRunner → TradeLedger →
fingerprint. `--write` фиксирует эталон (configs/reference/REF-001.json), `--check`
ловит регрессии. См. docs/roadmap/ARCHITECTURE_STANDARDIZATION_2026-09-30.md.

Запуск (на .7 с БД):
  .venv/bin/python scripts/reference_run.py --write
  .venv/bin/python scripts/reference_run.py --check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy import create_engine, text  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.engine import EngineConfig, EngineRunner  # noqa: E402
from app.engine.costs import CostModel  # noqa: E402
from app.engine.strategies import build_strategy  # noqa: E402

import ose_exit_matrix as oem  # noqa: E402

REF_PATH = ROOT / "configs" / "reference" / "REF-001.json"
DEFAULT_REF = {
    "id": "REF-001",
    "name": "canonical engine · rsi_trade_hub 10m SBER · signal-only exits",
    "dataset": {"source": "db", "ticker": "SBER", "interval": "10min",
                "period": ["2026-09-01", "2026-09-08"]},
    "strategy": {"engine": "rsi_trade_hub", "params": {}},
    "exit": {"policy": "signal_only"},
    "execution": {"qty": 1, "commission": 0.0005, "slippage_bps": 2.0},
}


def _figi(ticker: str) -> str:
    eng = create_engine(get_settings().database_url.replace("+asyncpg", ""), pool_pre_ping=True)
    with eng.connect() as c:
        f = c.execute(text("SELECT figi FROM instruments WHERE upper(ticker)=:t LIMIT 1"),
                      {"t": ticker.upper()}).scalar()
    if not f:
        raise SystemExit(f"нет figi для {ticker}")
    return str(f)


def _data_hash(bars) -> str:
    h = hashlib.sha256()
    for b in bars:
        h.update(f"{b.ts.isoformat()}|{b.open}|{b.high}|{b.low}|{b.close}|{b.volume}\n".encode())
    return h.hexdigest()[:16]


def run_reference(ref: dict) -> dict:
    """Детерминированный эталонный прогон: DB (фикс. данные) → EngineRunner → ledger."""
    ds = ref["dataset"]
    figi = _figi(ds["ticker"])
    tf_s = oem.TF_SECONDS[ds["interval"]]
    _rows, bars = oem._load_tf_cached(figi, ds["period"][0], ds["period"][1], tf_s)
    strat = build_strategy(ref["strategy"]["engine"], ref["strategy"].get("params") or None)
    ex = ref["execution"]
    cfg = EngineConfig(
        figi=figi, qty=int(ex["qty"]),
        cost_model=CostModel(commission_rate=float(ex["commission"]),
                             slippage_bps=float(ex["slippage_bps"])),
    )
    led = EngineRunner(strategy=strat, exit_policy=oem.NoExitPolicy(), config=cfg).run(bars)
    trades = led.trades
    return {
        "data_hash": _data_hash(bars),
        "bars": len(bars),
        "trades": len(trades),
        "net": round(sum(t.net_pnl for t in trades), 4),
        "fingerprint": led.fingerprint(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="зафиксировать эталон")
    ap.add_argument("--check", action="store_true", help="сверить с эталоном (регрессия)")
    ap.add_argument("--file", default=str(REF_PATH))
    a = ap.parse_args()
    path = Path(a.file)
    ref = json.loads(path.read_text(encoding="utf-8")) if path.exists() else dict(DEFAULT_REF)
    got = run_reference(ref)
    print(f"{ref['id']}: баров {got['bars']} · сделок {got['trades']} · net {got['net']:+.4f} · "
          f"fingerprint {got['fingerprint']}")
    exp = ref.get("expected")
    if a.check:
        if not exp:
            print("нет expected в эталоне — сначала --write")
            return 2
        keys = ("data_hash", "fingerprint", "trades", "net")
        bad = [k for k in keys if exp.get(k) != got.get(k)]
        if bad:
            print("РЕГРЕССИЯ:", ", ".join(bad))
            for k in bad:
                print(f"  {k}: expected {exp.get(k)!r} got {got.get(k)!r}")
            return 1
        print("OK: эталон совпадает")
        return 0
    if a.write or not exp:
        ref["expected"] = got
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ref, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
