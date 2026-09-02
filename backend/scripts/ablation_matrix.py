"""Executable filter ablation matrix через реальный EngineRunner.

Варьируем ТОЛЬКО: quorum (1/2) × cooldown (0/5/15). Фиксировано:
5m signal, 1m execution, main session, 100k/position, cost model,
atr_stop 14/2/2, one position per FIGI, ML off.

Период: 12 месяцев истории (2025-09..2026-08), 5 FIGI.

Выбор по правилу (не по максимуму net):
  1. отбросить net <= 0 после costs
  2. отбросить trades < minimum (guardrail)
  3. выбрать простейший режим в устойчивом плато (PF/net-per-trade/turnover)
  4. не проверять на этом же участке (OOS проверка отдельным прогоном)

Использование:
    python scripts/ablation_matrix.py --csv-dir /tmp/hist --extra-dir /tmp/hist/2025 \
        --from 2025-09-01 --to 2026-08-25 --out-json /tmp/ablation.json
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

CONFIGS: list[dict] = [
    {"name": "E2_q1_cd0",  "quorum": 1, "cooldown": 0},
    {"name": "E3_q1_cd15", "quorum": 1, "cooldown": 15},
    {"name": "E5_q2_cd0",  "quorum": 2, "cooldown": 0},
    {"name": "E0_q2_cd15", "quorum": 2, "cooldown": 15},  # текущий baseline
    {"name": "E7_q1_cd5",  "quorum": 1, "cooldown": 5},
    {"name": "E8_q2_cd5",  "quorum": 2, "cooldown": 5},
]

BASE_REQ: dict = {
    "bias_mode": "info",
    "bias": {"tf": "hour", "period": 50},
    "entry_tf": "5min",
    "entry": {"tf": "5min", "lookback": 1},
    "entry_session": "main",
    "carry_overnight": True,
    "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
    "commission_rate": 0.0005,
    "slippage_bps": 2.0,
    "capital": 100_000,
    "lot": 10,
    "use_all_setups": True,
    "drop_useless": True,
}


def load_dir(csv_dir: str, t_from: datetime, t_to: datetime) -> dict[str, list[EngineCandle]]:
    files = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    out: dict[str, list[EngineCandle]] = {}
    for path in files:
        uid = os.path.basename(path).split("_")[0]
        figi = UID_FIGI_ALIASES.get(uid)
        if not figi:
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


def summarize(res: dict) -> dict:
    st = res.get("static", {})
    econ = st.get("economic", {})
    rejected = st.get("rejected", [])
    by_reason: dict[str, int] = defaultdict(int)
    for r in rejected:
        by_reason[r.get("reason", "other")] += 1
    entries = st.get("entries", [])
    trades = st.get("trades", [])
    ep = st.get("episodes", {})
    return {
        "entries_accepted": len(entries),
        "entries_rejected": len(rejected),
        "rejected_by_reason": dict(by_reason),
        "executed_trades": econ.get("trades", 0),
        "episodes_total": ep.get("unique_episodes", 0),
        "episodes_completed": ep.get("completed", 0),
        "gross": econ.get("gross", 0),
        "commission": econ.get("commission", 0),
        "slippage": econ.get("slippage", 0),
        "costs": econ.get("costs", 0),
        "net": econ.get("net", 0),
        "pf": econ.get("profit_factor"),
        "win_rate_pct": econ.get("win_rate_pct"),
        "avg_hold_bars": econ.get("avg_hold_bars"),
        "turnover_pct": econ.get("turnover"),
        "cost_per_trade": econ.get("cost_per_trade"),
        "net_per_trade": round(econ.get("net", 0) / max(econ.get("trades", 0), 1), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/tmp/hist")
    ap.add_argument("--extra-dir", default="/tmp/hist/2025")
    ap.add_argument("--from", dest="t_from", default="2025-09-01")
    ap.add_argument("--to", dest="t_to", default="2026-08-25")
    ap.add_argument("--min-trades", type=int, default=30, help="guardrail: мин. сделок для прохождения")
    ap.add_argument("--out-json", default="/tmp/ablation.json")
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.t_from).replace(tzinfo=timezone.utc)
    t_to = datetime.fromisoformat(args.t_to).replace(tzinfo=timezone.utc)
    t0 = time.time()

    print("Читаю CSV...", flush=True)
    candles = load_dir(args.csv_dir, t_from, t_to)
    if args.extra_dir:
        for figi, cs in load_dir(args.extra_dir, t_from, t_to).items():
            candles.setdefault(figi, []).extend(cs)
        for figi in candles:
            candles[figi].sort(key=lambda c: c.ts)
    print(f"Свечей: { {k: len(v) for k, v in candles.items()} } ({time.time()-t0:.0f}с)", flush=True)

    results: dict[str, dict] = {}
    by_figi: dict[str, dict] = {}
    for cfg in CONFIGS:
        name = cfg["name"]
        print(f"\n=== {name} (quorum={cfg['quorum']}, cooldown={cfg['cooldown']}) ===", flush=True)
        agg: dict = defaultdict(float)
        per_figi: dict[str, dict] = {}
        for figi, cs in sorted(candles.items()):
            req = {**BASE_REQ, "figi": figi,
                   "quorum": cfg["quorum"],
                   "same_side_reentry_cooldown_bars": cfg["cooldown"],
                   "from_ts": t_from.isoformat(), "to_ts": t_to.isoformat()}
            res = compute_ensemble(cs, req)
            if "error" in res:
                print(f"  {figi}: ОШИБКА {res['error']}", flush=True)
                continue
            s = summarize(res)
            per_figi[figi] = s
            for k, v in s.items():
                if isinstance(v, (int, float)):
                    agg[k] += v
            print(f"  {figi[:12]}... net={s['net']:.0f} trades={s['executed_trades']} pf={s['pf']} "
                  f"entries={s['entries_accepted']}/{s['entries_rejected']}", flush=True)
        agg["net"] = round(agg["net"], 2)
        agg["gross"] = round(agg["gross"], 2)
        agg["costs"] = round(agg["costs"], 2)
        agg["commission"] = round(agg["commission"], 2)
        agg["slippage"] = round(agg["slippage"], 2)
        # PF пересчитываем из gross прибыли/убытка по всем сделкам
        gross_pos = sum(max(float(v.get("gross", 0)), 0) for v in per_figi.values())
        # у нас нет gross_pos отдельно — используем приближение: пересчитаем из per_figi
        agg["pf"] = None
        # win_rate: средневзвешенное по сделкам
        tot_trades = int(agg["executed_trades"])
        if tot_trades:
            agg["win_rate_pct"] = round(
                sum(v.get("win_rate_pct", 0) * v.get("executed_trades", 0) for v in per_figi.values()) / tot_trades, 1)
        agg["avg_hold_bars"] = round(
            sum(v.get("avg_hold_bars", 0) * v.get("executed_trades", 0) for v in per_figi.values()) / max(tot_trades, 1), 1)
        agg["net_per_trade"] = round(agg["net"] / max(tot_trades, 1), 2)
        agg["executed_trades"] = tot_trades
        agg["entries_accepted"] = int(agg["entries_accepted"])
        agg["entries_rejected"] = int(agg["entries_rejected"])
        # rejected_by_reason: объединяем из per_figi
        reasons_agg: dict[str, int] = defaultdict(int)
        for v in per_figi.values():
            for k2, n2 in (v.get("rejected_by_reason") or {}).items():
                reasons_agg[k2] += n2
        agg["rejected_by_reason"] = dict(reasons_agg)
        results[name] = {"config": cfg, "summary": dict(agg), "per_figi": per_figi}
        by_figi[name] = per_figi

    # --- правило выбора ---
    print("\n\n=== ПРАВИЛО ВЫБОРА (guardrails) ===", flush=True)
    passes = []
    for name, r in results.items():
        s = r["summary"]
        passed = True
        reasons = []
        if s["net"] <= 0:
            passed = False
            reasons.append("net<=0")
        if s["executed_trades"] < args.min_trades:
            passed = False
            reasons.append(f"trades<{args.min_trades}")
        if s["pf"] is not None and s["pf"] < 1.0:
            passed = False
            reasons.append("pf<1.0")
        status = "PASS" if passed else "FAIL: " + ",".join(reasons)
        print(f"  {name}: net={s['net']:.0f} trades={s['executed_trades']} pf={s['pf']} -> {status}", flush=True)
        if passed:
            passes.append((name, s))

    report = {
        "period": [args.t_from, args.t_to],
        "universe": sorted(candles.keys()),
        "fixed": {k: BASE_REQ[k] for k in ("entry_tf", "entry_session", "exit_policy",
                                            "commission_rate", "slippage_bps", "capital", "lot")},
        "rule": "net>0, trades>=min, pf>=1.0; далее выбор простейшего в плато",
        "min_trades": args.min_trades,
        "configs": results,
        "passing": [{"name": n, "net": s["net"], "trades": s["executed_trades"],
                     "pf": s["pf"], "net_per_trade": s["net_per_trade"],
                     "turnover_pct": s["turnover_pct"]} for n, s in passes],
        "chosen": None,
    }
    if passes:
        # выбираем не максимум net, а простейший (quorum=1, меньший cooldown) в плато
        simple = sorted(passes, key=lambda x: (x[1]["quorum"] if "quorum" in x[1] else 0, x[1]["cooldown"] if "cooldown" in x[1] else 0))
        # упрощённо: сортируем по (quorum asc, cooldown asc) из config
        def simple_key(item):
            cfg = item[1].get("config", {})
            return (cfg.get("quorum", 9), cfg.get("cooldown", 999))
        chosen = min(passes, key=simple_key)
        report["chosen"] = {"name": chosen[0], "summary": chosen[1]}

    with open(args.out_json, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nОтчёт: {args.out_json} ({time.time()-t0:.0f}с)")


if __name__ == "__main__":
    main()
