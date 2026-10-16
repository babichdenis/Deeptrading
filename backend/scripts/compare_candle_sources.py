#!/usr/bin/env python3
"""A5-C1 r3: сверка исторических 1m-свечей SBER — T-Invest ↔ MOEX ISS ↔ сохранённая БД.

Modes:
  offline (default) — читает сохранённые raw из --input, пишет результат в НОВЫЙ --out,
                      не обращается к сети; статусы источников пересчитываются из raw,
                      прежнему meta.status не доверяют; page-hashes проверяются;
  collect — ограниченный read-only сбор: SBER BBG004730N88 / MOEX SBER TQBR, 1min,
            окно 2026-09-08T12:30Z..13:10Z (ISS — один день с пагинацией и проверкой
            прогресса по последней метке). Raw иммутабельны; тело каждой ISS-страницы
            сохраняется ДО разбора/проверки прогресса.

Третья сторона — сохранённый db_probe.window из A3 facts.json, живая БД не читается.
Статусы разведены: overall_status = доступность/полнота загрузки источников;
comparison_quality_status = качество данных (INVALID/дубли/схема), не равен OK загрузки
(CLEAN — только структурная валидность, не отсутствие PRICE_MISMATCH/MISSING/
NOT_COMPARABLE). Malformed вход: повреждённый/отсутствующий raw-файл даёт безопасный
INCOMPLETE с кодом (file_missing/not_json/not_an_object/...), без traceback и без
ложного ok. Вызов T-Invest выполняется в дочернем процессе: wait(timeout) → terminate
→ kill → join; retry стартует только после завершения предыдущей попытки (максимум 2),
секреты в CLI/логи не передаются.
Exit codes: 0 = загрузка всех источников OK; 2 = SOURCE_UNAVAILABLE/INCOMPLETE
            (и usage-ошибки argparse); 3 = у --input нет каталога raw/ / --out уже
            содержит выходы / не заданы обязательные --input/--out / битый A3 facts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

FIGI = "BBG004730N88"
TICKER = "SBER"
BOARD = "TQBR"
INTERVAL = "1min"
CURRENCY = "RUB"
WINDOW_START = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 9, 8, 13, 10, tzinfo=timezone.utc)
WINDOW_MSK_TEXT = "15:30–16:10 МСК"
FOCUS_TS = datetime(2026, 9, 8, 12, 47, tzinfo=timezone.utc)
ISS_DAY = "2026-09-08"
MSK = timezone(timedelta(hours=3))
ISS_MAX_PAGES = 10
HTTP_TIMEOUT_S = 20
HTTP_RETRIES = 2
TINVEST_TIMEOUT_S = 20
TINVEST_ATTEMPTS = 2
TINVEST_CHILD_GRACE_S = 5
TINVEST_IPC_MAX_BYTES = 64 * 1024 * 1024
ISS_BODY_MAX_BYTES = 16 * 1024 * 1024
DEFAULT_PRICE_TOLERANCE = 0.001
REQUIRED_ISS_COLUMNS = ("begin", "open", "high", "low", "close", "volume")
ROOT = Path(__file__).resolve().parents[2]
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
DEFAULT_FACTS = ROOT / "backend/reports/architect_20261008/b1/20261008T031556Z-011190cc/facts.json"
DEFAULT_A5_DIR = ROOT / "backend/reports/architect_20261008/b1/a5"

SOURCE_ORDER = ("tinvest", "iss", "db")
CATEGORIES = ("MATCH", "PRICE_MISMATCH", "VOLUME_MISMATCH", "MISSING_IN_SOURCE",
              "DUPLICATE", "INVALID", "NOT_COMPARABLE")
PRIMARY_PRIORITY = ("INVALID", "DUPLICATE", "PRICE_MISMATCH", "MISSING_IN_SOURCE")
CONVERSION_VERSIONS = {
    "iss_begin_msk_to_utc": 1,
    "tinvest_quotation_units_nano": 1,
    "db_ts_passthrough_utc": 1,
    "db_ohlc_decimal_string_to_float": 2,
}
TIME_LABEL_NOTE = ("метка ts = НАЧАЛО минутного бара (канон проекта: floor-бакет UTC); "
                   "сверка begin-to-begin; ts вне границы минуты → INVALID")
MATCH_NOTE = ("MATCH = совпадение OHLC-цен по трем сторонам; volume=NOT_COMPARABLE "
              "(единицы не подтверждены) — полный матч ВСЕХ полей не утверждается")
SDK_NOTES = ("SDK t_tech.invest Client не экспонирует timeout/retries на вызове "
             "get_candles (проверено по установленному SDK: deadline не передаётся); "
             "поэтому вызов выполняется в отдельном дочернем процессе с конечным "
             "временем: wait(timeout) → terminate → kill → join; retry стартует только "
             "после завершения первой попытки, максимум 2 попытки; результат "
             "возвращается ограниченным IPC-файлом (≤64MiB), секреты в CLI/логи не "
             "попадают; ошибки сохраняются только классом исключения (без текста), "
             "чтобы не рисковать credentials")

VOLUME_UNITS_EVIDENCE = {
    "tinvest": {
        "claimed": "lots",
        "status": "VERIFIED_PRIMARY_DOC",
        "doc": "https://tinkoff.github.io/investAPI/marketdata/ — HistoricCandle.volume: "
               "«Объём сделок в лотах»",
    },
    "iss": {
        "claimed": None,
        "status": "UNVERIFIED",
        "doc": "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/"
               "securities/SBER/candles/columns.json — поле volume без текста единиц; "
               "value/volume внутри примера даёт цену (намёк на штуки) — вывод из "
               "данных, не первичный документ",
    },
    "db": {
        "claimed": None,
        "status": "UNVERIFIED",
        "doc": "писатель строки неизвестен (A3 origin=NOT_VERIFIED): в БД могли попасть "
               "лоты (T-Invest) или штуки (ISS) в зависимости от источника записи",
    },
    "lot_size_on_date": {
        "value": None,
        "status": "UNVERIFIED",
        "doc": "лот SBER на 2026-09-08 первичным документом не подтверждён; collect "
               "ограничен историческими запросами, справочник не запрашивался",
    },
    "tick_size_on_date": {
        "value": None,
        "status": "UNVERIFIED",
        "doc": "минимальный шаг цены не подтверждён → дельты в тиках не считаются",
    },
    "comparable": False,
    "verdict": "volume=NOT_COMPARABLE для всех источников (единицы/множитель не "
               "подтверждены первичными документами); значения сохраняются как есть",
}


class MissingRawInputs(Exception):
    pass


class OutDirNotEmpty(Exception):
    pass


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def parse_iss_begin(value) -> datetime | None:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MSK)
    return dt.astimezone(timezone.utc)


def parse_price(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            f = float(value)
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def error_class_of(exc: Exception) -> str:
    return type(exc).__name__


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def load_json_object(path: Path) -> tuple[dict | None, dict | None]:
    if not path.exists():
        return None, {"problem": "file_missing", "file": path.name}
    try:
        text = path.read_text()
    except OSError:
        return None, {"problem": "unreadable", "file": path.name}
    try:
        data = json.loads(text)
    except ValueError:
        return None, {"problem": "not_json", "file": path.name}
    if not isinstance(data, dict):
        return None, {"problem": "not_an_object", "file": path.name,
                      "top_level_type": type(data).__name__}
    return data, None


def load_db_window(facts_path: Path) -> tuple[list[dict], dict]:
    facts, problem = load_json_object(facts_path)
    if problem is not None:
        raise MissingRawInputs(f"A3 facts.json unusable ({problem['problem']}): "
                               f"{facts_path}")
    findings = facts.get("findings")
    db_probe = findings.get("db_probe") if isinstance(findings, dict) else None
    window = db_probe.get("window") if isinstance(db_probe, dict) else None
    if not isinstance(window, list):
        raise MissingRawInputs(f"db_probe.window not a list in {facts_path}")
    meta = facts.get("meta")
    if not isinstance(meta, dict):
        meta = {}
    provenance = {
        "source_file": str(facts_path),
        "source_file_sha256": sha256_file(facts_path),
        "subject": facts.get("subject"),
        "scope": meta.get("db_scope"),
        "window_utc": meta.get("window_utc"),
        "captured_at": meta.get("ts_utc") or facts.get("generated_at"),
        "note": "сохранённая A3-копия ответа БД (READ ONLY, REPEATABLE READ); "
                "живая БД в A5 не читается; origin записи NOT_VERIFIED (A3)",
    }
    return window, provenance


def norm_db_rows(window: list) -> tuple[list[dict], list[dict]]:
    rows = []
    issues = []
    for i, r in enumerate(window):
        if not isinstance(r, dict):
            issues.append({"row_index": i, "problem": "row_not_an_object"})
            continue
        rows.append({
            "row_index": i,
            "ts": parse_ts(r.get("ts")),
            "open": parse_price(r.get("open")), "high": parse_price(r.get("high")),
            "low": parse_price(r.get("low")), "close": parse_price(r.get("close")),
            "volume": r.get("volume"),
            "is_complete": None,
            "raw": r,
        })
    return rows, issues


def norm_tinvest_rows(payload: dict) -> tuple[list[dict], list[dict]]:
    rows = []
    issues = []
    candles = payload.get("candles")
    if not isinstance(candles, list):
        issues.append({"scope": "tinvest", "problem": "candles_not_a_list"})
        return rows, issues
    for i, c in enumerate(candles):
        if not isinstance(c, dict):
            issues.append({"row_index": i, "problem": "candle_not_an_object"})
            continue

        def price(field):
            f = c.get(field)
            if not isinstance(f, dict):
                return None
            units, nano = f.get("units"), f.get("nano")
            if not is_number(units) or not is_number(nano):
                return None
            v = units + nano / 1e9
            return v if math.isfinite(v) else None

        row = {
            "row_index": i,
            "ts": parse_ts(c.get("time")),
            "open": price("open"), "high": price("high"),
            "low": price("low"), "close": price("close"),
            "volume": c.get("volume"),
            "is_complete": c.get("is_complete"),
            "raw": c,
        }
        if row["ts"] is None:
            issues.append({"row_index": i, "problem": "bad_time"})
        for name in ("open", "high", "low", "close"):
            if row[name] is None:
                issues.append({"row_index": i, "problem": f"bad_quotation_{name}"})
        rows.append(row)
    return rows, issues


def norm_iss_rows(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    rows = []
    issues = []
    for page_no, page in enumerate(pages):
        body = page.get("body")
        if not isinstance(body, str):
            issues.append({"page": page_no, "problem": "body_missing"})
            continue
        try:
            data = json.loads(body)
        except (ValueError, TypeError):
            issues.append({"page": page_no, "problem": "body_not_json"})
            continue
        if not isinstance(data, dict):
            issues.append({"page": page_no, "problem": "body_not_an_object",
                           "top_level_type": type(data).__name__})
            continue
        candles = data.get("candles")
        if not isinstance(candles, dict):
            issues.append({"page": page_no, "problem": "candles_block_missing"})
            continue
        cols = candles.get("columns")
        # r4: до построения idx имена колонок обязаны быть строками и быть
        # уникальными (нестрока -> TypeError unhashable; дубликат -> неоднозначный idx).
        cols_ok = (isinstance(cols, list)
                   and all(isinstance(c, str) for c in cols)
                   and len(set(cols)) == len(cols)
                   and all(c in cols for c in REQUIRED_ISS_COLUMNS))
        if not cols_ok:
            issues.append({"page": page_no, "problem": "columns_schema_invalid"})
            continue
        idx = {n: i for i, n in enumerate(cols)}
        recs = candles.get("data")
        if not isinstance(recs, list):
            issues.append({"page": page_no, "problem": "data_not_a_list"})
            continue
        for j, rec in enumerate(recs):
            if not isinstance(rec, list) or len(rec) != len(cols):
                issues.append({"page": page_no, "row": j, "problem": "short_or_bad_row"})
                continue
            begin = rec[idx["begin"]]
            dt = None
            if isinstance(begin, str):
                try:
                    dt = datetime.fromisoformat(begin)
                except ValueError:
                    dt = None
                if dt is not None:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=MSK)
                    dt = dt.astimezone(timezone.utc)
            if dt is None:
                issues.append({"page": page_no, "row": j, "problem": "bad_begin"})

            def col(name):
                return rec[idx[name]]

            rows.append({
                "row_index": len(rows),
                "ts": dt,
                "open": parse_price(col("open")), "high": parse_price(col("high")),
                "low": parse_price(col("low")), "close": parse_price(col("close")),
                "volume": col("volume"),
                "is_complete": None,
                "raw": rec,
            })
    return rows, issues


def validate_bar(row: dict) -> list[str]:
    problems = []
    ts = row.get("ts")
    if ts is None:
        problems.append("bad_ts")
    elif ts.second != 0 or ts.microsecond != 0:
        problems.append("off_minute_ts")
    numeric = {}
    for name in ("open", "high", "low", "close"):
        v = row.get(name)
        if not is_number(v):
            problems.append(f"nonnumeric_{name}")
        else:
            numeric[name] = v
            if v <= 0:
                problems.append(f"nonpositive_{name}")
    if row.get("is_complete") is False:
        problems.append("incomplete")
    if len(numeric) == 4:
        o, h, l, c = numeric["open"], numeric["high"], numeric["low"], numeric["close"]
        if h < l:
            problems.append("impossible_high_low")
        if h < o or h < c:
            problems.append("impossible_high")
        if l > o or l > c:
            problems.append("impossible_low")
    return problems


def index_by_ts(rows: list[dict]) -> tuple[dict, list[dict]]:
    out: dict[datetime, list] = {}
    invalid = []
    for r in rows:
        ts = r.get("ts")
        if ts is None:
            invalid.append({"row_index": r.get("row_index"), "raw": r.get("raw"),
                            "problems": validate_bar(r)})
            continue
        if not (WINDOW_START <= ts < WINDOW_END):
            continue
        out.setdefault(ts, []).append(r)
    return out, invalid


def same_bar(a: dict, b: dict) -> bool:
    same_ohlcv = all(
        is_number(a.get(k)) and is_number(b.get(k)) and abs(a[k] - b[k]) <= 1e-12
        for k in ("open", "high", "low", "close")
    ) and (a.get("volume") == b.get("volume"))
    return same_ohlcv and a.get("is_complete") == b.get("is_complete")


def analyze_entries(entries: list[dict]) -> dict:
    details = {"entries": [
        {"row_index": e.get("row_index"), "problems": validate_bar(e)}
        for e in entries
    ]}
    problems_any = any(d["problems"] for d in details["entries"])
    conflict = any(not same_bar(entries[0], e) for e in entries[1:])
    reasons = []
    if problems_any:
        reasons.append("invalid")
    if conflict:
        reasons.append("duplicate_conflict")
    elif len(entries) > 1:
        reasons.append("duplicate_identical")
    comparable = (not problems_any) and (not conflict)
    return {
        "chosen": entries[0],
        "comparable": comparable,
        "reasons": reasons,
        "details": details,
    }


def price_deltas(a: dict, b: dict, tolerance: float) -> dict:
    out = {}
    for name in ("open", "high", "low", "close"):
        x, y = a.get(name), b.get(name)
        if not is_number(x) or not is_number(y):
            out[name] = None
            continue
        d = abs(x - y)
        denom = max(abs(x), abs(y))
        out[name] = {
            "abs": d,
            "rel": (d / denom) if denom else (0.0 if d == 0 else None),
            "within_tolerance": d <= tolerance,
            "exact": d == 0.0,
        }
    return out


def compare_sources(tinvest: list[dict], iss: list[dict], db: list[dict],
                    tolerance: float) -> dict:
    indexes: dict[str, dict] = {}
    invalid_ts: dict[str, list] = {}
    for src, rows in (("tinvest", tinvest), ("iss", iss), ("db", db)):
        idx, bad = index_by_ts(rows)
        indexes[src] = idx
        invalid_ts[src] = bad
    all_ts = sorted(set().union(*(set(ix) for ix in indexes.values())))
    rows = []
    category_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    primary_counts: dict[str, int] = {}
    exact_pairwise_rows = 0
    exact_3way_rows = 0
    invalid_entries_total = 0
    conflict_groups_total = 0
    duplicate_groups_total = 0

    for ts in all_ts:
        present = {}
        chosen: dict[str, dict | None] = {}
        comparable_srcs = []
        reasons: list[str] = []
        details: dict[str, dict] = {}
        for src in SOURCE_ORDER:
            entries = indexes[src].get(ts, [])
            present[src] = bool(entries)
            if not entries:
                chosen[src] = None
                continue
            analysis = analyze_entries(entries)
            chosen[src] = analysis["chosen"]
            for r in analysis["reasons"]:
                reasons.append(f"{r}_{src}")
            details[src] = analysis["details"]
            if "invalid" in analysis["reasons"]:
                invalid_entries_total += sum(
                    1 for e in analysis["details"]["entries"] if e["problems"])
            if "duplicate_conflict" in analysis["reasons"]:
                conflict_groups_total += 1
            if "duplicate_identical" in analysis["reasons"]:
                duplicate_groups_total += 1
            if analysis["comparable"]:
                comparable_srcs.append(src)

        missing = [s for s in SOURCE_ORDER if not present[s]]
        for s in missing:
            reasons.append(f"missing_in_{s}")

        deltas = {}
        price_mismatch = False
        pairwise_exact = bool(comparable_srcs) and len(comparable_srcs) >= 2
        three_way_exact = len(comparable_srcs) == len(SOURCE_ORDER)
        for i in range(len(comparable_srcs)):
            for j in range(i + 1, len(comparable_srcs)):
                pair = f"{comparable_srcs[i]}__{comparable_srcs[j]}"
                d = price_deltas(chosen[comparable_srcs[i]],
                                 chosen[comparable_srcs[j]], tolerance)
                deltas[pair] = d
                for v in d.values():
                    if v is None:
                        pairwise_exact = False
                        three_way_exact = False
                        continue
                    if not v["within_tolerance"]:
                        price_mismatch = True
                    if not v["exact"]:
                        pairwise_exact = False
                        three_way_exact = False
        if price_mismatch:
            reasons.append("price_mismatch")
        if pairwise_exact:
            exact_pairwise_rows += 1
        if three_way_exact:
            exact_3way_rows += 1

        volume_mismatch = False
        if VOLUME_UNITS_EVIDENCE["comparable"] and len(comparable_srcs) >= 2:
            vols = {s: chosen[s].get("volume") for s in comparable_srcs}
            if all(v is not None for v in vols.values()) and len(set(vols.values())) > 1:
                volume_mismatch = True
                reasons.append("volume_mismatch")

        categories = set()
        if any(r.startswith("invalid_") for r in reasons):
            categories.add("INVALID")
        if any(r.startswith("duplicate_") for r in reasons):
            categories.add("DUPLICATE")
        if missing:
            categories.add("MISSING_IN_SOURCE")
        if price_mismatch:
            categories.add("PRICE_MISMATCH")
        if volume_mismatch:
            categories.add("VOLUME_MISMATCH")
        categories.add("NOT_COMPARABLE")
        if (len(comparable_srcs) == len(SOURCE_ORDER) and not price_mismatch
                and not volume_mismatch):
            categories.add("MATCH")
        primary = next((c for c in PRIMARY_PRIORITY if c in categories), "MATCH")

        for r in reasons:
            reason_counts[r] = reason_counts.get(r, 0) + 1
        for c in categories:
            category_counts[c] = category_counts.get(c, 0) + 1
        primary_counts[primary] = primary_counts.get(primary, 0) + 1

        rows.append({
            "ts": ts.isoformat(),
            "present": present,
            "values": {
                s: ({
                    "open": chosen[s]["open"], "high": chosen[s]["high"],
                    "low": chosen[s]["low"], "close": chosen[s]["close"],
                    "volume": chosen[s]["volume"],
                } if chosen[s] else None)
                for s in SOURCE_ORDER
            },
            "comparable": {s: s in comparable_srcs for s in SOURCE_ORDER},
            "price_deltas": deltas,
            "exact_pairwise": pairwise_exact,
            "exact_3way": three_way_exact,
            "categories": sorted(categories),
            "primary_category": primary,
            "reasons": reasons,
            "details": details,
        })

    coverage = {
        s: {
            "present": sum(1 for r in rows if r["present"][s]),
            "union": len(rows),
            "share": (sum(1 for r in rows if r["present"][s]) / len(rows)) if rows else None,
        }
        for s in SOURCE_ORDER
    }
    quality = {
        "invalid_entries": invalid_entries_total,
        "conflict_groups": conflict_groups_total,
        "duplicate_identical_groups": duplicate_groups_total,
        "invalid_ts_rows": {s: len(v) for s, v in invalid_ts.items() if v},
    }
    return {
        "policy": {
            "window_utc": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
            "window_msk": WINDOW_MSK_TEXT,
            "price_tolerance_abs": tolerance,
            "price_tolerance_note": "порог фиксируется параметром ДО просмотра результата; "
                                    "exact — поле exact (дельта == 0); MATCH = только "
                                    "OHLC-цены, volume NOT_COMPARABLE",
            "time_label": TIME_LABEL_NOTE,
            "conversion_versions": dict(CONVERSION_VERSIONS),
            "categories": list(CATEGORIES),
            "primary_priority": list(PRIMARY_PRIORITY),
            "match_note": MATCH_NOTE,
            "duplicate_policy": "каждая запись дубля валидируется отдельно; конфликт или "
                                "любая невалидная запись → источник исключается из "
                                "сравнимых пар (не выдаётся exact по первой записи)",
            "volume_units": VOLUME_UNITS_EVIDENCE,
            "tick_size": VOLUME_UNITS_EVIDENCE["tick_size_on_date"],
            "sdk_notes": SDK_NOTES,
            "agreement_note": "совпадение — не «истина по голосованию»: каждая сторона "
                              "сравнивается отдельно, расхождение сохраняется как факт",
        },
        "row_count": len(rows),
        "rows": rows,
        "counts": {
            "primary": dict(sorted(primary_counts.items())),
            "by_category": dict(sorted(category_counts.items())),
            "by_reason": dict(sorted(reason_counts.items())),
            "exact_pairwise_rows": exact_pairwise_rows,
            "exact_3way_rows": exact_3way_rows,
        },
        "quality": quality,
        "invalid_ts": invalid_ts,
        "source_sizes_in_window": {s: len(indexes[s]) for s in SOURCE_ORDER},
        "coverage": coverage,
    }


def overall_load_status(sources_status: dict) -> str:
    for st in sources_status.values():
        if st.get("status") != "ok" or not st.get("load_complete", True):
            return "INCOMPLETE"
    return "OK"


def comparison_quality_status(comparison: dict, sources_status: dict) -> str:
    q = comparison["quality"]
    issues = (q["invalid_entries"] + q["conflict_groups"]
              + sum(q["invalid_ts_rows"].values()))
    for st in sources_status.values():
        issues += len(st.get("schema_issues") or [])
    return "CLEAN" if issues == 0 else "ISSUES"


def focus_row(comparison: dict, dt: datetime) -> dict | None:
    iso = dt.isoformat()
    for r in comparison["rows"]:
        if r["ts"] == iso:
            return r
    return None


def collect_iss(out_dir: Path) -> dict:
    raw_dir = out_dir / "raw"
    page_files = sorted(raw_dir.glob("iss_page_*.json"))
    if page_files:
        raise SystemExit("collect: raw ISS pages already exist (immutable), "
                         "use a fresh --out or offline mode")
    pages_meta = []
    status = "ok"
    error = None
    start = 0
    prev_last_begin = None
    window_last_bar = WINDOW_END - timedelta(minutes=1)
    for page_no in range(ISS_MAX_PAGES):
        url = (f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/{BOARD}"
               f"/securities/{TICKER}/candles.json?from={ISS_DAY}&till={ISS_DAY}"
               f"&interval=1&start={start}")
        body = None
        last_exc = None
        for attempt in range(HTTP_RETRIES):
            try:
                with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
                    body = resp.read().decode("utf-8")
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                time.sleep(1 + attempt)
        if body is None:
            status = "SOURCE_UNAVAILABLE" if page_no == 0 else "INCOMPLETE"
            error = {"class": error_class_of(last_exc or RuntimeError("empty response")),
                     "where": f"iss_page_{page_no}"}
            break
        body_bytes_original = len(body.encode("utf-8"))
        body_truncated = body_bytes_original > ISS_BODY_MAX_BYTES
        if body_truncated:
            body = body.encode("utf-8")[:ISS_BODY_MAX_BYTES].decode("utf-8", "replace")
        page_file = raw_dir / f"iss_page_{page_no:02d}.json"
        page_doc = {
            "fetched_at_utc": utc_now_iso(),
            "source": "iss.moex.com",
            "endpoint": url,
            "request": {"from": ISS_DAY, "till": ISS_DAY, "interval": 1, "start": start},
            "body_bytes_original": body_bytes_original,
            "body_truncated": body_truncated,
            "n_rows": None,
            "last_begin": None,
            "parse_ok": False,
            "body": body,
        }
        page_file.write_text(json.dumps(page_doc, ensure_ascii=False, indent=1))
        if body_truncated:
            status = "INCOMPLETE"
            error = {"class": "BodyTooLarge", "where": f"iss_page_{page_no}",
                     "reason_code": "body_truncated",
                     "body_bytes_original": body_bytes_original,
                     "body_bytes_saved": ISS_BODY_MAX_BYTES}
            pages_meta.append({
                "file": page_file.name, "start": start, "n_rows": None,
                "last_begin": None, "parse_ok": False,
                "sha256": sha256_file(page_file),
            })
            break
        try:
            data = json.loads(body)
            candles = data.get("candles")
            if not isinstance(candles, dict):
                raise ValueError("candles_block_missing")
            cols = candles.get("columns")
            if (not isinstance(cols, list)
                    or not all(isinstance(c, str) for c in cols)
                    or len(set(cols)) != len(cols)
                    or not all(c in cols for c in REQUIRED_ISS_COLUMNS)):
                raise ValueError("columns_schema_invalid")
            recs = candles.get("data")
            if not isinstance(recs, list):
                raise ValueError("data_not_a_list")
            n_rows = len(recs)
        except (ValueError, TypeError) as exc:
            status = "SOURCE_UNAVAILABLE" if page_no == 0 else "INCOMPLETE"
            error = {"class": error_class_of(exc), "where": f"iss_page_{page_no}",
                     "reason_code": "schema_or_json_invalid"}
            pages_meta.append({
                "file": page_file.name, "start": start, "n_rows": None,
                "last_begin": None, "parse_ok": False,
                "sha256": sha256_file(page_file),
            })
            break
        idx = {n: i for i, n in enumerate(cols)}
        last_begin = None
        bad_rows = 0
        for rec in recs:
            if not isinstance(rec, list) or len(rec) != len(cols):
                bad_rows += 1
                continue
            try:
                dt = datetime.fromisoformat(rec[idx["begin"]])
            except (ValueError, TypeError):
                bad_rows += 1
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=MSK)
            dt = dt.astimezone(timezone.utc)
            if last_begin is None or dt > last_begin:
                last_begin = dt
        page_doc["n_rows"] = n_rows
        page_doc["last_begin"] = last_begin.isoformat() if last_begin else None
        page_doc["parse_ok"] = not bad_rows
        page_file.write_text(json.dumps(page_doc, ensure_ascii=False, indent=1))
        pages_meta.append({
            "file": page_file.name,
            "start": start,
            "n_rows": n_rows,
            "last_begin": last_begin.isoformat() if last_begin else None,
            "parse_ok": not bad_rows,
            "sha256": sha256_file(page_file),
        })
        if bad_rows:
            status = "INCOMPLETE"
            error = {"class": "SchemaError", "where": f"iss_page_{page_no}",
                     "reason": f"bad_rows={bad_rows}"}
            break
        if prev_last_begin is not None and (last_begin is None
                                            or last_begin <= prev_last_begin):
            status = "INCOMPLETE"
            error = {"class": "PaginationStalled",
                     "where": f"iss_page_{page_no}",
                     "reason": f"last_begin {last_begin} not after {prev_last_begin}"}
            break
        prev_last_begin = last_begin
        if n_rows == 0:
            break
        start += n_rows
    else:
        status = "INCOMPLETE"
        error = {"class": "PageLimit", "reason": f"page limit {ISS_MAX_PAGES} reached"}

    max_ts = None
    for ref in pages_meta:
        lb = ref.get("last_begin")
        if lb:
            dt = parse_ts(lb)
            if dt and (max_ts is None or dt > max_ts):
                max_ts = dt
    covered = max_ts is not None and max_ts >= window_last_bar
    if status == "ok" and not covered:
        status = "INCOMPLETE"
        error = {"class": "WindowNotCovered",
                 "reason": f"max_ts={max_ts.isoformat() if max_ts else None} < "
                           f"{window_last_bar.isoformat()}"}
    complete = status == "ok" and bool(pages_meta) and covered

    meta = {
        "fetched_at_utc": utc_now_iso(),
        "source": "iss.moex.com",
        "endpoint_template": (f"https://iss.moex.com/iss/engines/stock/markets/shares/"
                              f"boards/{BOARD}/securities/{TICKER}/candles.json"
                              f"?from={ISS_DAY}&till={ISS_DAY}&interval=1&start=<n>"),
        "request": {"from": ISS_DAY, "till": ISS_DAY, "interval": 1},
        "analysis_window_utc": [WINDOW_START.isoformat(), WINDOW_END.isoformat()],
        "pages": pages_meta,
        "pagination_complete": complete,
        "max_pages": ISS_MAX_PAGES,
        "max_fetched_ts": max_ts.isoformat() if max_ts else None,
        "status": status,
        "error": error,
        "conversion": CONVERSION_VERSIONS["iss_begin_msk_to_utc"],
        "time_conversion": "ISS begin — MSK (Europe/Moscow без tz) → UTC",
        "progress_rule": "last_begin страницы обязан строго расти; повтор/стоп → INCOMPLETE",
    }
    (raw_dir / "iss_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    return meta


def _tinvest_get_candles() -> list[dict]:
    from t_tech.invest import CandleInterval, Client
    from app.config import get_settings
    settings = get_settings()
    with Client(settings.tinkoff_token) as client:
        resp = client.market_data.get_candles(
            figi=FIGI,
            interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
            from_=WINDOW_START,
            to=WINDOW_END,
        )
    return [{
        "time": c.time.isoformat() if isinstance(c.time, datetime) else str(c.time),
        "open": {"units": c.open.units, "nano": c.open.nano},
        "high": {"units": c.high.units, "nano": c.high.nano},
        "low": {"units": c.low.units, "nano": c.low.nano},
        "close": {"units": c.close.units, "nano": c.close.nano},
        "volume": c.volume,
        "is_complete": c.is_complete,
    } for c in resp.candles]


def _tinvest_worker_main(result_path: Path) -> int:
    out: dict = {"ok": False, "error_class": None, "candles": []}
    try:
        stub = os.environ.get("A5_TINVEST_STUB", "")
        if stub == "hang":
            time.sleep(3600)
        elif stub == "error":
            raise RuntimeError("stubbed failure token=SHOULD_NOT_APPEAR")
        elif stub == "ok":
            out["candles"] = []
            out["ok"] = True
            stub_payload = json.dumps(out, ensure_ascii=False)
            result_path.write_text(stub_payload)
            return 0
        if not out["ok"]:
            out["candles"] = _tinvest_get_candles()
            out["ok"] = True
    except BaseException as exc:
        out["error_class"] = error_class_of(exc)
    try:
        data = json.dumps(out, ensure_ascii=False)
        if len(data.encode("utf-8")) > TINVEST_IPC_MAX_BYTES:
            result_path.write_text(json.dumps(
                {"ok": False, "error_class": "IpcTooLarge", "candles": []}))
        else:
            result_path.write_text(data)
    except OSError:
        return 1
    return 0 if out["ok"] else 1


def _tinvest_worker_argv(result_path: Path) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()),
            "--_tinvest-worker", str(result_path)]


def _tinvest_attempt_once(timeout_s: float) -> tuple[list[dict] | None, dict | None]:
    with tempfile.TemporaryDirectory(prefix="a5_tinvest_") as tmp:
        result_path = Path(tmp) / "result.json"
        proc = subprocess.Popen(
            _tinvest_worker_argv(result_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.terminate()
            try:
                proc.wait(timeout=TINVEST_CHILD_GRACE_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if timed_out:
            return None, {"class": "TimeoutError", "timeout_s": timeout_s,
                          "child_pid": proc.pid,
                          "child_reaped": proc.returncode is not None,
                          "termination": "terminate→kill→join"}
        if not result_path.exists():
            return None, {"class": "WorkerCrash",
                          "exit_code": proc.returncode}
        if result_path.stat().st_size > TINVEST_IPC_MAX_BYTES:
            return None, {"class": "IpcTooLarge"}
        try:
            payload = json.loads(result_path.read_text())
        except (ValueError, OSError):
            return None, {"class": "WorkerIpcInvalid"}
        if not isinstance(payload, dict):
            return None, {"class": "WorkerIpcInvalid"}
        if not payload.get("ok"):
            return None, {"class": payload.get("error_class") or "WorkerFailed"}
        candles = payload.get("candles")
        if not isinstance(candles, list):
            return None, {"class": "WorkerIpcInvalid"}
        return candles, None


def collect_tinvest(out_dir: Path) -> dict:
    raw_dir = out_dir / "raw"
    target = raw_dir / "tinvest_sber_1m.json"
    if target.exists():
        raise SystemExit("collect: raw T-Invest file already exists (immutable), "
                         "use a fresh --out or offline mode")
    payload = {
        "fetched_at_utc": utc_now_iso(),
        "source": "t_tech.invest SDK",
        "representation": "SDK object serialization (market_data.get_candles), NOT a network wire dump",
        "instrument": {"figi": FIGI, "ticker": TICKER, "interval": INTERVAL,
                       "currency": CURRENCY, "mode": "основной режим T-Invest (без board)"},
        "request": {
            "figi": FIGI, "interval": INTERVAL,
            "from": WINDOW_START.isoformat(), "to": WINDOW_END.isoformat(),
            "endpoint": "market_data.GetCandles",
            "timeout_s": TINVEST_TIMEOUT_S,
            "attempts": TINVEST_ATTEMPTS,
            "execution": "отдельный дочерний процесс на попытку: "
                         "wait(timeout) → terminate → kill → join; retry только "
                         "после завершения предыдущей попытки",
            "sdk_notes": SDK_NOTES,
        },
        "conversion": CONVERSION_VERSIONS["tinvest_quotation_units_nano"],
        "status": "ok",
        "error": None,
        "candles": [],
    }
    last_error = None
    for attempt in range(TINVEST_ATTEMPTS):
        candles, err = _tinvest_attempt_once(TINVEST_TIMEOUT_S)
        if err is None:
            payload["candles"] = candles
            last_error = None
            break
        last_error = {**err, "attempt": attempt + 1}
        if attempt + 1 < TINVEST_ATTEMPTS:
            time.sleep(1 + attempt)
    if last_error is not None:
        payload["status"] = "SOURCE_UNAVAILABLE"
        payload["error"] = last_error
        payload["candles"] = []
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    return {
        "file": target.name,
        "status": payload["status"],
        "error": payload["error"],
        "n_candles": len(payload["candles"]),
        "sha256": sha256_file(target),
        "fetched_at_utc": payload["fetched_at_utc"],
        "load_complete": payload["status"] == "ok",
    }


def write_db_raw(out_dir: Path, facts_path: Path) -> dict:
    raw_dir = out_dir / "raw"
    db_file = raw_dir / "db_probe_window.json"
    if db_file.exists():
        raise SystemExit("collect: raw DB window already exists (immutable)")
    window, provenance = load_db_window(facts_path)
    db_file.write_text(json.dumps({
        "captured_at_utc": utc_now_iso(),
        "source": "A3 db_probe.window (saved facts.json), live DB not queried",
        "provenance": provenance,
        "conversion": CONVERSION_VERSIONS["db_ts_passthrough_utc"],
        "rows": window,
    }, ensure_ascii=False, indent=1))
    return {"file": db_file.name, "sha256": sha256_file(db_file), "provenance": provenance}


def load_raw(input_dir: Path) -> tuple[dict, dict, dict, dict, dict, dict]:
    raw_dir = input_dir / "raw"
    tinvest_file = raw_dir / "tinvest_sber_1m.json"
    iss_meta_file = raw_dir / "iss_meta.json"
    db_file = raw_dir / "db_probe_window.json"

    tinvest_payload, ti_problem = load_json_object(tinvest_file)
    if ti_problem is not None:
        ti_rows, ti_schema, ti_status, ti_complete = [], [ti_problem], "INCOMPLETE", False
        ti_error, ti_n_candles, ti_fetched = ti_problem, 0, None
    else:
        ti_rows, ti_schema = norm_tinvest_rows(tinvest_payload)
        ti_n_candles = len(tinvest_payload["candles"]) \
            if isinstance(tinvest_payload.get("candles"), list) else 0
        ti_fetched = tinvest_payload.get("fetched_at_utc")
        ti_error = tinvest_payload.get("error")
        status_field = tinvest_payload.get("status")
        if not isinstance(status_field, str):
            ti_status = "INCOMPLETE"
            ti_complete = False
            ti_error = {"problem": "missing_or_bad_status_field"}
        elif status_field != "ok":
            ti_status = status_field
            ti_complete = False
        elif ti_schema:
            ti_status = "INCOMPLETE"
            ti_complete = False
        else:
            ti_status = "ok"
            ti_complete = True
    tinvest_status = {
        "status": ti_status, "load_complete": ti_complete,
        "error": ti_error,
        "n_candles": ti_n_candles,
        "fetched_at_utc": ti_fetched,
        "schema_issues": ti_schema,
        "sha256": sha256_file(tinvest_file) if tinvest_file.exists() else None,
    }

    iss_meta, iss_problem = load_json_object(iss_meta_file)
    pages = []
    iss_load_issues = []
    prev_last_begin = None
    if iss_problem is not None:
        iss_load_issues.append(iss_problem)
        iss_refs = []
    else:
        iss_refs = iss_meta.get("pages")
        if not isinstance(iss_refs, list):
            iss_load_issues.append({"file": iss_meta_file.name,
                                    "problem": "pages_not_a_list"})
            iss_refs = []
    for ref in iss_refs:
        if not isinstance(ref, dict):
            iss_load_issues.append({"problem": "page_ref_invalid",
                                    "reason": "ref_not_an_object"})
            continue
        ref_file = ref.get("file")
        if not isinstance(ref_file, str) or not ref_file:
            iss_load_issues.append({"problem": "page_ref_invalid",
                                    "reason": "bad_file_field"})
            continue
        page_path = raw_dir / ref_file
        if not page_path.exists():
            iss_load_issues.append({"file": ref_file, "problem": "page_file_missing"})
            continue
        actual = sha256_file(page_path)
        if ref.get("sha256") and actual != ref["sha256"]:
            iss_load_issues.append({"file": ref_file, "problem": "page_hash_mismatch",
                                    "expected": ref["sha256"], "actual": actual})
        try:
            page = json.loads(page_path.read_text())
        except (ValueError, TypeError):
            iss_load_issues.append({"file": ref_file, "problem": "page_not_json"})
            continue
        if not isinstance(page, dict):
            iss_load_issues.append({"file": ref_file, "problem": "page_not_an_object"})
            continue
        pages.append(page)
        lb = parse_iss_begin(page.get("last_begin")) if page.get("last_begin") else None
        if lb is None:
            body = page.get("body")
            try:
                data = json.loads(body) if isinstance(body, str) else {}
                if not isinstance(data, dict):
                    data = {}
                candles_block = data.get("candles")
                if not isinstance(candles_block, dict):
                    candles_block = {}
                recs = candles_block.get("data") or []
                cols = candles_block.get("columns") or []
                if "begin" in cols and recs:
                    bi = cols.index("begin")
                    candidates = [parse_iss_begin(rec[bi]) for rec in recs
                                  if isinstance(rec, list) and bi < len(rec)]
                    candidates = [c for c in candidates if c]
                    lb = max(candidates) if candidates else None
            except (ValueError, TypeError):
                lb = None
        if prev_last_begin is not None and lb is not None and lb <= prev_last_begin:
            iss_load_issues.append({"file": ref_file,
                                    "problem": "pagination_no_progress",
                                    "last_begin": lb.isoformat(),
                                    "prev_last_begin": prev_last_begin.isoformat()})
        if lb is not None:
            prev_last_begin = lb
    if iss_problem is None and not iss_refs:
        iss_load_issues.append({"problem": "no_pages_in_meta"})
    iss_rows, iss_schema = norm_iss_rows(pages)
    max_ts = None
    for r in iss_rows:
        if r["ts"] and (max_ts is None or r["ts"] > max_ts):
            max_ts = r["ts"]
    window_last_bar = WINDOW_END - timedelta(minutes=1)
    covered = max_ts is not None and max_ts >= window_last_bar
    if not covered:
        iss_load_issues.append({"problem": "window_not_covered",
                                "max_ts": max_ts.isoformat() if max_ts else None})
    if iss_problem is not None:
        iss_status = "INCOMPLETE"
        iss_complete = False
        iss_error = iss_problem
    elif iss_meta.get("status") != "ok":
        iss_status = iss_meta.get("status") or "SOURCE_UNAVAILABLE"
        iss_complete = False
        iss_error = iss_meta.get("error")
    elif iss_load_issues or iss_schema or not iss_meta.get("pagination_complete"):
        iss_status = "INCOMPLETE"
        iss_complete = False
        iss_error = iss_meta.get("error")
    else:
        iss_status = "ok"
        iss_complete = True
        iss_error = None
    iss_status_dict = {
        "status": iss_status, "load_complete": iss_complete,
        "error": iss_error,
        "n_pages": len(iss_refs),
        "fetched_at_utc": None if iss_problem else iss_meta.get("fetched_at_utc"),
        "schema_issues": iss_schema + iss_load_issues,
        "max_fetched_ts": max_ts.isoformat() if max_ts else None,
    }

    db_payload, db_problem = load_json_object(db_file)
    if db_problem is not None:
        db_rows, db_schema, db_status, db_complete = [], [db_problem], "INCOMPLETE", False
        db_fetched = None
    else:
        db_window = db_payload.get("rows")
        if not isinstance(db_window, list):
            db_rows, db_status, db_complete = [], "INCOMPLETE", False
            db_schema = [{"problem": "rows_not_a_list"}]
        else:
            db_rows, db_schema = norm_db_rows(db_window)
            if db_schema:
                db_status, db_complete = "INCOMPLETE", False
            else:
                db_status, db_complete = "ok", True
        provenance = db_payload.get("provenance")
        db_fetched = provenance.get("captured_at") if isinstance(provenance, dict) else None
    db_status_dict = {
        "status": db_status, "load_complete": db_complete,
        "n_rows": len(db_rows),
        "fetched_at_utc": db_fetched,
        "schema_issues": db_schema,
        "note": "сохранённый исторический срез A3, не живой запрос",
    }
    return (ti_rows, tinvest_status, iss_rows, iss_status_dict,
            db_rows, db_status_dict)


def build_comparison(input_dir: Path, tolerance: float) -> tuple[dict, dict]:
    tinvest_rows, tinvest_status, iss_rows, iss_status, db_rows, db_status = \
        load_raw(input_dir)
    sources_status = {"tinvest": tinvest_status, "iss": iss_status, "db": db_status}
    comparison = compare_sources(tinvest_rows, iss_rows, db_rows, tolerance)
    comparison["sources_status"] = sources_status
    comparison["overall_status"] = overall_load_status(sources_status)
    comparison["comparison_quality_status"] = comparison_quality_status(
        comparison, sources_status)
    comparison["source_sizes_raw"] = {
        "tinvest": len(tinvest_rows), "iss": len(iss_rows), "db": len(db_rows),
    }
    comparison["instrument"] = {
        "figi": FIGI, "ticker": TICKER, "board": BOARD, "interval": INTERVAL,
        "price_currency": CURRENCY,
        "session_comparability": (
            f"окно {WINDOW_MSK_TEXT} — основная дневная сессия MOEX; ISS-ряд по TQBR, "
            "T-Invest — основной режим; для этого окна торговые режимы сопоставимы, "
            "но это утверждение об окне, а не об идентичности выгрузок"
        ),
        "volume_comparable": VOLUME_UNITS_EVIDENCE["comparable"],
        "match_note": MATCH_NOTE,
    }
    focus_neighbors = [
        focus_row(comparison, FOCUS_TS + timedelta(minutes=d))
        for d in (-3, -2, -1, 1, 2, 3)
    ]
    comparison["focus_bar"] = {
        "ts": FOCUS_TS.isoformat(),
        "note": "аномалия A3: OHLC=1.0, volume=0 в сохранённой БД",
        "row": focus_row(comparison, FOCUS_TS),
        "neighbors": focus_neighbors,
    }
    if comparison["overall_status"] != "OK":
        comparison["task_status_note"] = (
            "загрузка источников неполна/недоступна: тройная сверка не может считаться "
            "успешной по оставшейся паре; допустимая сдача — рабочий offline + "
            "корректный INCOMPLETE с сохранённой причиной, без ложного MATCH"
        )
    return comparison, sources_status


def render_report(comparison: dict, out_dir: Path, input_dir: Path, mode: str) -> str:
    lines = []
    lines.append(f"# A5 сверка источников свечей — {TICKER} {WINDOW_START:%Y-%m-%d} "
                 f"{WINDOW_START:%H:%M}–{WINDOW_END:%H:%M} UTC ({WINDOW_MSK_TEXT})")
    lines.append("")
    lines.append(f"- overall_status (загрузка): **{comparison['overall_status']}**")
    lines.append(f"- comparison_quality_status (данные): "
                 f"**{comparison['comparison_quality_status']}**")
    lines.append("  - CLEAN = только структурная валидность (нет INVALID/дублей/ошибок "
                 "схемы); это НЕ отсутствие PRICE_MISMATCH/MISSING_IN_SOURCE/"
                 "NOT_COMPARABLE — они считаются отдельно в counts")
    lines.append(f"- mode: `{mode}`; input raw: `{input_dir / 'raw'}`; "
                 f"выходы: `{out_dir}`")
    lines.append(f"- union timestamps: {comparison['row_count']}")
    lines.append(f"- time label: {comparison['policy']['time_label']}")
    lines.append(f"- price tolerance (abs): {comparison['policy']['price_tolerance_abs']}")
    lines.append(f"- {MATCH_NOTE}")
    lines.append("")
    lines.append("## Источники (загрузка и качество)")
    lines.append("")
    lines.append("| источник | load | строк в окне | raw строк | schema/issues | fetched_at |")
    lines.append("|---|---|---|---|---|---|")
    for s in SOURCE_ORDER:
        st = comparison["sources_status"].get(s, {})
        size = comparison["source_sizes_in_window"].get(s, 0)
        raw_size = comparison["source_sizes_raw"].get(s, 0)
        n_iss = len(st.get("schema_issues") or [])
        lines.append(
            f"| {s} | {st.get('status')} | {size} | {raw_size} | {n_iss} | "
            f"{st.get('fetched_at_utc') or '-'} |"
        )
    for s in SOURCE_ORDER:
        st = comparison["sources_status"].get(s, {})
        if st.get("error"):
            lines.append(f"\n{s} error: `{json.dumps(st['error'], ensure_ascii=False)}`")
        for issue in (st.get("schema_issues") or [])[:5]:
            lines.append(f"\n{s} issue: `{json.dumps(issue, ensure_ascii=False)}`")
    lines.append("")
    lines.append("## Counts по категориям")
    lines.append("")
    lines.append(f"- primary: {json.dumps(comparison['counts']['primary'], ensure_ascii=False)}")
    lines.append(f"- by_category (строка может попасть в несколько): "
                 f"{json.dumps(comparison['counts']['by_category'], ensure_ascii=False)}")
    lines.append(f"- by_reason: "
                 f"{json.dumps(comparison['counts']['by_reason'], ensure_ascii=False)}")
    lines.append(f"- exact_pairwise_rows (≥2 сравнимые стороны, все дельты = 0): "
                 f"{comparison['counts']['exact_pairwise_rows']}")
    lines.append(f"- exact_3way_rows (все 3 сравнимые стороны, все дельты = 0): "
                 f"{comparison['counts']['exact_3way_rows']}")
    lines.append(f"- quality: {json.dumps(comparison['quality'], ensure_ascii=False)}")
    lines.append("")
    lines.append("## Coverage (union timestamps)")
    lines.append("")
    lines.append("| источник | покрыто | из | доля |")
    lines.append("|---|---|---|---|")
    for s in SOURCE_ORDER:
        cov = comparison["coverage"][s]
        share = f"{cov['share']:.3f}" if cov["share"] is not None else "-"
        lines.append(f"| {s} | {cov['present']} | {cov['union']} | {share} |")
    lines.append("")
    lines.append("## Фокус-бар 12:47Z и соседи")
    lines.append("")
    lines.append("| ts | источник | open | high | low | close | volume | categories |")
    lines.append("|---|---|---|---|---|---|---|---|")
    focus = comparison.get("focus_bar", {}).get("row")
    focus_neighbors = comparison.get("focus_bar", {}).get("neighbors") or []
    for row in [focus] + focus_neighbors:
        if row is None:
            continue
        for s in SOURCE_ORDER:
            v = row["values"][s]
            cell = (f"{v['open']} | {v['high']} | {v['low']} | {v['close']} | {v['volume']}"
                    if v else "MISSING | - | - | - | -")
            lines.append(f"| {row['ts']} | {s} | {cell} | {','.join(row['categories'])} |")
        lines.append(f"| | *primary* | | | | | | **{row['primary_category']}** |")
    if focus and focus["price_deltas"]:
        lines.append("")
        lines.append(f"price_deltas фокус-бара: "
                     f"`{json.dumps(focus['price_deltas'], ensure_ascii=False)}`")
    lines.append("")
    lines.append("## Volume: единицы")
    lines.append("")
    lines.append(f"- {VOLUME_UNITS_EVIDENCE['verdict']}")
    lines.append(f"- tinvest: {VOLUME_UNITS_EVIDENCE['tinvest']['status']} — "
                 f"{VOLUME_UNITS_EVIDENCE['tinvest']['doc']}")
    lines.append(f"- iss: {VOLUME_UNITS_EVIDENCE['iss']['status']} — "
                 f"{VOLUME_UNITS_EVIDENCE['iss']['doc']}")
    lines.append(f"- db: {VOLUME_UNITS_EVIDENCE['db']['status']} — "
                 f"{VOLUME_UNITS_EVIDENCE['db']['doc']}")
    lines.append(f"- lot_size_on_date: {VOLUME_UNITS_EVIDENCE['lot_size_on_date']['status']}; "
                 f"tick_size_on_date: {VOLUME_UNITS_EVIDENCE['tick_size_on_date']['status']}"
                 f" → дельты в тиках не считаются")
    lines.append("")
    lines.append("## Что показывает отчёт")
    lines.append("")
    lines.append("- Отчёт показывает, что каждый источник отдаёт СЕЙЧАС против сохранённой "
                 "БД; совпадение двух источников — основание предложить отдельное "
                 "исправление, НЕ автоматическая замена и НЕ доказательство "
                 "исторического происхождения записи (A3 origin=NOT_VERIFIED).")
    lines.append("- Будущая точка подключения к CandleHub preflight: сверка ДО "
                 "merge/upsert с сохранением provenance и конфликтов каждой строки, "
                 "отчёт как артефакт перед записью; интеграция в A5 НЕ выполняется.")
    lines.append("- Объёмные значения не сопоставимы; VOLUME_MISMATCH не наступает по "
                 "построению, не потому что объёмы совпали. MATCH не означает совпадение "
                 "объёмов.")
    if comparison["overall_status"] != "OK":
        lines.append("")
        lines.append(f"**INCOMPLETE:** {comparison.get('task_status_note')}")
    if comparison["comparison_quality_status"] != "CLEAN":
        lines.append("")
        lines.append("**QUALITY ISSUES:** см. counts.by_reason и quality выше — "
                     "INVALID/дубли/проблемы схемы сохранены в comparison.json "
                     "(details с raw-index записей), ничего не схлопывается молча.")
    lines.append("")
    return "\n".join(lines)


OUTPUT_NAMES = ("comparison.json", "report.md", "manifest.json")


def refuse_existing_outputs(out_dir: Path) -> None:
    existing = [n for n in OUTPUT_NAMES if (out_dir / n).exists()]
    if existing:
        raise OutDirNotEmpty(
            f"output files already exist in {out_dir}: {', '.join(existing)}; "
            "выходы не перезаписываются — укажите новый --out")


def write_outputs(out_dir: Path, comparison: dict, mode: str,
                  input_dir: Path | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    refuse_existing_outputs(out_dir)
    comparison_file = out_dir / "comparison.json"
    comparison_file.write_text(json.dumps(
        comparison, sort_keys=True, ensure_ascii=False, indent=1, default=str))
    report = render_report(comparison, out_dir, input_dir or out_dir, mode)
    report_file = out_dir / "report.md"
    report_file.write_text(report)

    raw_dir = (input_dir or out_dir) / "raw"
    inputs = {}
    for p in sorted(raw_dir.glob("*.json")):
        inputs[p.name] = sha256_file(p)
    manifest = {
        "generator": Path(__file__).name,
        "mode": mode,
        "ts_utc": utc_now_iso(),
        "input_dir": str(input_dir) if input_dir else None,
        "raw_origin": ("collect в этом каталоге" if input_dir is None
                       else "собранный raw из --input (неизменяем)"),
        "instrument": comparison.get("instrument"),
        "policy": comparison["policy"],
        "sha256_script": sha256_file(Path(__file__).resolve()),
        "sha256_policy": hashlib.sha256(json.dumps(
            comparison["policy"], sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
        "sha256_inputs": inputs,
        "sha256_comparison": sha256_file(comparison_file),
        "sha256_report": sha256_file(report_file),
        "overall_status": comparison["overall_status"],
        "comparison_quality_status": comparison["comparison_quality_status"],
        "determinism_note": "comparison.json — функция raw-входов (--input) и policy; "
                            "повторный offline-run с тем же --input даёт тот же sha256",
    }
    (out_dir / "manifest.json").write_text(json.dumps(
        manifest, ensure_ascii=False, indent=1, default=str))
    print(f"comparison: {comparison_file}")
    print(f"report:     {report_file}")
    print(f"manifest:   {out_dir / 'manifest.json'}")
    print(f"sha256_comparison: {manifest['sha256_comparison']}")
    print(f"overall_status(load): {comparison['overall_status']}")
    print(f"comparison_quality_status: {comparison['comparison_quality_status']}")
    for s in SOURCE_ORDER:
        st = comparison["sources_status"].get(s, {})
        print(f"  {s}: {st.get('status')}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="compare_candle_sources.py",
        description="A5-C1 r3: сверка SBER 1m — T-Invest ↔ MOEX ISS ↔ сохранённая БД "
                    "(A3 db_probe). Offline не перезаписывает выходы: --input + новый --out. "
                    "Malformed raw → INCOMPLETE с кодом (без traceback). T-Invest вызов — "
                    "в дочернем процессе с wait/terminate/kill/join.",
        epilog=(
            "examples:\n"
            "  # offline (default): чтение raw из --input, выходы в НОВЫЙ --out\n"
            "  python scripts/compare_candle_sources.py \\\n"
            "      --input <collected_artifact_dir> --out <new_out_dir>\n"
            "  # collect: ограниченный read-only сбор в свежий --out + сравнение\n"
            "  python scripts/compare_candle_sources.py --mode collect --out <fresh_dir>\n"
            "\n"
            "exit codes: 0 = загрузка OK; 2 = SOURCE_UNAVAILABLE/INCOMPLETE (и "
            "usage-ошибки argparse); 3 = missing raw / --out содержит выходы / "
            "не заданы --input/--out"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mode", choices=("offline", "collect"), default="offline",
                   help="offline — сохранённые файлы (default); collect — read-only "
                        "сеть (SBER 1m, 2026-09-08 12:30–13:10Z)")
    p.add_argument("--input", type=Path, default=None,
                   help="каталог собранного артефакта с raw/ (обязателен в offline)")
    p.add_argument("--out", type=Path, default=None,
                   help="каталог для выходов (comparison/report/manifest); в collect "
                        "также сюда пишется raw; выходы никогда не перезаписываются; "
                        "default в collect: <repo>/backend/reports/architect_20261008/"
                        "b1/a5/<utc-stamp>")
    p.add_argument("--price-tolerance", type=float, default=DEFAULT_PRICE_TOLERANCE,
                   help=f"abs-порог цены, фиксируется ДО просмотра результата "
                        f"(default {DEFAULT_PRICE_TOLERANCE})")
    p.add_argument("--facts", type=Path, default=DEFAULT_FACTS,
                   help="путь к сохранённому A3 facts.json (только collect; "
                        "третья сторона сверки, живая БД не читается)")
    return p


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "--_tinvest-worker":
        if len(raw_argv) != 2:
            return 3
        return _tinvest_worker_main(Path(raw_argv[1]))
    args = build_parser().parse_args(raw_argv)
    if not math.isfinite(args.price_tolerance) or args.price_tolerance < 0:
        print("error: --price-tolerance must be a finite number >= 0", file=sys.stderr)
        return 2
    tolerance = args.price_tolerance

    if args.mode == "offline":
        if args.input is None or args.out is None:
            print("error: offline requires --input <collected_artifact_dir> "
                  "and --out <new_out_dir>", file=sys.stderr)
            return 3
        input_dir = Path(args.input)
        out_dir = Path(args.out)
        if not (input_dir / "raw").is_dir():
            print(f"error: --input has no raw/ directory: {input_dir}", file=sys.stderr)
            return 3
        try:
            refuse_existing_outputs(out_dir)
        except OutDirNotEmpty as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
    else:
        input_dir = None
        out_dir = args.out
        if out_dir is None:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            out_dir = DEFAULT_A5_DIR / stamp
        out_dir = Path(out_dir)
        if out_dir.exists():
            try:
                refuse_existing_outputs(out_dir)
            except OutDirNotEmpty as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 3

    try:
        if args.mode == "collect":
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "raw").mkdir(exist_ok=True)
            print(f"[collect] out: {out_dir}")
            db_meta = write_db_raw(out_dir, Path(args.facts))
            print(f"[collect] db_probe_window: {db_meta['sha256'][:12]}…")
            iss_meta = collect_iss(out_dir)
            print(f"[collect] iss: {iss_meta['status']} pages={len(iss_meta['pages'])} "
                  f"complete={iss_meta['pagination_complete']}")
            ti_meta = collect_tinvest(out_dir)
            print(f"[collect] tinvest: {ti_meta['status']} n={ti_meta['n_candles']}")
            input_dir = out_dir

        comparison, _ = build_comparison(input_dir, tolerance)
        write_outputs(out_dir, comparison, args.mode,
                      None if args.mode == "collect" else input_dir)
    except MissingRawInputs as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except OutDirNotEmpty as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    return 0 if comparison["overall_status"] == "OK" else 2


if __name__ == "__main__":
    sys.exit(main())
