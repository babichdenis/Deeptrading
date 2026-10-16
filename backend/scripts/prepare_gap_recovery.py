#!/usr/bin/env python3
"""A23: подготовка ограниченной сверки пропусков. Два этапа в одном скрипте.

  plan    — Этап 1: из принятого A19 (recovery_request.json) строит request_plan.json
            с ровно 89 уникальными минутами, FIGI/ticker, UTC half-open интервалами
            (±5min, слияние пересекающихся внутри ticker/day), приоритетом трёх VKCO
            и локальными hashes. Для каждого окна — argv существующего A5 ЛИБО точное
            недостающее соответствие CLI. Command-массивы НЕ выполняются.

  consume — Этап 2: offline intake. Читает явно сохранённые responses + manifest/hash,
            сверяет с планом, выдаёт comparison.jsonl (exists/absent/unavailable/
            not_comparable/invalid_input) с provenance. Нормализацию цен/времени
            переиспользует из A5 (compare_candle_sources), не дублирует.

Сеть/suites/реальные загрузки/исправления CSV/DB/пересчёт стратегий — НЕТ.
A5/A10/A12/A19 — только read-only. .env/секреты в вывод не попадают.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MSK = timezone(timedelta(hours=3))
UTC = timezone.utc
WINDOW_PAD_MIN = 5
EXPECTED_INTERVAL = "1min"
PILOT_TICKER = "VKCO"
PILOT_DAYS = ("2026-07-10", "2026-08-05", "2026-08-06")
TICKERS = ["SBER", "GAZP", "LKOH", "VKCO", "SFIN"]


def find_backend() -> Path:
    for p in [HERE, *HERE.parents]:
        if (p / "scripts" / "compare_candle_sources.py").is_file():
            return p
    raise SystemExit("BLOCKED: backend не найден")


BACKEND = find_backend()
A19 = BACKEND / "reports/architect_20261010/b1/a19/20261010T210630Z"
A10 = BACKEND / "reports/architect_20261009/b1/a10/20261009T213649Z/csv"
A5_PATH = BACKEND / "scripts/compare_candle_sources.py"

A5_CONSTANTS = {
    "FIGI": "BBG004730N88", "TICKER": "SBER", "BOARD": "TQBR", "INTERVAL": "1min",
    "WINDOW_START": "2026-09-08T12:30:00+00:00", "WINDOW_END": "2026-09-08T13:10:00+00:00",
    "ISS_DAY": "2026-09-08", "ISS_MAX_PAGES": 10, "HTTP_TIMEOUT_S": 20, "HTTP_RETRIES": 2,
    "TINVEST_TIMEOUT_S": 20, "TINVEST_ATTEMPTS": 2,
}


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_a5():
    """Загрузить A5 как модуль (read-only) для переиспользования offline-функций."""
    spec = importlib.util.spec_from_file_location("a5_compare", A5_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def figi_by_ticker() -> dict:
    """FIGI из A10 raw (read-only, первая строка данных)."""
    out = {}
    for t in TICKERS:
        with (A10 / f"{t}.csv").open(encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            first = next(r)
            out[t] = first["figi"]
    return out


def parse_utc(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp forbidden: {s}")
    return dt.astimezone(UTC)


def build_windows(missing_by_ticker: dict) -> list:
    """Окна ±5min вокруг каждой недостающей минуты, half-open [start,end),
    слияние пересекающихся/смежных внутри ticker/day. Без расширения до истории.
    """
    raw = []
    for t in sorted(missing_by_ticker):
        for ms in sorted(missing_by_ticker[t]):
            ts = parse_utc(ms)
            start = ts - timedelta(minutes=WINDOW_PAD_MIN)
            end = ts + timedelta(minutes=WINDOW_PAD_MIN + 1)
            raw.append({"ticker": t, "target": ts, "start": start, "end": end})
    grouped = {}
    for w in raw:
        day = w["start"].astimezone(MSK).date().isoformat()
        grouped.setdefault((w["ticker"], day), []).append(w)
    windows = []
    for (t, day) in sorted(grouped):
        items = sorted(grouped[(t, day)], key=lambda x: (x["start"], x["end"]))
        merged = []
        for w in items:
            if merged and w["start"] <= merged[-1]["end"]:
                merged[-1]["end"] = max(merged[-1]["end"], w["end"])
                merged[-1]["targets"].append(w["target"])
            else:
                merged.append({"start": w["start"], "end": w["end"],
                               "targets": [w["target"]]})
        for i, m in enumerate(merged):
            windows.append({
                "window_id": f"{t}__{day}__w{i:02d}",
                "ticker": t, "day_msk": day,
                "interval_utc_half_open": [m["start"].isoformat(), m["end"].isoformat()],
                "target_minutes_utc": [x.isoformat() for x in sorted(m["targets"])],
                "n_targets": len(m["targets"]),
                "n_minute_bars_in_window": int((m["end"] - m["start"]).total_seconds() // 60),
            })
    return windows


def a5_argv_for_window(w: dict, figi: str, out_sub: str) -> dict:
    """argv существующего A5 ЛИБО точное недостающее соответствие CLI.

    A5 collect захардкожен константами модуля (FIGI/TICKER/BOARD/ISS_DAY/
    WINDOW_START/WINDOW_END/INTERVAL) и НЕ принимает их через CLI; offline-режим
    читает сохранённый --input + новый --out. Поэтому per-window argv собирается
    для offline-режима (если raw уже собран), а для collect описывается точное
    недостающее соответствие CLI (какие константы нужно параметризовать).
    """
    iss_day = w["day_msk"]
    offline_argv = [
        sys.executable, str(A5_PATH),
        "--mode", "offline",
        "--input", "<collected_artifact_dir_for_" + w["window_id"] + ">",
        "--out", out_sub,
    ]
    collect_missing = [
        {"constant": "TICKER", "a5_value": A5_CONSTANTS["TICKER"], "needed": w["ticker"],
         "cli_flag": None, "note": "модульная константа, CLI-параметра нет"},
        {"constant": "FIGI", "a5_value": A5_CONSTANTS["FIGI"], "needed": figi,
         "cli_flag": None, "note": "модульная константа, CLI-параметра нет"},
        {"constant": "ISS_DAY", "a5_value": A5_CONSTANTS["ISS_DAY"], "needed": iss_day,
         "cli_flag": None, "note": "модульная константа, CLI-параметра нет"},
        {"constant": "WINDOW_START", "a5_value": A5_CONSTANTS["WINDOW_START"],
         "needed": w["interval_utc_half_open"][0], "cli_flag": None,
         "note": "модульная константа, CLI-параметра нет"},
        {"constant": "WINDOW_END", "a5_value": A5_CONSTANTS["WINDOW_END"],
         "needed": w["interval_utc_half_open"][1], "cli_flag": None,
         "note": "модульная константа, CLI-параметра нет"},
        {"constant": "BOARD", "a5_value": A5_CONSTANTS["BOARD"], "needed": A5_CONSTANTS["BOARD"],
         "cli_flag": None, "note": "TQBR предполагается для всех пяти тикеров — проверить"},
    ]
    return {
        "window_id": w["window_id"],
        "offline_argv": offline_argv,
        "offline_argv_executable_now": False,
        "offline_argv_note": "выполняется только когда raw для этого окна уже сохранён в --input",
        "collect_argv": None,
        "collect_cli_supported": False,
        "collect_missing_cli_correspondence": collect_missing,
    }


def cmd_plan(args) -> int:
    rec_path = A19 / "recovery_request.json"
    rec = json.loads(rec_path.read_text(encoding="utf-8"))
    missing_by_ticker = {}
    for req in rec["requests"]:
        missing_by_ticker[req["instrument"]] = sorted(req["missing_session_minutes_utc"])
    n_total = sum(len(v) for v in missing_by_ticker.values())
    if n_total != 89:
        print(f"BLOCKED: ожидалось 89 уникальных минут, получено {n_total}", file=sys.stderr)
        return 2

    figi = figi_by_ticker()
    windows = build_windows(missing_by_ticker)
    pilot_windows = [w for w in windows
                     if w["ticker"] == PILOT_TICKER and w["day_msk"] in PILOT_DAYS]
    pilot_days = sorted({w["day_msk"] for w in pilot_windows})

    # число вызовов: ISS — по distinct (ticker, day) [from=till=day возвращает весь день];
    # T-Invest GetCandles — по окну (from/to параметризуемы).
    iss_calls = len({(w["ticker"], w["day_msk"]) for w in windows})
    iss_calls_pilot = len({(w["ticker"], w["day_msk"]) for w in pilot_windows})
    tinv_calls = len(windows)
    tinv_calls_pilot = len(pilot_windows)

    iss_max_pages = A5_CONSTANTS["ISS_MAX_PAGES"]
    http_to, http_retries = A5_CONSTANTS["HTTP_TIMEOUT_S"], A5_CONSTANTS["HTTP_RETRIES"]
    tinv_to, tinv_att = A5_CONSTANTS["TINVEST_TIMEOUT_S"], A5_CONSTANTS["TINVEST_ATTEMPTS"]
    iss_worst_s = iss_calls * iss_max_pages * http_to * (http_retries + 1)
    tinv_worst_s = tinv_calls * tinv_to * (tinv_att + 1)
    max_wall_s = iss_worst_s + tinv_worst_s

    out_windows = []
    for w in windows:
        argv = a5_argv_for_window(w, figi[w["ticker"]],
                                  f"out/{w['window_id']}")
        out_windows.append({**w, "figi": figi[w["ticker"]],
                            "pilot": w in pilot_windows, **argv})

    targets_flat = sorted({m for v in missing_by_ticker.values() for m in v})
    plan = {
        "task": "A23 этап 1 — plan: ограниченный read-only сбор для сверки пропусков A19",
        "builder": "Builder-1",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "network_executed": False,
        "authorization_for_network": "NOT_GRANTED — план готов к ОГРАНИЧЕННОМУ read-only сбору по отдельному разрешению; авторизации на сеть пока нет",
        "a19_source": {"path": str(rec_path.relative_to(BACKEND)),
                       "sha256": sha256_file(rec_path)},
        "a10_csv_sha256": {f"{t}.csv": sha256_file(A10 / f"{t}.csv") for t in TICKERS},
        "target_minutes": {
            "count_unique": len(targets_flat),
            "expected_89": len(targets_flat) == 89,
            "by_ticker": {t: len(v) for t, v in sorted(missing_by_ticker.items())},
            "minutes_utc": targets_flat,
        },
        "figi_by_ticker": figi,
        "window_rule": {
            "pad_min": WINDOW_PAD_MIN,
            "interval": "UTC half-open [target-5min, target+6min) — ±5min вокруг целевой минуты",
            "merge": "пересекающиеся/смежные окна сливаются внутри ticker/day; без расширения до всей истории",
        },
        "windows": out_windows,
        "n_windows": len(windows),
        "call_budget": {
            "iss": {"calls": iss_calls, "basis": "distinct (ticker, day): ISS from=till=day возвращает весь день",
                    "max_pages_per_call": iss_max_pages, "http_timeout_s": http_to,
                    "http_retries": http_retries, "worst_case_s": iss_worst_s},
            "tinvest": {"calls": tinv_calls, "basis": "один GetCandles (figi, from, to, 1min) на окно",
                        "timeout_s": tinv_to, "attempts": tinv_att, "worst_case_s": tinv_worst_s},
            "total_max_wall_time_s": max_wall_s,
            "total_max_wall_time_min": round(max_wall_s / 60, 1),
        },
        "pilot": {
            "scope": "только три VKCO даты (A19 deep-dive)",
            "ticker": PILOT_TICKER, "days_msk": pilot_days,
            "n_windows": len(pilot_windows),
            "window_ids": [w["window_id"] for w in pilot_windows],
            "iss_calls": iss_calls_pilot, "tinvest_calls": tinv_calls_pilot,
        },
        "comparability_rule": {
            "same_instrument_market_interval_session_only": True,
            "iss_no_comparable_1m": "NOT_COMPARABLE — не превращать 10m в вымышленные минуты",
            "interval_expected": EXPECTED_INTERVAL,
        },
        "a5_reference": {
            "path": str(A5_PATH.relative_to(BACKEND)),
            "sha256": sha256_file(A5_PATH),
            "collect_constants": A5_CONSTANTS,
            "collect_cli_supported": False,
            "note": "A5 collect захардкожен константами модуля; per-window сбор через CLI невозможен без параметризации (не моя зона — A5 read-only)",
        },
        "secrets_policy": ".env/секреты не читаются и не выводятся",
    }
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "request_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("windows:", len(windows), "| targets:", len(targets_flat), "| ==89:", len(targets_flat) == 89)
    print("iss_calls:", iss_calls, "| tinvest_calls:", tinv_calls,
          "| max_wall_s:", max_wall_s)
    print("pilot windows:", len(pilot_windows), "days:", pilot_days)
    return 0


def _status_for_targets(a5, targets: list, resp: dict, expected: dict) -> list:
    """Статус по каждой целевой минуте одного response, строгая сверка с планом.

    REWORK (архитектор, DATA intake):
    1) Явный fetch_status (без дефолта 'ok') и явная bool pagination_complete (без
       дефолта True). Отсутствие доказательства полной загрузки НЕ превращается в absent.
    2) Declared hash КАЖДОГО raw page/body (ISS) или payload (T-Invest) с совпадением;
       hash файла-обёртки не заменяет проверку полноты/хэшей страниц.
    3) Привязка response→план: source/window_id/ticker/figi/board(iss)/interval/UTC-окно.
       Бар другого инструмента/окна не может быть exists для target.
    4) Дубликаты: идентичные → дедуп с заметкой; конфликт OHLCV → invalid/conflict,
       НЕ exists/verified_present. Любая нормализационная потеря строк делает отсутствие
       недоказуемым → НЕ absent.
    """
    source = resp.get("source")
    interval = resp.get("interval")
    fetch_status = resp.get("fetch_status")                 # явный, без дефолта
    pagination_complete = resp.get("pagination_complete")   # явный, без дефолта

    prov = {
        "source": source, "window_id": resp.get("window_id"),
        "ticker": resp.get("ticker"), "figi": resp.get("figi"),
        "board": resp.get("board"), "fetch_status": fetch_status,
        "pagination_complete": pagination_complete, "interval": interval,
        "expected_ticker": expected.get("ticker"),
        "expected_figi": expected.get("figi"),
        "expected_window_utc_half_open": expected.get("interval_utc_half_open"),
    }

    def row(ts, status, reason, extra=None):
        d = {"target_minute_utc": ts, "status": status, "reason": reason, **prov}
        if extra:
            d.update(extra)
        return d

    # --- Привязка response → план (п.2/п.3) ---
    if source not in ("iss", "tinvest"):
        return [row(t, "invalid_input", f"unknown_source_{source}") for t in targets]
    if resp.get("window_id") != expected.get("window_id"):
        return [row(t, "invalid_input", "window_id_mismatch") for t in targets]
    if resp.get("ticker") != expected.get("ticker"):
        return [row(t, "invalid_input",
                    f"instrument_mismatch_got_{resp.get('ticker')}_expected_{expected.get('ticker')}")
                for t in targets]
    if resp.get("figi") != expected.get("figi"):
        return [row(t, "invalid_input", "figi_mismatch") for t in targets]
    if interval != EXPECTED_INTERVAL:
        return [row(t, "not_comparable", f"interval_{interval}_not_1min") for t in targets]
    if resp.get("window_utc_half_open") != expected.get("interval_utc_half_open"):
        return [row(t, "invalid_input", "window_utc_mismatch") for t in targets]
    if source == "iss" and resp.get("board") != A5_CONSTANTS["BOARD"]:
        return [row(t, "invalid_input", f"board_mismatch_got_{resp.get('board')}") for t in targets]

    # --- Явный успешный статус + доказанная полнота (п.1) ---
    if fetch_status is None:
        return [row(t, "invalid_input", "fetch_status_missing") for t in targets]
    if fetch_status != "ok":
        return [row(t, "unavailable", f"fetch_{fetch_status}") for t in targets]
    if pagination_complete is None:
        return [row(t, "invalid_input", "pagination_complete_missing") for t in targets]
    if not isinstance(pagination_complete, bool):
        return [row(t, "invalid_input", "pagination_complete_not_bool") for t in targets]
    if pagination_complete is False:
        return [row(t, "unavailable", "incomplete_pagination_not_empty_data") for t in targets]

    # --- Нормализация (через A5) + проверка declared hash страниц/payload (п.2) ---
    rows, issues, hash_problems = [], [], []
    try:
        if source == "iss":
            pages = resp.get("pages")
            if not isinstance(pages, list):
                return [row(t, "invalid_input", "pages_not_a_list") for t in targets]
            if not pages:
                return [row(t, "unavailable", "no_raw_pages_absence_unproven") for t in targets]
            for pi, page in enumerate(pages):
                body = page.get("body")
                declared = page.get("sha256")
                if declared is None:
                    hash_problems.append(f"page_{pi}_declared_hash_missing")
                elif isinstance(body, str) and \
                        hashlib.sha256(body.encode("utf-8")).hexdigest() != declared:
                    hash_problems.append(f"page_{pi}_hash_mismatch")
            rows, issues = a5.norm_iss_rows(pages)
        elif source == "tinvest":
            payload = resp.get("payload")
            if not isinstance(payload, dict):
                return [row(t, "invalid_input", "payload_not_an_object") for t in targets]
            declared = resp.get("payload_sha256")
            canon = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            if declared is None:
                hash_problems.append("payload_declared_hash_missing")
            elif hashlib.sha256(canon).hexdigest() != declared:
                hash_problems.append("payload_hash_mismatch")
            rows, issues = a5.norm_tinvest_rows(payload)
    except Exception as exc:  # повреждение ответа != пустые данные
        return [row(t, "invalid_input", f"normalize_error_{type(exc).__name__}") for t in targets]

    if hash_problems:
        return [row(t, "invalid_input", f"hash_{hash_problems[0]}") for t in targets]

    fatal = [i for i in issues if i.get("problem") in (
        "body_not_json", "candles_block_missing", "columns_schema_invalid",
        "data_not_a_list", "body_missing", "candles_not_a_list", "body_not_an_object")]
    if fatal:
        return [row(t, "invalid_input", f"fatal_{fatal[0]['problem']}") for t in targets]

    # Потери строк при нормализации → отсутствие цели недоказуемо (п.4): потерянная
    # строка МОГЛА быть этой минутой, поэтому НЕ превращаем в absent.
    row_loss = [i for i in issues if i.get("problem") in (
        "bad_begin", "short_or_bad_row", "bad_time", "candle_not_an_object",
        "bad_quotation_open", "bad_quotation_high", "bad_quotation_low", "bad_quotation_close")]

    # --- Индекс по ts с обработкой дубликатов (п.4) ---
    index = {}
    for r in rows:
        ts = r.get("ts")
        if ts is not None:
            index.setdefault(ts, []).append(r)

    out = []
    for t in targets:
        ts = parse_utc(t)
        matches = index.get(ts, [])
        if not matches:
            if row_loss:
                out.append(row(t, "invalid_input", "normalization_loss_absence_unconfirmed",
                               {"row_loss_issues": row_loss}))
            else:
                # явно полный ПУСТОЙ ответ: полнота доказана, хэши сошлись, потерь нет;
                # absent = SOURCE_ABSENT, причина UNKNOWN (НЕ доказательство no-trade)
                out.append(row(t, "absent", "SOURCE_ABSENT_reason_UNKNOWN"))
            continue
        first = matches[0]
        identical = all(a5.same_bar(first, m) for m in matches[1:])
        if not identical:
            versions = [{k: m.get(k) for k in ("open", "high", "low", "close", "volume")}
                        for m in matches]
            out.append(row(t, "invalid_input", "conflicting_duplicate_bars",
                           {"n_duplicates": len(matches), "versions": versions}))
            continue
        problems = a5.validate_bar(first)
        src_bar = {k: first.get(k) for k in ("open", "high", "low", "close", "volume")}
        extra = {"bar_valid": not problems, "bar_problems": problems, "source_bar": src_bar}
        if len(matches) > 1:
            extra["duplicate_note"] = f"{len(matches)} identical bars deduplicated"
        out.append(row(t, "exists", "candle_found", extra))
    return out


def cmd_consume(args) -> int:
    a5 = load_a5()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    resp_dir = Path(args.responses)
    manifest_path = resp_dir / "manifest.json"
    if not manifest_path.is_file():
        print(f"BLOCKED: нет manifest.json в {resp_dir}", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    listed = {e["file"]: e for e in manifest.get("responses", [])}

    # карта окно -> ожидаемое окно плана (для привязки response к плану, п.2/п.3)
    plan_windows = {w["window_id"]: w for w in plan["windows"]}
    win_targets = {w["window_id"]: w["target_minutes_utc"] for w in plan["windows"]}

    results = []
    seen_window_ids = set()
    for fname, meta in sorted(listed.items()):
        fpath = resp_dir / fname
        wid = meta.get("window_id")
        # лишние/неизвестные window id → invalid (не молча пропускать)
        if wid not in plan_windows:
            results.append({"target_minute_utc": None, "status": "invalid_input",
                            "reason": f"unknown_window_id_{wid}_not_in_plan",
                            "source": meta.get("source"), "window_id": wid})
            continue
        seen_window_ids.add(wid)
        if not fpath.is_file():
            for t in win_targets.get(wid, []):
                results.append({"target_minute_utc": t, "status": "invalid_input",
                                "reason": "response_file_missing",
                                "source": meta.get("source"), "window_id": wid})
            continue
        actual_sha = sha256_file(fpath)
        if actual_sha != meta.get("sha256"):
            for t in win_targets.get(wid, []):
                results.append({"target_minute_utc": t, "status": "invalid_input",
                                "reason": "manifest_hash_mismatch",
                                "source": meta.get("source"), "window_id": wid,
                                "declared_sha256": meta.get("sha256"),
                                "computed_sha256": actual_sha})
            continue
        resp = json.loads(fpath.read_text(encoding="utf-8"))
        targets = win_targets.get(wid, [])
        # привязка к плану: сверяем ticker/figi/interval/окно/источник внутри _status_for_targets
        results.extend(_status_for_targets(a5, targets, resp, plan_windows[wid]))

    # окна плана, на которые не пришёл ни один response (пилот/план): честно помечаем,
    # но это НЕ absent — это отсутствие самого ответа (unavailable: no_response_in_manifest)
    for wid in sorted(plan_windows):
        if wid not in seen_window_ids and args.strict_missing_response:
            for t in win_targets.get(wid, []):
                results.append({"target_minute_utc": t, "status": "unavailable",
                                "reason": "no_response_in_manifest", "window_id": wid})

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    jl = out / "comparison.jsonl"
    with jl.open("w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = {
        "task": "A23 этап 2 — consume: offline intake сохранённых responses",
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan_path": str(Path(args.plan)),
        "responses_dir": str(resp_dir),
        "n_results": len(results), "by_status": counts,
        "network_executed": False,
        "note": "absent => SOURCE_ABSENT, причина UNKNOWN; повреждение/неполная пагинация/ошибка ответа != пустые данные",
    }
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("results:", len(results), "| by_status:", counts)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="A23 подготовка сверки пропусков (plan/consume), без сети")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("plan", help="Этап 1: request_plan.json из A19")
    sp.add_argument("--out", required=True)
    sp.set_defaults(func=cmd_plan)
    sc = sub.add_parser("consume", help="Этап 2: offline intake сохранённых responses")
    sc.add_argument("--plan", required=True)
    sc.add_argument("--responses", required=True)
    sc.add_argument("--out", required=True)
    sc.add_argument("--strict-missing-response", action="store_true",
                    help="окна плана без response → unavailable(no_response_in_manifest), НЕ absent")
    sc.set_defaults(func=cmd_consume)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
