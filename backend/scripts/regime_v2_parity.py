"""Stage C.4 — golden/reference parity: legacy RegimeDetector vs Regime v2 (read-only).

На одних и тех же барах сравниваются:
- legacy canonical `RegimeDetector` (5 строк, дефолтные пороги 19/0.5/71/78/2.75);
- v2 по решениям владельца 2026-10-02: direction только H1, trend descriptive,
  volatility на range/RV-перцентиле, structure derived, confidence — однозначность.

Ничего не включает в runtime: provider остаётся legacy. Артефакты:
parity_report.md + parity_summary.json (read-only сессия БД).

Запуск:
  ~/.venvs/deeptrading/bin/python scripts/regime_v2_parity.py \
    --dsn postgresql://deeptrading:deeptrading@192.168.1.7:5432/deeptrading \
    --days 93 --to 2026-10-02T08:00:00Z --top 6 --tfs 300,3600
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.ensemble import resample
from app.services.regime import RegimeDetector
from app.services.regime_calibration import session_of
from app.services.regime_v2.classifier import (
    RegimeV2ClassifierParams,
    classify_row,
    derive_legacy,
)
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

import regime_calibration as rcal

LEGACY_STATES = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL")
V2_STATES = LEGACY_STATES


def build_records(bars, tf_sec: int, window: int) -> list[dict]:
    legacy_rows = RegimeDetector().compute(list(bars))
    ms = compute_measurements(bars, RegimeV2Params(window=window))
    records: list[dict] = []
    for i, m in enumerate(ms):
        if legacy_rows and i < len(legacy_rows):
            lrow = legacy_rows[i]
        else:
            lrow = {"state": "NEUTRAL", "reason": "no_data"}
        obs = classify_row(m["ts"], tf_sec, m, session_of(m["ts"]))
        records.append({
            "session": session_of(m["ts"]),
            "legacy": lrow["state"],
            "legacy_reason": lrow["reason"],
            "v2": derive_legacy(obs),
            "direction": obs.direction,
            "volatility": obs.volatility,
            "structure": obs.structure,
            "confidence": obs.confidence,
            "reasons": ",".join(obs.reason_codes),
        })
    return records


def counter_share(counter: Counter, total: int) -> dict:
    return {k: {"n": counter.get(k, 0), "share": (counter.get(k, 0) / total) if total else 0.0}
            for k in sorted(counter)}


def aggregate(records: list[dict]) -> dict:
    n = len(records)
    legacy_c = Counter(r["legacy"] for r in records)
    v2_c = Counter(r["v2"] for r in records)
    matrix: dict[str, dict[str, int]] = {ls: {vs: 0 for vs in V2_STATES} for ls in LEGACY_STATES}
    for r in records:
        matrix.setdefault(r["legacy"], {vs: 0 for vs in V2_STATES})
        if r["v2"] in matrix[r["legacy"]]:
            matrix[r["legacy"]][r["v2"]] += 1
    agree = sum(1 for r in records if r["legacy"] == r["v2"])
    sessions: dict[str, dict] = {}
    for sess in sorted({r["session"] for r in records}):
        sr = [r for r in records if r["session"] == sess]
        sessions[sess] = {
            "n": len(sr),
            "agreement": (sum(1 for r in sr if r["legacy"] == r["v2"]) / len(sr)) if sr else 0.0,
            "legacy": dict(Counter(r["legacy"] for r in sr)),
            "v2": dict(Counter(r["v2"] for r in sr)),
        }
    disagreements = Counter(
        f"{r['legacy']} -> {r['v2']}" for r in records if r["legacy"] != r["v2"])
    conf = sorted(r["confidence"] for r in records if r["confidence"] > 0)
    conf_q = {}
    if conf:
        for q in (10, 50, 90):
            idx = min(int(len(conf) * q / 100), len(conf) - 1)
            conf_q[f"p{q}"] = conf[idx]
    return {
        "n": n,
        "agreement": agree / n if n else 0.0,
        "legacy": counter_share(legacy_c, n),
        "v2": counter_share(v2_c, n),
        "matrix": matrix,
        "sessions": sessions,
        "top_disagreements": dict(sorted(disagreements.items(), key=lambda kv: -kv[1])[:12]),
        "direction": dict(Counter(r["direction"] for r in records)),
        "volatility": dict(Counter(r["volatility"] for r in records)),
        "structure": dict(Counter(r["structure"] for r in records)),
        "confidence_quantiles": conf_q,
        "v2_reasons": dict(sorted(Counter(r["reasons"] for r in records).items(),
                                  key=lambda kv: -kv[1])[:10]),
    }


def render_report(summary: dict) -> str:
    meta = summary["meta"]
    lines: list[str] = []
    a = lines.append
    a("# Regime v2 — Parity legacy ↔ v2 (Stage C.4, read-only)")
    a("")
    a(f"- Dataset: {', '.join(meta['tickers'])}, {meta['days']} дней, {meta['t_from']} .. {meta['t_to']}")
    a(f"- TFs: {', '.join(meta['tf_labels'])}; v2: W={meta['window']}, direction_tfs={meta['direction_tfs']}, "
      f"c_unknown={meta['c_unknown']}; legacy — дефолтные пороги")
    a(f"- DSN: {meta['dsn_host']} (read-only); provider остаётся legacy, runtime не менялся")
    a("")
    a("**Сравнение по строкам:** legacy `RegimeDetector` и `derive_legacy(v2)` на каждом баре.")
    a("")
    for tf in meta["tf_labels"]:
        agg = summary["blocks"][tf]
        a(f"## {tf}")
        a("")
        a(f"- bars: {agg['n']}, agreement: **{agg['agreement'] * 100:.1f}%**")
        a("")
        a("| label | legacy | v2 |")
        a("|---|---|---|")
        for st in LEGACY_STATES:
            a(f"| {st} | {agg['legacy'].get(st, {}).get('n', 0)} "
              f"({agg['legacy'].get(st, {}).get('share', 0) * 100:.1f}%) | "
              f"{agg['v2'].get(st, {}).get('n', 0)} "
              f"({agg['v2'].get(st, {}).get('share', 0) * 100:.1f}%) |")
        a("")
        a("**Матрица legacy → v2 (n, % строки):**")
        a("")
        a("| legacy \\ v2 | " + " | ".join(V2_STATES) + " |")
        a("|---|" + "|".join(["---"] * len(V2_STATES)) + "|")
        for ls in LEGACY_STATES:
            row = agg["matrix"].get(ls, {})
            total = sum(row.values()) or 1
            cells = [f"{row.get(vs, 0)} ({row.get(vs, 0) / total * 100:.0f}%)" for vs in V2_STATES]
            a(f"| {ls} | " + " | ".join(cells) + " |")
        a("")
        a("**Сессии:**")
        a("")
        a("| session | n | agreement | v2: TREND_UP | TREND_DOWN | RANGE | HIGH_VOL | NEUTRAL |")
        a("|---|---|---|---|---|---|---|---|")
        for sess, d in agg["sessions"].items():
            v2s = d["v2"]
            a(f"| {sess} | {d['n']} | {d['agreement'] * 100:.1f}% | "
              f"{v2s.get('TREND_UP', 0)} | {v2s.get('TREND_DOWN', 0)} | {v2s.get('RANGE', 0)} | "
              f"{v2s.get('HIGH_VOLATILITY', 0)} | {v2s.get('NEUTRAL', 0)} |")
        a("")
        a(f"**Оси v2:** direction {agg['direction']}; volatility {agg['volatility']}; "
          f"structure {agg['structure']}; confidence {agg['confidence_quantiles']}")
        a("")
        a("**Топ расхождений (legacy -> v2):** " + ", ".join(
            f"{k}: {v}" for k, v in agg["top_disagreements"].items()))
        a("")
    a("## Ограничения")
    a("")
    a("- Parity диагностическая: provider default остаётся `legacy`; БД/UI/гейты не менялись.")
    a("- v2-метка — производная от observation; 5m direction=FLAT по решению владельца, "
      "поэтому TREND_* на 5m в v2 не появляются.")
    a("- Hysteresis нет: сравнение по-барное; следующий этап — parity каждого префикса.")
    a("")
    return "\n".join(lines)


async def run(args):
    import os
    dsn = args.dsn or os.environ.get("REGIME_CAL_DSN")
    if not dsn:
        from app.config import get_settings
        dsn = get_settings().database_url.replace("+asyncpg", "")
    t_to = datetime.fromisoformat(args.to.replace("Z", "+00:00")) if args.to else datetime.now(UTC)
    if t_to.tzinfo is None:
        t_to = t_to.replace(tzinfo=UTC)
    t_from = t_to - timedelta(days=args.days)
    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]
    params = RegimeV2ClassifierParams()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = await rcal.open_readonly(dsn)
    try:
        instruments = await rcal.select_universe(conn, t_from, t_to, args)
        raw: dict[str, list[dict]] = {}
        per_ticker: dict[str, dict] = {}
        for inst in instruments:
            candles = await rcal.load_1m(conn, inst["figi"], t_from, t_to)
            per_ticker.setdefault(inst["ticker"], {})
            for tf in tfs:
                bars = resample(candles, tf)
                recs = build_records(bars, tf, args.window)
                label = rcal.tf_label(tf)
                raw.setdefault(label, []).extend(recs)
                per_ticker[inst["ticker"]][label] = aggregate(recs)
    finally:
        await conn.close()

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": args.days, "t_from": t_from.isoformat(), "t_to": t_to.isoformat(),
        "tickers": [i["ticker"] for i in instruments],
        "tf_labels": [rcal.tf_label(tf) for tf in tfs],
        "window": args.window,
        "direction_tfs": list(params.direction_tfs),
        "c_unknown": params.c_unknown,
        "dsn_host": dsn.split("@")[-1],
    }
    blocks = {label: aggregate(recs) for label, recs in raw.items()}
    summary = {"meta": meta, "blocks": blocks, "per_ticker": per_ticker}
    (out_dir / "parity_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = render_report(summary)
    report_path = out_dir / "parity_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=None)
    p.add_argument("--tickers", default=None)
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--to", default=None)
    p.add_argument("--tfs", default="300,3600")
    p.add_argument("--window", type=int, default=12)
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--min-bars", type=int, default=50000)
    p.add_argument("--out-dir", default="reports/regime_v2")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path = asyncio.run(run(args))
    print(f"report: {path}")


if __name__ == "__main__":
    main()
