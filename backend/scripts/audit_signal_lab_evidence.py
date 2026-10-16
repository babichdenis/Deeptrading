"""Read-only аудитор доказательств Signal Lab (задача A1, ARCH-20261007).

Режимы:
  --file-only              только локальные файлы отчётов/конфигов (никакой БД);
  --db-url DSN             чтение БД: REPEATABLE READ + READ ONLY,
                           statement_timeout=30s, запросы только по двум run_id.
                           Альтернатива — env SIGNAL_LAB_AUDIT_DSN.
  --check-snapshot DIR     пересчёт метрик из сохранённого снимка без БД
                           (проверка повторяемости, см. snapshot_cells.json).

Скрипт НЕ запускает генераторы fixed/report, ничего не пишет в БД, не печатает
DSN/токены/account_id. Артефакты — в
backend/reports/architect_20261007/b1/<unique_run_id>/, существующие каталоги
не перезаписываются.

Повторный прогон по тому же снимку даёт идентичные вычисляемые поля;
отличаются только generated_at и id выходного каталога. Версия аудитора,
хэш снимка ячеек и хэш свечей фиксируются в manifest.json.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

DEFAULT_PROFILE = "sl1_tp2"
LOW_PRICE_FLOOR = 5.0  # «подозрительно дешёвая» сделка/свеча для дорогих тикеров
BAD_BAR_CAP = 10.0     # open < 10₽ в 1m-свечи считается аномальным кандидатом
A_RUN = "a336abe5-c7ce-4f6b-affb-42366774a419"
B_RUN = "bc811f38-e9ea-40ae-b703-1d1076cbdb3b"
DEFAULT_CELLS = [
    {"run_id": A_RUN, "engine": "momentum_1bar", "tf": "5min", "side": "SELL",
     "ticker": "VKCO", "regime": "RANGE", "session": "morning"},
    {"run_id": A_RUN, "engine": "momentum_1bar", "tf": "5min", "side": "BUY",
     "ticker": "SFIN", "regime": "NEUTRAL", "session": "day"},
    {"run_id": B_RUN, "engine": "williams_range_hub", "tf": "1min", "side": "BUY",
     "ticker": "SBER", "regime": "NEUTRAL", "session": "day"},
]
STATUS_FILES_ONLY = "NOT_VERIFIED (файловый режим, БД не запрашивалась)"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_json(value) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_entry(path: Path) -> dict:
    st = path.stat()
    return {
        "path": str(path),
        "size": st.st_size,
        "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
        "sha256": sha256_file(path),
    }


def load_cells_csv(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_summary(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    profile = None
    if "профиль " in text:
        profile = text.split("профиль ", 1)[1].split("\n", 1)[0].strip()
    cells = excluded = None
    for line in text.splitlines():
        if line.startswith("Ячеек (без weekend/night):"):
            body = line.split(":", 1)[1].replace("исключено:", " ").split()
            cells = int(body[0].rstrip(";"))
            excluded = int(body[1].rstrip(";."))
    return {"profile": profile, "cells": cells, "excluded": excluded}


def locate_cell(rows: list[dict], cell: dict) -> dict | None:
    for r in rows:
        if (r["engine"] == cell["engine"] and r["tf"] == cell["tf"]
                and r["side"] == cell["side"] and r["ticker"] == cell["ticker"]
                and r["regime"] == cell["regime"] and r["session"] == cell["session"]):
            return r
    return None


def is_win(net) -> bool:
    return net is not None and net > 0


def cell_stats(rows: list[dict]) -> dict:
    """Независимый пересчёт метрик ячейки из сырых строк fixed-exit.

    n / wins / losses / nulls считаются по net_return; доп. конечность цен
    (entry_px/exit_px) и доходности — отдельными счётчиками non_finite_*.
    """
    nets = [r.get("net_return") for r in rows]
    nulls = sum(1 for v in nets if v is None)
    non_finite_net = sum(1 for v in nets if v is not None and not math.isfinite(float(v)))
    non_finite_px = 0
    for r in rows:
        for k in ("entry_px", "exit_px"):
            v = r.get(k)
            if v is not None and not math.isfinite(float(v)):
                non_finite_px += 1
    wins = [r for r in rows if is_win(r.get("net_return"))]
    losses = [r for r in rows if r.get("net_return") is not None and r["net_return"] <= 0]
    valid = [r for r in rows if r.get("net_return") is not None]
    sw = sum(r["net_return"] for r in wins)
    sl = sum(r["net_return"] for r in losses)
    rw = sum(r["net_return"] * r["entry_px"] for r in wins)
    rl = sum(r["net_return"] * r["entry_px"] for r in losses)
    rs = [r["r_multiple"] for r in rows if r.get("r_multiple") is not None]
    return {
        "n": len(rows), "wins": len(wins), "losses": len(losses), "nulls": nulls,
        "non_finite_net": non_finite_net, "non_finite_prices": non_finite_px,
        "sum_win_norm": sw, "sum_loss_norm": sl,
        "pf_norm": (sw / abs(sl)) if sl else None,
        "pf_rub": (rw / abs(rl)) if rl else None,
        "mean_net_pct": (sum(r["net_return"] for r in valid) / len(valid) * 100) if valid else None,
        "sum_win_rub": rw, "sum_loss_rub": rl,
        "avg_win_rub": (rw / len(wins)) if wins else None,
        "avg_loss_rub": (rl / len(losses)) if losses else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "sum_net_norm": sw + sl,
    }


def published_columns(stats: dict) -> dict:
    """Правила округления app/lab/report.py — объяснение точности публикации."""
    n, wins = stats["n"], stats["wins"]
    pf = stats["pf_norm"]
    return {
        "n": n, "wins": wins, "losses": stats["losses"],
        "winrate_pct": round(wins / n * 100, 1) if n else 0,
        "pf": round(pf, 2) if pf is not None else None,
        "avg_net_pct": round((stats["mean_net_pct"] or 0.0), 4),
        "avg_win_rub": round(stats["avg_win_rub"], 2) if stats["avg_win_rub"] is not None else None,
        "avg_loss_rub": round(stats["avg_loss_rub"], 2) if stats["avg_loss_rub"] is not None else None,
        "avg_r": round(stats["avg_r"], 3) if stats["avg_r"] is not None else None,
    }


def compare_to_published(published: dict, recomputed: dict) -> dict:
    deltas = {}
    for key, val in recomputed.items():
        got = published.get(key)
        if got in (None, ""):
            got_norm = None
        else:
            got_norm = float(got)
        match = ((got_norm is None and val is None)
                 or (got_norm is not None and val is not None
                     and abs(got_norm - float(val)) < 1e-9))
        deltas[key] = {"published": got, "recomputed": val, "match": match}
    return deltas


def as_dt(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def ts_key(value):
    """Нормализация метки времени в UTC ISO-second для сравнения.

    lab_fixed_exits отдаёт entry_ts текстом, candles.ts — datetime; формат
    (T vs пробел) и оффсет должны приводиться к одной шкале. Naive → UTC.
    """
    d = as_dt(value)
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC).isoformat(timespec="seconds")


def overlap_stats(rows: list[dict]) -> dict:
    """ПРИБЛИЖЁННАЯ интервальная диагностика (не портфельная симуляция).

    Одна позиция на FIGI предполагается. Интервалы [entry_ts, exit_ts]:
      - нормализуются в UTC (ts_key) — сравнение корректно при разных оффсетах;
      - равенство entry_ts == exit_ts допускается как нулевой интервал и НЕ
        считается перекрытием;
      - exit_ts — это метка бара закрытия/выхода в данных, НЕ момент
        внутрисвечного исполнения; реальный тайминг отсечки выхода неизвестен.
    Результат — диагностика множеств/порядка, НЕ исполнимый портфель и НЕ
    окончательный вердикт по стратегии.
    """
    iv = sorted((ts_key(r.get("entry_ts")), ts_key(r.get("exit_ts")),
                 r.get("event_id"))
                for r in rows if r.get("entry_ts") and r.get("exit_ts"))
    overlapping = 0
    cur_end = None
    for en, ex, _eid in iv:
        if cur_end is not None and en < cur_end:
            overlapping += 1
        if cur_end is None or ex > cur_end:
            cur_end = ex
    ordered = sorted((ts_key(r.get("entry_ts")), r.get("event_id"), i, r)
                     for i, r in enumerate(rows)
                     if r.get("entry_ts") and r.get("exit_ts"))
    greedy, cur_end = [], None
    for _en, _eid, _i, r in ordered:
        en = ts_key(r.get("entry_ts"))
        ex = ts_key(r.get("exit_ts"))
        if cur_end is None or en >= cur_end:
            greedy.append(r)
            cur_end = ex
    return {
        "method": "approximate_interval_diagnostic",
        "note": ("интервалы по сохранённым entry_ts/exit_ts, НЕ момент "
                 "внутрисвечного исполнения; не портфельная симуляция"),
        "equality_rule": "entry_ts == exit_ts — нулевой интервал, не перекрытие",
        "utc_normalized": True,
        "events": len(rows), "overlapping_entries": overlapping,
        "greedy_count": len(greedy),
        "greedy_stats": cell_stats(greedy) if greedy else None,
    }


def period_half_boundary(start: str, end: str) -> str | None:
    """Первая/вторая половина по календарной середине подтверждённого периода run."""
    try:
        d0 = datetime.fromisoformat(str(start)[:10]).date()
        d1 = datetime.fromisoformat(str(end)[:10]).date()
    except (TypeError, ValueError):
        return None
    return (d0 + (d1 - d0) // 2).isoformat()


def period_stats(rows: list[dict], h1_last_day: str | None = None) -> dict:
    days = sorted({str(r["start_ts"])[:10] for r in rows if r.get("start_ts")})
    months = Counter(str(r["start_ts"])[:7] for r in rows if r.get("start_ts"))
    if h1_last_day is None and days:
        h1_last_day = period_half_boundary(days[0], days[-1])
    halves = {}
    if h1_last_day:
        halves = Counter("H1" if str(r["start_ts"])[:10] <= h1_last_day else "H2"
                         for r in rows if r.get("start_ts"))
    out = {"trading_days": len(days), "first_day": days[0] if days else None,
           "last_day": days[-1] if days else None, "months": dict(sorted(months.items())),
           "halves": dict(sorted(halves.items())), "h1_boundary": h1_last_day,
           "single_month": len(months) == 1}
    if h1_last_day:
        for name, pred in (("H1", lambda d: d <= h1_last_day),
                           ("H2", lambda d: d > h1_last_day)):
            sub = [r for r in rows if r.get("start_ts")
                   and pred(str(r["start_ts"])[:10])]
            out[f"{name}_stats"] = cell_stats(sub) if sub else None
    return out


def fill_price(base: float, is_buy: bool, slippage_bps: float, tick: float = 0.01) -> float:
    slip = base * slippage_bps / 10_000
    adjusted = slip if is_buy else -slip
    return round(round((base + adjusted) / tick) * tick, 10)


def recalc_net(entry_px: float, exit_px: float, side: str,
               commission_rate: float, slippage_bps: float) -> float:
    """Независимая пересчётная формула net (семантика app/lab/fixed.py)."""
    is_long = str(side).upper() in ("BUY", "LONG")
    ef = fill_price(entry_px, is_long, slippage_bps)
    xf = fill_price(exit_px, not is_long, slippage_bps)
    gross = (xf - ef) / ef * (1.0 if is_long else -1.0)
    return gross - commission_rate - commission_rate * (xf / ef)


def normalisation_check(rows: list[dict], side: str, costs: dict) -> dict:
    deltas = []
    for r in rows:
        if r.get("net_return") is None or r.get("entry_px") is None or r.get("exit_px") is None:
            continue
        try:
            px = (float(r["entry_px"]), float(r["exit_px"]), float(r["net_return"]))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in px):
            continue
        net = recalc_net(r["entry_px"], r["exit_px"], side,
                         float(costs.get("commission_rate", 0.0)),
                         float(costs.get("slippage_bps", 0.0)))
        deltas.append(abs(net - r["net_return"]))
    return {"checked": len(deltas), "max_abs_delta": max(deltas) if deltas else None,
            "ok": bool(deltas) and max(deltas) < 1e-6}


def scan_bad_bars(candles: list[dict], ref_price: float, factor: float = 0.5) -> list[dict]:
    return [c for c in candles
            if c.get("open") is not None and ref_price
            and (float(c["open"]) < ref_price * factor
                 or float(c["open"]) > ref_price / factor)]


def config_hash_from_payload(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cell_filters(cell: dict, all_rows: list[dict]) -> list[dict]:
    return [r for r in all_rows
            if r.get("regime_label") == cell["regime"]
            and r.get("regime_session") == cell["session"]]


def sensitivity(rows: list[dict], floor: float = LOW_PRICE_FLOOR) -> dict:
    """Чувствительность к аномальным ценам: два разных исключения.

    drop_affected   — убраны ВСЕ сделки с entry_px<floor ИЛИ exit_px<floor;
    drop_max_win    — убрана ОДНА сделка с максимальным net_return (по event_id
                      как стабильный tie-break).
    Это разные меры: они отвечают на разные вопросы и не взаимозаменяемы.
    """
    max_win = None
    for r in rows:
        if r.get("net_return") is None:
            continue
        if max_win is None or r["net_return"] > max_win["net_return"] or (
                r["net_return"] == max_win["net_return"]
                and (r.get("event_id") or 0) < (max_win.get("event_id") or 0)):
            max_win = r
    drop_max = [r for r in rows if r is not max_win] if max_win is not None else list(rows)
    affected = [r for r in rows
                if (r.get("entry_px") is not None and float(r["entry_px"]) < floor)
                or (r.get("exit_px") is not None and float(r["exit_px"]) < floor)]
    affected_ids = {(r.get("event_id")) for r in affected}
    drop_affected = [r for r in rows if r.get("event_id") not in affected_ids]
    return {
        "price_floor": floor,
        "dropped_affected": {
            "n": len(affected),
            "event_ids": sorted({str(i) for i in affected_ids if i is not None}),
            "stats": cell_stats(drop_affected) if drop_affected else None,
        },
        "dropped_max_win": {
            "n": 1 if max_win is not None else 0,
            "event_id": max_win.get("event_id") if max_win is not None else None,
            "net_return": max_win.get("net_return") if max_win is not None else None,
            "stats": cell_stats(drop_max) if drop_max else None,
        },
    }


def regime_availability_note(persisted_at) -> str:
    """Статус доступности regime-признака на момент исторического решения.

    created_at в lab_regime_observations — момент записи офлайн-расчёта
    (persisted_at), НЕ доказательство (не)доступности признака в момент
    сигнала: он не учитывает ни входы RegimeProvider в рантайме, ни то,
    что решение принималось по ЗАКРЫТОМУ TF-бару. Без этого анализа —
    строго NOT_VERIFIED, ни true, ни false.
    """
    return ("NOT_VERIFIED: подтверждена только метка записи (persisted_at); "
            "доступность признака на момент решения (входы RegimeProvider, "
            "видимость закрытых TF-баров) не анализировалась")


def analyze_cell(in_cell: list[dict], cell: dict, costs: dict,
                 h1_boundary: str | None, published: dict | None) -> dict:
    """Все метрики ячейки из уже отфильтрованных строк; без БД."""
    stats = cell_stats(in_cell)
    pub_cols = published_columns(stats) if in_cell else {}
    deltas = compare_to_published(published, pub_cols) if published else None
    ov = overlap_stats(in_cell)
    per = period_stats(in_cell, h1_boundary)
    persisted = sorted({r.get("regime_created_at") for r in in_cell
                        if r.get("regime_created_at")})
    out = {
        "cell": cell, "published_row": published,
        "recomputed": stats, "published_recomputed": pub_cols,
        "deltas": deltas,
        "exact_match": bool(deltas) and all(d["match"] for d in deltas.values()),
        "normalisation": normalisation_check(in_cell, cell["side"], costs),
        "overlap": ov, "period": per,
        "sensitivity": sensitivity(in_cell),
        "versions": {
            "fixed_cfg_hashes": sorted({r.get("cfg_hash") for r in in_cell
                                        if r.get("cfg_hash")}),
            "regime_versions": sorted({(r.get("regime_version"),
                                        r.get("regime_params_hash"))
                                       for r in in_cell if r.get("regime_version")}),
            "regime_persisted_at": persisted,
            "regime_causal_availability": regime_availability_note(persisted),
        },
        "null_outcomes": stats["nulls"],
        "finite_ok": stats["non_finite_net"] == 0 and stats["non_finite_prices"] == 0,
    }
    return out


def audit_cell_file_part(cell: dict, published: dict | None, summary: dict) -> dict:
    return {"cell": cell, "published_row": published,
            "summary_profile": summary.get("profile"),
            "recomputed": STATUS_FILES_ONLY, "cardinality": STATUS_FILES_ONLY,
            "normalisation": STATUS_FILES_ONLY, "overlap": STATUS_FILES_ONLY,
            "exact_match": None}


CELL_ROWS_SQL = """
SELECT e.id AS event_id, e.start_ts, e.end_ts, e.duration_bars, e.start_signal_id,
       e.figi, e.tf_seconds, e.start_close_ts,
       f.profile_code, f.cfg_hash, f.entry_ts, f.entry_px, f.exit_ts, f.exit_px,
       f.exit_reason, f.gross_return, f.net_return, f.r_multiple, f.atr_entry,
       f.bars_held_1m, f.bars_held_tf,
       r.version AS regime_version, r.params_hash AS regime_params_hash,
       r.created_at AS regime_created_at,
       r.measurements->>'label' AS regime_label, r.session AS regime_session
