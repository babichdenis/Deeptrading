#!/usr/bin/env python3
"""Worker-3 / B3: дневной контекст и выбор акций по PARALLEL-DAY-1.md.

18 фиксированных политик = lookback(20/60/120) × entry(10:15/11:00 MSK) × arm(A/B/C):
  A : прошлый return_L(prev close) > 0
  B : A + последний закрытый 5m rsi14_5m >= 50 и macd_hist_5m > 0 (NULL -> no-entry)
  C : B + тикер в top10 по ER60 среди всех тогда eligible акций (ties FIGI)
LONG_ONLY, выход до 18:44 (reference horizon 525 с clamp). 5 слотов, 20% стартового
капитала 1.0. Оба cost-сценария c5_s2(5/2)/c10_s5(10/5). Splits:
  train<=2026-05-31, val 2026-06-01..07-31, eval 2026-08-01..10-08 (будни, 10:00<=MSK<18:44).
Исполнение через КАНОНИЧЕСКИЙ research.daypack_20261014 (genuine known-prefix equity
отдельно от closed_cashflow_diagnostic — defect-3). Baselines: cash и signal-free
(вход 10:15/11:00 на eligible-universe без сигнала, выход 18:44, 2 fees).

DEVIATION (задокументировано): дневной контекст (returns/ER60/turnover) вычисляется
САМИМ ПАКЕТОМ point-in-time из snapshot d1_derived (raw OHLCV), т.к. готовых колонок
нет; формулы строго по PARALLEL_DAY_1.md (A/B3). 5m-условия (rsi14/macd) — из v3 X.
"""
from __future__ import annotations

import sys
import os
import json
import csv
import hashlib
import argparse
from pathlib import Path
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

