#!/usr/bin/env python3
"""A10: immutable выгрузка минутных серий и offline DQ для дневного исследования.

Два подрежима:
  export --out <NEW_DIR>   READ ONLY REPEATABLE READ из настоящей PostgreSQL (.7),
                           потоковая выгрузка 5 фиксированных тикеров/FIGI A8 в
                           CSV по тикеру; manifest с hash кода/SQL/CSV и статусом.
  dq     --run-dir <DIR>   Offline DQ ТОЛЬКО по сохранённым CSV (без БД): coverage
                           всех календарных дат окна и всех тикеров, дефектные
                           координаты, внутренние дыры, полнота warmup, ключевые
                           бары/минуты конфигураций; валидация чтения через A7
                           load_minute_csv (только чтение).

Окно [2026-06-08T00:00:00Z, 2026-10-09T00:00:00Z), interval=1, ts = начало
минуты, значения raw без округления/исправлений. Сессия A7 [10:00,18:45) MSK
= mods 600..1124 (максимум 525 уникальных минут). Сеть/ensure/writes/DDL/боты
запрещены. Пропущенные целиком даты отмечаются явно, календарь NOT_VERIFIED.
Известная аномалия SBER 2026-09-08T12:47Z сохраняется и помечается
KNOWN_ANOMALY_A5 без автоисправления.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import os
import platform
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("BLOCKED: asyncpg недоступен", file=sys.stderr)
    raise SystemExit(2)

UTC = timezone.utc
MSK = timezone(timedelta(hours=3))
TICKER_FIGI = (
    ("SBER", "BBG004730N88"),
    ("GAZP", "BBG004730RP0"),
    ("LKOH", "BBG004731032"),
    ("VKCO", "TCS00A106YF0"),
    ("SFIN", "BBG003LYCMB1"),
)
WINDOW_START = datetime(2026, 6, 8, tzinfo=UTC)
WINDOW_END = datetime(2026, 10, 9, tzinfo=UTC)
WARMUP_DAYS = 3
INTERVAL = 1
SESSION_MIN_MOD = 600
SESSION_MAX_MOD = 1124
BAR_LABEL_MIN = 600
BAR_LABEL_MAX = 1110
BAR_LABEL_STEP = 10
LIMIT_PER_TICKER = 200_000
LIMIT_TOTAL = 1_000_000
KNOWN_ANOMALY = ("SBER", "BBG004730N88", "2026-09-08T12:47:00+00:00")
FLAG_COUNTER = {"duplicate": "duplicates"}
STATEMENT_TIMEOUT = "15s"
LOCK_TIMEOUT = "2s"
CONNECT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 15.0
PREFETCH = 5000
CSV_FIELDS = ("ticker", "figi", "interval", "ts", "open", "high", "low", "close", "volume")

KEY_BARS = {"10:00": 600, "10:20": 620, "10:30": 630, "12:00": 720,
            "14:00": 840, "18:20": 1100, "18:30": 1110}
CONFIG_POINTS = {
    "S1-A": {"decision": "12:00", "entry": "12:10"},
    "S1-B": {"decision": "14:00", "entry": "14:10"},
    "S2-A": {"decision": "10:20", "entry": "10:30"},
    "S2-B": {"decision": "10:30", "entry": "10:40"},
}
TIME_EXIT = "18:30"
ENTRY_MINUTES = {"12:10", "14:10", "10:30", "10:40"}

EXPORT_SQL = """
SELECT ts, open, high, low, close, volume
FROM candles
WHERE figi = $1 AND interval = $2 AND ts >= $3 AND ts < $4
ORDER BY ts
"""

COUNT_SQL = """
SELECT count(*) FROM candles
WHERE figi = $1 AND interval = $2 AND ts >= $3 AND ts < $4
"""

IDENT_SQL = """
SELECT version() AS pg_version,
       current_database() AS db,
       current_user AS db_user,
       inet_server_addr()::text AS server_addr,
       inet_server_port() AS server_port,
       current_setting('transaction_read_only') AS tx_read_only,
       current_setting('statement_timeout') AS stmt_timeout,
       current_setting('lock_timeout') AS lock_timeout
