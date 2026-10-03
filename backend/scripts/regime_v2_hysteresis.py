"""Stage C.5 — flicker/lag отчёт hysteresis Regime v2 (read-only).

Сравнивает на одних барах: legacy `RegimeDetector`, stateless v2 и варианты
hysteresis (confirm_bars/min_tenure + sticky structure 0.4/0.25). Метрики:
transitions/1000, singleton share, длины run, lag смены (окно 24 бара).
Provider остаётся legacy; runtime не меняется.

Запуск:
  ~/.venvs/deeptrading/bin/python scripts/regime_v2_hysteresis.py \
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
from app.services.regime_calibration import percentile
from app.services.regime_v2.classifier import RegimeV2ClassifierParams, classify_row, derive_legacy
from app.services.regime_v2.hysteresis import HysteresisParams, RegimeV2State
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements

import regime_calibration as rcal

LAG_WINDOW = 24
VARIANTS: tuple[tuple[str, HysteresisParams | None], ...] = (
    ("stateless", None),
    ("mild", HysteresisParams(confirm_bars=2, min_tenure=1)),
    ("default", HysteresisParams(confirm_bars=2, min_tenure=3)),
    ("strong", HysteresisParams(confirm_bars=3, min_tenure=5)),
)
LABELS = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL")


def run_lengths(seq: list[str]) -> list[int]:
    runs: list[int] = []
    prev: str | None = None
    n = 0
    for s in seq:
        if s == prev:
            n += 1
        else:
            if prev is not None:
                runs.append(n)
            prev = s
            n = 1
    if prev is not None:
        runs.append(n)
    return runs


def variant_metrics(seqs: list[list[str]]) -> dict:
    all_runs: list[int] = []
    total_obs = 0
    transitions = 0
    for seq in seqs:
        runs = run_lengths(seq)
        all_runs.extend(runs)
        total_obs += len(seq)
        transitions += max(len(runs) - 1, 0)
    n_runs = len(all_runs)
    return {
        "bars": total_obs,
        "transitions": transitions,
        "transitions_per_1000": (transitions / total_obs * 1000) if total_obs else 0.0,
        "runs": n_runs,
        "singleton_share": (sum(1 for r in all_runs if r == 1) / n_runs) if n_runs else 0.0,
        "mean_run": (total_obs / n_runs) if n_runs else 0.0,
        "median_run": percentile(all_runs, 50) if all_runs else 0.0,
    }


def lag_stats(raw: list[str], smooth: list[str]) -> dict:
    lags: list[int] = []
    missed = 0
    for i in range(1, len(raw)):
        if raw[i] == raw[i - 1]:
            continue
        target = raw[i]
        found = None
        for j in range(i, min(i + LAG_WINDOW + 1, len(smooth))):
            if smooth[j] == target:
                found = j - i
                break
        if found is None:
            missed += 1
        else:
            lags.append(found)
    changes = len(lags) + missed
    return {
        "raw_changes": changes,
        "adopted": len(lags),
        "adopted_share": (len(lags) / changes) if changes else 0.0,
        "adopted_within_window": (len(lags) / changes) if changes else 0.0,
        "missed": missed,
        "lags": lags,
        "lag_p50": percentile(lags, 50) if lags else None,
        "lag_p90": percentile(lags, 90) if lags else None,
        "lag_mean": (sum(lags) / len(lags)) if lags else None,
    }


def render_report(summary: dict) -> str:
    meta = summary["meta"]
    lines: list[str] = []
    a = lines.append
    a("# Regime v2 — Hysteresis: flicker / lag (Stage C.5, read-only)")
    a("")
    a(f"- Dataset: {', '.join(meta['tickers'])}, {meta['days']} дней, {meta['t_from']} .. {meta['t_to']}")
    a(f"- TFs: {', '.join(meta['tf_labels'])}; v2 W={meta['window']}; sticky structure 0.4/0.25; "
      f"lag-окно {LAG_WINDOW} баров")
    a(f"- DSN: {meta['dsn_host']} (read-only); provider остаётся legacy, runtime не менялся")
    a("")
    for tf in meta["tf_labels"]:
        agg = summary["blocks"][tf]
        a(f"## {tf}")
        a("")
        a("| вариант | transitions/1000 | singleton share | mean run | median run | "
          "lag p50 | lag p90 | принято в окне |")
        a("|---|---|---|---|---|---|---|---|")
        for name, v in agg["metrics"].items():
            lg = agg["lag"].get(name, {})
            a(f"| {name} | {v['transitions_per_1000']:.1f} | {v['singleton_share'] * 100:.1f}% | "
              f"{v['mean_run']:.1f} | {v['median_run']:.1f} | "
              f"{lg.get('lag_p50') if lg.get('lag_p50') is not None else '—'} | "
              f"{lg.get('lag_p90') if lg.get('lag_p90') is not None else '—'} | "
              f"{(lg.get('adopted_share', 0) * 100):.1f}% |")
        a("")
        a("| label | stateless | default hyst |")
        a("|---|---|---|")
        bars = agg["bars"]
        for st in LABELS:
            r = agg["dist_stateless"].get(st, 0) / bars * 100 if bars else 0.0
            d = agg["dist_default"].get(st, 0) / bars * 100 if bars else 0.0
            a(f"| {st} | {r:.1f}% | {d:.1f}% |")
        a("")
    a("## Ограничения")
    a("")
    a("- Provider default остаётся `legacy`; runtime/гейты/UI не менялись.")
    a("- Метрики по всем барам (включая weekend); лаг — только для смен, принятых в окне "
      f"{LAG_WINDOW} баров; непринятые считаются отдельно.")
    a("- `stateless` — те же измерения v2 без состояния (не legacy).")
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
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cp = RegimeV2ClassifierParams()

    conn = await rcal.open_readonly(dsn)
    try:
        instruments = await rcal.select_universe(conn, t_from, t_to, args)
        seqs: dict[str, dict[str, list[list[str]]]] = {}
        lags: dict[str, dict[str, list[dict]]] = {}
        for inst in instruments:
            candles = await rcal.load_1m(conn, inst["figi"], t_from, t_to)
            for tf in tfs:
                bars = resample(candles, tf)
                label = rcal.tf_label(tf)
                legacy_rows = RegimeDetector().compute(list(bars))
                legacy_seq = [r["state"] for r in legacy_rows] or ["NEUTRAL"] * len(bars)
                ms = compute_measurements(bars, RegimeV2Params(window=args.window))
                stateless = [derive_legacy(classify_row(m["ts"], tf, m, params=cp)) for m in ms]
                per_variant: dict[str, list[str]] = {"stateless": stateless}
                for name, hp in VARIANTS:
                    if hp is None:
                        continue
                    state = RegimeV2State(cp, hp)
                    sm = []
                    for m in ms:
                        _, _, lab = state.update(m["ts"], tf, m)
                        sm.append(lab)
                    per_variant[name] = sm
                seqs.setdefault(label, {"legacy": [], **{n: [] for n, _ in VARIANTS}})
                seqs[label]["legacy"].append(legacy_seq)
                for n in per_variant:
                    seqs[label][n].append(per_variant[n])
                lags.setdefault(label, {n: [] for n, hp in VARIANTS if hp is not None})
                for n in lags[label]:
                    lags[label][n].append(lag_stats(stateless, per_variant[n]))
    finally:
        await conn.close()

    blocks: dict[str, dict] = {}
    for label, data in seqs.items():
        metrics = {"legacy": variant_metrics(data["legacy"])}
        for n, _ in VARIANTS:
            metrics[n] = variant_metrics(data[n])
        lag_out: dict[str, dict] = {}
        for n, ll in lags[label].items():
            changes = sum(x["raw_changes"] for x in ll)
            adopted = sum(x["adopted"] for x in ll)
            all_lags = [lag for x in ll for lag in x["lags"]]
            lag_out[n] = {
                "raw_changes": changes,
                "adopted": adopted,
                "adopted_share": (adopted / changes) if changes else 0.0,
                "missed": changes - adopted,
                "lag_p50": percentile(all_lags, 50) if all_lags else None,
                "lag_p90": percentile(all_lags, 90) if all_lags else None,
                "lag_mean": (sum(all_lags) / len(all_lags)) if all_lags else None,
            }
        dist_stateless: Counter = Counter()
        dist_default: Counter = Counter()
        for s in data["stateless"]:
            dist_stateless.update(s)
        for s in data["default"]:
            dist_default.update(s)
        blocks[label] = {
            "metrics": metrics,
            "lag": lag_out,
            "dist_stateless": dict(dist_stateless),
            "dist_default": dict(dist_default),
            "bars": sum(len(s) for s in data["stateless"]),
        }

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": args.days, "t_from": t_from.isoformat(), "t_to": t_to.isoformat(),
        "tickers": [i["ticker"] for i in instruments],
        "tf_labels": [rcal.tf_label(tf) for tf in tfs],
        "window": args.window,
        "lag_window": LAG_WINDOW,
        "variants": [n for n, _ in VARIANTS],
        "dsn_host": dsn.split("@")[-1],
    }
    summary = {"meta": meta, "blocks": blocks}
    (out_dir / "hysteresis_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = render_report(summary)
    report_path = out_dir / "hysteresis_report.md"
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
