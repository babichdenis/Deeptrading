"""RESEARCH-CORE-V2 (B1): машинный eligibility + coverage для передачи B3.

Строит ТОЛЬКО из сохранённого snapshot manifest (median daily turnover + m1
coverage). Без PnL/будущего оборота. Контракт: liquidity = median дневного
денежного оборота (close*volume) на первых 20 полных weekday db-daily сессиях
preperiod; coverage = доля weekday-дней с m1-данными внутри окна. Оба порога
фиксируются здесь ДО какой-либо портфельной сборки и сохраняются как фикстура.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path("reports/architect_20261011/research_core_v2/b1/20261011T1947Z")
SNAP = BASE / "snapshot"

# ---- зафиксированные пороги (записаны ДО портфельной сборки; не подобраны) ----
LIQUIDITY_FLOOR = 10_000_000.0   # медианный дневной денежный оборот, руб.
COVERAGE_FLOOR = 0.90            # доля weekday-дней окна с m1-данными
MIN_SESSIONS = 100               # минимум сессий с данными для инструмента


def load_manifest() -> dict:
    return json.loads((SNAP / "manifest.json").read_text())


def weekday_window_days(params: dict) -> int:
    from datetime import date, timedelta
    s = date.fromisoformat(params["start"][:10])
    e = date.fromisoformat(params["end"][:10])   # end эксклюзивен
    n, d = 0, s
    while d < e:
        if d.weekday() < 5:
            n += 1
        d += timedelta(days=1)
    return n


def main() -> None:
    m = load_manifest()
    wd_total = weekday_window_days(m["params"])
    out = {
        "contract": {
            "liquidity_metric": "median daily turnover close*volume over first 20 full weekday db-daily sessions (preperiod)",
            "liquidity_floor": LIQUIDITY_FLOOR,
            "coverage_metric": "share of weekday window days with m1 data",
            "coverage_floor": COVERAGE_FLOOR,
            "min_sessions": MIN_SESSIONS,
            "built_without": "PnL / future turnover / any strategy output",
        },
        "window": {"start": m["params"]["start"], "end": m["params"]["end"],
                   "weekday_total": wd_total},
        "tickers": {},
    }
    elig = []
    for figi, entry in sorted(m["per_ticker"].items()):
        t = entry.get("ticker", figi)
        m1 = entry.get("m1", {})
        days = m1.get("days_with_data") or 0
        cov = min(days / wd_total, 1.0) if wd_total else 0.0
        raw_turn = entry.get("preperiod_median_daily_turnover")
        turn = float(raw_turn) if raw_turn is not None else None
        liq_ok = turn is not None and turn >= LIQUIDITY_FLOOR
        cov_ok = cov >= COVERAGE_FLOOR and days >= MIN_SESSIONS
        has_m1 = (m1.get("written_rows") or 0) > 0
        out["tickers"][t] = {
            "figi": figi, "m1_rows": m1.get("written_rows") or 0,
            "sessions": days, "coverage": round(cov, 4),
            "median_daily_turnover_preperiod": turn,
            "liquidity_ok": liq_ok, "coverage_ok": cov_ok,
            "has_m1_data": has_m1,
            "eligible": bool(has_m1 and liq_ok and cov_ok),
            "exclusion_reason": None if (has_m1 and liq_ok and cov_ok) else (
                "no_m1_data" if not has_m1 else
                ("low_coverage" if not cov_ok else "low_liquidity")),
        }
        if out["tickers"][t]["eligible"]:
            elig.append(t)
    out["eligible_tickers"] = sorted(elig)
    out["n_eligible"] = len(elig)
    dest = BASE / "eligibility_machine.json"
    dest.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print(f"eligible: {len(elig)}/{len(out['tickers'])} -> {sorted(elig)}")
    print(f"исключено:")
    for t, e in sorted(out["tickers"].items()):
        if not e["eligible"]:
            print(f"  {t:6s} {e['exclusion_reason']:14s} cov={e['coverage']} "
                  f"sessions={e['sessions']} turn={e['median_daily_turnover_preperiod']}")


if __name__ == "__main__":
    sys.exit(main())