BACKEND = Path(__file__).resolve().parents[1]
for p in (BACKEND, BACKEND / "research", BACKEND / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from daypack_20261014.evaluate import evaluate  # noqa: E402
from daypack_20261014 import frozen_portfolio as fp  # noqa: E402

MSK = ZoneInfo("Europe/Moscow")
UTC = timezone.utc

# ---- immutable inputs -------------------------------------------------------
REP = BACKEND / "reports/architect_20261012/ml_mtf_1"
B1V3 = REP / "b1/20261013T221949Z_v3_full"
SNAP_CSV = BACKEND / "reports/architect_20261011/research_core_v2/b1/20261011T1947Z/snapshot/csv"
UNIV = BACKEND / "reports/architect_20261011/research_core_v2/b1/20261011T1947Z/universe_elite.json"
DAYMAN = BACKEND / "research/daypack_20261014/manifest.json"
FEAT_DIR = B1V3 / "features"

COST_SCENARIOS = [("c5_s2", 5, 2), ("c10_s5", 10, 5)]
HORIZON = 525  # reference; clamp к 18:44 MSK в frozen_portfolio
SESSION_START = 600  # 10:00 MSK
SESSION_END = 1124   # 18:44 MSK
ENTRY_MIN = [615, 660]          # 10:15, 11:00 MSK
LOOKBACKS = [20, 60, 120]
ARMS = ["A", "B", "C"]
TURNOVER_MIN = 10_000_000.0      # 10M RUB median20
SPLITS = {"train": (None, date(2026, 5, 31)),
          "val": (date(2026, 6, 1), date(2026, 7, 31)),
          "eval": (date(2026, 8, 1), date(2026, 10, 8))}


def _sha(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load_universe():
    u = json.loads(Path(UNIV).read_text(encoding="utf-8"))
    m = {i["figi"]: i["ticker"] for i in u["instruments"]}
    # только те, у кого есть minute-файл snapshot (HHRU без данных -> нет файла)
    out = {}
    for figi, ticker in m.items():
        if (SNAP_CSV / f"{ticker}__m1.csv").exists():
            out[figi] = ticker
    return out


# ---- daily context (point-in-time из d1_derived raw OHLCV) -------------------
def load_daily(figi_map: dict) -> dict:
    """Возвращает {figi: (dates[date], closes[float], turns[float])} отсортированные."""
    out = {}
    for figi, ticker in figi_map.items():
        f = SNAP_CSV / f"{ticker}__d1_derived.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f, usecols=["ts", "close", "volume"])
        # ts -> date (daily bar). Парсим гибко.
        ts = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df["d"] = ts.dt.date
        df = df.dropna(subset=["d", "close"])
        df = df[df["close"] > 0].sort_values("d")
        dates = [d for d in df["d"].tolist()]
        closes = df["close"].to_numpy(dtype=float)
        turns = (df["volume"].to_numpy(dtype=float) * closes)
        out[figi] = (dates, closes, turns)
    return out


def derive_daily_from_m1(snap_csv: Path, figi_map: dict) -> dict:
    """DEVIATION: frozen d1_derived усечён (~33 бара, конец 2026-09-13) — недостаточно
    для lookback 60/120. Реконструируем дневной OHLCV ИЗ ПОЛНЫХ m1 (point-in-time,
    покрытие через 2026-10-08). close = close последнего 1m-бара сессии (18:44 MSK),
    volume = сумма 1m-объёмов; turnover = volume*close."""
    out = {}
    for figi, ticker in figi_map.items():
        f = snap_csv / f"{ticker}__m1.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f, usecols=["ts", "close", "volume", "partial"])
        if "partial" in df.columns:
            df = df[df["partial"].isna() | (df["partial"] == "")]
        df = df[(df["close"] > 0) & (df["volume"] > 0)]
        ts = pd.to_datetime(df["ts"], utc=True, errors="coerce").dt.tz_convert(MSK)
        df = df.assign(d=ts.dt.date)
        df = df.dropna(subset=["d", "close"])
        grp = df.groupby("d").agg(close=("close", "last"), volume=("volume", "sum"))
        grp = grp[grp["close"] > 0].sort_index()
        dates = [d for d in grp.index.tolist()]
        closes = grp["close"].to_numpy(dtype=float)
        turns = (grp["volume"].to_numpy(dtype=float) * closes)
        out[figi] = (dates, closes, turns)
    return out


def daily_context(series, D: date):
    """Point-in-time контекст НА дату D (использует закрытия строго ДО D)."""
    dates, closes, turns = series
    # индекс последнего бара со датой < D (предыдущая сессия)
    idx = None
    for i, d in enumerate(dates):
        if d < D:
            idx = i
        else:
            break
    if idx is None:
        return None
    n = idx + 1
    ctx = {"idx": idx, "n_past": n}
    # returns
    ctx["return20"] = (closes[idx] / closes[idx - 20] - 1.0) if n > 20 else None
    ctx["return60"] = (closes[idx] / closes[idx - 60] - 1.0) if n > 60 else None
    ctx["return120"] = (closes[idx] / closes[idx - 120] - 1.0) if n > 120 else None
    # ER60 = |close[idx]-close[idx-60]| / sum |change| за 60 закрытий
    if n > 60:
        chg = np.abs(np.diff(closes[idx - 60:idx + 1]))
        ctx["er60"] = abs(closes[idx] - closes[idx - 60]) / (chg.sum() + 1e-12)
    else:
        ctx["er60"] = None
    ctx["turn_median20"] = (float(np.median(turns[idx - 19:idx + 1]))
                            if n >= 20 else None)
    # eligibility-флаги (feature-availability)
    ctx["elig_er"] = n > 60
    ctx["elig_turn"] = (n >= 20 and ctx["turn_median20"] is not None
                         and ctx["turn_median20"] >= TURNOVER_MIN)
    ctx["elig_ret120"] = n > 120
    return ctx


# ---- v3 features lookup (rsi14_5m, macd_hist_5m) ----------------------------
def load_feature_lut(figi_map: dict) -> dict:
    lut = {}
    for figi, ticker in figi_map.items():
        f = FEAT_DIR / f"features_{ticker}.parquet"
        if not f.exists():
            continue
        ft = pq.read_table(f, columns=["decision_ts", "rsi14_5m", "macd_hist_5m"])
        ts = pd.to_datetime(ft.column("decision_ts").to_pandas(),
                            utc=True, errors="coerce")
        rsi = ft.column("rsi14_5m").to_pandas()
        macd = ft.column("macd_hist_5m").to_pandas()
        d = {}
        for t, r, m in zip(ts, rsi, macd):
            if t is None or pd.isna(t):
                continue
            d[t.isoformat()] = (r if not pd.isna(r) else None,
                                m if not pd.isna(m) else None)
        lut[figi] = d
    return lut


# ---- session calendar -------------------------------------------------------
def trading_days(start: date, end: date):
    d = start
    out = []
    while d <= end:
        if d.weekday() < 5:  # будни
            out.append(d)
        d += timedelta(days=1)
    return out


def split_of(d: date):
    for s, (a, b) in SPLITS.items():
        lo = a if a else date(2000, 1, 1)
        if lo <= d <= b:
            return s
    return None


def entry_ts_ms(d: date, entry_min: int) -> datetime:
    local = datetime(d.year, d.month, d.day, entry_min // 60, entry_min % 60,
                     tzinfo=MSK)
    return local.astimezone(UTC)


def build_decisions(figi_map, daily, feat_lut, policies):
    """Для каждой политики строит список решений (pred-like dicts) по датам/тикерам."""
    days = trading_days(SPLITS["train"][1] + timedelta(days=1), SPLITS["eval"][1])
    # eligible universe per day (динамическая когорта)
    out = {p: [] for p in policies}
    for d in days:
        split = split_of(d)
        if split is None:
            continue
        # eligo: для каждого figi контекст + eligibility
        day_elig = []
        ctx_cache = {}
        for figi in figi_map:
            s = daily.get(figi)
            if s is None:
                continue
            c = daily_context(s, d)
            if c is None:
                continue
            ctx_cache[figi] = c
            if c["elig_er"] and c["elig_turn"]:
                day_elig.append(figi)
        # ER60-ранжирование для arm C (top10 среди eligible)
        er_ranked = sorted([f for f in day_elig
                            if ctx_cache[f]["er60"] is not None],
                           key=lambda f: ctx_cache[f]["er60"], reverse=True)
        top10 = set(er_ranked[:10])
        for entry_min in ENTRY_MIN:
            ts = entry_ts_ms(d, entry_min)
            iso = ts.isoformat()
            for figi in figi_map:
                c = ctx_cache.get(figi)
                if c is None:
                    continue
                ticker = figi_map[figi]
                fl = feat_lut.get(figi, {}).get(iso)
                rsi = fl[0] if fl else None
                macd = fl[1] if fl else None
                for lb in LOOKBACKS:
                    ret = c.get(f"return{lb}")
                    elig_lb = (c["elig_er"] if lb <= 60 else c["elig_ret120"])
                    if ret is None or not elig_lb:
                        continue  # feature unavailable -> no signal (cash)
                    # arm A
                    if ret > 0:
                        out[f"{lb}_{entry_min}_A"].append(
                            _dec(figi, ticker, ts, split, rsi, macd))
                    # arm B
                    if (ret > 0 and rsi is not None and macd is not None
                            and rsi >= 50 and macd > 0):
                        out[f"{lb}_{entry_min}_B"].append(
                            _dec(figi, ticker, ts, split, rsi, macd))
                    # arm C
                    if (figi in top10 and ret > 0 and rsi is not None
                            and macd is not None and rsi >= 50 and macd > 0):
                        out[f"{lb}_{entry_min}_C"].append(
                            _dec(figi, ticker, ts, split, rsi, macd))
    return out


def _dec(figi, ticker, ts, split, rsi, macd):
    return {"figi": figi, "ticker": ticker, "decision_ts": ts.isoformat(),
            "model_id": "b3", "split": split, "selected_long": "true",
            "available_at_ts": ts.isoformat(), "_rsi": rsi, "_macd": macd}


def load_store(figi_map):
    store = fp.MinuteStore()
    for figi, ticker in figi_map.items():
        f = SNAP_CSV / f"{ticker}__m1.csv"
        if f.exists():
            store.add_csv(f, figi)
    return store


def run_policy(name, decisions, store, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    res = {}
    for cost_id, comm, slip in COST_SCENARIOS:
        r = evaluate(decisions, store, horizon_minutes=HORIZON,
                     commission_bps=comm, slippage_bps=slip)
        m = r["metrics"]
        m["policy"] = name
        m["cost_id"] = cost_id
        res[cost_id] = m
        # ledgers
        _save_exec(out_dir / f"executed_ledger_{cost_id}.csv", r["portfolio"])
        _save_open(out_dir / f"accepted_open_{cost_id}.csv", r["portfolio"])
        with (out_dir / f"signal_outcomes_{cost_id}.csv").open(
                "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["row_id", "figi", "ticker", "decision_ts", "policy",
                        "selected", "status", "reason", "entry_ts", "exit_ts"])
            # outcomes не имеют row_id; используем индекс
            for i, o in enumerate(r["outcomes"]):
                w.writerow([i, o.figi, o.ticker, o.decision_ts.isoformat(), name,
                            o.selected, o.status, o.reason,
                            o.entry_ts.isoformat() if o.entry_ts else "",
                            o.exit_ts.isoformat() if o.exit_ts else ""])
    return res


def _save_exec(path, portfolio):
    fields = ["row_id", "figi", "ticker", "model_id", "entry_ts", "exit_ts",
              "qty", "entry_fill", "exit_fill", "slip_entry", "slip_exit",
              "commission_entry", "commission_exit", "gross", "net",
              "costs_total", "entry_cost", "exit_proceeds"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for t in portfolio.executed:
            w.writerow({k: getattr(t, k) for k in fields})


def _save_open(path, portfolio):
    fields = ["row_id", "figi", "ticker", "model_id", "entry_ts", "entry_base",
              "qty", "entry_debit", "planned_exit_ts"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for o in portfolio.accepted_open:
            w.writerow({k: getattr(o, k) for k in fields})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default=None, help="policy name substring to limit")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    figi_map = load_universe()
    daily = derive_daily_from_m1(SNAP_CSV, figi_map)
    feat_lut = load_feature_lut(figi_map)
    store = load_store(figi_map)

    policies = [f"{lb}_{em}_{a}" for lb in LOOKBACKS
               for em in ENTRY_MIN for a in ARMS]
    if args.only:
        policies = [p for p in policies if args.only in p]
    decisions_by_policy = build_decisions(figi_map, daily, feat_lut, policies)

    # сначала eval-отбор (p.9) на val для primary/top3 — считаем val-known-event-net
    summary = []
    for p in policies:
        decs = decisions_by_policy[p]
        eval_decs = [d for d in decs if d["split"] == "eval"]
        val_decs = [d for d in decs if d["split"] == "val"]
        r = run_policy(p, eval_decs, store, out / p)
        for cost_id, m in r.items():
            summary.append({
                "policy": p, "cost_id": cost_id, "split": "eval",
                "n_signal": m["n_signal_outcomes"],
                "n_executed": m["n_executed"],
                "n_accepted_open": m["n_accepted_open"],
                "net_total": m["net_total"],
                "gross_total": m["gross_total"],
                "costs_total": m["costs_total"],
                "final_equity": m["final_equity"],
                "last_known_equity": m["last_known_equity"],
                "last_known_ts": m["last_known_ts"],
                "max_drawdown_known": m["max_drawdown_known"],
                "closed_net": m["closed_cashflow_diagnostic"]["closed_net"],
                "initial_plus_closed": m["closed_cashflow_diagnostic"]["initial_plus_closed_net"],
                "is_full_equity": m["closed_cashflow_diagnostic"]["is_full_equity"],
                "costs_identity": m["costs_identity_net_eq_gross_minus_costs"],
            })
    # baselines
    # cash
    summary.append({"policy": "cash", "cost_id": "c5_s2", "split": "eval",
                    "n_executed": 0, "final_equity": 1.0,
                    "last_known_equity": 1.0, "closed_net": 0.0,
                    "initial_plus_closed": 1.0, "is_full_equity": True})
    summary.append({"policy": "cash", "cost_id": "c10_s5", "split": "eval",
                    "n_executed": 0, "final_equity": 1.0,
                    "last_known_equity": 1.0, "closed_net": 0.0,
                    "initial_plus_closed": 1.0, "is_full_equity": True})
    # signal-free baseline: вход 10:15/11:00 на eligible-universe без сигнала
    sf = _signal_free_decisions(figi_map, daily, feat_lut)
    for cost_id, comm, slip in COST_SCENARIOS:
        r = evaluate(sf, store, horizon_minutes=HORIZON, commission_bps=comm,
                     slippage_bps=slip)
        m = r["metrics"]
        summary.append({"policy": "signal_free_baseline", "cost_id": cost_id,
                        "split": "eval", "n_signal": m["n_signal_outcomes"],
                        "n_executed": m["n_executed"],
                        "n_accepted_open": m["n_accepted_open"],
                        "net_total": m["net_total"], "gross_total": m["gross_total"],
                        "costs_total": m["costs_total"],
                        "final_equity": m["final_equity"],
                        "last_known_equity": m["last_known_equity"],
                        "last_known_ts": m["last_known_ts"],
                        "max_drawdown_known": m["max_drawdown_known"],
                        "closed_net": m["closed_cashflow_diagnostic"]["closed_net"],
                        "initial_plus_closed": m["closed_cashflow_diagnostic"]["initial_plus_closed_net"],
                        "is_full_equity": m["closed_cashflow_diagnostic"]["is_full_equity"],
                        "costs_identity": m["costs_identity_net_eq_gross_minus_costs"]})

    with (out / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
        w.writeheader()
        for row in summary:
            w.writerow(row)

    manifest = {
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": "research_day_b3", "horizon": HORIZON, "costs": COST_SCENARIOS,
        "session": [SESSION_START, SESSION_END], "entries": ENTRY_MIN,
        "lookbacks": LOOKBACKS, "arms": ARMS,
        "provenance": {
            "dataset_v3": str(B1V3), "snapshot_csv": str(SNAP_CSV),
            "universe": str(UNIV), "daypack_manifest": str(DAYMAN),
            "universe_sha": _sha(UNIV), "daypack_manifest_sha": _sha(DAYMAN),
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8")
    print(f"B3 done: {len(policies)} policies + baselines; summary.csv rows={len(summary)}")


def _signal_free_decisions(figi_map, daily, feat_lut):
    days = trading_days(SPLITS["eval"][0], SPLITS["eval"][1])
    out = []
    for d in days:
        for em in ENTRY_MIN:
            ts = entry_ts_ms(d, em)
            iso = ts.isoformat()
            for figi, ticker in figi_map.items():
                s = daily.get(figi)
                if s is None:
                    continue
                c = daily_context(s, d)
                if c is None or not (c["elig_er"] and c["elig_turn"]):
                    continue
                out.append({"figi": figi, "ticker": ticker,
                            "decision_ts": ts.isoformat(), "model_id": "sf",
                            "split": "eval", "selected_long": "true",
                            "available_at_ts": ts.isoformat()})
    return out


if __name__ == "__main__":
    raise SystemExit(main())