FROM lab_signal_event_runs e
JOIN lab_fixed_exits f
  ON f.signal_id = e.start_signal_id AND f.profile_code = $1
LEFT JOIN lab_regime_observations r
  ON r.figi = e.figi AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
WHERE e.run_id = $2 AND e.strategy_id = $3 AND e.tf = $4
  AND e.side = $5 AND e.ticker = $6
"""

CARD_SQL = """
SELECT
  (SELECT count(*) FROM lab_signal_event_runs e
    WHERE e.run_id = $1 AND e.strategy_id = $2 AND e.tf = $3
      AND e.side = $4 AND e.ticker = $5) AS events,
  (SELECT count(*) FROM lab_signal_event_runs e
    WHERE e.run_id = $1 AND e.strategy_id = $2 AND e.tf = $3
      AND e.side = $4 AND e.ticker = $5
      AND (SELECT count(*) FROM lab_fixed_exits f
            WHERE f.signal_id = e.start_signal_id AND f.profile_code = $6) <> 1
  ) AS bad_fixed,
  (SELECT count(*) FROM lab_signal_event_runs e
    WHERE e.run_id = $1 AND e.strategy_id = $2 AND e.tf = $3
      AND e.side = $4 AND e.ticker = $5
      AND (SELECT count(*) FROM lab_regime_observations r
            WHERE r.figi = e.figi AND r.tf_seconds = e.tf_seconds
              AND r.obs_ts = e.start_ts) <> 1
  ) AS bad_regime,
  (SELECT count(DISTINCT f.cfg_hash) FROM lab_signal_event_runs e
    JOIN lab_fixed_exits f ON f.signal_id = e.start_signal_id
    WHERE e.run_id = $1 AND e.strategy_id = $2 AND e.tf = $3
      AND e.side = $4 AND e.ticker = $5 AND f.profile_code = $6
  ) AS fixed_cfg_hashes,
  (SELECT count(DISTINCT r.version || ':' || r.params_hash)
     FROM lab_signal_event_runs e
     JOIN lab_regime_observations r ON r.figi = e.figi
       AND r.tf_seconds = e.tf_seconds AND r.obs_ts = e.start_ts
    WHERE e.run_id = $1 AND e.strategy_id = $2 AND e.tf = $3
      AND e.side = $4 AND e.ticker = $5
  ) AS regime_versions
