"""Read-only диагностика канонического RegimeDetector по боевой БД.

Отвечает на вопросы: какова реальная доля NEUTRAL/mixed, какие конъюнкты
трендового дерева чаще всего не проходят, как отличаются 5m и H1 при одних
и тех же параметрах (drift_bars=6), есть ли flickering.

Ничего не пишет в БД (сессия принудительно read-only), не меняет production
код. Артефакты: regime_calibration_report.md, regime_calibration_summary.json,
regime_calibration_features.csv.

Запуск (с мака .6, боевая БД на .7):
  ~/.venvs/deeptrading/bin/python scripts/regime_calibration.py \
    --dsn postgresql://deeptrading:deeptrading@192.168.1.7:5432/deeptrading \
    --days 93 --to 2026-10-02T08:00:00Z --top 6 --tfs 300,3600
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg
from app.engine.models import Candle as EngineCandle
from app.services.regime import RegimeDetector, compute_regime
from app.services.regime_calibration import (
    FEATURE_KEYS,
    bar_range_ratio,
    classify_mixed,
    percentiles,
    session_of,
    summarize_mixed,
    transition_stats,
    trend_conditions,
)

MSK = ZoneInfo("Europe/Moscow")
TF_LABELS = {300: "5m", 3600: "1h", 60: "1m", 600: "10m", 900: "15m", 1800: "30m", 7200: "2h", 14400: "4h", 86400: "1d"}

CSV_FIELDS = (
    "ticker", "figi", "tf", "ts_utc", "ts_msk", "session", "state", "reason",
    "close", "atr_pct", "atr_percentile", "ema_slope", "adx", "volume_ratio",
    "drift_pct", "consistency", "range_ratio", "mixed_code",
)


def tf_label(tf_sec: int) -> str:
    return TF_LABELS.get(tf_sec, f"{tf_sec}s")


def pct(x: float | None, digits: int = 1) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.{digits}f}%"


def share_pct(by_state: dict, state: str) -> str:
    return pct(by_state.get(state, {}).get("share_valid", 0))


def fmt(x: float | None, digits: int = 4) -> str:
    if x is None:
        return "—"
    return f"{x:.{digits}g}"


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


async def open_readonly(dsn: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(dsn)
    await conn.execute("SET default_transaction_read_only = on")
    return conn


async def select_universe(conn, t_from: datetime, t_to: datetime, args) -> list[dict]:
    if args.tickers:
        out = []
        for t in [x.strip() for x in args.tickers.split(",") if x.strip()]:
            row = await conn.fetchrow(
                "SELECT ticker, figi, avg_daily_turnover FROM universe "
                "WHERE ticker = $1 OR figi = $1 LIMIT 1", t)
            if row is None:
                raise SystemExit(f"тикер/figi {t!r} не найден в universe")
            bars = await conn.fetchval(
                "SELECT count(*) FROM candles WHERE figi=$1 AND interval=1 "
                "AND ts >= $2 AND ts < $3", row["figi"], t_from, t_to)
            out.append({"ticker": row["ticker"], "figi": row["figi"],
                        "avg_daily_turnover": float(row["avg_daily_turnover"] or 0),
                        "bars_1m": int(bars or 0)})
        return out
    rows = await conn.fetch(
        "SELECT u.ticker, u.figi, u.avg_daily_turnover, "
        "       count(c.ts) AS bars_1m "
        "FROM universe u "
        "LEFT JOIN candles c ON c.figi = u.figi AND c.interval = 1 "
        "     AND c.ts >= $1 AND c.ts < $2 "
        "WHERE u.eligible_tier = 'eligible' "
        "GROUP BY u.ticker, u.figi, u.avg_daily_turnover "
        "HAVING count(c.ts) >= $3 "
        "ORDER BY u.avg_daily_turnover DESC NULLS LAST "
        "LIMIT $4", t_from, t_to, args.min_bars, args.top)
    return [{"ticker": r["ticker"], "figi": r["figi"],
             "avg_daily_turnover": float(r["avg_daily_turnover"] or 0),
             "bars_1m": int(r["bars_1m"] or 0)} for r in rows]


async def load_1m(conn, figi: str, t_from: datetime, t_to: datetime) -> list[EngineCandle]:
    rows = await conn.fetch(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE figi = $1 AND interval = 1 AND ts >= $2 AND ts < $3 "
        "ORDER BY ts", figi, t_from, t_to)
    return [EngineCandle(ts=r["ts"], open=float(r["open"]), high=float(r["high"]),
                         low=float(r["low"]), close=float(r["close"]),
                         volume=float(r["volume"] or 0)) for r in rows]


def build_observations(ticker: str, figi: str, tf_sec: int,
                       candles: list[EngineCandle], det: RegimeDetector) -> tuple[list[dict], list]:
    states, _, bars = compute_regime(candles, tf_sec)
    obs: list[dict] = []
    for idx, (row, bar) in enumerate(zip(states, bars, strict=True)):
        f = row.get("features")
        rr = bar_range_ratio(bar.high, bar.low, bar.close, f["atr_pct"]) if f else None
        o: dict = {
            "ticker": ticker, "figi": figi, "tf": tf_sec, "bar_idx": idx,
            "ts": row["ts"], "session": session_of(row["ts"]),
            "state": row["state"], "reason": row["reason"],
            "close": float(bar.close), "range_ratio": rr,
        }
        if f:
            for k in FEATURE_KEYS:
                if k != "range_ratio":
                    o[k] = f[k]
        if row["reason"] == "mixed":
            o["mixed_code"] = classify_mixed(f, rr, det)["code"]
        obs.append(o)
    return obs, bars


def feature_map(obs_row: dict) -> dict:
    return {k: obs_row[k] for k in FEATURE_KEYS if k != "range_ratio" and k in obs_row}


def coverage_counts(obs: list[dict]) -> dict:
    valid = [o for o in obs if o["reason"] != "warmup"]
    states = Counter(o["state"] for o in valid)
    reasons = Counter(o["reason"] for o in obs)
    n = len(valid)
    return {
        "total": len(obs), "warmup": len(obs) - n, "valid": n,
        "by_state": {k: {"n": v, "share_valid": (v / n if n else 0.0)}
                     for k, v in sorted(states.items())},
        "by_reason": dict(sorted(reasons.items())),
    }


def group_feature_percentiles(obs: list[dict]) -> dict:
    groups = {
        "all_valid": [o for o in obs if o["reason"] != "warmup"],
        "RANGE": [o for o in obs if o["state"] == "RANGE"],
        "TREND_UP": [o for o in obs if o["state"] == "TREND_UP"],
        "TREND_DOWN": [o for o in obs if o["state"] == "TREND_DOWN"],
        "HIGH_VOLATILITY": [o for o in obs if o["state"] == "HIGH_VOLATILITY"],
        "NEUTRAL_mixed": [o for o in obs if o["reason"] == "mixed"],
    }
    out: dict[str, dict] = {}
    for name, rows in groups.items():
        out[name] = {"n": len(rows)}
        for key in FEATURE_KEYS:
            vals = [o.get(key) for o in rows]
            if any(v is not None for v in vals):
                out[name][key] = percentiles([v for v in vals if v is not None])
    return out


def session_stats(obs: list[dict]) -> dict:
    valid = [o for o in obs if o["reason"] != "warmup"]
    out: dict[str, dict] = {}
    for s in sorted({o["session"] for o in valid}):
        rows = [o for o in valid if o["session"] == s]
        mixed = sum(1 for o in rows if o["reason"] == "mixed")
        states = Counter(o["state"] for o in rows)
        out[s] = {
            "valid": len(rows), "mixed": mixed,
            "mixed_share": mixed / len(rows) if rows else 0.0,
            "state_share": {k: v / len(rows) for k, v in sorted(states.items())},
        }
    return out


def hv_overlap(obs: list[dict], det: RegimeDetector) -> dict:
    hv = [o for o in obs if o["state"] == "HIGH_VOLATILITY"]
    cnt: Counter = Counter()
    for o in hv:
        t = trend_conditions(o, det)
        if t["up_pass"]:
            cnt["would_trend_up"] += 1
        elif t["dn_pass"]:
            cnt["would_trend_down"] += 1
        else:
            cnt["no_trend_conditions"] += 1
    return {"high_vol_bars": len(hv), **dict(sorted(cnt.items()))}


def mixed_answers(obs: list[dict], diags: list[dict], det: RegimeDetector) -> dict:
    valid = [o for o in obs if o["reason"] != "warmup"]
    mixed = [o for o in obs if o["reason"] == "mixed"]
    total = len(valid)
    up_signal = [d for d in diags if d["primary"] == "up"]
    dn_signal = [d for d in diags if d["primary"] == "down"]
    conflict = [d for d in diags if d["primary"] == "none"]

    def near(vals: list[float], lo: float, hi: float) -> int:
        return sum(1 for v in vals if lo <= v < hi)

    def failed_share(rows: list[dict], conj: str) -> float:
        return (sum(1 for d in rows if conj in d[f"failed_{'up' if d['primary'] == 'up' else 'dn'}"])
                / len(rows)) if rows else 0.0

    adx_failed_up = [o["adx"] for o, d in zip(mixed, diags, strict=True)
                     if d["primary"] == "up" and "adx" in d["failed_up"]]
    adx_failed_dn = [o["adx"] for o, d in zip(mixed, diags, strict=True)
                     if d["primary"] == "down" and "adx" in d["failed_dn"]]
    cons_failed_up = [o["consistency"] for o, d in zip(mixed, diags, strict=True)
                      if d["primary"] == "up" and "cons" in d["failed_up"]]
    cons_failed_dn = [o["consistency"] for o, d in zip(mixed, diags, strict=True)
                      if d["primary"] == "down" and "cons" in d["failed_dn"]]
    drift_conflict = [abs(o["drift_pct"]) for o, d in zip(mixed, diags, strict=True)
                      if d["primary"] == "none"]
    shares = {
        "neutral_share_of_valid": (len(mixed) / total) if total else 0.0,
        "mixed_n": len(mixed),
        "up_signal_n": len(up_signal),
        "dn_signal_n": len(dn_signal),
        "conflict_n": len(conflict),
        "up_only_adx": sum(1 for d in up_signal if d["blockers"] == ["adx"]),
        "up_only_cons": sum(1 for d in up_signal if d["blockers"] == ["cons"]),
        "up_both": sum(1 for d in up_signal if len(d["blockers"]) == 2),
        "dn_only_adx": sum(1 for d in dn_signal if d["blockers"] == ["adx"]),
        "dn_only_cons": sum(1 for d in dn_signal if d["blockers"] == ["cons"]),
        "dn_both": sum(1 for d in dn_signal if len(d["blockers"]) == 2),
        "adx_failed_up": len(adx_failed_up),
        "adx_failed_dn": len(adx_failed_dn),
        "adx_up_below15_share": (near(adx_failed_up, 0, 15) / len(adx_failed_up)) if adx_failed_up else 0.0,
        "adx_up_15_19_share": (near(adx_failed_up, 15, det.adx_threshold) / len(adx_failed_up)) if adx_failed_up else 0.0,
        "adx_dn_below15_share": (near(adx_failed_dn, 0, 15) / len(adx_failed_dn)) if adx_failed_dn else 0.0,
        "adx_dn_15_19_share": (near(adx_failed_dn, 15, det.adx_threshold) / len(adx_failed_dn)) if adx_failed_dn else 0.0,
        "cons_failed_up": len(cons_failed_up),
        "cons_failed_dn": len(cons_failed_dn),
        "cons_up_near_miss_066_071": (near(cons_failed_up, det.cons_relax_pct, det.cons_pct) / len(cons_failed_up)) if cons_failed_up else 0.0,
        "cons_dn_near_miss_029_034": (near(cons_failed_dn, 1 - det.cons_pct, 1 - det.cons_relax_pct) / len(cons_failed_dn)) if cons_failed_dn else 0.0,
        "conflict_drift_margin_p50": percentiles(drift_conflict).get("p50") if drift_conflict else None,
        "conflict_low_drift_share": (near(drift_conflict, 0.0, det.drift_pct) / len(drift_conflict)) if drift_conflict else 0.0,
    }
    return shares


def boundary_count(mixed: list[dict], diags: list[dict], det: RegimeDetector) -> dict:
    eps = {
        "drift_pct": (0.001, (det.drift_pct, -det.drift_pct, det.drift_strong_pct, -det.drift_strong_pct)),
        "consistency": (0.005, (det.cons_pct, det.cons_relax_pct, 1 - det.cons_pct, 1 - det.cons_relax_pct)),
        "adx": (0.05, (det.adx_threshold,)),
        "ema_slope": (0.001, (det.slope_threshold * 100, -det.slope_threshold * 100)),
        "atr_percentile": (0.05, (det.atr_percentile_threshold,)),
    }
    near = 0
    inconsistent = 0
    for o, d in zip(mixed, diags, strict=True):
        hit = any(abs(float(o[k]) - b) <= e for k, (e, bounds) in eps.items() for b in bounds)
        if hit:
            near += 1
        if d["trend_up_pass"] or d["trend_dn_pass"]:
            inconsistent += 1
    return {"mixed_near_boundary": near,
            "mixed_near_boundary_share": (near / len(mixed)) if mixed else 0.0,
            "mixed_rounding_inconsistent": inconsistent}


def series_stats(obs: list[dict], det: RegimeDetector) -> dict:
    coverage = coverage_counts(obs)
    valid = [o for o in obs if o["reason"] != "warmup"]
    mixed = [o for o in obs if o["reason"] == "mixed"]
    diags = [classify_mixed(feature_map(o), o.get("range_ratio"), det) for o in mixed]
    out = {
        "coverage": coverage,
        "features_by_group": group_feature_percentiles(obs),
        "sessions": session_stats(obs),
        "transitions": transition_stats([o["state"] for o in valid]),
        "mixed": summarize_mixed(diags),
        "mixed_answers": mixed_answers(obs, diags, det),
        "hv_overlap": hv_overlap(obs, det),
        "boundary": boundary_count(mixed, diags, det),
    }
    return out


def weekday_stats(obs: list[dict], det: RegimeDetector) -> dict:
    wd = [o for o in obs if o["session"] != "weekend"]
    mixed = [o for o in wd if o["reason"] == "mixed"]
    diags = [classify_mixed(feature_map(o), o.get("range_ratio"), det) for o in mixed]
    return {
        "coverage": coverage_counts(wd),
        "mixed_answers": mixed_answers(wd, diags, det),
        "transitions": transition_stats([o["state"] for o in wd if o["reason"] != "warmup"]),
    }


def render_report(summary: dict) -> str:
    meta = summary["meta"]
    lines: list[str] = []
    a = lines.append
    a("# Regime Calibration Report — canonical RegimeDetector (read-only)")
    a("")
    a("## Executive summary")
    a("")
    a(f"- Dataset: {', '.join(meta['tickers'])}")
    a(f"- Period: {meta['t_from']} .. {meta['t_to']} ({meta['days']} дней)")
    a(f"- Timeframes: {', '.join(meta['tf_labels'])}; detector parameters — дефолты "
      f"(drift_pct={meta['detector']['drift_pct']}, drift_bars={meta['detector']['drift_bars']}, "
      f"cons_pct={meta['detector']['cons_pct']}, adx_threshold={meta['detector']['adx_threshold']}, "
      f"atr_percentile_threshold={meta['detector']['atr_percentile_threshold']}, "
      f"range_mult={meta['detector']['range_mult']})")
    a(f"- DSN: {meta['dsn_host']} (сессия read-only), generated {meta['generated_at']}")
    a("")
    for tf in meta["tf_labels"]:
        cov = summary["overall"][tf]["coverage"]
        n = cov["valid"]
        st = cov["by_state"]
        a(f"**{tf}**: valid observations {n}, warmup {cov['warmup']} "
          f"({pct(cov['warmup'] / cov['total'] if cov['total'] else 0)}). "
          f"NEUTRAL/mixed {pct(st.get('NEUTRAL', {}).get('share_valid', 0))}, "
          f"RANGE {pct(st.get('RANGE', {}).get('share_valid', 0))}, "
          f"TREND_UP {pct(st.get('TREND_UP', {}).get('share_valid', 0))}, "
          f"TREND_DOWN {pct(st.get('TREND_DOWN', {}).get('share_valid', 0))}, "
          f"HIGH_VOLATILITY {pct(st.get('HIGH_VOLATILITY', {}).get('share_valid', 0))}.")
        wd = summary["overall_weekday"][tf]["coverage"]
        wst = wd["by_state"]
        a(f"  - без выходных (weekday-only): valid {wd['valid']}, "
          f"NEUTRAL/mixed {pct(wst.get('NEUTRAL', {}).get('share_valid', 0))}, "
          f"RANGE {pct(wst.get('RANGE', {}).get('share_valid', 0))}, "
          f"TREND_UP {pct(wst.get('TREND_UP', {}).get('share_valid', 0))}, "
          f"TREND_DOWN {pct(wst.get('TREND_DOWN', {}).get('share_valid', 0))}, "
          f"HIGH_VOLATILITY {pct(wst.get('HIGH_VOLATILITY', {}).get('share_valid', 0))}.")
    a("")
    a("Смежные определения: `NEUTRAL/mixed` — финальный else дерева решений "
      "(режим не классифицирован), `warmup` — первые 64 бара ряда.")
    a("")

    a("## 1. Coverage: state × timeframe")
    a("")
    headers = ["TF", "valid", "warmup", "RANGE", "TREND_UP", "TREND_DOWN", "HIGH_VOL", "NEUTRAL/mixed"]
    rows = []
    for tf in meta["tf_labels"]:
        cov = summary["overall"][tf]["coverage"]
        st = cov["by_state"]
        rows.append([tf, cov["valid"], cov["warmup"], share_pct(st, "RANGE"), share_pct(st, "TREND_UP"),
                     share_pct(st, "TREND_DOWN"), share_pct(st, "HIGH_VOLATILITY"), share_pct(st, "NEUTRAL")])
    a(md_table(headers, rows))
    a("")
    a("### Coverage без выходных (weekday-only)")
    a("")
    headers = ["TF", "valid", "RANGE", "TREND_UP", "TREND_DOWN", "HIGH_VOL", "NEUTRAL/mixed"]
    rows = []
    for tf in meta["tf_labels"]:
        cov = summary["overall_weekday"][tf]["coverage"]
        st = cov["by_state"]
        rows.append([tf, cov["valid"], share_pct(st, "RANGE"), share_pct(st, "TREND_UP"),
                     share_pct(st, "TREND_DOWN"), share_pct(st, "HIGH_VOLATILITY"), share_pct(st, "NEUTRAL")])
    a(md_table(headers, rows))
    a("")

    a("### Coverage по тикерам")
    a("")
    headers = ["Ticker", "TF", "valid", "mixed", "RANGE", "TREND_UP", "TREND_DOWN", "HIGH_VOL"]
    rows = []
    for tk, tdata in summary["per_ticker"].items():
        for tf in meta["tf_labels"]:
            st = tdata[tf]["coverage"]
            g = st["by_state"]
            n = st["valid"]
            rows.append([tk, tf, n, share_pct(g, "NEUTRAL"), share_pct(g, "RANGE"), share_pct(g, "TREND_UP"),
                         share_pct(g, "TREND_DOWN"), share_pct(g, "HIGH_VOLATILITY")])
    a(md_table(headers, rows))
    a("")

    a("## 2. Почему NEUTRAL/mixed: разбор условий")
    a("")
    for tf in meta["tf_labels"]:
        ma = summary["overall"][tf]["mixed_answers"]
        a(f"### {tf}: mixed = {ma['mixed_n']} ({pct(ma['neutral_share_of_valid'])} от valid)")
        a("")
        a(f"- Есть направленный сигнал (slope+drift согласованы): вверх {ma['up_signal_n']}, "
          f"вниз {ma['dn_signal_n']}; конфликт slope/drift — {ma['conflict_n']}")
        a(f"- up-сигнал, блокирует только ADX: {ma['up_only_adx']}, только consistency: {ma['up_only_cons']}, "
          f"оба: {ma['up_both']}")
        a(f"- down-сигнал, блокирует только ADX: {ma['dn_only_adx']}, только consistency: {ma['dn_only_cons']}, "
          f"оба: {ma['dn_both']}")
        a(f"- ADX-блок у up: <15 — {pct(ma['adx_up_below15_share'])}, 15..19 — {pct(ma['adx_up_15_19_share'])}")
        a(f"- ADX-блок у down: <15 — {pct(ma['adx_dn_below15_share'])}, 15..19 — {pct(ma['adx_dn_15_19_share'])}")
        a(f"- consistency-near-miss: up {pct(ma['cons_up_near_miss_066_071'])} строк в [0.66,0.71), "
          f"down {pct(ma['cons_dn_near_miss_029_034'])} строк в (0.29,0.34]")
        a(f"- конфликт slope/drift: p50 |drift| = {fmt(ma['conflict_drift_margin_p50'])}%, "
          f"доля с |drift| < 0.5% — {pct(ma['conflict_low_drift_share'])}")
        a("")
        sm = summary["overall"][tf]["mixed"]
        rows = [[code, n] for code, n in list(sm["by_code"].items())[:12]]
        a(md_table(["mixed_code", "n"], rows))
        a("")
    a("Маргины до ближайшего RANGE/HIGH_VOL порога (mixed):")
    a("")
    for tf in meta["tf_labels"]:
        m = summary["overall"][tf]["mixed"]["margins"]
        rows = []
        for key, p in m.items():
            rows.append([key, fmt(p.get("p25")), fmt(p.get("p50")), fmt(p.get("p75"))])
        a(f"**{tf}**")
        a("")
        a(md_table(["margin", "p25", "p50", "p75"], rows))
        a("")

    a("## 3. Feature distributions: mixed vs состояния")
    a("")
    for tf in meta["tf_labels"]:
        g = summary["overall"][tf]["features_by_group"]
        for key in ("adx", "atr_percentile", "ema_slope", "drift_pct", "consistency", "range_ratio", "atr_pct"):
            rows = []
            for gname in ("all_valid", "RANGE", "TREND_UP", "TREND_DOWN", "HIGH_VOLATILITY", "NEUTRAL_mixed"):
                gg = g.get(gname, {})
                p = gg.get(key)
                if not p:
                    continue
                rows.append([gname, gg.get("n", 0), fmt(p.get("p05")), fmt(p.get("p25")),
                             fmt(p.get("p50")), fmt(p.get("p75")), fmt(p.get("p95"))])
            a(f"**{tf} · {key}**")
            a("")
            a(md_table(["group", "n", "p05", "p25", "p50", "p75", "p95"], rows))
            a("")

    a("## 4. 5m vs H1 (одни и те же параметры, drift_bars=6)")
    a("")
    a(f"- Эффективное окно дрейфа: 5m — {6 * 5} минут, 1h — {6 * 60} минут (6 часов)")
    a("")
    rows = []
    for tf in meta["tf_labels"]:
        o = summary["overall"][tf]
        ma = o["mixed_answers"]
        mix = o["features_by_group"]["NEUTRAL_mixed"]
        tr = o["transitions"]
        rows.append([
            tf,
            pct(ma["neutral_share_of_valid"]),
            fmt(mix.get("atr_pct", {}).get("p50")),
            fmt(mix.get("adx", {}).get("p50")),
            fmt(mix.get("drift_pct", {}).get("p50")),
            fmt(mix.get("consistency", {}).get("p50")),
            fmt(mix.get("range_ratio", {}).get("p50")),
            f"{tr['transitions'] / (o['coverage']['valid'] / 1000):.1f}" if o["coverage"]["valid"] else "—",
            pct(tr["singleton_share"]),
        ])
    a(md_table(["TF", "mixed share", "ATR% p50", "ADX p50", "drift% p50", "cons p50",
                "range_ratio p50", "переходов/1000 bars", "singleton runs"], rows))
    a("")

    a("## 5. Сессии")
    a("")
    for tf in meta["tf_labels"]:
        sess = summary["overall"][tf]["sessions"]
        rows = []
        for s, d in sess.items():
            rows.append([s, d["valid"], d["mixed"], pct(d["mixed_share"]),
                         pct(d["state_share"].get("TREND_UP", 0)),
                         pct(d["state_share"].get("TREND_DOWN", 0)),
                         pct(d["state_share"].get("RANGE", 0)),
                         pct(d["state_share"].get("HIGH_VOLATILITY", 0))])
        a(f"**{tf}**")
        a("")
        a(md_table(["session", "valid", "mixed", "mixed share", "TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL"], rows))
        a("")

    a("## 6. Переходы состояний")
    a("")
    rows = []
    for tf in meta["tf_labels"]:
        tr = summary["overall"][tf]["transitions"]
        rows.append([tf, tr["observations"], tr["transitions"], f"{tr['mean_run']:.1f}",
                     fmt(tr["median_run"]), tr["max_run"], tr["singleton_runs"], pct(tr["singleton_share"])])
    a(md_table(["TF", "bars", "transitions", "mean run", "median run", "max run",
                "singleton runs", "singleton share"], rows))
    a("")

    a("## 7. HIGH_VOLATILITY vs trend (перекрытие)")
    a("")
    rows = []
    for tf in meta["tf_labels"]:
        h = summary["overall"][tf]["hv_overlap"]
        n = h["high_vol_bars"]
        rows.append([tf, n,
                     f"{h.get('would_trend_up', 0)} ({pct(h.get('would_trend_up', 0) / n if n else 0)})",
                     f"{h.get('would_trend_down', 0)} ({pct(h.get('would_trend_down', 0) / n if n else 0)})",
                     pct(h.get("no_trend_conditions", 0) / n if n else 0)])
    a(md_table(["TF", "HIGH_VOL bars", "прошли бы TREND_UP", "прошли бы TREND_DOWN", "нет трендовых условий"], rows))
    a("")
    a("Проверка по округлённым features: HIGH_VOL стоит в дереве раньше тренда, "
      "поэтому эти бары не могут стать TREND_*.")
    a("")

    a("## 8. Ответы на контрольные вопросы")
    a("")
    ans = summary["answers"]
    for i, (q, v) in enumerate(ans.items(), 1):
        a(f"{i}. **{q}** — {v}")
    a("")
    a("## 9. Ограничения метода")
    a("")
    boundary_bits = []
    inconsistent_bits = []
    for tf in meta["tf_labels"]:
        b = summary["overall"][tf]["boundary"]
        boundary_bits.append(f"{tf} {b['mixed_near_boundary']} ({pct(b['mixed_near_boundary_share'])})")
        inconsistent_bits.append(f"{tf} {b['mixed_rounding_inconsistent']}")
    a("- Разбор mixed использует округлённые features детектора; строк у границ порогов: "
      + ", ".join(boundary_bits) + "; из них tree-непротиворечивых (trend_pass=True): "
      + ", ".join(inconsistent_bits))
    a("- Форвардные исходы не считались: это диагностика текущего детектора, не калибровка.")
    a("- Пороговые параметры detector не менялись.")
    weekend_bits = []
    for tf in meta["tf_labels"]:
        o = summary["overall"][tf]
        n = o["coverage"]["valid"] or 1
        weekend_bits.append(f"{tf} {pct(o['sessions'].get('weekend', {}).get('valid', 0) / n)}")
    a("- В БД есть выходные свечи (круглосуточные, объём ~10x ниже дневного); "
      "их доля среди valid: " + ", ".join(weekend_bits)
      + ". Runtime в выходные не торгует — weekday-only срез дан в §1.")
    a("")
    return "\n".join(lines)


def build_answers(summary: dict, meta: dict) -> dict:
    ans: dict[str, str] = {}
    o5 = summary["overall"].get("5m", {})
    o1 = summary["overall"].get("1h", {})
    c5 = o5.get("coverage", {}).get("by_state", {})
    c1 = o1.get("coverage", {}).get("by_state", {})
    m5 = o5.get("coverage", {})
    m1 = o1.get("coverage", {})
    neutral5 = (c5.get("NEUTRAL", {}).get("share_valid", 0))
    neutral1 = (c1.get("NEUTRAL", {}).get("share_valid", 0))
    w5 = summary.get("overall_weekday", {}).get("5m", {}).get("coverage", {}).get("by_state", {})
    w1 = summary.get("overall_weekday", {}).get("1h", {}).get("coverage", {}).get("by_state", {})
    wd5 = w5.get("NEUTRAL", {}).get("share_valid", 0)
    wd1 = w1.get("NEUTRAL", {}).get("share_valid", 0)
    warm_share5 = m5.get("warmup", 0) / m5.get("total", 1) if m5.get("total") else 0
    warm_share1 = m1.get("warmup", 0) / m1.get("total", 1) if m1.get("total") else 0
    ans["Действительно ли ~50% observations NEUTRAL (mixed)?"] = (
        f"Нет: 5m — {pct(neutral5)}, 1h — {pct(neutral1)} от valid-наблюдений "
        f"(без выходных: {pct(wd5)} и {pct(wd1)}).")
    ans["Какая доля NEUTRAL — warmup, какая — mixed?"] = (
        f"warmup ничтожен: {pct(warm_share5)} рядов 5m и {pct(warm_share1)} рядов 1h; "
        f"NEUTRAL/mixed — {pct(neutral5)} и {pct(neutral1)} от valid.")
    code5 = list(o5.get("mixed", {}).get("by_code", {}).items())[:5]
    code1 = list(o1.get("mixed", {}).get("by_code", {}).items())[:5]
    ans["Какие условия чаще всего блокируют TREND_UP/TREND_DOWN?"] = (
        "5m: " + ", ".join(f"{k}={v}" for k, v in code5) + "; 1h: " + ", ".join(f"{k}={v}" for k, v in code1) + ".")
    mg5 = o5.get("mixed", {}).get("margins", {})
    ans["Как распределены ADX/drift/consistency/slope у mixed?"] = (
        "см. §2-3; p50 ADX mixed 5m — " + str(fmt(o5.get("features_by_group", {}).get("NEUTRAL_mixed", {}).get("adx", {}).get("p50")))
        + ", drift — " + str(fmt(o5.get("features_by_group", {}).get("NEUTRAL_mixed", {}).get("drift_pct", {}).get("p50")))
        + ", cons — " + str(fmt(o5.get("features_by_group", {}).get("NEUTRAL_mixed", {}).get("consistency", {}).get("p50")))
        + "; смещение до диапазона — margin p50 "
        + str(fmt(mg5.get("range_drift_margin", {}).get("p50"))) + " п.п. drift")
    ans["Насколько отличаются 5m и H1?"] = (
        f"mixed share {pct(neutral5)} vs {pct(neutral1)}; переходов/1000 bars — "
        f"{o5.get('transitions', {}).get('transitions', 0) / max(m5.get('valid', 1), 1) * 1000:.1f} vs "
        f"{o1.get('transitions', {}).get('transitions', 0) / max(m1.get('valid', 1), 1) * 1000:.1f}; "
        "детали — §4.")
    d5 = mg5.get("range_drift_margin", {})
    d1 = o1.get("mixed", {}).get("margins", {}).get("range_drift_margin", {})
    ans["Признаки, что абсолютный drift_pct=0.5% не масштабируется между ТФ?"] = (
        f"окно 6 баров = 30 мин на 5m и 6 часов на 1h; запас до порога у mixed p50 "
        f"{fmt(d5.get('p50'))} п.п. (5m) vs {fmt(d1.get('p50'))} п.п. (1h).")
    ma5 = o5.get("mixed_answers", {})
    ans["Признаки, что cons_pct=0.71 слишком жёсткий?"] = (
        f"consistency-near-miss в [0.66,0.71): up {pct(ma5.get('cons_up_near_miss_066_071', 0))} "
        f"из {ma5.get('cons_failed_up', 0)} отказов; down в (0.29,0.34] — "
        f"{pct(ma5.get('cons_dn_near_miss_029_034', 0))} из {ma5.get('cons_failed_dn', 0)}.")
    h5 = o5.get("hv_overlap", {})
    hv_n = h5.get("high_vol_bars", 0)
    ans["Признаки, что HIGH_VOLATILITY перекрывает направленный тренд?"] = (
        f"5m: из {hv_n} HV-баров прошли бы TREND_UP {h5.get('would_trend_up', 0)}, "
        f"TREND_DOWN {h5.get('would_trend_down', 0)}; 1h: "
        f"{o1.get('hv_overlap', {}).get('would_trend_up', 0)}/{o1.get('hv_overlap', {}).get('would_trend_down', 0)} "
        f"из {o1.get('hv_overlap', {}).get('high_vol_bars', 0)}.")
    t5 = o5.get("transitions", {})
    t1 = o1.get("transitions", {})
    ans["Есть ли flickering, требующий hysteresis?"] = (
        f"singleton runs: 5m {pct(t5.get('singleton_share', 0))} ({t5.get('singleton_runs', 0)}), "
        f"1h {pct(t1.get('singleton_share', 0))} ({t1.get('singleton_runs', 0)}); "
        f"median run: {fmt(t5.get('median_run'))} / {fmt(t1.get('median_run'))} бар.")
    ans["Что вынести в следующую calibration dataset?"] = (
        "features (ADX, DI spread, ATR percentile, slope/ATR, drift/ATR, consistency, ER, range/ATR, volume ratio) "
        "+ forward outcomes (return/ATR, MFE/MAE, future ER/consistency) на 12/24/48 баров, "
        "по тикерам/ТФ/сессиям, с сохранением строк у границ порогов.")
    return ans


async def run(args) -> Path:
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
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = await open_readonly(dsn)
    try:
        instruments = await select_universe(conn, t_from, t_to, args)
        det = RegimeDetector()
        per_ticker: dict[str, dict[str, dict]] = {}
        all_obs: list[dict] = []
        for inst in instruments:
            candles = await load_1m(conn, inst["figi"], t_from, t_to)
            per_ticker.setdefault(inst["ticker"], {})
            for tf in tfs:
                obs, _bars = build_observations(inst["ticker"], inst["figi"], tf, candles, det)
                per_ticker[inst["ticker"]][tf_label(tf)] = series_stats(obs, det)
                all_obs.extend(obs)
    finally:
        await conn.close()

    overall: dict[str, dict] = {}
    overall_weekday: dict[str, dict] = {}
    for tf in tfs:
        label = tf_label(tf)
        obs = [o for o in all_obs if o["tf"] == tf]
        overall[label] = series_stats(obs, det)
        overall_weekday[label] = weekday_stats(obs, det)

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
        "tf_labels": [tf_label(tf) for tf in tfs],
        "detector": {k: getattr(det, k) for k in (
            "slope_threshold", "adx_threshold", "atr_percentile_threshold", "range_mult",
            "drift_pct", "drift_bars", "drift_strong_pct", "cons_pct", "cons_relax_pct",
            "cons_bars", "atr_period", "ema_slope", "ema_fast", "ema_slow", "window")},
        "dsn_host": dsn.split("@")[-1],
    }
    summary = {"meta": meta, "overall": overall, "overall_weekday": overall_weekday,
               "per_ticker": per_ticker}
    summary["answers"] = build_answers(summary, meta)

    (out_dir / "regime_calibration_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    with (out_dir / "regime_calibration_features.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for o in all_obs:
            row = dict(o)
            row["tf"] = tf_label(o["tf"])
            row["ts_utc"] = o["ts"].isoformat()
            row["ts_msk"] = o["ts"].astimezone(MSK).isoformat()
            w.writerow(row)

    report = render_report(summary)
    report_path = out_dir / "regime_calibration_report.md"
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
    p.add_argument("--top", type=int, default=6)
    p.add_argument("--min-bars", type=int, default=50000)
    p.add_argument("--out-dir", default="reports/regime_calibration")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    path = asyncio.run(run(args))
    print(f"report: {path}")


if __name__ == "__main__":
    main()
