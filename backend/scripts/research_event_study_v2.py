#!/usr/bin/env python3
"""RESEARCH-CORE-V2 (B2): gross signal study отдельно от SL/TP и затрат.

Цель ветки — разделить четыре вопроса: (1) направление после сигнала,
(2) влияние выхода, (3) влияние costs, (4) переносимость по инструментам/периодам.

Что делает модуль
-----------------
1. Читает immutable 1m-экспорт (A10 `--layout a10` или snapshot B1
   `--layout snapshot`) и гоняет 15 НЕИЗМЕННЫХ генераторов BATCH-15
   (`batch15_group_a/b/c.evaluate`), сохраняет нормализованные события в
   `events.jsonl` c устойчивым `event_id = cid|ticker|day`.
2. Для каждого сигнала считает заранее объявленный исследовательский режим
   TIME_EXIT_ONLY: signed raw open-to-open return от точной минуты входа до
   точной минуты выхода. Горизонты 30/60/120 минут и 18:30 MSK. Никакого
   SL/TP/trailing. Выход за сессию запрещён; нет entry/exit -> UNKNOWN.
   Промежуточный пропуск не мешает endpoint return, но path metrics UNKNOWN.
3. Сохраняет gross, net при 5/2 и 10/5 bps на сторону и break-even total
   costs на единичный входной номинал. Формулы берутся из общего
   `research_execution` (settle_entry/settle_exit), не дублируются.
4. Контроли направления: «всегда long» и «всегда short» на тех же моментах.
5. Дневной режим (архитектор): признаки считаются на предыдущем ЗАВЕРШЁННОМ
   торговом дне, никогда на будущем/текущем. Фиксированные признаки
   returns20/60/120/250, SMA20/60/120/250, realized vol20/60, ER60. Один
   фиксированный режим: UP/DOWN/MIXED. Армсы A (без фильтра), B (long только
   UP / short только DOWN, MIXED -> no_trade, UNKNOWN -> unknown_filter),
   C (как B, но ежедневно top10 по ER60, ties по ticker, ranking только на
   prev close).
6. Сводка по candidate/horizon/ticker/month + side×regime×ticker + arms A/B/C,
   плюс paired-сравнение с готовым SL2ATR/TP3ATR BATCH-15 по event_id.

Все периоды — development/exploratory, НЕ нетронутый holdout. Стоимости — два
парных сценария, не независимые наблюдения. Сырые ценовые PnL разных акций не
суммируются в портфельную доходность.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import day_strategies_core as a7
import day_candidate_execution as a24
from app.engine import research_execution

TOOL = "research_event_study_v2"
UTC = timezone.utc

HORIZONS = (
    ("h30", 30),
    ("h60", 60),
    ("h120", 120),
    ("eod1830", None),   # close of the 18:20 bar -> open of 18:30 MSK
)
COST_SCENARIOS = (
    (5.0, 2.0, "c5_s2"),
    (10.0, 5.0, "c10_s5"),
)
DEFAULT_TICKERS = ("GAZP", "LKOH", "SFIN", "VKCO")
DEFAULT_EVAL_START = date(2026, 6, 11)
DEFAULT_EVAL_END = date(2026, 10, 8)

# -- daily-regime fixed windows (architect). 120/250 descriptive only.
RET_WINDOWS = (20, 60, 120, 250)
SMA_WINDOWS = (20, 60, 120, 250)
VOL_WINDOWS = (20, 60)
ER_WINDOW = 60

GROUP_MODULES = ("batch15_group_a", "batch15_group_b", "batch15_group_c")
CANDIDATE_IDS = tuple(f"B{i:02d}" for i in range(1, 16))


# ------------------------------------------------------------------ helpers

def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def _mean(xs):
    xs = [x for x in xs if _finite(x)]
    return statistics.mean(xs) if xs else None


def _median(xs):
    xs = [x for x in xs if _finite(x)]
    return statistics.median(xs) if xs else None


def _load_groups():
    """Импортирует три группы BATCH-15, регистрирует их spec hashes в A24."""
    mods = {}
    for name in GROUP_MODULES:
        mod = __import__(name)
        mods[name] = mod
    merged = {}
    for mod in mods.values():
        merged.update(mod.SPEC_HASHES)
    a24.register_specs(merged)
    by_cid = {}
    for mod in mods.values():
        for cid in mod.CANDIDATE_IDS:
            by_cid[cid] = mod
    if set(by_cid) != set(CANDIDATE_IDS):
        missing = sorted(set(CANDIDATE_IDS) - set(by_cid))
        raise RuntimeError(f"groups do not cover all candidates; missing {missing}")
    spec_hashes = {cid: by_cid[cid].SPEC_HASHES[cid] for cid in CANDIDATE_IDS}
    return by_cid, spec_hashes


def _input_path(input_root: Path, layout: str, ticker: str) -> Path:
    if layout == "snapshot":
        return input_root / f"{ticker}__m1.csv"
    return input_root / f"{ticker}.csv"


# ------------------------------------------------------------------ daily regime

def daily_series(tdata: a7.TickerData):
    """Список (day, close, full) по сессионным дням данных.

    Завершённым считается день с полным 10min-баром 18:30 (последний полный
    бар сессии; 18:40 в этом срезе неполный — сессия кончается 18:44). Дневной
    close = close бара 18:30. День без этого бара — partial и НЕ используется
    как вход признаков режима. Corporate-action/adjustment: UNVERIFIED.
    """
    out = []
    for d in sorted(tdata.minutes_by_day):
        bar = a7._full_bar(tdata, d, "18:30")
        if bar is not None:
            out.append((d, bar.close, True))
        elif tdata.minutes_by_day[d]:
            last = max(tdata.minutes_by_day[d], key=lambda b: b.ts)
            out.append((d, last.close, False))
    return out


def _daily_features_for(series):
    """series = [(day, close, full)] -> {day: features}. Использует только полные дни.

    Признак считается на данных ДО текущего дня (close_prev = предыдущий полный
    день). Недостаточная/невалидная история -> значение UNKNOWN (None + reason).
    """
    full = [(d, c) for d, c, ok in series if ok and _finite(c) and c > 0]
    closes = [c for _, c in full]
    days = [d for d, _ in full]
    out = {}
    for i, d in enumerate(days):
        feat = {
            "day": d.isoformat(),
            "close": closes[i],
            "ret20": None, "ret60": None, "ret120": None, "ret250": None,
            "sma20": None, "sma60": None, "sma120": None, "sma250": None,
            "vol20": None, "vol60": None, "er60": None,
        }
        for w in RET_WINDOWS:
            if i - w >= 0:
                feat[f"ret{w}"] = closes[i] / closes[i - w] - 1.0
        for w in SMA_WINDOWS:
            if i - w + 1 >= 0:
                feat[f"sma{w}"] = sum(closes[i - w + 1:i + 1]) / w
        dret = [closes[k] / closes[k - 1] - 1.0 for k in range(1, i + 1)]
        for w in VOL_WINDOWS:
            if len(dret) >= w:
                seg = dret[-w:]
                feat[f"vol{w}"] = statistics.pstdev(seg)
        if i - ER_WINDOW >= 0:
            num = abs(closes[i] - closes[i - ER_WINDOW])
            den = sum(abs(closes[k] - closes[k - 1])
                      for k in range(i - ER_WINDOW + 1, i + 1))
            feat["er60"] = 0.0 if den == 0.0 else num / den
        out[d] = feat
    return out, days


def _previous_day(days, feat_by_day, day):
    """Предыдущий завершённый полный день строго до `day`."""
    prev = None
    for d in days:
        if d < day:
            prev = d
        else:
            break
    return prev


def regime_for(days, feat_by_day, day):
    """(regime, detail) на основе признаков ПРЕДЫДУЩЕГО завершённого дня.

    UP  если close_prev > SMA60 и ret60 > 0
    DOWN если close_prev < SMA60 и ret60 < 0
    иначе MIXED; недостаточная история -> (None, reason) => UNKNOWN.
    """
    prev = _previous_day(days, feat_by_day, day)
    if prev is None:
        return None, "no_previous_completed_session"
    f = feat_by_day[prev]
    cp, sma60, r60 = f["close"], f["sma60"], f["ret60"]
    if not _finite(cp) or not _finite(sma60) or not _finite(r60):
        missing = [n for n, v in (("close", cp), ("sma60", sma60), ("ret60", r60))
                   if not _finite(v)]
        return None, f"insufficient_daily_history:{','.join(missing)}"
    if cp > sma60 and r60 > 0:
        return "UP", "close_prev>sma60 and ret60>0"
    if cp < sma60 and r60 < 0:
        return "DOWN", "close_prev<sma60 and ret60<0"
    return "MIXED", "close_prev/sma60 and ret60 not aligned"


# ------------------------------------------------------------------ outcomes

def _session_minute(ts: datetime) -> bool:
    return a7.is_session_minute(ts)


def horizon_outcome(tdata, day, entry_ts, side, exit_ts, costs_list):
    """TIME_EXIT_ONLY endpoint outcome. UNKNOWN при отсутствии entry/exit.

    Возвращает dict: status ok|unknown, reason, entry_base, exit_base, gross,
    net per cost id, break_even (gross/entry_base), costs per cost id,
    path_complete (нет ли пропущенной минуты между entry и exit).
    """
    em = tdata.minute_by_ts.get(entry_ts)
    if em is None:
        return {"status": "unknown", "reason": "missing_entry_minute"}
    if not _session_minute(exit_ts) or a7.msk_date(exit_ts) != day \
            or a7.msk_mod(exit_ts) > a7.SESSION_MAX_MOD:
        return {"status": "unknown", "reason": "exit_out_of_session"}
    xm = tdata.minute_by_ts.get(exit_ts)
    if xm is None:
        return {"status": "unknown", "reason": "missing_exit_minute"}

    entry_base = em.open
    exit_base = xm.open
    if not (_finite(entry_base) and entry_base > 0 and _finite(exit_base) and exit_base > 0):
        return {"status": "unknown", "reason": "nonfinite_price"}

    gross = side * (exit_base - entry_base)
    gross_frac = gross / entry_base
    path_complete = True
    gap_ts = None
    t = entry_ts + timedelta(minutes=1)
    while t < exit_ts:
        if t not in tdata.minute_by_ts:
            path_complete = False
            gap_ts = t
            break
        t += timedelta(minutes=1)

    nets = {}
    costs_frac = {}
    for commission_bps, slippage_bps, cost_id in costs_list:
        costs = a7.CostParams(commission_bps, slippage_bps)
        fills = research_execution.settle_entry(entry_base, side, costs)
        settled = research_execution.settle_exit(
            entry_base, exit_base, side, costs,
            entry_fill=fills["entry_fill"],
            commission_entry=fills["commission_entry"])
        nets[cost_id] = settled["net"] / entry_base
        costs_frac[cost_id] = settled["costs_total"] / entry_base
    return {
        "status": "ok", "reason": "",
        "entry_base": entry_base, "exit_base": exit_base,
        "gross": gross_frac,
        "net": nets,
        "costs": costs_frac,
        "break_even": gross_frac,
        "path_complete": path_complete,
        "path_gap_ts": gap_ts.isoformat() if gap_ts else "",
    }


# ------------------------------------------------------------------ per-ticker

def _parse_ts(raw: str) -> datetime:
    s = raw.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError("ts_naive")
    return dt.astimezone(UTC)


def evaluate_ticker(ticker, tdata, days, costs_list, spec_hashes, input_hash,
                    by_cid, feat_days, feat_by_day):
    events = []
    for day in days:
        regime, regime_detail = regime_for(feat_days, feat_by_day, day)
        prev = _previous_day(feat_days, feat_by_day, day)
        fprev = feat_by_day.get(prev, {}) if prev is not None else {}
        er60 = fprev.get("er60")
        for cid in CANDIDATE_IDS:
            mod = by_cid[cid]
            ev = mod.evaluate(cid, tdata, day)
            etype = ev.get("type")
            row = {
                "event_id": f"{cid}|{ticker}|{day.isoformat()}",
                "candidate_id": cid, "ticker": ticker, "day": day.isoformat(),
                "spec_hash": spec_hashes[cid], "input_hash": input_hash,
                "type": etype,
                "reason": ev.get("reason", "") if etype != "signal" else "",
                "side": ev.get("side", ""),
                "decision_label": ev.get("decision_label", ""),
                "entry_ts": ev.get("intended_entry_time", ""),
                "regime": regime if regime is not None else "UNKNOWN",
                "regime_detail": regime_detail,
                "er60": er60 if _finite(er60) else None,
                "horizons": {},
                "controls": {},
            }
            if etype == "signal":
                entry_ts = _parse_ts(ev["intended_entry_time"])
                side = a24.SIDE_INT[ev["side"]]
                for hname, minutes in HORIZONS:
                    if minutes is None:
                        exit_ts = a7.label_ts(day, "18:30")
                    else:
                        exit_ts = entry_ts + timedelta(minutes=minutes)
                    row["horizons"][hname] = horizon_outcome(
                        tdata, day, entry_ts, side, exit_ts, costs_list)
                    # direction controls on the same fixed endpoints
                    c = horizon_outcome(tdata, day, entry_ts, 1, exit_ts, costs_list)
                    if c["status"] == "ok":
                        raw = c["gross"]
                        row["controls"][hname] = {
                            "always_long": raw, "always_short": -raw}
                    else:
                        row["controls"][hname] = {
                            "always_long": None, "always_short": None,
                            "reason": c["reason"]}
            events.append(row)
    return events


# ------------------------------------------------------------------ arm assignment

def assign_arms(events, top_n=10):
    """Проставляет event['arm_a/b/c'] и event['arm_reason'].

    A — все сигналы. B — long только UP / short только DOWN; MIXED -> no_trade;
    UNKNOWN -> unknown_filter. C — как B, но ежедневно top_n по ER60 (desc,
    ties по ticker) на prev close; при <top_n берём доступные.
    """
    by_day = {}
    for e in events:
        if e["type"] != "signal":
            continue
        regime = e["regime"]
        if regime == "MIXED":
            e["arm_b"], e["arm_reason"] = False, "no_trade_mixed"
        elif regime == "UNKNOWN":
            e["arm_b"], e["arm_reason"] = False, "unknown_filter"
        elif (e["side"] == "long" and regime == "UP") or \
                (e["side"] == "short" and regime == "DOWN"):
            e["arm_b"], e["arm_reason"] = True, "regime_aligned"
        else:
            e["arm_b"], e["arm_reason"] = False, "regime_misaligned"
        e["arm_a"] = True
        e["arm_c"] = False
        e["arm_c_reason"] = ""
        by_day.setdefault(e["day"], []).append(e)

    for day, group in by_day.items():
        eligible = [e for e in group if e["arm_b"] and _finite(e.get("er60"))]
        eligible.sort(key=lambda e: (-e["er60"], e["ticker"]))
        for e in eligible[:top_n]:
            e["arm_c"], e["arm_c_reason"] = True, "top_er60"
        for e in eligible[top_n:]:
            e["arm_c_reason"] = "outside_top_er60"
    for e in events:
        if e["type"] != "signal":
            e["arm_a"] = e["arm_b"] = e["arm_c"] = False
            e["arm_reason"] = "not_signal"
            e["arm_c_reason"] = "not_signal"
    return events


# ------------------------------------------------------------------ aggregation

def _metrics(events, hname, arm=None):
    """Известные события с endpoint-исходом на горизонте hname."""
    rows = []
    unknown = 0
    for e in events:
        if e["type"] != "signal":
            continue
        if arm and not e.get(arm):
            continue
        h = e["horizons"].get(hname)
        if h is None or h["status"] != "ok":
            unknown += 1
            continue
        rows.append((e, h))
    gross = [h["gross"] for _, h in rows]
    net = {cid: [h["net"][cid] for _, h in rows] for _, _, cid in COST_SCENARIOS}
    costs = {cid: [h["costs"][cid] for _, h in rows] for _, _, cid in COST_SCENARIOS}
    wins = [g for g in net.get("c5_s2", []) if g > 0]
    losses = [g for g in net.get("c5_s2", []) if g < 0]
    days = {}
    for e, h in rows:
        days.setdefault(e["day"], []).append(h["net"]["c5_s2"])
    positive_days = sum(1 for v in days.values() if _mean(v) is not None and _mean(v) > 0)
    out = {
        "n_signals": sum(1 for e in events
                         if e["type"] == "signal" and (not arm or e.get(arm))),
        "n_known": len(rows),
        "n_unknown": unknown,
        "gross_mean": _mean(gross),
        "gross_median": _median(gross),
        "break_even_mean": _mean(gross),   # break-even cost == gross per unit notional
        "positive_days": positive_days,
        "known_days": len(days),
        "denominators": {"n_known": len(rows), "n_unknown": unknown},
    }
    for cid in net:
        out[f"net_mean_{cid}"] = _mean(net[cid])
        out[f"net_median_{cid}"] = _median(net[cid])
        out[f"cost_drag_mean_{cid}"] = _mean(costs[cid])
        out[f"gross_gt_cost_share_{cid}"] = (
            _mean([1.0 if g > c else 0.0 for g, c in zip(gross, costs[cid])])
            if rows else None)
    out["win_rate"] = (len(wins) / len(rows)) if rows else None
    out["avg_win"] = _mean(wins)
    out["avg_loss"] = _mean(losses)
    out["n_wins"] = len(wins)
    out["n_losses"] = len(losses)
    return out


def build_summary(events, spec_hashes, meta):
    summary = {"meta": meta, "candidate_horizon": [], "by_ticker": [],
               "by_month": [], "controls": [], "regime": [], "arms": [],
               "unknowns": []}
    for cid in CANDIDATE_IDS:
        cev = [e for e in events if e["candidate_id"] == cid]
        for hname, _ in HORIZONS:
            m = _metrics(cev, hname)
            m.update({"candidate_id": cid, "horizon": hname})
            # net of gross-vs-cost decision for cheap cost
            summary["candidate_horizon"].append(m)
    # per ticker (cheap cost reference) x horizon
    for cid in CANDIDATE_IDS:
        for ticker in sorted({e["ticker"] for e in events}):
            cev = [e for e in events if e["candidate_id"] == cid and e["ticker"] == ticker]
            for hname, _ in HORIZONS:
                m = _metrics(cev, hname)
                m.update({"candidate_id": cid, "ticker": ticker, "horizon": hname})
                summary["by_ticker"].append(m)
    # by month
    months = sorted({e["day"][:7] for e in events})
    for cid in CANDIDATE_IDS:
        for mo in months:
            cev = [e for e in events if e["candidate_id"] == cid and e["day"][:7] == mo]
            m = _metrics(cev, "h60")
            m.update({"candidate_id": cid, "month": mo})
            summary["by_month"].append(m)
    # controls: signal vs always long/short on matched known endpoints
    for cid in CANDIDATE_IDS:
        for hname, _ in HORIZONS:
            sig, lng, sht = [], [], []
            for e in events:
                if e["candidate_id"] != cid or e["type"] != "signal":
                    continue
                h = e["horizons"].get(hname)
                c = e["controls"].get(hname)
                if not h or h["status"] != "ok" or not c:
                    continue
                if c.get("always_long") is None:
                    continue
                sig.append(h["gross"])
                lng.append(c["always_long"])
                sht.append(c["always_short"])
            summary["controls"].append({
                "candidate_id": cid, "horizon": hname,
                "n": len(sig),
                "signal_gross_mean": _mean(sig),
                "always_long_gross_mean": _mean(lng),
                "always_short_gross_mean": _mean(sht),
                "signal_minus_long": (_mean(sig) - _mean(lng)) if sig and lng else None,
                "signal_minus_short": (_mean(sig) - _mean(sht)) if sig and sht else None,
            })
    # side x regime x ticker
    buckets = {}
    for e in events:
        if e["type"] != "signal":
            continue
        h = e["horizons"].get("h60")
        key = (e["candidate_id"], e["side"], e["regime"], e["ticker"])
        b = buckets.setdefault(key, {"gross": [], "unknown": 0})
        if h and h["status"] == "ok":
            b["gross"].append(h["gross"])
        else:
            b["unknown"] += 1
    for (cid, side, regime, ticker), b in sorted(buckets.items()):
        summary["regime"].append({
            "candidate_id": cid, "side": side, "regime": regime, "ticker": ticker,
            "n_known": len(b["gross"]), "n_unknown": b["unknown"],
            "gross_mean": _mean(b["gross"]),
        })
    # arms
    for arm in ("arm_a", "arm_b", "arm_c"):
        for hname, _ in HORIZONS:
            m = _metrics(events, hname, arm=arm)
            m.update({"arm": arm, "horizon": hname})
            summary["arms"].append(m)
    # explicit unknowns
    unk = {}
    for e in events:
        if e["type"] != "signal":
            continue
        for hname, _ in HORIZONS:
            h = e["horizons"].get(hname)
            if not h or h["status"] != "ok":
                r = (h or {}).get("reason", "missing")
                unk[r] = unk.get(r, 0) + 1
        if e["regime"] == "UNKNOWN":
            unk["regime_unknown"] = unk.get("regime_unknown", 0) + 1
    summary["unknowns"] = [{"reason": k, "count": v} for k, v in sorted(unk.items())]
    return summary


def load_batch15_trades(batch15_root: Path):
    """event_id -> {gross, net_c5_s2, net_c10_s5} из сохранённых BATCH-15 ledger."""
    out = {}
    for cell in sorted(batch15_root.rglob("trades.csv")):
        try:
            with cell.open() as fh:
                for row in csv.DictReader(fh):
                    if row.get("status") != "FILLED":
                        continue
                    eid = row.get("event_id", "")
                    if not eid:
                        continue
                    eb = float(row["entry_base"])
                    rec = out.setdefault(eid, {"gross": {}, "net": {}})
                    rec["gross"]["c5_s2"] = float(row["gross"]) / eb
                    rec["net"]["c5_s2"] = float(row["net"]) / eb
        except (OSError, ValueError):
            continue
    return out


def paired_vs_sltp(events, batch15_root: Path):
    """Paired-сравнение TIME_EXIT vs сохранённый SL2ATR/TP3ATR по event_id."""
    saved = load_batch15_trades(batch15_root)
    rows = []
    if not saved:
        return {"available": False, "reason": "no_batch15_trades_found"}
    for hname, _ in HORIZONS:
        time_g, sl_g, net_t, net_s = [], [], [], []
        n_intersection = 0
        n_time_only = 0
        for e in events:
            if e["type"] != "signal":
                continue
            b = saved.get(e["event_id"])
            h = e["horizons"].get(hname)
            if not b or not h or h["status"] != "ok":
                continue
            n_intersection += 1
            time_g.append(h["gross"])
            net_t.append(h["net"]["c5_s2"])
            if "c5_s2" in b["gross"]:
                sl_g.append(b["gross"]["c5_s2"])
                net_s.append(b["net"]["c5_s2"])
        for e in events:
            if e["type"] != "signal":
                continue
            h = e["horizons"].get(hname)
            if h and h["status"] == "ok" and e["event_id"] not in saved:
                n_time_only += 1
        rows.append({
            "horizon": hname,
            "n_intersection_known": n_intersection,
            "n_time_only_known": n_time_only,
            "time_exit_gross_mean": _mean(time_g),
            "sltp_gross_mean": _mean(sl_g),
            "time_exit_net_c5_s2_mean": _mean(net_t),
            "sltp_net_c5_s2_mean": _mean(net_s),
        })
    return {"available": True, "basis": "paired_intersection_known_only",
            "note": "SL2ATR/TP3ATR — сохранённый BATCH-15 ledger (c5_s2); "
                    "TIME_EXIT — endpoints h30/h60/h120/18:30.",
            "rows": rows}


# ------------------------------------------------------------------ CLI run

def _ensure_out(out: Path):
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"--out {out} уже существует и не пуст (immutable-каталог)")
    out.mkdir(parents=True, exist_ok=True)


def cmd_run(args):
    out = Path(args.out)
    _ensure_out(out)
    cp_dir = out / "checkpoints"
    cp_dir.mkdir(exist_ok=True)

    by_cid, spec_hashes = _load_groups()
    layout = args.layout
    input_root = Path(args.input_root)
    costs_list = COST_SCENARIOS

    eval_start = date.fromisoformat(args.eval_start)
    eval_end = date.fromisoformat(args.eval_end)
    warmup_start = date.fromisoformat(args.warmup_start) if args.warmup_start \
        else eval_start - timedelta(days=400)

    here = Path(__file__).resolve().parent
    code_sha = {p.name: sha256_file(p) for p in sorted(here.glob("batch15_*.py"))}
    code_sha["research_event_study_v2.py"] = sha256_file(Path(__file__))
    code_sha["day_strategies_core.py"] = sha256_file(here / "day_strategies_core.py")

    all_events = []
    per_ticker_meta = {}
    for ticker in args.tickers:
        ipath = _input_path(input_root, layout, ticker)
        if not ipath.exists():
            per_ticker_meta[ticker] = {"status": "missing_input", "path": str(ipath)}
            continue
        by_ticker, load_issues, csv_sha = a7.load_minute_csv(str(ipath))
        if ticker not in by_ticker:
            per_ticker_meta[ticker] = {"status": "ticker_absent", "path": str(ipath)}
            continue
        minutes = [b for b in by_ticker[ticker] if a7.is_session_minute(b.ts)]
        issues: list = list(load_issues)
        tdata = a7.build_ticker_data(ticker, minutes, warmup_start, issues)
        series = daily_series(tdata)
        feat_by_day, feat_days = _daily_features_for(series)
        days = a7.eval_days(tdata, eval_start, eval_end)
        events = evaluate_ticker(ticker, tdata, days, costs_list, spec_hashes,
                                 csv_sha, by_cid, feat_days, feat_by_day)
        cp = {"ticker": ticker, "input_sha256": csv_sha, "path": str(ipath),
              "days": len(days), "events": events,
              "dq_issues": len(issues)}
        (cp_dir / f"{ticker}.json").write_text(
            json.dumps(cp, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        all_events.extend(events)
        per_ticker_meta[ticker] = {
            "status": "ok", "path": str(ipath), "sha256": csv_sha,
            "days": len(days), "events": len(events), "dq_issues": len(issues)}

    assign_arms(all_events, top_n=args.top_n)

    meta = {
        "tool": TOOL, "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "layout": layout, "input_root": str(input_root),
        "eval_start": eval_start.isoformat(), "eval_end": eval_end.isoformat(),
        "warmup_start": warmup_start.isoformat(),
        "candidates": list(CANDIDATE_IDS), "spec_hashes": spec_hashes,
        "cost_scenarios": [{"commission_bps": c, "slippage_bps": s, "id": i}
                           for c, s, i in COST_SCENARIOS],
        "horizons": [h for h, _ in HORIZONS],
        "code_sha": code_sha,
        "inputs": per_ticker_meta,
        "phase": "development_exploratory", "holdout": "NOT_ASSIGNED",
        "mode": "TIME_EXIT_ONLY_no_sltp",
        "note": ("TIME_EXIT_ONLY — исследовательский режим без SL/TP; endpoint "
                 "open-to-open return. Сырые PnL разных акций не суммируются. "
                 "costs — парные сценарии. Не holdout."),
    }

    (out / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False, allow_nan=False)
                  for e in all_events) + "\n", encoding="utf-8")

    summary = build_summary(all_events, spec_hashes, meta)
    if args.batch15_root:
        summary["paired_vs_sltp"] = paired_vs_sltp(all_events, Path(args.batch15_root))
    (out / "event_study.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8")

    n_sig = sum(1 for e in all_events if e["type"] == "signal")
    print(f"{TOOL}: tickers={len(per_ticker_meta)} events={len(all_events)} "
          f"signals={n_sig} out={out}")
    return 0


def cmd_summary(args):
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cp_dir = Path(args.checkpoints)
    all_events = []
    for cp in sorted(cp_dir.glob("*.json")):
        data = json.loads(cp.read_text(encoding="utf-8"))
        all_events.extend(data["events"])
    assign_arms(all_events, top_n=args.top_n)
    meta = {"tool": TOOL, "mode": "summary_from_checkpoints",
            "checkpoints": str(cp_dir)}
    spec_hashes = {}
    for e in all_events:
        spec_hashes[e["candidate_id"]] = e["spec_hash"]
    summary = build_summary(all_events, spec_hashes, meta)
    if args.batch15_root:
        summary["paired_vs_sltp"] = paired_vs_sltp(all_events, Path(args.batch15_root))
    (out / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False, allow_nan=False)
                  for e in all_events) + "\n", encoding="utf-8")
    (out / "event_study.json").write_text(
        json.dumps(summary, ensure_ascii=False, allow_nan=False) + "\n",
        encoding="utf-8")
    print(f"{TOOL}: summary events={len(all_events)} out={out}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="RESEARCH-CORE-V2 event study (B2)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="посчитать event study и сохранить events.jsonl")
    r.add_argument("--input-root", required=True)
    r.add_argument("--layout", choices=("a10", "snapshot"), default="a10")
    r.add_argument("--out", required=True)
    r.add_argument("--tickers", nargs="*", default=list(DEFAULT_TICKERS))
    r.add_argument("--eval-start", default=DEFAULT_EVAL_START.isoformat())
    r.add_argument("--eval-end", default=DEFAULT_EVAL_END.isoformat())
    r.add_argument("--warmup-start", default=None)
    r.add_argument("--top-n", type=int, default=10)
    r.add_argument("--batch15-root", default=None)
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("summary", help="собрать сводку из checkpoints")
    s.add_argument("--checkpoints", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--top-n", type=int, default=10)
    s.add_argument("--batch15-root", default=None)
    s.set_defaults(func=cmd_summary)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