"""

RUN_META_SQL = """
SELECT id, run_key, name, stage, status, dataset_key, dataset_version,
       period_from, period_to, warmup_from, source_tf, code_version,
       engine_version, cost_model_version, regime_version, config_hash,
       universe, generated_tfs, engines, exit_profiles, horizons,
       timeout_bars, costs, session_policy, stats, error,
       created_at, updated_at, finished_at
FROM lab_experiment_runs WHERE id = ANY($1::text[])
"""

CENSUS_LOW_OPEN_SQL = """
SELECT figi, count(*)::int AS n
FROM candles
WHERE interval = 1 AND open < $1
  AND ts >= $2::timestamptz AND ts < $3::timestamptz
GROUP BY figi ORDER BY n DESC, figi
"""


async def db_run_meta(conn, run_ids: list[str]) -> dict:
    rows = await conn.fetch(RUN_META_SQL, run_ids)
    out = {}
    for r in rows:
        d = dict(r)
        for k in ("universe", "generated_tfs", "engines", "exit_profiles", "horizons",
                  "costs", "session_policy", "stats"):
            if isinstance(d.get(k), str):
                with contextlib.suppress(TypeError, ValueError):
                    d[k] = json.loads(d[k])
        d["unknown_fields"] = [k for k in ("dataset_version",) if not d.get(k)]
        out[d["id"]] = d
    return out


async def db_cell_rows(conn, cell: dict, profile: str) -> list[dict]:
    rows = await conn.fetch(CELL_ROWS_SQL, profile, cell["run_id"], cell["engine"],
                            cell["tf"], cell["side"], cell["ticker"])
    return [dict(r) for r in rows]


async def db_cardinality(conn, cell: dict, profile: str) -> dict:
    r = dict(await conn.fetchrow(CARD_SQL, cell["run_id"], cell["engine"], cell["tf"],
                                 cell["side"], cell["ticker"], profile))
    return {"events": r["events"],
            "scope_note": ("events считаются по engine/tf/side/ticker ДО фильтра "
                           "regime/session; размер ячейки (после regime) — 'cell_n'"),
            "signals_with_fixed_rows_ne_1": r["bad_fixed"],
            "signals_with_regime_rows_ne_1": r["bad_regime"],
            "distinct_fixed_cfg_hashes": r["fixed_cfg_hashes"],
            "distinct_regime_versions": r["regime_versions"],
            "ok": r["bad_fixed"] == 0 and r["bad_regime"] == 0
            and r["fixed_cfg_hashes"] <= 1 and r["regime_versions"] <= 1}


async def db_candles(conn, figi: str, ts_from: str, ts_to: str) -> list[dict]:
    rows = await conn.fetch("""
        SELECT ts, open, high, low, close, volume FROM candles
        WHERE figi = $1 AND interval = 1
          AND ts >= $2::timestamptz AND ts <= $3::timestamptz ORDER BY ts""",
        figi, as_dt(ts_from), as_dt(ts_to))
    return [dict(r) for r in rows]


async def db_contributors(conn, cell: dict, profile: str, limit: int = 15) -> list[dict]:
    rows = await conn.fetch(CELL_ROWS_SQL + """
        AND COALESCE(r.measurements->>'label','(no_regime)') = $7
        AND COALESCE(r.session,'(no_session)') = $8
        ORDER BY ABS(f.net_return) DESC LIMIT $9""",
        profile, cell["run_id"], cell["engine"], cell["tf"], cell["side"],
        cell["ticker"], cell["regime"], cell["session"], limit)
    return [dict(r) for r in rows]


async def db_bad_price_exits(conn, run_id: str, profile: str,
                             price_floor: float) -> list[dict]:
    rows = await conn.fetch("""
        SELECT e.strategy_id, e.tf, e.side, e.ticker,
               count(*) AS n,
               count(*) FILTER (WHERE f.entry_px < $3) AS bad_entry,
               count(*) FILTER (WHERE f.exit_px < $3) AS bad_exit
        FROM lab_signal_event_runs e
        JOIN lab_fixed_exits f ON f.signal_id = e.start_signal_id
        WHERE e.run_id = $1 AND f.profile_code = $2
          AND (f.entry_px < $3 OR f.exit_px < $3)
        GROUP BY 1,2,3,4 ORDER BY n DESC, 1""",
        run_id, profile, price_floor)
    return [dict(r) for r in rows]


async def db_census_low_open(conn, ts_from: str, ts_to: str,
                             price_cap: float = BAD_BAR_CAP) -> list[dict]:
    """Перепись НЕлокальная: бары open<price_cap за окно ПО ВСЕМ figi БД.

    Сохраняется в артефакты, чтобы утверждения о «единственности» опирались
    на сохранённый запрос, а не на ручные выборки по одному FIGI.
    """
    if not ts_from or not ts_to:
        return []
    rows = await conn.fetch(CENSUS_LOW_OPEN_SQL, price_cap, as_dt(ts_from), as_dt(ts_to))
    return [{"window": (ts_from, ts_to), "price_cap": price_cap, **dict(r)} for r in rows]


async def sber_trace(conn, cell: dict, profile: str, meta: dict) -> dict:
    contribs = await db_contributors(conn, cell, profile, limit=15)
    figi = contribs[0]["figi"] if contribs else None
    candles = []
    if figi and contribs:
        ts_lo = min(c["entry_ts"] for c in contribs)
        ts_hi = max(c["exit_ts"] for c in contribs)
        candles = await db_candles(conn, figi, ts_lo, ts_hi)
    candle_by_ts = {ts_key(c["ts"]): c for c in candles}
    rows = []
    needed = []
    for c in contribs:
        entry_c = candle_by_ts.get(ts_key(c["entry_ts"]))
        exit_c = candle_by_ts.get(ts_key(c["exit_ts"]))
        for ck in (entry_c, exit_c):
            if ck is not None and ck not in needed:
                needed.append(ck)
        rows.append({
            "start_ts": c["start_ts"], "entry_ts": c["entry_ts"], "entry_px": c["entry_px"],
            "entry_candle_open": float(entry_c["open"]) if entry_c else None,
            "entry_candle_volume": entry_c["volume"] if entry_c else None,
            "exit_ts": c["exit_ts"], "exit_px": c["exit_px"], "exit_reason": c["exit_reason"],
            "exit_candle_open": float(exit_c["open"]) if exit_c else None,
            "exit_candle_volume": exit_c["volume"] if exit_c else None,
            "net_return": c["net_return"], "r_multiple": c["r_multiple"],
            "atr_entry": c["atr_entry"],
            "entry_matches_candle": bool(entry_c)
            and abs(float(entry_c["open"]) - float(c["entry_px"])) < 1e-9,
            "exit_matches_candle": bool(exit_c)
            and abs(float(exit_c["open"]) - float(c["exit_px"])) < 1e-9,
        })
    run_period = meta.get(cell["run_id"], {})
    bad_bars = await db_bad_bars(conn, [figi] if figi else [],
                                 run_period.get("period_from", ""),
                                 run_period.get("period_to", ""), BAD_BAR_CAP)
    blast = await db_bad_price_exits(conn, cell["run_id"], profile, LOW_PRICE_FLOOR)
    window = []
    if figi:
        window = await db_candles(conn, figi, "2026-09-08T12:40:00+00:00",
                                  "2026-09-08T12:56:00+00:00")
    return {"contributors": rows, "matched_candles": [dict(c) for c in needed],
            "anomaly_window_candles": [dict(c) for c in window],
            "bad_bars": [dict(b) for b in bad_bars],
            "blast_radius": blast, "contributor_count": len(rows)}


async def db_bad_bars(conn, figis: list[str], ts_from: str, ts_to: str,
                      price_cap: float) -> list[dict]:
    if not figis or not ts_from or not ts_to:
        return []
    rows = await conn.fetch("""
        SELECT figi, ts, open, high, low, close, volume FROM candles
        WHERE interval = 1 AND open < $1
          AND ts >= $2::timestamptz AND ts < $3::timestamptz
          AND figi = ANY($4::text[]) ORDER BY ts""",
        price_cap, as_dt(ts_from), as_dt(ts_to), figis)
    return [dict(r) for r in rows]


def config_hash_checks(meta: dict, backend: Path) -> dict:
    from app.lab.config import canonical_payload, load_config
    out = {}
    cfg_paths = {A_RUN: "configs/signal_lab/validation_2026-08.json",
                 B_RUN: "configs/signal_lab/discovery_2026-09.json"}
    repo_root = backend.parent
    for run_id, m in meta.items():
        rel = cfg_paths.get(run_id)
        cv = m.get("code_version") or ""
        entry = {"code_version": cv, "stored_config_hash": m.get("config_hash"),
                 "config_file": rel,
                 "config_payload_matches_stored": False,
                 "reproduced_from": None,
                 "claim": None,
                 "executed_code_provenance": "NOT_VERIFIED",
                 "data_snapshot_provenance": "NOT_VERIFIED",
                 "commit_exists": None}
        if rel and (backend / rel).exists():
            cfg = load_config(backend / rel)
            payload = canonical_payload(cfg, cv)
            h_file = config_hash_from_payload(payload)
            payload["engines"] = m.get("engines") or payload["engines"]
            h_db_engines = config_hash_from_payload(payload)
            entry["file_hash"] = h_file
            entry["file_hash_with_db_engines"] = h_db_engines
            entry["db_engines_match_file"] = (m.get("engines") or []) == list(cfg.engines)
            if h_file == m.get("config_hash"):
                entry["config_payload_matches_stored"] = True
                entry["reproduced_from"] = "config_file+claimed_code_version"
                entry["claim"] = ("config_hash воспроизводится из config-файла и заявленного "
                                  "code_version; это согласованность payload, НЕ доказательство "
                                  "того, какие байты кода исполнялись и какой снимок данных был")
            elif h_db_engines == m.get("config_hash"):
                entry["config_payload_matches_stored"] = True
                entry["reproduced_from"] = "engines из metadata run в БД"
                entry["claim"] = ("config_hash воспроизводится ТОЛЬКО при подстановке engines из "
                                  "metadata run (раскрытие '*' в файле даёт другой состав); "
                                  "указанное поведение не детерминировано перезапуском")
            else:
                entry["claim"] = "config_hash НЕ воспроизводится доступными источниками"
        if cv:
            entry["commit_exists"] = git_commit_exists(repo_root, cv)
        entry["executed_code_provenance"] = (
            "NOT_VERIFIED: подтверждён только заявленный code_version "
            f"{'(коммит существует в репо)' if entry['commit_exists'] else '(коммит не проверен)'}; "
            "какие байты фактически исполнялись и какой снимок данных — не подтверждено")
        entry["data_snapshot_provenance"] = (
            "NOT_VERIFIED: источники данных run (снимки candles на момент прогона) "
            "из текущей БД не восстанавливаются")
        out[run_id] = entry
    return out


def git_commit_exists(repo_root: Path, commit: str, timeout: float = 5.0) -> bool | None:
    try:
        proc = subprocess.run(["git", "-C", str(repo_root), "cat-file", "-t", commit],
                              capture_output=True, text=True, timeout=timeout)
        return proc.returncode == 0 and proc.stdout.strip() == "commit"
    except (OSError, subprocess.SubprocessError):
        return None


async def run_db_audit(args, cells: list[dict], files: list[Path]) -> dict:
    import asyncpg

    dsn = args.db_url or os.environ.get("SIGNAL_LAB_AUDIT_DSN")
    if not dsn:
        raise SystemExit("DB-режим требует --db-url или env SIGNAL_LAB_AUDIT_DSN")
    conn = await asyncpg.connect(dsn, timeout=10)
    results: dict = {"mode": "db", "cells": [], "checks": {}}
    snap_cells: list[dict] = []
    match_candles: list[dict] = []
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SET LOCAL statement_timeout='30s'")
            run_ids = sorted({c["run_id"] for c in cells})
            meta = await db_run_meta(conn, run_ids)
            results["run_meta"] = meta
            for cell in cells:
                rows = await db_cell_rows(conn, cell, args.profile)
                in_cell = cell_filters(cell, rows)
                published = None
                csv_path = (BACKEND / "reports" / "signal_lab" / "report"
                            / f"{cell['run_id']}_cells.csv")
                if csv_path.exists():
                    published = locate_cell(load_cells_csv(csv_path), cell)
                run_meta_local = meta.get(cell["run_id"], {})
                h1 = period_half_boundary(run_meta_local.get("period_from", ""),
                                          run_meta_local.get("period_to", ""))
                costs = run_meta_local.get("costs") or {}
                a = analyze_cell(in_cell, cell, costs, h1, published)
                card = await db_cardinality(conn, cell, args.profile)
                card["row_events_match"] = card["events"] == len(rows)
                card["cell_n"] = len(in_cell)
                card["ok"] = card["ok"] and card["row_events_match"]
                a["cardinality"] = card
                a["costs_used"] = costs
                a["h1_boundary_used"] = h1
                results["cells"].append(a)
                snap_cells.append({"cell": cell, "profile": args.profile,
                                   "rows": in_cell, "costs": costs, "h1_boundary": h1})
                if cell["ticker"] == "SBER":
                    trace = await sber_trace(conn, cell, args.profile, meta)
                    results["checks"]["sber"] = trace
                    match_candles = trace["matched_candles"]
                    results["checks"]["sber_anomaly_window"] = trace["anomaly_window_candles"]
            results["checks"]["config_hash"] = config_hash_checks(meta, BACKEND)
            cens = {}
            for rid in run_ids:
                m = meta.get(rid, {})
                cens[rid] = await db_census_low_open(conn, m.get("period_from", ""),
                                                     m.get("period_to", ""))
            results["checks"]["low_open_census"] = cens
            results["checks"]["db_readonly"] = {
                "isolation": "repeatable_read", "readonly": True,
                "statement_timeout": "30s", "run_ids": run_ids}
    finally:
        await conn.close()
    results["_snapshot"] = {"cells": snap_cells, "matched_candles": match_candles}
    return results


def file_audit(cells: list[dict], files: list[Path]) -> dict:
    results: dict = {"mode": "file", "cells": [], "checks": {}}
    csv_by_run, summary_by_run = {}, {}
    for p in files:
        if p.name.endswith("_cells.csv"):
            csv_by_run[p.name.split("_")[0]] = p
        if p.name.endswith("_summary.md"):
            summary_by_run[p.name.split("_")[0]] = p
    for cell in cells:
        run = cell["run_id"]
        published = None
        summary = {}
        if run in csv_by_run:
            published = locate_cell(load_cells_csv(csv_by_run[run]), cell)
        if run in summary_by_run:
            summary = parse_summary(summary_by_run[run])
        results["cells"].append(audit_cell_file_part(cell, published, summary))
    for run, p in csv_by_run.items():
        rows = load_cells_csv(p)
        res = {"rows": len(rows), "sha256": sha256_file(p)}
        if run in summary_by_run:
            sm = parse_summary(summary_by_run[run])
            res["summary_cells"] = sm["cells"]
            res["summary_profile"] = sm["profile"]
            res["summary_matches_csv"] = sm["cells"] == len(rows)
        results["checks"][run] = res
    return results


def write_artifacts(out_dir: Path, manifest: dict, results: dict,
                    auditor_sha: str) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=False)
    written = []
    manifest = dict(manifest)
    manifest["auditor"] = {"sha256": auditor_sha, "line_count": None}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    written.append("manifest.json")

    snap = results.pop("_snapshot", None)
    if snap is not None:
        cells_path = out_dir / "snapshot_cells.json"
        items = [{"cell": s["cell"], "profile": s["profile"], "rows": s["rows"],
                  "costs": s["costs"], "h1_boundary": s["h1_boundary"]}
                 for s in snap["cells"]]
        cells_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        written.append("snapshot_cells.json")
        manifest["snapshot_cells_sha256"] = sha256_file(cells_path)
        if snap["matched_candles"]:
            cpath = out_dir / "snapshot_candles.json"
            cpath.write_text(json.dumps(snap["matched_candles"], ensure_ascii=False,
                                        indent=1, default=str), encoding="utf-8")
            written.append("snapshot_candles.json")
            manifest["snapshot_candles_sha256"] = sha256_file(cpath)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    checks = results.get("checks") or {}
    cell_rows = []
    for e in results["cells"]:
        rec = e.get("recomputed") if isinstance(e.get("recomputed"), dict) else {}
        ov = e.get("overlap") if isinstance(e.get("overlap"), dict) else {}
        per = e.get("period") if isinstance(e.get("period"), dict) else {}
        gstats = ov.get("greedy_stats") or {}
        cell_rows.append({
            **e["cell"],
            "exact_match_csv": e.get("exact_match"),
            "n": rec.get("n"), "wins": rec.get("wins"), "losses": rec.get("losses"),
            "pf_norm": rec.get("pf_norm"), "pf_rub": rec.get("pf_rub"),
            "mean_net_pct": rec.get("mean_net_pct"),
            "overlapping_entries": ov.get("overlapping_entries"),
            "greedy_count": ov.get("greedy_count"),
            "greedy_pf_norm": gstats.get("pf_norm"),
            "greedy_sum_net": gstats.get("sum_net_norm"),
            "trading_days": per.get("trading_days"),
            "months": json.dumps(per.get("months") or {}, ensure_ascii=False),
            "halves": json.dumps(per.get("halves") or {}, ensure_ascii=False),
            "h1_pf": (per.get("H1_stats") or {}).get("pf_norm"),
            "h2_pf": (per.get("H2_stats") or {}).get("pf_norm"),
        })
    if cell_rows:
        with open(out_dir / "cells_recompute.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(cell_rows[0].keys()))
            w.writeheader()
            w.writerows(cell_rows)
        written.append("cells_recompute.csv")
    sber = checks.get("sber") or {}
    if sber.get("contributors"):
        with open(out_dir / "sber_contributors.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(sber["contributors"][0].keys()))
            w.writeheader()
            w.writerows(sber["contributors"])
        written.append("sber_contributors.csv")
    if sber.get("blast_radius"):
        with open(out_dir / "sber_blast_radius.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(sber["blast_radius"][0].keys()))
            w.writeheader()
            w.writerows(sber["blast_radius"])
        written.append("sber_blast_radius.csv")
    census = checks.get("low_open_census") or {}
    census_rows = []
    for _rid, rows in census.items():
        census_rows.extend(rows)
    if census_rows:
        with open(out_dir / "low_open_census.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(census_rows[0].keys()))
            w.writeheader()
            w.writerows(census_rows)
        written.append("low_open_census.csv")
    (out_dir / "checks.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    written.append("checks.json")
    return written


def recompute_from_snapshot(out_dir: Path, auditor_sha: str) -> tuple[int, list[dict]]:
    """Пересчёт метрик из snapshot_cells.json без БД; сравнение с checks.json.

    Возвращает (код, детали): 0 — все ячейки согласованы; 1 — есть расхождение.
    """
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    stored_cells = json.loads((out_dir / "checks.json").read_text(encoding="utf-8"))["cells"]
    snap_items = json.loads((out_dir / "snapshot_cells.json").read_text(encoding="utf-8"))
    detail = []
    bad = 0
    for item, stored in zip(snap_items, stored_cells, strict=True):
        recomputed = analyze_cell(item["rows"], item["cell"], item["costs"],
                                  item["h1_boundary"], stored.get("published_row"))
        diffs = []
        old = stored["recomputed"]
        new = recomputed["recomputed"]
        for key in ("n", "wins", "losses", "sum_win_norm", "sum_loss_norm", "pf_norm",
                    "pf_rub", "mean_net_pct", "avg_r", "sum_net_norm"):
            a, b = old.get(key), new.get(key)
            if a is None and b is None:
                continue
            if a is None or b is None or not math.isclose(float(a), float(b),
                                                          rel_tol=1e-9, abs_tol=1e-9):
                diffs.append(key)
        opub, npub = stored.get("published_recomputed") or {}, recomputed["published_recomputed"]
        for key in opub:
            if opub.get(key) != npub.get(key):
                diffs.append(f"published.{key}")
        cell_det = {
            "cell": item["cell"], "rows_recomputed": len(item["rows"]),
            "consistent": not diffs, "diffs": diffs,
            "stored_snapshot_hash_present": bool(manifest.get("snapshot_cells_sha256")),
            "auditor_matches": manifest.get("auditor", {}).get("sha256") == auditor_sha,
        }
        detail.append(cell_det)
        if diffs:
            bad += 1
    (out_dir / "snapshot_recompute_check.json").write_text(
        json.dumps({"consistent": bad == 0, "auditor_sha256": auditor_sha,
                    "n_cells": len(snap_items), "detail": detail},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return 0 if bad == 0 else 1, detail


def unique_out_dir(root: Path) -> Path:
    for _ in range(50):
        rid = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        cand = root / rid
        if not cand.exists():
            return cand
    raise SystemExit("не удалось подобрать уникальный каталог выдачи")


def collect_source_files() -> list[Path]:
    files = []
    for run in (A_RUN, B_RUN):
        files.append(BACKEND / "reports" / "signal_lab" / "report" / f"{run}_summary.md")
        files.append(BACKEND / "reports" / "signal_lab" / "report" / f"{run}_cells.csv")
    files.append(BACKEND / "configs" / "signal_lab" / "validation_2026-08.json")
    files.append(BACKEND / "configs" / "signal_lab" / "discovery_2026-09.json")
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only аудитор evidence Signal Lab (A1)")
    ap.add_argument("--db-url", default=None,
                    help="DSN только для чтения; можно задать env SIGNAL_LAB_AUDIT_DSN "
                         "(скрипт его не печатает)")
    ap.add_argument("--file-only", action="store_true", help="не обращаться к БД")
    ap.add_argument("--check-snapshot", default=None, metavar="DIR",
                    help="пересчитать метрики из сохранённого снимка без БД")
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--out-root",
                    default=str(BACKEND / "reports" / "architect_20261007" / "b1"))
    args = ap.parse_args(argv)

    auditor_sha = sha256_file(Path(__file__))
    if args.check_snapshot:
        code, detail = recompute_from_snapshot(Path(args.check_snapshot), auditor_sha)
        for d in detail:
            print(f"  {d['cell']['ticker']}: consistent={d['consistent']} "
                  f"rows={d['rows_recomputed']} diffs={d['diffs']}")
        print("snapshot recompute:", "OK" if code == 0 else "MISMATCH",
              "->", Path(args.check_snapshot) / "snapshot_recompute_check.json")
        return code

    files = collect_source_files()
    missing = [str(p) for p in files if not p.exists()]
    if missing:
        print("MISSING files:", *missing, sep="\n  ")
        files = [p for p in files if p.exists()]

    cells = [dict(c) for c in DEFAULT_CELLS]
    has_dsn = bool(args.db_url or os.environ.get("SIGNAL_LAB_AUDIT_DSN"))
    if args.file_only or not has_dsn:
        results = file_audit(cells, files)
        results["mode"] = "file" if args.file_only else "file (DB-DSN не задан)"
    else:
        import asyncio
        results = asyncio.run(run_db_audit(args, cells, files))

    manifest = build_manifest(files, results.get("run_meta"), {
        "profile": args.profile, "cells": cells, "mode": results["mode"],
        "db_used": results.get("mode") == "db",
    })
    out_dir = unique_out_dir(Path(args.out_root))
    written = write_artifacts(out_dir, manifest, results, auditor_sha)

    print(f"audit_signal_lab_evidence: mode={results['mode']}")
    for e in results["cells"]:
        c = e["cell"]
        rec = e.get("recomputed")
        if isinstance(rec, dict):
            pf_n = rec.get("pf_norm")
            pf_r = rec.get("pf_rub")
            print(f"  {c['engine']}/{c['tf']}/{c['ticker']}/{c['side']}"
                  f"/{c['regime']}/{c['session']}: n={rec['n']} "
                  f"pf_norm={round(pf_n, 4) if pf_n else pf_n} "
                  f"pf_rub={round(pf_r, 4) if pf_r else pf_r} "
                  f"exact_match={e.get('exact_match')}")
        else:
            print(f"  {c['engine']}/{c['tf']}/{c['ticker']}: published={e.get('published_row')}")
    print("artifacts:", out_dir)
    for name in written:
        print("  -", name)
    return 0


def build_manifest(files: list[Path], run_meta: dict | None, extra: dict) -> dict:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "files": [file_entry(p) for p in files],
        "runs": run_meta or {},
        "extra": extra,
    }


if __name__ == "__main__":
    raise SystemExit(main())