#!/usr/bin/env python3
"""RESEARCH-CORE-V2 (B1): immutable snapshot exporter из действующей БД.

READ ONLY REPEATABLE READ из PostgreSQL, потоковое чтение (cursor+prefetch),
immutable destination (непустой --out → отказ), CSV по тикеру/интервалу,
diagnostics (дубликаты/невалидные строки), gap summaries (пропущенные
weekday-дни, покрытие сессии), derived daily из 1m (только при известной
сессии; partial помечается явно), manifest с code/query/input hashes.

Свечи не чинятся и не синтезируются; анализ допускает сигнал только при
нужных данных. Дневные свечи (interval=24) берутся из БД как есть; там, где
их нет, а 1m есть — дневные агрегируются из 1m с source=derived_from_1m.
Corporate-action/adjustment status: UNVERIFIED.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import statistics
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
SESSION_MOD_START = 600      # 10:00 MSK
SESSION_MOD_END = 1124       # 18:44 MSK (последняя минута основной сессии)
SESSION_MINUTES = SESSION_MOD_END - SESSION_MOD_START + 1  # 525
PREPERIOD_SESSIONS = 20

CSV_FIELDS = ("ticker", "figi", "interval", "ts", "open", "high", "low",
              "close", "volume", "source", "partial")

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
       current_setting('transaction_read_only') AS tx_read_only
"""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_z(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if dt else None


def validate_values(o, h, l, c, v) -> str | None:
    for name, x in (("open", o), ("high", h), ("low", l), ("close", c)):
        try:
            fx = float(x)
        except (TypeError, ValueError):
            return f"non_numeric_{name}"
        if not (fx > 0):
            return f"nonpositive_{name}"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "non_numeric_volume"
    if fv < 0:
        return "negative_volume"
    if float(h) < float(l):
        return "high_lt_low"
    return None


def msk_mod(ts: datetime) -> int:
    loc = ts.astimezone(MSK)
    return loc.hour * 60 + loc.minute


async def export_ticker(conn, ticker: str, figi: str, interval: int,
                        start: datetime, end: datetime, csv_dir: Path,
                        invalid_fh, diagnostics: dict) -> dict:
    planned = await conn.fetchval(COUNT_SQL, figi, interval, start, end)
    out = csv_dir / f"{ticker}__m{interval}.csv"
    count = dup = invalid = 0
    first = last = None
    days: set[date] = set()
    session_minutes_by_day: dict[date, set[int]] = {}
    prev_ts = None
    daily_agg: dict[date, dict] = {}
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_FIELDS)
        async for row in conn.cursor(EXPORT_SQL, figi, interval, start, end,
                                     prefetch=5000):
            ts = row["ts"].astimezone(UTC)
            if prev_ts is not None and ts == prev_ts:
                dup += 1
                continue
            prev_ts = ts
            reason = validate_values(row["open"], row["high"], row["low"],
                                     row["close"], row["volume"])
            if reason is not None:
                invalid += 1
                invalid_fh.writerow((ticker, figi, interval, iso_z(ts), reason))
                continue
            w.writerow((ticker, figi, interval, iso_z(ts),
                        str(row["open"]), str(row["high"]), str(row["low"]),
                        str(row["close"]), str(row["volume"]), "db", ""))
            count += 1
            d = ts.date()
            days.add(d)
            if first is None:
                first = ts
            last = ts
            if interval == 1:
                mod = msk_mod(ts)
                session_minutes_by_day.setdefault(d, set()).add(mod)
                agg = daily_agg.setdefault(d, {
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": int(row["volume"]), "max_mod": mod})
                agg["high"] = max(agg["high"], float(row["high"]))
                agg["low"] = min(agg["low"], float(row["low"]))
                agg["close"] = float(row["close"])
                agg["volume"] += int(row["volume"])
                agg["max_mod"] = max(agg["max_mod"], mod)
    diagnostics.update({
        "planned_rows": planned, "written_rows": count,
        "duplicates_skipped": dup, "invalid_rows": invalid,
        "min_ts_utc": iso_z(first), "max_ts_utc": iso_z(last),
        "days_with_data": len(days)})
    return daily_agg, session_minutes_by_day, days


def gap_summary(start: datetime, end: datetime, days: set[date],
                session_by_day: dict[date, set[int]] | None) -> dict:
    missing_weekdays = []
    d = start.date()
    while d < end.date():
        if d.weekday() < 5 and d not in days:
            missing_weekdays.append(d.isoformat())
        d += timedelta(days=1)
    out = {"missing_weekdays_n": len(missing_weekdays),
           "missing_weekdays": missing_weekdays[:60],
           "missing_weekdays_truncated": len(missing_weekdays) > 60}
    if session_by_day is not None:
        cov = []
        for day, mods in session_by_day.items():
            full = sum(1 for m in mods if SESSION_MOD_START <= m <= SESSION_MOD_END)
            cov.append(full / SESSION_MINUTES)
        out["session_coverage_pct_mean"] = (
            round(100.0 * statistics.mean(cov), 2) if cov else None)
    return out


async def run(args) -> int:
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        print(f"BLOCKED: --out {out} существует и не пуст (immutable destination)",
              file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    csv_dir = out / "csv"
    csv_dir.mkdir()

    universe_path = Path(args.universe_file)
    uni = json.loads(universe_path.read_text(encoding="utf-8"))
    instruments = uni["instruments"]
    if args.limit is not None:
        instruments = instruments[: args.limit]

    start = datetime.fromisoformat(args.start).astimezone(UTC)
    end = datetime.fromisoformat(args.end).astimezone(UTC)
    d1_start = datetime.fromisoformat(args.daily_start or args.start).astimezone(UTC)
    d1_end = end

    conn = await asyncpg.connect(args.db_url, timeout=30)
    code_sha = sha256_file(Path(__file__).resolve())
    invalid_path = out / "invalid_rows.csv"
    per_ticker: dict[str, dict] = {}
    try:
        async with conn.transaction(isolation="repeatable_read", readonly=True):
            await conn.execute("SET statement_timeout = '120000'")
            ident = await conn.fetchrow(IDENT_SQL)
            with invalid_path.open("w", encoding="utf-8", newline="") as inv:
                invw = csv.writer(inv)
                invw.writerow(("ticker", "figi", "interval", "ts", "reason"))
                for inst in instruments:
                    ticker, figi = inst["ticker"], inst["figi"]
                    entry: dict = {"ticker": ticker, "figi": figi}
                    m1_diag: dict = {}
                    daily_agg, sess_m, days_m1 = await export_ticker(
                        conn, ticker, figi, 1, start, end, csv_dir, invw, m1_diag)
                    m1_diag["gaps"] = gap_summary(start, end, days_m1, sess_m)
                    entry["m1"] = m1_diag
                    d1_diag: dict = {}
                    _, _, days_d1 = await export_ticker(conn, ticker, figi, 24,
                                                        d1_start, d1_end,
                                                        csv_dir, invw, d1_diag)
                    d1_diag["gaps"] = gap_summary(d1_start, d1_end, days_d1, None)
                    entry["d1"] = d1_diag
                    # derived daily из 1m там, где нет db daily
                    db_days = set()
                    d1_csv = csv_dir / f"{ticker}__m24.csv"
                    with d1_csv.open(encoding="utf-8") as fh:
                        rd = csv.DictReader(fh)
                        db_days = {datetime.fromisoformat(r["ts"]).date() for r in rd}
                    derived = 0
                    derived_path = csv_dir / f"{ticker}__d1_derived.csv"
                    with derived_path.open("w", encoding="utf-8", newline="") as fh:
                        w = csv.writer(fh)
                        w.writerow(CSV_FIELDS)
                        for d, a in sorted(daily_agg.items()):
                            if d in db_days or not (d1_start.date() <= d < d1_end.date()):
                                continue
                            partial = a["max_mod"] < SESSION_MOD_END
                            w.writerow((ticker, figi, 24,
                                        datetime(d.year, d.month, d.day,
                                                 tzinfo=UTC).isoformat(),
                                        a["open"], a["high"], a["low"],
                                        a["close"], a["volume"],
                                        "derived_from_1m",
                                        "partial_session" if partial else ""))
                            derived += 1
                    entry["d1_derived_rows"] = derived
                    # медианный дневной оборот preperiod: первые 20 полных weekday db daily
                    turns = []
                    with d1_csv.open(encoding="utf-8") as fh:
                        rd = csv.DictReader(fh)
                        for r in rd:
                            d = datetime.fromisoformat(r["ts"]).date()
                            if d.weekday() < 5:
                                turns.append(float(r["close"]) * int(r["volume"]))
                                if len(turns) >= PREPERIOD_SESSIONS:
                                    break
                    entry["preperiod_median_daily_turnover"] = (
                        round(statistics.median(turns), 2) if turns else None)
                    per_ticker[ticker] = entry
    finally:
        await conn.close()

    if invalid_path.stat().st_size <= 1:
        invalid_path.unlink(missing_ok=True)
    manifest = {
        "tool": "research_snapshot_v2",
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": {"start": args.start, "end": args.end,
                   "daily_start": args.daily_start or args.start,
                   "daily_end": args.end},
        "universe_file": str(universe_path),
        "universe_sha256": sha256_file(universe_path),
        "n_universe_elite": uni.get("n_instruments"),
        "code_sha256": code_sha,
        "sql_sha256": {"export": hashlib.sha256(EXPORT_SQL.encode()).hexdigest(),
                       "count": hashlib.sha256(COUNT_SQL.encode()).hexdigest()},
        "db_ident": dict(ident) if ident else None,
        "corporate_actions_status": "UNVERIFIED",
        "calendar_status": "NOT_VERIFIED",
        "adjustments": "none — raw values без округления/исправлений",
        "per_ticker": per_ticker,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1, default=str) + "\n")
    n_rows = sum(t["m1"]["written_rows"] for t in per_ticker.values())
    print(f"snapshot: tickers={len(per_ticker)} m1_rows={n_rows} out={out}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="RESEARCH-CORE-V2 immutable snapshot exporter (B1)")
    p.add_argument("--start", required=True, help="момент UTC начала m1-окна")
    p.add_argument("--end", required=True, help="момент UTC конца окна (исключительно)")
    p.add_argument("--daily-start", default=None, help="начало daily-окна (default = --start)")
    p.add_argument("--universe-file", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--db-url", default="postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading")
    p.add_argument("--limit", type=int, default=None, help="ограничить число тикеров (debug)")
    args = p.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
