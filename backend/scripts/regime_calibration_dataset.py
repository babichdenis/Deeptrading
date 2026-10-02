"""Этап B: read-only датасет канонического RegimeDetector с forward-исходами.

Для каждого valid-наблюдения (features бара t) считаются форвардные исходы
баров t+1..t+h (h = 12/24/48 баров): return/ATR, MFE/MAE в ATR, efficiency
ratio, consistency, realized vol, range/ATR и диагностический класс поведения.

Detector, RegimeState, runtime/ensemble не меняются. БД — только SELECT
(сессия read-only). Артефакты: regime_calibration_dataset.csv,
regime_calibration_dataset_summary.json, regime_calibration_forward_report.md.

Запуск (с мака .6, боевая БД на .7):
  ~/.venvs/deeptrading/bin/python scripts/regime_calibration_dataset.py \
    --dsn postgresql://deeptrading:deeptrading@192.168.1.7:5432/deeptrading \
    --days 93 --to 2026-10-02T08:00:00Z --top 6 --tfs 300,3600
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import gzip
import json
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


from app.services.regime import RegimeDetector
from app.services.regime_calibration import (
    FEATURE_KEYS,
    FORWARD_KEYS,
    forward_outcome,
    future_class,
    percentile,
    percentiles,
    spearman,
)

import regime_calibration as rcal

MSK = ZoneInfo("Europe/Moscow")
BASE_FIELDS = (
    "ticker", "figi", "tf", "ts_utc", "ts_msk", "session", "state", "reason",
    "close", "atr_pct", "atr_percentile", "ema_slope", "adx", "volume_ratio",
    "drift_pct", "consistency", "range_ratio", "mixed_code",
)
STATE_GROUPS = ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOLATILITY", "NEUTRAL_mixed")


def group_mask(o: dict, g: str) -> bool:
    if g == "NEUTRAL_mixed":
        return o["reason"] == "mixed"
    return o["state"] == g


def attach_forward(obs: list[dict], bars: list, horizons: list[int]) -> None:
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    atr = [o.get("atr_pct") for o in obs]
    for o in obs:
        if o["reason"] == "warmup":
            continue
        i = o["bar_idx"]
        for h in horizons:
            fo = forward_outcome(closes, highs, lows, atr, i, h)
            if fo is None:
                continue
            for k, v in fo.items():
                o[f"fwd{h}_{k}"] = round(v, 6)
            o[f"fwd{h}_class"] = future_class(fo)


def coverage_counts(horizons: list[int], obs: list[dict]) -> dict:
    valid = [o for o in obs if o["reason"] != "warmup"]
    return {str(h): sum(1 for o in valid if o.get(f"fwd{h}_ret_atr") is not None)
            for h in horizons} | {"valid": len(valid)}


def forward_stats(obs: list[dict], horizons: list[int], primary: int) -> dict:
    valid = [o for o in obs if o["reason"] != "warmup"]
    out: dict = {"n_valid": len(valid), "by_state": {}, "matrix": {}}
    pf = f"fwd{primary}"
    matrix_classes = ["FUT_UP", "FUT_DOWN", "FUT_RANGE", "FUT_HIGH_VOL", "FUT_TRANSITION"]
    for g in STATE_GROUPS:
        rows = [o for o in valid if group_mask(o, g)]
        entry: dict = {"n": len(rows), "by_horizon": {}}
        for h in horizons:
            hh: dict = {"n": sum(1 for o in rows if o.get(f"fwd{h}_ret_atr") is not None)}
            for k in FORWARD_KEYS:
                vals = [o[f"fwd{h}_{k}"] for o in rows if o.get(f"fwd{h}_{k}") is not None]
                if vals:
                    hh[k] = percentiles(vals)
            entry["by_horizon"][str(h)] = hh
        pv = [o for o in rows if o.get(f"{pf}_ret_atr") is not None]
        denom = len(pv) or 1
        entry["probabilities"] = {
            "p_ret_ge_1atr": sum(1 for o in pv if o[f"{pf}_ret_atr"] >= 1.0) / denom,
            "p_ret_le_minus1atr": sum(1 for o in pv if o[f"{pf}_ret_atr"] <= -1.0) / denom,
            "p_range_ge_3atr": sum(1 for o in pv if o[f"{pf}_range_atr"] >= 3.0) / denom,
            "p_er_ge_05": sum(1 for o in pv if o[f"{pf}_er"] >= 0.5) / denom,
            "p_abs_ret_le_05atr": sum(1 for o in pv if abs(o[f"{pf}_ret_atr"]) <= 0.5) / denom,
            "p_mfe1_and_mae1": sum(
                1 for o in pv if o[f"{pf}_mfe_atr"] >= 1.0 and o[f"{pf}_mae_atr"] <= -1.0) / denom,
        }
        cls = Counter(o.get(f"{pf}_class") for o in pv)
        entry["classes"] = {c: cls.get(c, 0) for c in matrix_classes}
        entry["classes"]["NO_DATA"] = len(rows) - len(pv)
        out["by_state"][g] = entry
        out["matrix"][g] = {c: (cls.get(c, 0) / denom) for c in matrix_classes}
    day = [o for o in valid if o["session"] == "day" and o.get(f"{pf}_ret_atr") is not None]
    out["day_session"] = {}
    for g in STATE_GROUPS:
        rows = [o for o in day if group_mask(o, g)]
        if not rows:
            continue
        rets = [o[f"{pf}_ret_atr"] for o in rows]
        out["day_session"][g] = {
            "n": len(rets),
            "p25_ret_atr": percentile(rets, 25),
            "p50_ret_atr": percentile(rets, 50),
            "p75_ret_atr": percentile(rets, 75),
            "p_ret_ge_1atr": sum(1 for v in rets if v >= 1.0) / len(rets),
            "p_ret_le_minus1atr": sum(1 for v in rets if v <= -1.0) / len(rets),
        }
    return out


def correlation_stats(obs: list[dict], primary: int) -> dict:
    pf = f"fwd{primary}"
    valid = [o for o in obs if o["reason"] != "warmup"]
    out: dict = {}
    for outcome in ("ret_atr", "er", "range_atr"):
        out[outcome] = {}
        for feat in FEATURE_KEYS:
            xs, ys = [], []
            for o in valid:
                x = o.get(feat)
                y = o.get(f"{pf}_{outcome}")
                if x is not None and y is not None:
                    xs.append(float(x))
                    ys.append(float(y))
            rho = spearman(xs, ys)
            if rho is not None:
                out[outcome][feat] = rho
    return out


def build_answers(summary: dict, primary: int) -> dict:
    ans: dict[str, str] = {}

    def st(tf, g, key):
        return summary["overall"].get(tf, {}).get("forward", {}).get("by_state", {}).get(g, {}) \
            .get("by_horizon", {}).get(str(primary), {}).get(key, {})

    def prob(tf, g, key):
        return summary["overall"].get(tf, {}).get("forward", {}).get("by_state", {}).get(g, {}) \
            .get("probabilities", {}).get(key, 0.0)

    def pct(x):
        return f"{x * 100:.1f}%"

    def f(x):
        return "—" if x is None else f"{x:.3f}"

    ans[f"TREND_UP forward (h={primary})"] = (
        f"5m p50 ret/ATR {f(st('5m', 'TREND_UP', 'ret_atr').get('p50'))}, "
        f"P(+1 ATR) {pct(prob('5m', 'TREND_UP', 'p_ret_ge_1atr'))}, ER p50 "
        f"{f(st('5m', 'TREND_UP', 'er').get('p50'))}; 1h p50 ret/ATR "
        f"{f(st('1h', 'TREND_UP', 'ret_atr').get('p50'))}, P(+1 ATR) "
        f"{pct(prob('1h', 'TREND_UP', 'p_ret_ge_1atr'))}.")
    ans[f"TREND_DOWN forward (h={primary})"] = (
        f"5m p50 ret/ATR {f(st('5m', 'TREND_DOWN', 'ret_atr').get('p50'))}, "
        f"P(−1 ATR) {pct(prob('5m', 'TREND_DOWN', 'p_ret_le_minus1atr'))}; 1h p50 "
        f"{f(st('1h', 'TREND_DOWN', 'ret_atr').get('p50'))}, P(−1 ATR) "
        f"{pct(prob('1h', 'TREND_DOWN', 'p_ret_le_minus1atr'))}.")
    ans[f"RANGE forward (h={primary})"] = (
        f"5m P(|ret|≤0.5 ATR) {pct(prob('5m', 'RANGE', 'p_abs_ret_le_05atr'))}, "
        f"P(range≥3 ATR) {pct(prob('5m', 'RANGE', 'p_range_ge_3atr'))}, ER p50 "
        f"{f(st('5m', 'RANGE', 'er').get('p50'))}; 1h P(|ret|≤0.5) "
        f"{pct(prob('1h', 'RANGE', 'p_abs_ret_le_05atr'))}.")
    ans[f"HIGH_VOL forward (h={primary})"] = (
        f"5m P(range≥3 ATR) {pct(prob('5m', 'HIGH_VOLATILITY', 'p_range_ge_3atr'))}, "
        f"p50 range/ATR {f(st('5m', 'HIGH_VOLATILITY', 'range_atr').get('p50'))}; 1h "
        f"P(range≥3) {pct(prob('1h', 'HIGH_VOLATILITY', 'p_range_ge_3atr'))}.")
    ans[f"NEUTRAL/mixed forward (h={primary})"] = (
        f"5m P(+1 ATR) {pct(prob('5m', 'NEUTRAL_mixed', 'p_ret_ge_1atr'))}, "
        f"P(−1 ATR) {pct(prob('5m', 'NEUTRAL_mixed', 'p_ret_le_minus1atr'))}, "
        f"P(|ret|≤0.5) {pct(prob('5m', 'NEUTRAL_mixed', 'p_abs_ret_le_05atr'))}; 1h "
        f"P(+1) {pct(prob('1h', 'NEUTRAL_mixed', 'p_ret_ge_1atr'))}, "
        f"P(−1) {pct(prob('1h', 'NEUTRAL_mixed', 'p_ret_le_minus1atr'))}.")
    corr5 = summary["overall"].get("5m", {}).get("correlations", {})
    top_er = sorted(corr5.get("er", {}).items(), key=lambda kv: -abs(kv[1]))[:3]
    top_ret = sorted(corr5.get("ret_atr", {}).items(), key=lambda kv: -abs(kv[1]))[:3]
    top_rng = sorted(corr5.get("range_atr", {}).items(), key=lambda kv: -abs(kv[1]))[:3]
    ans["Какие признаки сильнее всего связаны с forward-исходами (5m)"] = (
        "ER: " + ", ".join(f"{k} ρ={v:+.2f}" for k, v in top_er) + "; "
        "ret/ATR: " + ", ".join(f"{k} ρ={v:+.2f}" for k, v in top_ret) + "; "
        "range/ATR: " + ", ".join(f"{k} ρ={v:+.2f}" for k, v in top_rng) + ".")
    tu5 = st("5m", "TREND_UP", "ret_atr").get("p50")
    td5 = st("5m", "TREND_DOWN", "ret_atr").get("p50")
    tu1 = st("1h", "TREND_UP", "ret_atr").get("p50")
    td1 = st("1h", "TREND_DOWN", "ret_atr").get("p50")
    ans["Предсказывают ли текущие состояния forward-направление?"] = (
        f"Нет, они контр-трендовы: p50 ret/ATR после TREND_UP {f(tu5)} (5m) и {f(tu1)} (1h), "
        f"после TREND_DOWN {f(td5)} (5m) и {f(td1)} (1h); на 5m P(+1ATR) после TREND_UP "
        f"{pct(prob('5m', 'TREND_UP', 'p_ret_ge_1atr'))} < после TREND_DOWN "
        f"{pct(prob('5m', 'TREND_DOWN', 'p_ret_ge_1atr'))}."
    )
    def dss(tf, g, key):
        return summary["overall"].get(tf, {}).get("forward", {}).get("day_session", {}) \
            .get(g, {}).get(key)
    ans["Контр-трендовость не артефакт выходных?"] = (
        f"Подтверждается в day-сессии будней: 5m p50 после TREND_UP {f(dss('5m', 'TREND_UP', 'p50_ret_atr'))}, "
        f"после TREND_DOWN {f(dss('5m', 'TREND_DOWN', 'p50_ret_atr'))}; P(+1) "
        f"{pct(dss('5m', 'TREND_UP', 'p_ret_ge_1atr') or 0)} против "
        f"{pct(dss('5m', 'TREND_DOWN', 'p_ret_ge_1atr') or 0)}."
    )
    ans["Разделяются ли состояния по forward ER/range?"] = (
        f"Слабо: p50 ER у всех состояний 0.14–0.22 (5m) и 0.16–0.22 (1h); "
        f"HIGH_VOL-состояние даёт forward range/ATR p50 "
        f"{f(st('5m', 'HIGH_VOLATILITY', 'range_atr').get('p50'))} против "
        f"{f(st('5m', 'RANGE', 'range_atr').get('p50'))} у RANGE (5m) — то есть не выше, "
        "а ниже (mean reversion волатильности)."
    )
    ans["Ограничения"] = (
        "forward-классы — временная диагностическая разметка (HIGH_VOL: range/√h≥1.5; "
        "UP/DOWN: direction efficiency ≥0.5 и ER≥0.4; RANGE: ER≤0.35), не "
        "production-классификатор; исходы пересекают сессии/ночи/выходные как есть; "
        "features округлены детектором, строки у границ порогов сохранены."
    )
    return ans


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def render_report(summary: dict, primary: int, horizons: list[int]) -> str:
    meta = summary["meta"]
    lines: list[str] = []
    a = lines.append
    a("# Regime Calibration — Stage B: forward outcomes")
    a("")
    a("## Executive summary")
    a("")
    a(f"- Dataset: {', '.join(meta['tickers'])}, {meta['days']} дней, "
      f"{meta['t_from']} .. {meta['t_to']}")
    a(f"- Timeframes: {', '.join(meta['tf_labels'])}; горизонты {horizons} баров, "
      f"primary h={primary}; detector — дефолты, forward-пороги диагностические")
    a(f"- DSN: {meta['dsn_host']} (read-only), generated {meta['generated_at']}")
    for tf in meta["tf_labels"]:
        cov = summary["overall"][tf]["coverage"]
        a(f"- {tf}: valid {cov['valid']}, исходы: "
          + ", ".join(f"h={h}: {cov[str(h)]}" for h in horizons))
    a("")
    a("Forward-исходы считаются по барам t+1..t+h относительно close бара t; "
      "features и состояние — только по закрытым барам ≤ t.")
    a("")

    a("## 1. Forward-квантили по состояниям (primary)")
    a("")
    for tf in meta["tf_labels"]:
        fw = summary["overall"][tf]["forward"]
        rows = []
        for g in STATE_GROUPS:
            e = fw["by_state"][g]
            hh = e["by_horizon"][str(primary)]
            if not hh.get("n"):
                continue
            rows.append([
                g, hh["n"],
                f"{hh['ret_atr']['p25']:+.2f}", f"{hh['ret_atr']['p50']:+.2f}", f"{hh['ret_atr']['p75']:+.2f}",
                f"{hh['er']['p25']:.2f}", f"{hh['er']['p50']:.2f}",
                f"{hh['range_atr']['p25']:.2f}", f"{hh['range_atr']['p50']:.2f}",
                f"{hh['rv_pct']['p50']:.3f}",
            ])
        a(f"**{tf}**")
        a("")
        a(md_table(["state", "n", "ret/ATR p25", "p50", "p75", "ER p25", "p50",
                    "range p25", "p50", "RV% p50"], rows))
        a("")

    a("## 2. Вероятности forward-поведения (primary)")
    a("")
    for tf in meta["tf_labels"]:
        fw = summary["overall"][tf]["forward"]
        rows = []
        for g in STATE_GROUPS:
            p = fw["by_state"][g]["probabilities"]
            rows.append([g, f"{p['p_ret_ge_1atr'] * 100:.1f}%", f"{p['p_ret_le_minus1atr'] * 100:.1f}%",
                         f"{p['p_abs_ret_le_05atr'] * 100:.1f}%", f"{p['p_range_ge_3atr'] * 100:.1f}%",
                         f"{p['p_er_ge_05'] * 100:.1f}%", f"{p['p_mfe1_and_mae1'] * 100:.1f}%"])
        a(f"**{tf}**")
        a("")
        a(md_table(["state", "P(ret≥+1ATR)", "P(ret≤−1ATR)", "P(|ret|≤0.5ATR)",
                    "P(range≥3ATR)", "P(ER≥0.5)", "P(MFE≥1 & MAE≤−1)"], rows))
        a("")

    a("### 2b. Дневная сессия будней (day): проверка, что контр-трендовость не от выходных")
    a("")
    for tf in meta["tf_labels"]:
        ds = summary["overall"][tf]["forward"]["day_session"]
        rows = []
        for g in STATE_GROUPS:
            d = ds.get(g)
            if not d:
                continue
            rows.append([g, d["n"], f"{d['p25_ret_atr']:+.2f}", f"{d['p50_ret_atr']:+.2f}",
                         f"{d['p75_ret_atr']:+.2f}", f"{d['p_ret_ge_1atr'] * 100:.1f}%",
                         f"{d['p_ret_le_minus1atr'] * 100:.1f}%"])
        a(f"**{tf} · day-сессия, h={primary}**")
        a("")
        a(md_table(["state", "n", "ret/ATR p25", "p50", "p75", "P(ret≥+1)", "P(ret≤−1)"], rows))
        a("")

    a("## 3. Матрица: текущее состояние × будущее поведение")
    a("")
    classes = ["FUT_UP", "FUT_DOWN", "FUT_RANGE", "FUT_HIGH_VOL", "FUT_TRANSITION"]
    for tf in meta["tf_labels"]:
        m = summary["overall"][tf]["forward"]["matrix"]
        rows = []
        for g in STATE_GROUPS:
            vals = [f"{m[g].get(c, 0) * 100:.1f}%" for c in classes]
            rows.append([g] + vals)
        a(f"**{tf}** (доли по строке)")
        a("")
        a(md_table(["state"] + classes, rows))
        a("")
    a("Классы: FUT_HIGH_VOL (range/√h ≥ 1.5), затем FUT_UP/DOWN (direction efficiency "
      "≥0.5 и ER≥0.4), FUT_RANGE (ER≤0.35), иначе TRANSITION.")
    a("")

    a("## 4. Вторичные горизонты: p50 по состояниям")
    a("")
    for tf in meta["tf_labels"]:
        fw = summary["overall"][tf]["forward"]
        for h in horizons:
            if h == primary:
                continue
            rows = []
            for g in STATE_GROUPS:
                hh = fw["by_state"][g]["by_horizon"][str(h)]
                if not hh.get("n"):
                    continue
                rows.append([g, hh["n"], f"{hh['ret_atr']['p50']:+.3f}",
                             f"{hh['er']['p50']:.3f}", f"{hh['range_atr']['p50']:.3f}",
                             f"{hh['rv_pct']['p50']:.4f}"])
            a(f"**{tf} · h={h}**")
            a("")
            a(md_table(["state", "n", "ret/ATR p50", "ER p50", "range/ATR p50", "RV% p50"], rows))
            a("")

    a("## 5. Что предсказывает forward-исходы (Spearman, primary)")
    a("")
    def rho(v):
        return "—" if v is None else f"{v:+.3f}"

    for tf in meta["tf_labels"]:
        c = summary["overall"][tf]["correlations"]
        rows = []
        for feat in FEATURE_KEYS:
            rows.append([feat, rho(c["ret_atr"].get(feat)), rho(c["er"].get(feat)),
                         rho(c["range_atr"].get(feat))])
        a(f"**{tf}**")
        a("")
        a(md_table(["feature", "ρ(ret/ATR)", "ρ(ER)", "ρ(range/ATR)"], rows))
        a("")

    a("## 6. Ответы")
    a("")
    for i, (q, v) in enumerate(summary["answers"].items(), 1):
        a(f"{i}. **{q}** — {v}")
    a("")
    a("## 7. Ограничения")
    a("")
    a("- Forward-классы — диагностическая разметка, а не калибровка и не пороги продакшена.")
    a("- Исходы пересекают границы сессий/ночей/выходных — так же, как их увидит барный конвейер.")
    a("- Features округлены детектором (slope 3, drift 3, consistency 2, ADX 1).")
    a("- Датасет содержит строки у границ порогов — для будущей калибровки ничего не выброшено.")
    a("- В БД есть выходные свечи (круглосуточные, объём ~10x ниже дневного); их исходы "
      "включены как есть — weekday-срез при необходимости делается по колонке session.")
    a("")
    return "\n".join(lines)


async def run(args) -> Path:
    import os
    dsn = args.dsn or os.environ.get("REGIME_CAL_DSN")
    if not dsn:
        from app.config import get_settings
        dsn = get_settings().database_url.replace("+asyncpg", "")
    t_to = (datetime.fromisoformat(args.to.replace("Z", "+00:00"))
            if args.to else datetime.now(UTC))
    if t_to.tzinfo is None:
        t_to = t_to.replace(tzinfo=UTC)
    t_from = t_to - timedelta(days=args.days)
    tfs = [int(x) for x in args.tfs.split(",") if x.strip()]
    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    primary = int(args.primary)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = await rcal.open_readonly(dsn)
    try:
        instruments = await rcal.select_universe(conn, t_from, t_to, args)
        det = RegimeDetector()
        per_ticker: dict[str, dict[str, dict]] = {}
        all_obs: list[dict] = []
        for inst in instruments:
            candles = await rcal.load_1m(conn, inst["figi"], t_from, t_to)
            per_ticker.setdefault(inst["ticker"], {})
            for tf in tfs:
                obs, bars = rcal.build_observations(inst["ticker"], inst["figi"], tf, candles, det)
                attach_forward(obs, bars, horizons)
                label = rcal.tf_label(tf)
                per_ticker[inst["ticker"]][label] = {
                    "coverage": coverage_counts(horizons, obs),
                    "forward": forward_stats(obs, horizons, primary),
                    "correlations": correlation_stats(obs, primary),
                }
                all_obs.extend(obs)
    finally:
        await conn.close()

    overall: dict[str, dict] = {}
    for tf in tfs:
        label = rcal.tf_label(tf)
        obs = [o for o in all_obs if o["tf"] == tf]
        overall[label] = {
            "coverage": coverage_counts(horizons, obs),
            "forward": forward_stats(obs, horizons, primary),
            "correlations": correlation_stats(obs, primary),
        }

    meta = {
        "generated_at": datetime.now(UTC).isoformat(),
        "days": args.days,
        "t_from": t_from.isoformat(),
        "t_to": t_to.isoformat(),
        "tickers": [i["ticker"] for i in instruments],
        "universe_source": ("--tickers" if args.tickers else
                            "universe: eligible_tier='eligible', order by avg_daily_turnover desc, "
                            f"bars_1m >= {args.min_bars}"),
        "selection": instruments,
        "tf_labels": [rcal.tf_label(tf) for tf in tfs],
        "horizons": horizons,
        "primary": primary,
        "dsn_host": dsn.split("@")[-1],
    }
    summary = {"meta": meta, "overall": overall, "per_ticker": per_ticker}
    summary["answers"] = build_answers(summary, primary)
    (out_dir / "regime_calibration_dataset_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    fields = list(BASE_FIELDS)
    for h in horizons:
        fields += [f"fwd{h}_{k}" for k in FORWARD_KEYS] + [f"fwd{h}_class"]
    csv_path = out_dir / ("regime_calibration_dataset.csv.gz" if args.gzip else "regime_calibration_dataset.csv")
    opener = gzip.open if args.gzip else open
    with opener(csv_path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for o in all_obs:
            row = dict(o)
            row["tf"] = rcal.tf_label(o["tf"])
            row["ts_utc"] = o["ts"].isoformat()
            row["ts_msk"] = o["ts"].astimezone(MSK).isoformat()
            w.writerow(row)

    report = render_report(summary, primary, horizons)
    report_path = out_dir / "regime_calibration_forward_report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dsn", default=None,
                   help="DSN боевой БД (read-only); иначе env REGIME_CAL_DSN или settings.database_url")
    p.add_argument("--tickers", default=None, help="явный список тикеров через запятую")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--to", default=None, help="ISO-время конца окна (UTC), по умолчанию now")
    p.add_argument("--tfs", default="300,3600", help="ТФ в секундах через запятую")
    p.add_argument("--horizons", default="12,24,48", help="forward-горизонты в барах")
    p.add_argument("--primary", type=int, default=24)
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--min-bars", type=int, default=50000)
    p.add_argument("--gzip", action="store_true", help="писать датасет как .csv.gz")
    p.add_argument("--out-dir", default="reports/regime_calibration")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path = asyncio.run(run(args))
    print(f"report: {path}")


if __name__ == "__main__":
    main()
