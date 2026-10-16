#!/usr/bin/env python3
"""A8: инвентаризация пригодности минутных серий `candles` для дневных S1/S2.

Только READ ONLY: `default_transaction_read_only=on`, `statement_timeout=15s`,
`lock_timeout=2s`; никаких writes/инжеста/API/перезапусков/автотестов.
Источник — настоящая PostgreSQL (хост/бд/логин/пароль из backend/.env, значения
не печатаются и не пишутся в артефакты). Один агрегирующий запрос на тикер по
индексу (figi, interval, ts); точечная проверка аномалии входит в тот же запрос
(uncorrelated scalar subquery — InitPlan, один прогон). Семантику ts подтверждена
по writer: ts = c.time T-Invest (начало бара), interval=1 = 1min
(services/tinvest.py:38,100). Пропущенный целиком день не объявляется
торговым/неторговым: календарь NOT_VERIFIED. Выход в НОВОМ каталоге:
coverage.csv, inventory_summary.json, queries.sql, manifest.json.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("BLOCKED: asyncpg недоступен в этом интерпретаторе", file=sys.stderr)
    raise SystemExit(2)

UTC = timezone.utc
DEFAULT_TICKERS = ("SBER", "GAZP", "LKOH", "VKCO", "SFIN")
SESSION_MIN_MOD, SESSION_MAX_MOD = 600, 1125
BAR_LABEL_MIN, BAR_LABEL_MAX = 600, 1110
ANOMALY_TS = "2026-09-08T12:47:00+00:00"
STATEMENT_TIMEOUT = "15s"
LOCK_TIMEOUT = "2s"
CONNECT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 15.0

AGG_SQL = """
WITH sess AS (
  SELECT ts, open, high, low, close, volume,
         (ts AT TIME ZONE 'Europe/Moscow') AS msk,
         (extract(hour FROM (ts AT TIME ZONE 'Europe/Moscow')) * 60
          + extract(minute FROM (ts AT TIME ZONE 'Europe/Moscow')))::int AS mod
  FROM candles
  WHERE figi = $1 AND interval = $2 AND ts >= $3 AND ts < $4
),
buckets AS (
  SELECT msk::date AS day, (mod / 10) * 10 AS blab, count(DISTINCT mod) AS mins
  FROM sess
  WHERE mod BETWEEN 600 AND 1125
  GROUP BY 1, 2
),
perday AS (
  SELECT msk::date AS day,
         count(*) AS rows_total,
         count(DISTINCT ts) AS unique_ts,
         min(ts) AS min_ts_utc,
         max(ts) AS max_ts_utc,
         count(*) FILTER (WHERE mod BETWEEN 600 AND 1125) AS session_rows,
         count(DISTINCT ts) FILTER (WHERE mod BETWEEN 600 AND 1125)
           AS session_unique_minutes,
         count(*) FILTER (WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0)
           AS nonpositive_ohlc,
         count(*) FILTER (WHERE high < GREATEST(open, close)
                            OR low > LEAST(open, close) OR high < low)
           AS ohlc_broken,
         count(*) FILTER (WHERE volume < 0) AS negative_volume,
         count(*) FILTER (WHERE volume = 0) AS zero_volume,
         count(*) FILTER (WHERE open < 10) AS open_lt_10,
         count(*) FILTER (WHERE mod = 630) AS m_1030,
         count(*) FILTER (WHERE mod = 640) AS m_1040,
         count(*) FILTER (WHERE mod = 730) AS m_1210,
         count(*) FILTER (WHERE mod = 850) AS m_1410,
         count(*) FILTER (WHERE mod = 1110) AS m_1830
  FROM sess
  GROUP BY 1
),
bk AS (
  SELECT day,
         count(*) FILTER (WHERE mins = 10) AS full_buckets,
         count(*) FILTER (WHERE mins = 10 AND blab IN (600, 620, 630, 720, 840,
                                                       1100, 1110)) AS key_bars_full,
         array_agg(blab ORDER BY blab) FILTER (WHERE mins = 10) AS full_labels
  FROM buckets
  WHERE blab BETWEEN 600 AND 1110
  GROUP BY day
)
SELECT p.day::text, p.rows_total, p.unique_ts,
       to_char(p.min_ts_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
       to_char(p.max_ts_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
       p.session_rows, p.session_unique_minutes,
       p.nonpositive_ohlc, p.ohlc_broken, p.negative_volume, p.zero_volume,
       p.open_lt_10,
       p.m_1030, p.m_1040, p.m_1210, p.m_1410, p.m_1830,
       COALESCE(b.full_buckets, 0), COALESCE(b.key_bars_full, 0),
       COALESCE(array_to_string(b.full_labels, '|'), ''),
       (SELECT count(*) FROM candles a
         WHERE a.figi = $1 AND a.interval = $2
           AND a.ts = $5::timestamptz) AS anomaly_rows,
       (SELECT COALESCE(a.open::text, 'нет_строки') FROM candles a
         WHERE a.figi = $1 AND a.interval = $2
           AND a.ts = $5::timestamptz) AS anomaly_open
FROM perday p
LEFT JOIN bk b ON b.day = p.day
ORDER BY p.day
"""

SCHEMA_SQL = """
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE table_name IN ('candles', 'instruments')
ORDER BY table_name, ordinal_position
"""

DICT_SQL = """
SELECT ticker, figi, class_code
FROM instruments
WHERE upper(ticker) = ANY($1::text[])
ORDER BY ticker, figi
"""

COVERAGE_FIELDS = (
    "ticker", "figi", "class_code", "day_msk", "rows_total", "unique_ts",
    "duplicates", "min_ts_utc", "max_ts_utc", "session_rows",
    "session_unique_minutes", "nonpositive_ohlc", "ohlc_broken",
    "negative_volume", "zero_volume", "open_lt_10", "full_10m_buckets",
    "key_bars_full", "m_1030", "m_1040", "m_1210", "m_1410", "m_1830",
    "full_labels", "anomaly_rows", "anomaly_open",
)

TOTAL_KEYS = (
    "rows_total", "unique_ts", "session_rows", "session_unique_minutes",
    "nonpositive_ohlc", "ohlc_broken", "negative_volume", "zero_volume",
    "open_lt_10", "full_10m_buckets", "key_bars_full", "m_1030", "m_1040",
    "m_1210", "m_1410", "m_1830", "anomaly_rows",
)


class BlockedConnection(Exception):
    pass


class BlockedQuery(Exception):
    pass


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


def day_gaps(days: list[date]) -> list[str]:
    if not days:
        return []
    present = set(days)
    out = []
    d = days[0]
    while d <= days[-1]:
        if d not in present:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def write_offline_map(out: Path, params: dict, error: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "queries.sql").write_text(
        f"-- schema\n{SCHEMA_SQL}\n-- dictionary\n{DICT_SQL}\n"
        f"-- aggregate per figi (params: {json.dumps(params, ensure_ascii=False)})\n"
        f"{AGG_SQL}\n",
        encoding="utf-8")
    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool": "day_data_inventory.py",
        "status": "BLOCKED_CONNECTION_OR_QUERY",
        "error": error,
        "mode": "READ ONLY (default_transaction_read_only=on)",
        "sql_timeout": STATEMENT_TIMEOUT,
        "lock_timeout": LOCK_TIMEOUT,
        "db": params["db"],
        "params": {k: v for k, v in params.items() if k != "db"},
        "offline_query_map": "queries.sql",
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")


async def collect(env: dict, args, start_dt: datetime, end_dt: datetime):
    try:
        conn = await asyncpg.connect(
            host=env["POSTGRES_HOST"], port=int(env["POSTGRES_PORT"]),
            user=env["POSTGRES_USER"], password=env["POSTGRES_PASSWORD"],
            database=env["POSTGRES_DB"], timeout=CONNECT_TIMEOUT,
            command_timeout=COMMAND_TIMEOUT)
    except Exception as e:
        code = getattr(e, "sqlstate", None) or type(e).__name__
        raise BlockedConnection(f"{code}: {type(e).__name__}: {e}") from e
    try:
        try:
            await conn.execute("SET default_transaction_read_only = on")
            await conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
            await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            schema_rows = await conn.fetch(SCHEMA_SQL)
            dict_rows = await conn.fetch(
                DICT_SQL, [t.upper() for t in args.tickers])

            by_ticker: dict = {}
            for r in dict_rows:
                by_ticker.setdefault(r["ticker"], []).append(dict(r))
            ambiguity = {
                t: [m["figi"] for m in by_ticker.get(t, [])]
                for t in args.tickers if len(by_ticker.get(t, [])) != 1
            }
            missing_tickers = [t for t in args.tickers if t not in by_ticker]

            coverage_rows = []
            figi_summary = []
            anomaly_dt = datetime.fromisoformat(ANOMALY_TS)
            for t in args.tickers:
                for match in by_ticker.get(t, []):
                    rows = await conn.fetch(
                        AGG_SQL, match["figi"], args.interval,
                        start_dt, end_dt, anomaly_dt)
                    days = []
                    totals = {k: 0 for k in TOTAL_KEYS}
                    for r in rows:
                        d = date.fromisoformat(r[0])
                        days.append(d)
                        totals["rows_total"] += r[1]
                        totals["unique_ts"] += r[2]
                        totals["session_rows"] += r[5]
                        totals["session_unique_minutes"] += r[6]
                        totals["nonpositive_ohlc"] += r[7]
                        totals["ohlc_broken"] += r[8]
                        totals["negative_volume"] += r[9]
                        totals["zero_volume"] += r[10]
                        totals["open_lt_10"] += r[11]
                        totals["m_1030"] += r[12]
                        totals["m_1040"] += r[13]
                        totals["m_1210"] += r[14]
                        totals["m_1410"] += r[15]
                        totals["m_1830"] += r[16]
                        totals["full_10m_buckets"] += r[17]
                        totals["key_bars_full"] += r[18]
                        totals["anomaly_rows"] = max(totals["anomaly_rows"], r[20])
                        coverage_rows.append({
                            "ticker": t, "figi": match["figi"],
                            "class_code": match["class_code"], "day_msk": r[0],
                            "rows_total": r[1], "unique_ts": r[2],
                            "duplicates": r[1] - r[2],
                            "min_ts_utc": r[3], "max_ts_utc": r[4],
                            "session_rows": r[5], "session_unique_minutes": r[6],
                            "nonpositive_ohlc": r[7], "ohlc_broken": r[8],
                            "negative_volume": r[9], "zero_volume": r[10],
                            "open_lt_10": r[11],
                            "full_10m_buckets": r[17], "key_bars_full": r[18],
                            "m_1030": r[12], "m_1040": r[13], "m_1210": r[14],
                            "m_1410": r[15], "m_1830": r[16],
                            "full_labels": r[19],
                            "anomaly_rows": r[20], "anomaly_open": r[21],
                        })
                    gaps = day_gaps(days)
                    figi_summary.append({
                        "ticker": t, "figi": match["figi"],
                        "class_code": match["class_code"],
                        "days_present": len(days),
                        "first_day_msk": days[0].isoformat() if days else None,
                        "last_day_msk": days[-1].isoformat() if days else None,
                        "missing_days_in_span": gaps,
                        "missing_days_calendar": "NOT_VERIFIED",
                        "totals": totals,
                    })
        except (BlockedConnection, BlockedQuery):
            raise
        except Exception as e:
            code = getattr(e, "sqlstate", None) or type(e).__name__
            raise BlockedQuery(f"{code}: {type(e).__name__}: {e}") from e
    finally:
        await conn.close()
    return coverage_rows, figi_summary, ambiguity, missing_tickers, schema_rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A8 inventory (READ ONLY)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--tickers", nargs="+", default=list(DEFAULT_TICKERS))
    ap.add_argument("--window-days", type=int, default=120)
    ap.add_argument("--end", default="2026-10-09T00:00:00+00:00")
    ap.add_argument("--env", type=Path,
                    default=Path(__file__).resolve().parents[1] / ".env")
    ap.add_argument("--interval", type=int, default=1)
    args = ap.parse_args(argv)

    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        ap.error(f"--out {out} уже существует и не пуст (immutable-каталог)")

    env = load_env(args.env)
    required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_DB")
    missing = [k for k in required if not env.get(k)]
    if missing:
        print(f"BLOCKED_ENV: нет ключей {missing} в {args.env}", file=sys.stderr)
        return 2

    end_dt = datetime.fromisoformat(args.end)
    start_dt = end_dt - timedelta(days=args.window_days)
    db_info = {"host": env["POSTGRES_HOST"], "port": int(env["POSTGRES_PORT"]),
               "database": env["POSTGRES_DB"]}
    params = {"start_utc": start_dt.isoformat(),
              "end_utc_exclusive": end_dt.isoformat(),
              "window_days": args.window_days, "interval": args.interval,
              "tickers": list(args.tickers), "anomaly_ts": ANOMALY_TS,
              "db": db_info}

    try:
        coverage_rows, figi_summary, ambiguity, missing_tickers, schema_rows = \
            asyncio.run(collect(env, args, start_dt, end_dt))
    except BlockedConnection as e:
        write_offline_map(out, params, f"BLOCKED_CONNECTION: {e}")
        print(f"BLOCKED_CONNECTION: {e} (offline-карта запроса сохранена)",
              file=sys.stderr)
        return 3
    except BlockedQuery as e:
        write_offline_map(out, params, f"BLOCKED_QUERY: {e}")
        print(f"BLOCKED_QUERY: {e} (offline-карта запроса сохранена)",
              file=sys.stderr)
        return 4

    out.mkdir(parents=True, exist_ok=True)
    cov_path = out / "coverage.csv"
    with cov_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COVERAGE_FIELDS)
        w.writeheader()
        for r in sorted(coverage_rows,
                        key=lambda x: (x["ticker"], x["figi"], x["day_msk"])):
            w.writerow(r)

    summary = {
        "tool": "day_data_inventory.py",
        "source": "PostgreSQL candles (interval=1, ts=начало бара) — READ ONLY",
        "window": {"start_utc": start_dt.isoformat(),
                   "end_utc_exclusive": end_dt.isoformat(),
                   "window_days": args.window_days},
        "session_semantics": {
            "inventory_session": "10:00–18:45 MSK (mods 600..1125)",
            "a7_session": "10:00–18:44 MSK (mods 600..1124); минута 18:45 в A7 не используется",
            "ts": "начало минутного бара (writer services/tinvest.py: c.time)",
            "calendar": "NOT_VERIFIED: пропущенный целиком день не объявляется ни торговым, ни неторговым",
        },
        "tickers_requested": list(args.tickers),
        "tickers_missing": missing_tickers,
        "figi_ambiguity": ambiguity,
        "figi_resolution_note": "неоднозначность показана, выбор не производился",
        "figi_summary": figi_summary,
        "anomaly_check": {
            "ts": ANOMALY_TS,
            "note": "SBER 2026-09-08T12:47Z: только фиксация присутствия, без правок/удалений",
        },
        "schema_map": [dict(r) for r in schema_rows],
    }
    (out / "inventory_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")

    (out / "queries.sql").write_text(
        f"-- schema\n{SCHEMA_SQL}\n-- dictionary\n{DICT_SQL}\n"
        f"-- aggregate per figi (params: start={start_dt.isoformat()}, "
        f"end={end_dt.isoformat()} exclusive, interval={args.interval}, "
        f"anomaly_ts={ANOMALY_TS})\n{AGG_SQL}\n",
        encoding="utf-8")

    manifest = {
        "run_id": out.name,
        "generated_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": sys.version.split()[0],
        "tool": "day_data_inventory.py",
        "mode": "READ ONLY (default_transaction_read_only=on)",
        "sql_timeout": STATEMENT_TIMEOUT,
        "lock_timeout": LOCK_TIMEOUT,
        "db": db_info,
        "params": {k: v for k, v in params.items() if k != "db"},
        "code_sha256": {"day_data_inventory.py":
                        sha256_file(Path(__file__).resolve())},
        "outputs": {},
        "rows_written": {"coverage.csv": len(coverage_rows)},
        "status": "OK",
    }
    for name in ("coverage.csv", "inventory_summary.json", "queries.sql"):
        manifest["outputs"][name] = sha256_file(out / name)
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8")

    print(f"A8 inventory done: {out} (coverage rows={len(coverage_rows)}, "
          f"figi={len(figi_summary)}, ambiguity={len(ambiguity)}, "
          f"missing_tickers={missing_tickers})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