"""


def load_env(path: Path) -> dict:
    env = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def msk_mod(ts: datetime) -> int:
    loc = ts.astimezone(MSK)
    return loc.hour * 60 + loc.minute


def msk_date(ts: datetime) -> date:
    return ts.astimezone(MSK).date()


def iso_z(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def window_dates() -> list[date]:
    """MSK-даты, чья сессия A7 [10:00,18:45) пересекает окно [start,end)."""
    day = msk_date(WINDOW_START)
    out = []
    while True:
        sess_start = datetime(day.year, day.month, day.day, 10, 0, tzinfo=MSK)
        sess_end = datetime(day.year, day.month, day.day, 18, 45, tzinfo=MSK)
        if sess_start >= WINDOW_END:
            break
        if sess_end > WINDOW_START:
            out.append(day)
        day += timedelta(days=1)
    return out


def execution_identity() -> dict:
    ident = {
        "execution_host": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
    }
    try:
        out = os.popen("ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null").read().strip()
        ident["execution_ip"] = out.split()[0] if out else ""
    except Exception:
        ident["execution_ip"] = ""
    is_server_7 = ident["execution_host"].startswith("MacBook-ProDen") or \
        ident["execution_ip"].startswith("192.168.1.7")
    ident["is_server_7"] = bool(is_server_7)
    return ident


class BlockedConnection(Exception):
    pass


class BlockedQuery(Exception):
    pass


async def export_ticker(conn, ticker: str, figi: str, csv_dir: Path) -> dict:
    total = await conn.fetchval(COUNT_SQL, figi, INTERVAL, WINDOW_START, WINDOW_END)
    over_limit = total > LIMIT_PER_TICKER
    out = csv_dir / f"{ticker}.csv"
    count = 0
    first = last = None
    status = "INCOMPLETE" if over_limit else "FULL_EXPORT"
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        async for row in conn.cursor(EXPORT_SQL, figi, INTERVAL, WINDOW_START,
                                     WINDOW_END, prefetch=PREFETCH):
            if count >= LIMIT_PER_TICKER:
                status = "INCOMPLETE"
                break
            ts = row["ts"].astimezone(UTC)
            if first is None:
                first = ts
            last = ts
            w.writerow((ticker, figi, INTERVAL, iso_z(ts),
                        str(row["open"]), str(row["high"]), str(row["low"]),
                        str(row["close"]), str(row["volume"])))
            count += 1
    return {"ticker": ticker, "figi": figi, "planned_rows": total,
            "written_rows": count, "over_per_ticker_limit": over_limit,
            "min_ts_utc": iso_z(first) if first else None,
            "max_ts_utc": iso_z(last) if last else None,
            "status": status}


async def run_export(args) -> int:
    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        print(f"BLOCKED: --out {out} уже существует и не пуст", file=sys.stderr)
        return 2
    env = load_env(args.env)
    required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_DB")
    missing = [k for k in required if not env.get(k)]
    if missing:
        print(f"BLOCKED_ENV: нет ключей {missing}", file=sys.stderr)
        return 2

    ident = execution_identity()
    db_host = env["POSTGRES_HOST"]
    local_host = db_host in ("127.0.0.1", "localhost", "::1")
    if local_host and not ident["is_server_7"]:
        print("BLOCKED: localhost-БД допустим только при исполнении на сервере .7",
              file=sys.stderr)
        return 2

    try:
        conn = await asyncpg.connect(
            host=db_host, port=int(env["POSTGRES_PORT"]), user=env["POSTGRES_USER"],
            password=env["POSTGRES_PASSWORD"], database=env["POSTGRES_DB"],
            timeout=CONNECT_TIMEOUT, command_timeout=COMMAND_TIMEOUT,
            server_settings={"statement_timeout": STATEMENT_TIMEOUT,
                             "lock_timeout": LOCK_TIMEOUT,
                             "default_transaction_read_only": "on"})
    except Exception as e:
        code = getattr(e, "sqlstate", None) or type(e).__name__
        print(f"BLOCKED_CONNECTION: {code}: {e}", file=sys.stderr)
        return 3

    csv_dir = out / "csv"
    csv_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": "day_data_export.py",
        "mode": "export",
        "db": {"host": db_host, "port": int(env["POSTGRES_PORT"]),
               "database": env["POSTGRES_DB"]},
        "execution": ident,
        "window": {"start_utc": iso_z(WINDOW_START), "end_utc_exclusive": iso_z(WINDOW_END),
                   "interval": INTERVAL, "warmup_calendar_days": WARMUP_DAYS},
        "sql": {"count": COUNT_SQL, "export": EXPORT_SQL},
        "code_sha256": sha256_file(Path(__file__).resolve()),
        "limits": {"per_ticker": LIMIT_PER_TICKER, "total": LIMIT_TOTAL},
        "tickers": [],
        "outputs": {},
        "status": "INCOMPLETE",
    }
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            ident_db = await conn.fetchrow(IDENT_SQL)
            manifest["db_identity"] = {
                "pg_version": ident_db["pg_version"],
                "database": ident_db["db"],
                "user": ident_db["db_user"],
                "server_addr": ident_db["server_addr"],
                "server_port": ident_db["server_port"],
                "transaction_read_only": ident_db["tx_read_only"],
                "statement_timeout": ident_db["stmt_timeout"],
                "lock_timeout": ident_db["lock_timeout"],
            }
            if ident_db["tx_read_only"] != "on":
                raise BlockedQuery("transaction_read_only != on")
            total_written = 0
            for ticker, figi in TICKER_FIGI:
                try:
                    res = await export_ticker(conn, ticker, figi, csv_dir)
                except Exception as e:
                    code = getattr(e, "sqlstate", None) or type(e).__name__
                    res = {"ticker": ticker, "figi": figi, "error": f"{code}: {e}",
                           "status": "INCOMPLETE"}
                manifest["tickers"].append(res)
                total_written += res.get("written_rows", 0)
                if res.get("status") != "FULL_EXPORT":
                    manifest["status"] = "INCOMPLETE"
                if total_written > LIMIT_TOTAL:
                    manifest["status"] = "INCOMPLETE"
                    manifest["total_limit_exceeded"] = True
                    break
            if total_written <= LIMIT_TOTAL and all(
                    t.get("status") == "FULL_EXPORT" for t in manifest["tickers"]):
                manifest["status"] = "FULL_EXPORT"
            manifest["total_written_rows"] = total_written
    except BlockedQuery as e:
        (out / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8")
        print(f"BLOCKED_QUERY: {e}", file=sys.stderr)
        return 4
    finally:
        await conn.close()

    for ticker, _ in TICKER_FIGI:
        p = csv_dir / f"{ticker}.csv"
        if p.exists():
            manifest["outputs"][f"csv/{ticker}.csv"] = sha256_file(p)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    print(f"A10 export done: {out} status={manifest['status']} rows={manifest.get('total_written_rows')}")
    return 0 if manifest["status"] == "FULL_EXPORT" else 5


def parse_float(raw: str):
    s = (raw or "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def run_dq(args) -> int:
    source: Path = args.source_run_dir.resolve()
    out: Path = args.out.resolve()
    csv_dir = source / "csv"
    if not csv_dir.is_dir():
        print(f"BLOCKED: нет каталога исходных CSV {csv_dir}", file=sys.stderr)
        return 2
    if out.exists() and any(out.iterdir()):
        print(f"BLOCKED: --out {out} уже существует и не пуст "
              f"(immutable-каталог r2)", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)

    source_manifest_path = source / "manifest.json"
    source_manifest_sha = sha256_file(source_manifest_path) \
        if source_manifest_path.exists() else None
    source_export_code_sha = None
    if source_manifest_path.exists():
        try:
            source_export_code_sha = json.loads(
                source_manifest_path.read_text(encoding="utf-8")).get("code_sha256")
        except Exception:
            source_export_code_sha = None

    dates = window_dates()
    days_set = set(dates)
    warmup_dates = set(dates[:WARMUP_DAYS])

    cov_index: dict = {}
    for ticker, figi in TICKER_FIGI:
        for d in dates:
            cov_index[(ticker, d)] = {
                "ticker": ticker, "figi": figi, "day_msk": d.isoformat(),
                "weekday": d.strftime("%a"), "is_weekend": d.weekday() >= 5,
                "in_warmup": d in warmup_dates, "present": False,
                "rows_total": 0, "session_unique_minutes": 0,
                "full_10min_bars": 0,
                "duplicates": 0, "null_field": 0, "nonfinite": 0,
                "nonpositive_price": 0, "broken_ohlc": 0, "negative_volume": 0,
                "zero_volume": 0, "off_minute": 0, "known_anomaly": 0,
                "_session_mods": set(), "_bucket_mods": {},
            }

    issue_rows = []
    per_csv = {}
    for ticker, figi in TICKER_FIGI:
        p = csv_dir / f"{ticker}.csv"
        if not p.exists():
            per_csv[ticker] = {"present": False, "sha256": None, "rows": 0}
            continue
        per_csv[ticker] = {"present": True, "sha256": sha256_file(p), "rows": 0}
        seen = {}
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for lineno, row in enumerate(reader, start=2):
                per_csv[ticker]["rows"] += 1
                t = (row.get("ticker") or "").strip()
                raw_ts = (row.get("ts") or "").strip()
                coords = {"ticker": t or ticker, "ts": raw_ts, "lineno": lineno}
                try:
                    ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        raise ValueError("naive")
                    ts = ts.astimezone(UTC)
                except ValueError:
                    issue_rows.append({**coords, "code": "ts_unparsable"})
                    continue
                d = msk_date(ts)
                if d not in days_set:
                    issue_rows.append({**coords, "code": "date_not_in_window_dates",
                                       "day_msk": d.isoformat()})
                    continue
                cov = cov_index[(t, d)]
                cov["present"] = True
                mod = msk_mod(ts)
                flags = []
                if ts.second != 0 or ts.microsecond != 0:
                    flags.append("off_minute")
                vals = {}
                for name in ("open", "high", "low", "close", "volume"):
                    val = parse_float(row.get(name, ""))
                    if val is None:
                        flags.append("null_field")
                    vals[name] = val
                o, h, l, c, v = (vals["open"], vals["high"], vals["low"],
                                 vals["close"], vals["volume"])
                if all(x is not None for x in (o, h, l, c, v)):
                    if not all(math.isfinite(x) for x in (o, h, l, c, v)):
                        flags.append("nonfinite")
                    else:
                        if min(o, h, l, c) <= 0:
                            flags.append("nonpositive_price")
                        if h < l or h < max(o, c) or l > min(o, c):
                            flags.append("broken_ohlc")
                        if v < 0:
                            flags.append("negative_volume")
                        if v == 0:
                            flags.append("zero_volume")
                key = (t, ts)
                if key in seen:
                    flags.append("duplicate")
                seen[key] = True
                if (t, figi, iso_z(ts)) == KNOWN_ANOMALY:
                    flags.append("known_anomaly")
                for fl in sorted(set(flags)):
                    cnt = FLAG_COUNTER.get(fl, fl)
                    cov[cnt] = cov.get(cnt, 0) + 1
                cov["rows_total"] += 1
                if len(flags) > 0:
                    issue_rows.append({**coords, "code": "|".join(sorted(set(flags))),
                                       "day_msk": d.isoformat(), "mod_msk": mod})
                on_grid = ts.second == 0 and ts.microsecond == 0
                if on_grid and SESSION_MIN_MOD <= mod <= SESSION_MAX_MOD:
                    cov["_session_mods"].add(mod)
                    lbl = mod - (mod % BAR_LABEL_STEP)
                    if BAR_LABEL_MIN <= lbl <= BAR_LABEL_MAX:
                        cov["_bucket_mods"].setdefault(lbl, set()).add(mod)

    for cov in cov_index.values():
        cov["session_unique_minutes"] = len(cov["_session_mods"])
        cov["full_10min_bars"] = sum(1 for mins in cov["_bucket_mods"].values()
                                     if len(mins) == 10)
        for hhmm, mod in KEY_BARS.items():
            cov[f"bar_{hhmm.replace(':', '')}"] = "yes" if (
                len(cov["_bucket_mods"].get(mod, set())) == 10) else "no"
        for name, pts in CONFIG_POINTS.items():
            dec_hhmm = pts["decision"]
            dec_mod = int(dec_hhmm[:2]) * 60 + int(dec_hhmm[3:])
            cov[f"{name}_decision_minute_{dec_hhmm.replace(':', '')}"] = (
                "yes" if dec_mod in cov["_session_mods"] else "no")
            cov[f"{name}_decision_bar_full_{dec_hhmm.replace(':', '')}"] = (
                "yes" if len(cov["_bucket_mods"].get(dec_mod, set())) == 10 else "no")
            ent_hhmm = pts["entry"]
            ent_mod = int(ent_hhmm[:2]) * 60 + int(ent_hhmm[3:])
            cov[f"{name}_entry_minute_{ent_hhmm.replace(':', '')}"] = (
                "yes" if ent_mod in cov["_session_mods"] else "no")
        ex_hhmm = TIME_EXIT
        ex_mod = int(ex_hhmm[:2]) * 60 + int(ex_hhmm[3:])
        cov[f"time_exit_minute_{ex_hhmm.replace(':', '')}"] = (
            "yes" if ex_mod in cov["_session_mods"] else "no")
        cov[f"time_exit_bar_full_{ex_hhmm.replace(':', '')}"] = (
            "yes" if len(cov["_bucket_mods"].get(ex_mod, set())) == 10 else "no")

    cov_fields = ["ticker", "figi", "day_msk", "weekday", "is_weekend", "in_warmup",
                  "present", "rows_total", "session_unique_minutes", "full_10min_bars",
                  "duplicates", "null_field", "nonfinite", "nonpositive_price",
                  "broken_ohlc", "negative_volume", "zero_volume", "off_minute",
                  "known_anomaly"]
    for hhmm in KEY_BARS:
        cov_fields.append(f"bar_{hhmm.replace(':', '')}")
    for name in CONFIG_POINTS:
        cov_fields.append(f"{name}_decision_minute_"
                          f"{CONFIG_POINTS[name]['decision'].replace(':', '')}")
        cov_fields.append(f"{name}_decision_bar_full_"
                          f"{CONFIG_POINTS[name]['decision'].replace(':', '')}")
        cov_fields.append(f"{name}_entry_minute_"
                          f"{CONFIG_POINTS[name]['entry'].replace(':', '')}")
    cov_fields.append(f"time_exit_minute_{TIME_EXIT.replace(':', '')}")
    cov_fields.append(f"time_exit_bar_full_{TIME_EXIT.replace(':', '')}")

    cov_path = out / "dq_coverage.csv"
    with cov_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cov_fields, extrasaction="ignore")
        w.writeheader()
        for c in sorted(cov_index.values(), key=lambda x: (x["ticker"], x["day_msk"])):
            w.writerow(c)

    gaps_rows = []
    for c in sorted(cov_index.values(), key=lambda x: (x["ticker"], x["day_msk"])):
        if not c["present"]:
            continue
        missing = [m for m in range(SESSION_MIN_MOD, SESSION_MAX_MOD + 1)
                   if m not in c["_session_mods"]]
        if missing:
            gaps_rows.append({
                "ticker": c["ticker"], "day_msk": c["day_msk"],
                "missing_session_minutes": len(missing),
                "first_missing_mod": missing[0], "last_missing_mod": missing[-1],
                "missing_mods": "|".join(str(m) for m in missing),
            })
    gaps_path = out / "dq_gaps.csv"
    with gaps_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "day_msk",
                                           "missing_session_minutes",
                                           "first_missing_mod", "last_missing_mod",
                                           "missing_mods"])
        w.writeheader()
        for r in gaps_rows:
            w.writerow(r)

    issues_path = out / "dq_issues.csv"
    with issues_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "ts", "lineno", "code",
                                           "day_msk", "mod_msk"],
                           extrasaction="ignore")
        w.writeheader()
        for r in issue_rows:
            w.writerow(r)

    full_absent_days = {}
    per_ticker_summary = {}
    for ticker, figi in TICKER_FIGI:
        rows = [cov_index[(ticker, d)] for d in dates]
        present = [r for r in rows if r["present"]]
        absent = [r["day_msk"] for r in rows if not r["present"]]
        full_absent_days[ticker] = absent
        per_ticker_summary[ticker] = {
            "figi": figi,
            "days_in_window": len(dates),
            "days_present": len(present),
            "days_fully_absent": absent,
            "weekend_days_present": sum(1 for r in present if r["is_weekend"]),
            "full_10min_bars_total": sum(r["full_10min_bars"] for r in present),
            "session_unique_minutes_total": sum(r["session_unique_minutes"]
                                                for r in present),
            "days_with_session_525": sum(1 for r in present
                                         if r["session_unique_minutes"] == 525),
            "dq_flags": {k: sum(r[k] for r in present)
                         for k in ("duplicates", "null_field", "nonfinite",
                                   "nonpositive_price", "broken_ohlc",
                                   "negative_volume", "zero_volume", "off_minute",
                                   "known_anomaly")},
        }

    dq_manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": "day_data_export.py",
        "mode": "dq",
        "source": {
            "source_run_dir": str(source),
            "csv_dir": str(csv_dir),
            "export_manifest": str(source_manifest_path),
            "export_manifest_sha256": source_manifest_sha,
            "export_manifest_code_sha256": source_export_code_sha,
            "csv_sha256": per_csv,
        },
        "output_dir": str(out),
        "window": {"start_utc": iso_z(WINDOW_START), "end_utc_exclusive": iso_z(WINDOW_END)},
        "session_semantics": {"session": "[10:00,18:45) MSK", "mods": [SESSION_MIN_MOD, SESSION_MAX_MOD],
                              "max_unique_minutes": 525,
                              "full_10min_bar": "ровно 10 различных ts на минутной сетке",
                              "grid": "только ts с секундами=0 и микросекундами=0 входят в session/bucket",
                              "decision_columns": "decision_minute = наличие минуты; "
                                                  "decision_bar_full = полный 10min-бар; "
                                                  "наличие минуты != исполнимый сигнал",
                              "calendar": "NOT_VERIFIED"},
        "warmup": {"calendar_days": WARMUP_DAYS,
                   "dates": [d.isoformat() for d in sorted(warmup_dates)],
                   "sufficiency": "не предполагается"},
        "config_points": CONFIG_POINTS,
        "key_bars": KEY_BARS,
        "time_exit": TIME_EXIT,
        "dates_count": len(dates),
        "per_ticker": per_ticker_summary,
        "days_fully_absent": full_absent_days,
        "issues_total": len(issue_rows),
        "code_sha256": sha256_file(Path(__file__).resolve()),
        "inputs": per_csv,
        "outputs": {},
    }
    for name in ("dq_coverage.csv", "dq_gaps.csv", "dq_issues.csv"):
        dq_manifest["outputs"][name] = sha256_file(out / name)

    a7_val = validate_with_a7(csv_dir)
    dq_manifest["a7_load_minute_csv"] = a7_val
    (out / "dq_manifest.json").write_text(
        json.dumps(dq_manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")
    print(f"A10 dq done: {out} coverage={len(dates) * len(TICKER_FIGI)} "
          f"issues={len(issue_rows)} a7_files_ok={sum(1 for v in a7_val.values() if v.get('readable'))}")
    return 0


def validate_with_a7(csv_dir: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    out = {}
    try:
        import day_strategies_core as core
    except Exception as e:
        return {"_import_error": f"{type(e).__name__}: {e}"}
    for ticker, _ in TICKER_FIGI:
        p = csv_dir / f"{ticker}.csv"
        if not p.exists():
            out[ticker] = {"readable": False, "reason": "file_missing"}
            continue
        try:
            bars, issues, sha = core.load_minute_csv(p)
            out[ticker] = {
                "readable": True,
                "tickers_parsed": sorted(bars.keys()),
                "bars": sum(len(v) for v in bars.values()),
                "issues": len(issues),
                "sha256": sha,
            }
        except Exception as e:
            out[ticker] = {"readable": False,
                           "reason": f"{type(e).__name__}: {e}"}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A10 export/dq")
    sub = ap.add_subparsers(dest="mode", required=True)
    pe = sub.add_parser("export")
    pe.add_argument("--out", required=True, type=Path)
    pe.add_argument("--env", type=Path,
                    default=Path(__file__).resolve().parents[1] / ".env")
    pd = sub.add_parser("dq")
    pd.add_argument("--source-run-dir", required=True, type=Path)
    pd.add_argument("--out", required=True, type=Path)
    args = ap.parse_args(argv)
    if args.mode == "export":
        return asyncio.run(run_export(args))
    return run_dq(args)


if __name__ == "__main__":
    raise SystemExit(main())
