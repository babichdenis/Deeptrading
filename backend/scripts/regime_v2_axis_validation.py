"""Stage C.2 — read-only валидация независимых осей Regime v2 на боевой БД.

Для каждого TF и окна W считает измерения v2 (канонические ATR/EMA/ER/ADX)
и проверяет, предсказывает ли каждая ось свой forward-исход:
Spearman, знаковое соответствие (signed-оси), бакеты; train (70%) / OOS (30%);
основной срез weekday/day, отдельно weekend. Сетка горизонтов h (6/12/24/48)
показывает, живёт ли семантика оси на коротких/длинных горизонтах.
Пороги продакшена не меняются.

Запуск:
  ~/.venvs/deeptrading/bin/python scripts/regime_v2_axis_validation.py \
    --dsn postgresql://deeptrading:deeptrading@192.168.1.7:5432/deeptrading \
    --days 93 --to 2026-10-02T08:00:00Z --top 6 --tfs 300,3600
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.services.ensemble import resample
from app.services.regime_calibration import forward_outcome, session_of
from app.services.regime_v2.measurements import RegimeV2Params, compute_measurements
from app.services.regime_v2.validation import bucket_table, pair_metrics

import regime_calibration as rcal

TRAIN_FRAC = 0.7
AXES = (
    ("direction_drift", "drift_atr", "ret_atr", True),
    ("direction_slope", "slope_atr", "ret_atr", True),
    ("direction_di", "di_spread", "ret_atr", True),
    ("trend_er", "er", "er", False),
    ("consistency", "consistency", "er", False),
    ("vol_atrperc", "atr_percentile", "range_atr", False),
    ("vol_rv", "rv_atr", "rv_atr", False),
    ("range", "range_atr", "range_atr", False),
)
DIRECTION_EDGES = (-1.5, -1.0, -0.5, -0.2, 0.2, 0.5, 1.0, 1.5)
ER_EDGES = (0.1, 0.2, 0.3, 0.4, 0.5)


def build_rows(bars, window: int, horizons: list[int]) -> list[dict]:
    ms = compute_measurements(bars, RegimeV2Params(window=window))
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    atr_pct = [m["atr_pct"] for m in ms]
    rows: list[dict] = []
    for i, m in enumerate(ms):
        row = {"ts": ms[i]["ts"], "session": session_of(ms[i]["ts"]), **m}
        has_any = False
        for h in horizons:
            fo = forward_outcome(closes, highs, lows, atr_pct, i, h)
            if fo is None:
                continue
            row[f"fwd{h}_ret_atr"] = fo["ret_atr"]
            row[f"fwd{h}_er"] = fo["er"]
            row[f"fwd{h}_range_atr"] = fo["range_atr"]
            row[f"fwd{h}_rv_atr"] = (fo["rv_pct"] / m["atr_pct"]) if m["atr_pct"] else None
            has_any = True
        if has_any:
            rows.append(row)
    return rows


def split_train_oos(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    ordered = sorted(rows, key=lambda r: r["ts"])
    cut = int(len(ordered) * TRAIN_FRAC)
    return ordered[:cut], ordered[cut:]


def slice_metrics(key: str, base: str, h: int, signed: bool,
                  train: list[dict], oos: list[dict]) -> dict:
    outcome = f"fwd{h}_{base}"
    return {"train": pair_metrics(train, key, outcome),
            "oos": pair_metrics(oos, key, outcome),
            "signed": signed}


def verdict(metrics: dict, signed: bool) -> str:
    rho = metrics.get("spearman")
    sm = metrics.get("sign_match")
    if rho is None:
        return "no_data"
    if signed:
        return "pass" if (rho > 0 and (sm is None or sm >= 0.55)) else "fail"
    return "pass" if rho > 0 else "fail"


def _f(x):
    return "—" if x is None else f"{x:+.3f}"


def _p(x):
    return "—" if x is None else f"{x * 100:.1f}%"


def _edge(x):
    return "−∞" if x is None else f"{x:+.1f}"


def render_report(summary: dict) -> str:
    meta = summary["meta"]
    primary = meta["primary"]
    lines: list[str] = []
    a = lines.append
    a("# Regime v2 — Axis Validation (Stage C.2, read-only)")
    a("")
    a(f"- Dataset: {', '.join(meta['tickers'])}, {meta['days']} дней, {meta['t_from']} .. {meta['t_to']}")
    a(f"- TFs: {', '.join(meta['tf_labels'])}; окна W = {meta['windows']}, "
      f"горизонты h = {meta['horizons']} (primary h={primary}); train {int(TRAIN_FRAC * 100)}% / OOS по времени")
    a(f"- Срез осей: weekday/day (основной), weekend — отдельно; DSN {meta['dsn_host']} (read-only)")
    a("")
    a("**Критерии приёмки (зафиксированы до прогона OOS):** signed-оси (direction) — "
      "OOS Spearman > 0 и знаковое соответствие ≥ 0.55; unsigned — OOS Spearman > 0. "
      "Провал ≠ правка порогов: ось перепроектируется.")
    a("")
    for tf in meta["tf_labels"]:
        for w in meta["windows"]:
            block = summary["blocks"][tf][str(w)]
            a(f"## {tf} · W={w} (primary h={primary})")
            a("")
            a("| ось | срез | n train | n OOS | ρ train | ρ OOS | sign tr | sign OOS | verdict |")
            a("|---|---|---|---|---|---|---|---|---|")
            for ax in block["axes"]:
                for sl, m in (("weekday/day", ax["day"]), ("weekend", ax["weekend"])):
                    tr, oo = m["train"], m["oos"]
                    sm_tr = _p(tr["sign_match"]) if ax["signed"] else "—"
                    sm_oo = _p(oo["sign_match"]) if ax["signed"] else "—"
                    a("| " + " | ".join(str(x) for x in (
                        ax["axis"], sl, tr["n"], oo["n"], _f(tr["spearman"]), _f(oo["spearman"]),
                        sm_tr, sm_oo, verdict(oo, ax["signed"]))) + " |")
            a("")
    a("## Чувствительность к горизонту (weekday/day, OOS ρ)")
    a("")
    for tf in meta["tf_labels"]:
        for w in meta["windows"]:
            block = summary["blocks"][tf][str(w)]
            hs = meta["horizons"]
            a(f"**{tf} · W={w}**: " + " | ".join(f"h={h}" for h in hs))
            a("")
            a("| ось | " + " | ".join(f"h={h}" for h in hs) + " |")
            a("|---|" + "|".join(["---"] * len(hs)) + "|")
            for ax in block["axes"]:
                vals = []
                for h in hs:
                    m = block["sensitivity"][ax["axis"]].get(str(h))
                    vals.append(_f(m["oos_spearman"]) if m else "—")
                a("| " + ax["axis"] + " | " + " | ".join(vals) + " |")
            a("")
    for tf in meta["tf_labels"]:
        w = meta["default_window"]
        block = summary["blocks"][tf][str(w)]
        a(f"## Бакеты (weekday/day, W={w}, h={primary})")
        a("")
        for name in ("direction_drift", "trend_er"):
            a(f"**{tf} · {name}**")
            a("")
            a("| от | до | n | p25 | p50 | p75 |")
            a("|---|---|---|---|---|---|")
            for b in block["buckets"][name]:
                a("| " + " | ".join(str(x) for x in (
                    _edge(b["lo"]), _edge(b["hi"]), b["n"],
                    f"{b['p25']:+.3f}", f"{b['p50']:+.3f}", f"{b['p75']:+.3f}")) + " |")
            a("")
    a("## Ограничения")
    a("")
    a("- Измерения v2 не подключены к runtime; пороги продакшена не менялись.")
    a("- Forward-исходы пересекают сессии/ночи/выходные; weekend показан отдельно.")
    a("- Сетка горизонтов — диагностика формы семантики; решение об оси принимается по OOS.")
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
    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    primary = int(args.primary)
    if primary not in horizons:
        raise SystemExit(f"primary h={primary} не входит в горизонты {horizons}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = await rcal.open_readonly(dsn)
    try:
        instruments = await rcal.select_universe(conn, t_from, t_to, args)
        raw: dict[str, dict[int, list[dict]]] = {}
        for inst in instruments:
            candles = await rcal.load_1m(conn, inst["figi"], t_from, t_to)
            for tf in tfs:
                bars = resample(candles, tf)
                label = rcal.tf_label(tf)
                raw.setdefault(label, {})
                for w in windows:
                    raw[label].setdefault(w, []).extend(build_rows(bars, w, horizons))
    finally:
        await conn.close()

    blocks: dict[str, dict[str, dict]] = {}
    for tf_label, by_w in raw.items():
        blocks[tf_label] = {}
        for w, rows in by_w.items():
            train, oos = split_train_oos(rows)

            def day(rs):
                return [r for r in rs if r["session"] == "day"]

            def we(rs):
                return [r for r in rs if r["session"] == "weekend"]
            axes = []
            for label, key, base, signed in AXES:
                day_m = slice_metrics(key, base, primary, signed, day(train), day(oos))
                we_m = slice_metrics(key, base, primary, signed, we(train), we(oos))
                axes.append({"axis": label, "key": key, "outcome": f"fwd{primary}_{base}",
                             "signed": signed, "day": day_m, "weekend": we_m})
            sensitivity: dict[str, dict[str, dict]] = {}
            for label, key, base, signed in AXES:
                sensitivity[label] = {}
                for h in horizons:
                    m = slice_metrics(key, base, h, signed, day(train), day(oos))["oos"]
                    sensitivity[label][str(h)] = {"oos_spearman": m["spearman"],
                                                  "oos_sign_match": m["sign_match"],
                                                  "oos_n": m["n"]}
            blocks[tf_label][str(w)] = {
                "axes": axes,
                "sensitivity": sensitivity,
                "buckets": {
                    "direction_drift": bucket_table(day(oos), "drift_atr",
                                                    f"fwd{primary}_ret_atr", DIRECTION_EDGES),
                    "trend_er": bucket_table(day(oos), "er", f"fwd{primary}_er", ER_EDGES),
                },
            }

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": args.days, "t_from": t_from.isoformat(), "t_to": t_to.isoformat(),
        "tickers": [i["ticker"] for i in instruments],
        "tf_labels": [rcal.tf_label(tf) for tf in tfs],
        "windows": windows, "default_window": windows[len(windows) // 2],
        "horizons": horizons, "primary": primary,
        "dsn_host": dsn.split("@")[-1],
    }
    summary = {"meta": meta, "blocks": blocks}
    (out_dir / "axis_validation_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = render_report(summary)
    report_path = out_dir / "axis_validation_report.md"
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
    p.add_argument("--windows", default="6,12,24")
    p.add_argument("--horizons", default="6,12,24,48")
    p.add_argument("--primary", type=int, default=24)
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
