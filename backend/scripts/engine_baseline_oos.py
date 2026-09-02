"""Executable EngineRunner baseline (entry-only replay) на OOS периоде.

Правильная интерпретация (см. MEMORY.md):
  - это НЕ all-candidates audit (тот — в ml_train_csv.py);
  - здесь полный replay реальной стратегии: 5m setup/quorum → entry candidate →
    session=main → one position per FIGI → cooldown 15 → exit policy →
    next 1m open → real qty (lot) → commission+slippage → mark-to-market equity.

Использование:
    python scripts/engine_baseline_oos.py \
        --extra-dir /tmp/hist/2025 \
        --from 2026-07-01 --to 2026-08-25 \
        --out-json /tmp/engine_oos.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.engine.models import Candle as EngineCandle  # noqa: E402
from app.services.ensemble import compute_ensemble  # noqa: E402
from app.services.ml_meta import UID_FIGI_ALIASES  # noqa: E402
from app.services.quant_analytics import compute_metrics, build_html_report  # noqa: E402


def load_dir(csv_dir: str, figis: set[str] | None = None,
             t_from: datetime | None = None, t_to: datetime | None = None) -> dict[str, list[EngineCandle]]:
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    out: dict[str, list[EngineCandle]] = {}
    for path in files:
        uid = os.path.basename(path).split("_")[0]
        figi = UID_FIGI_ALIASES.get(uid)
        if not figi or (figis and figi not in figis):
            continue
        with open(path) as f:
            for line in f:
                parts = line.strip().rstrip(";").split(";")
                if len(parts) < 7:
                    continue
                _, ts, o, c, h, l, v = parts[:7]
                try:
                    ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if t_from and ts_dt < t_from:
                    continue
                if t_to and ts_dt > t_to:
                    continue
                try:
                    out.setdefault(figi, []).append(EngineCandle(
                        ts=ts_dt, open=float(o), high=float(h),
                        low=float(l), close=float(c), volume=float(v)))
                except ValueError:
                    continue
    for figi in out:
        out[figi].sort(key=lambda c: c.ts)
    return out


# параметры реальной стратегии (ensemble_main_v1, main)
REQ: dict = {
    "bias_mode": "info",
    "bias": {"tf": "hour", "period": 50},
    "entry_tf": "5min",
    "entry": {"tf": "5min", "lookback": 1},
    "entry_session": "main",
    "quorum": 2,
    "same_side_reentry_cooldown_bars": 15,
    "carry_overnight": True,
    "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
    "commission_rate": 0.0005,
    "slippage_bps": 2.0,
    "capital": 100_000,
    "lot": 10,
    "use_all_setups": True,
    "drop_useless": True,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/tmp/hist")
    ap.add_argument("--extra-dir", default="/tmp/hist/2025")
    ap.add_argument("--figis", default="")
    ap.add_argument("--from", dest="t_from", default="2026-07-01")
    ap.add_argument("--to", dest="t_to", default="2026-08-25")
    ap.add_argument("--out-json", default="/tmp/engine_oos.json")
    args = ap.parse_args()

    figis = set(args.figis.split(",")) if args.figis else None
    t_from = datetime.fromisoformat(args.t_from).replace(tzinfo=timezone.utc)
    t_to = datetime.fromisoformat(args.t_to).replace(tzinfo=timezone.utc)
    t0 = time.time()

    print("Читаю CSV (OOS)...", flush=True)
    candles = load_dir(args.csv_dir, figis, t_from, t_to)
    if args.extra_dir:
        for figi, cs in load_dir(args.extra_dir, figis, t_from, t_to).items():
            candles.setdefault(figi, []).extend(cs)
        for figi in candles:
            candles[figi].sort(key=lambda c: c.ts)
    print(f"Свечей: { {k: len(v) for k, v in candles.items()} } ({time.time()-t0:.0f}с)", flush=True)

    all_trades: list[dict] = []
    by_figi: dict[str, dict] = {}
    for figi, cs in sorted(candles.items()):
        print(f"  прогон {figi} ({len(cs)} свечей)...", flush=True)
        req = {**REQ, "figi": figi,
               "from_ts": t_from.isoformat(), "to_ts": t_to.isoformat()}
        res = compute_ensemble(cs, req)
        if "error" in res:
            print(f"    ОШИБКА: {res['error']}", flush=True)
            by_figi[figi] = {"error": res["error"]}
            continue
        econ = res["static"]["economic"]
        trades = res["static"].get("trades") or []
        for t in trades:
            t["figi"] = figi
        all_trades.extend(trades)
        by_figi[figi] = {
            "trades": econ["trades"], "gross": econ["gross"], "costs": econ["costs"],
            "net": econ["net"], "pf": econ["profit_factor"],
            "accepted": len(res["static"].get("funnel", {}).get("quorum", {}).get("accepted", [])) if isinstance(res["static"].get("funnel"), dict) else None,
        }
        print(f"    net={econ['net']:.0f} trades={econ['trades']} pf={econ.get('profit_factor')}", flush=True)

    # mark-to-market equity по закрытым сделкам + unrealized отсутствует (все закрыты движком)
    trades_rows = [{
        "ts": t.get("exit_ts") or t.get("entry_ts"), "net": t["net"], "gross": t["gross"],
        "commission": t.get("commission", 0), "slippage": t.get("slippage", 0),
        "total_costs": t.get("costs", 0), "figi": t.get("figi", ""),
    } for t in all_trades]

    metrics = compute_metrics(trades_rows, portfolio_capital=500_000)
    total = {
        "gross": round(sum(t["gross"] for t in all_trades), 2),
        "commission": round(sum(t.get("commission", 0) for t in all_trades), 2),
        "slippage": round(sum(t.get("slippage", 0) for t in all_trades), 2),
        "costs": round(sum(t.get("costs", 0) for t in all_trades), 2),
        "net": round(sum(t["net"] for t in all_trades), 2),
        "n_trades": len(all_trades),
    }
    meta = {
        "run_id": f"engine_oos_{args.t_from}_{args.t_to}",
        "dataset_version": "csv_2025_2026",
        "cost_model": "commission 0.05% + slippage 2bps",
        "portfolio_capital": 500_000,
        "max_concurrent_positions": 5,
        "timezone": "Europe/Moscow",
        "time_range": [args.t_from, args.t_to],
        "trade_source": "compute_ensemble EngineRunner replay (entry-only, main session)",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    print("\n=== EXECUTABLE ENGINE-RUNNER BASELINE (entry-only replay) ===")
    print(json.dumps(total, ensure_ascii=False, indent=2))
    print("\n=== QuantStats метрики ===")
    print(json.dumps({k: v for k, v in metrics.items()}, ensure_ascii=False, indent=2, default=str))
    print("\n=== By FIGI ===")
    print(json.dumps(by_figi, ensure_ascii=False, indent=2))

    html = build_html_report(trades_rows, portfolio_capital=500_000,
                             title="EngineRunner baseline OOS",
                             metadata=meta, out_path=f"/tmp/qs_engine_oos_{args.t_from}_{args.t_to}.html")
    print("\nHTML:", html, flush=True)

    report = {"meta": meta, "total": total, "quantstats": metrics, "by_figi": by_figi,
              "trades": all_trades[:200]}
    with open(args.out_json, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"Отчёт: {args.out_json} ({time.time()-t0:.0f}с)")


if __name__ == "__main__":
    main()
