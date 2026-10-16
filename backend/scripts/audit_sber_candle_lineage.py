"""audit_sber_candle_lineage — происхождение аномального бара SBER (A3).

Предмет: 2026-09-08 12:47:00+00:00 SBER (BBG004730N88), interval=1,
         O=H=L=C=1.0, volume=0 — единственная такая строка для FIGI.

Офлайн по умолчанию: fingerprint-анализ барного ряда/снимка без сети и БД.
--db-url: ограниченное READ-ONLY чтение (окно 12:30–13:10 + census 1.0 +
          интервальный состав дня + candle_integrity) — REPEATABLE READ,
          statement_timeout=30s. Внешних API и записи НЕТ.

Вывод: JSON (--save <dir> в reports/architect_20261008/b1/<unique>/) + таблица.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SBER_FIGI = "BBG004730N88"
ANOMALY = {"ts": "2026-09-08 12:47:00+00:00", "open": "1.0", "high": "1.0",
           "low": "1.0", "close": "1.0", "volume": 0}
W0 = datetime(2026, 9, 8, 12, 30, tzinfo=timezone.utc)
W1 = datetime(2026, 9, 8, 13, 10, tzinfo=timezone.utc)
DAY0 = datetime(2026, 9, 8, 0, 0, tzinfo=timezone.utc)
DAY1 = datetime(2026, 9, 9, 0, 0, tzinfo=timezone.utc)

SQL_WINDOW = (
    "SELECT ts, open, high, low, close, volume FROM candles "
    "WHERE figi=$1 AND interval=1 AND ts>=$2 AND ts<$3 ORDER BY ts"
)
SQL_INTERVAL5 = (
    "SELECT ts, open, high, low, close, volume FROM candles "
    "WHERE figi=$1 AND interval=5 AND ts>=$2 AND ts<$3 ORDER BY ts"
)
SQL_CENSUS = (
    "SELECT ts, open, high, low, close, volume FROM candles "
    "WHERE figi=$1 AND interval=1 AND ts>=$2 AND ts<$3 "
    "AND open=1 AND low=1 AND high=1 AND close=1 AND volume=0 "
    "ORDER BY ts"
)
SQL_COUNTS = (
    "SELECT interval, count(*) AS count FROM candles WHERE figi=$1 "
    "AND ts>=$2 AND ts<$3 "
    "GROUP BY interval ORDER BY interval"
)
SQL_SINGLE_5M_DAY = (
    "SELECT ts, open, high, low, close, volume FROM candles "
    "WHERE figi=$1 AND interval=5 "
    "AND ts>=$2 AND ts<$3 "
    "ORDER BY ts"
)
SQL_INTEGRITY = (
    "SELECT interval, to_char(day,'YYYY-MM-DD') AS d, \"rows\", min_ts, max_ts, "
    "volume_sum, missing_active FROM candle_integrity WHERE figi=$1 "
    "AND day>=$2 AND day<$3 ORDER BY interval, d"
)
SQL_QUERIES = {
    "window_1m": SQL_WINDOW,
    "window_interval5": SQL_INTERVAL5,
    "flat_one_census": SQL_CENSUS,
    "interval_counts_day": SQL_COUNTS,
    "single_5m_row_day": SQL_SINGLE_5M_DAY,
    "integrity_sber": SQL_INTEGRITY,
}


def is_flat_one_volzero(o, h, l, c, v) -> bool:
    """Фингерпринт placeholder-бара: OHLC ровно 1.0, объём 0."""
    try:
        return (float(o) == float(h) == float(l) == float(c) == 1.0) and int(v or 0) == 0
    except (TypeError, ValueError):
        return False


def structural_ok(o, h, l, c) -> bool:
    """Минимальный структурный бар-гейт (как app/services/candle_guard.bar_ok):
    1.0-бар структурно ВАЛИДЕН (положителен, h>=l, contained)."""
    try:
        o, h, l, c = float(o), float(h), float(l), float(c)
    except (TypeError, ValueError):
        return False
    if o <= 0 or c <= 0 or h <= 0 or l <= 0:
        return False
    if h < l or h < o or h < c or l > o or l > c:
        return False
    return True


def jump_ratio(prev_close, close) -> float | None:
    try:
        p, c_ = float(prev_close), float(close)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return abs(c_ - p) / p


def section_sha256(obj) -> str:
    """Детерминированный sha256 JSON-секции (sort_keys, default=str), нормализует int->str? нет; проще сериализуем стабильно."""
    # Нормализуем типы числовых ключей (иногда из asyncpg int)
    def _norm(x):
        if isinstance(x, dict):
            return {str(k) if isinstance(k, (int, float)) else k: _norm(v) for k, v in x.items()}
        if isinstance(x, list):
            return [_norm(v) for v in x]
        return x
    return hashlib.sha256(
        json.dumps(_norm(obj), sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def cadence_hist(rows, n=32) -> Counter:
    """Промежутки между соседними барами (минуты). rows: [{'ts': str}, ...]."""
    def _dt(s):
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    diffs = [int(round((_dt(b["ts"]) - _dt(a["ts"])).total_seconds() / 60))
             for a, b in zip(rows, rows[1:])]
    return Counter(diffs)


def snapshot_analysis(snapshot_path: str | Path) -> dict:
    rows = json.loads(Path(snapshot_path).read_text())
    rows = sorted(rows, key=lambda r: str(r["ts"]))
    flat = [r for r in rows if is_flat_one_volzero(r.get("open"), r.get("high"),
                                                   r.get("low"), r.get("close"),
                                                   r.get("volume"))]
    return {
        "total": len(rows),
        "first_ts": rows[0]["ts"] if rows else None,
        "last_ts": rows[-1]["ts"] if rows else None,
        "cadence_minutes": dict(sorted(cadence_hist(rows).items())),
        "placeholder_like": flat,
    }


@dataclass
class Findings:
    window_rows: list = field(default_factory=list)
    interval5_in_window: list = field(default_factory=list)
    flat_one_census: list = field(default_factory=list)
    interval_counts_day: dict = field(default_factory=dict)
    single_5m_row_day: list = field(default_factory=list)
    integrity_sber: list = field(default_factory=list)
    queried: bool = False
    flat_one_census_count: int | None = None
    interval5_count: int | None = None


async def probe_db(conn, figi: str) -> Findings:
    f = Findings()
    f.window_rows = [dict(r) for r in await conn.fetch(SQL_WINDOW, figi, W0, W1)]
    f.interval5_in_window = [dict(r) for r in await conn.fetch(SQL_INTERVAL5, figi, W0, W1)]
    f.flat_one_census = [dict(r) for r in await conn.fetch(SQL_CENSUS, figi, W0, W1)]
    f.interval_counts_day = {r["interval"]: r["count"] for r in await conn.fetch(SQL_COUNTS, figi, W0, W1)}
    f.single_5m_row_day = [dict(r) for r in await conn.fetch(SQL_SINGLE_5M_DAY, figi, W0, W1)]
    f.integrity_sber = [dict(r) for r in await conn.fetch(SQL_INTEGRITY, figi, DAY0, DAY1)]
    f.queried = True
    return f


def fact_table(f: Findings) -> list[dict]:
    facts: list[dict] = []
    queried = bool(getattr(f, "queried", False))

    def _obs_or_unknown(name: str, rows, empty_value, present_value):
        if rows:
            facts.append({"fact": f"{name}: observed", "source": src_of[name],
                          "kind": "observed", "value": present_value(rows)})
        elif queried:
            facts.append({"fact": f"{name}: queried selection is empty (0 rows — known empty, not unknown)",
                          "source": src_of[name], "kind": "observed", "value": empty_value})
        else:
            facts.append({"fact": f"{name}: unknown (query not run)",
                          "source": src_of[name], "kind": "unknown", "value": None})

    src_of = {
        "census": "probe_db.flat_one_census",
        "integrity": "probe_db.integrity_sber",
        "interval counts": "probe_db.interval_counts_day",
        "interval=5 in window": "probe_db.interval5_in_window",
    }
    census_count = getattr(f, "flat_one_census_count", None)
    if census_count is None:
        census_count = len(f.flat_one_census) if f.flat_one_census else 0
    if census_count:
        facts.append({"fact": "census: 1m flat-1.0/vol0 rows in selection",
                      "source": src_of["census"], "kind": "observed",
                      "value": {"count": census_count,
                                "rows": f.flat_one_census or None}})
    elif queried:
        facts.append({"fact": "census: queried selection is empty (0 rows — known empty, not unknown)",
                      "source": src_of["census"], "kind": "observed", "value": {"count": 0}})
    else:
        facts.append({"fact": "census: unknown (query not run)",
                      "source": src_of["census"], "kind": "unknown", "value": None})
    _obs_or_unknown("integrity", f.integrity_sber, [], lambda r: r)
    _obs_or_unknown("interval counts", f.interval_counts_day, {},
                    lambda r: r)
    interval5_count = getattr(f, "interval5_count", None)
    if interval5_count is None:
        interval5_count = len(f.interval5_in_window) if f.interval5_in_window else 0
    if interval5_count:
        facts.append({"fact": "interval=5 in window selection: count",
                      "source": src_of["interval=5 in window"], "kind": "observed",
                      "value": interval5_count})
    elif queried:
        facts.append({"fact": "interval=5 in window selection: queried selection is empty (0 rows — known empty, not unknown)",
                      "source": src_of["interval=5 in window"], "kind": "observed", "value": 0})
    else:
        facts.append({"fact": "interval=5 in window selection: unknown (query not run)",
                      "source": src_of["interval=5 in window"], "kind": "unknown", "value": None})

    if getattr(f, "window_rows", None) and len(f.window_rows) > 1:
        try:
            ch = dict(sorted(cadence_hist(f.window_rows).items()))
        except Exception:
            ch = {}
        facts.append({"fact": "cadence: intervals between consecutive bars in selection",
                      "source": "probe_db.window_rows cadence", "kind": "observed",
                      "value": ch})
    elif getattr(f, "window_rows", None):
        facts.append({"fact": "cadence: fewer than 2 timestamps in selection — insufficient",
                      "source": "probe_db.window_rows cadence", "kind": "observed",
                      "value": {}})
    elif queried:
        facts.append({"fact": "cadence: queried selection is empty (0 rows — known empty, not unknown)",
                      "source": "probe_db.window_rows cadence", "kind": "observed",
                      "value": {}})
    else:
        facts.append({"fact": "cadence: unknown (query not run)",
                      "source": "probe_db.window_rows cadence", "kind": "unknown",
                      "value": None})
    # code_observation
    facts.append({"fact": "structural_ok: 1.0-бар структурно валиден для bar_ok",
                  "source": "audit_sber_candle_lineage.structural_ok",
                  "kind": "code_observation", "value": True})
    facts.append({"fact": "jump: 280->1.0 gives ~0.996>0.40; runtime._candle_ok rejects jump>0.40 only if prev_close known — historical prev_close unknown",
                  "source": "candle_guard.jump_ratio/runtime._candle_ok",
                  "kind": "code_observation", "value": True})
    facts.append({"fact": "writers: tinvest/upsert_candles, data_backfill._UP, moex upserts, runtime._flush_persist_once — no bar_ok/jump gate before upsert (current review)",
                  "source": "code review writer'ов", "kind": "code_observation",
                  "value": True})
    facts.append({"fact": "lab.load_1m reads interval=1 verbatim (no validation), does not write candles",
                  "source": "app/lab/data.py:75-84", "kind": "code_observation",
                  "value": True})
    facts.append({"fact": "candles lacks source/created_at — attribution limited by available sources",
                  "source": "app/models/candle.py", "kind": "code_observation", "value": True})
    return facts




def infer(ft: list | None) -> dict:
    ft = ft or []

    def _observed(source: str):
        for x in ft:
            if x.get("source", "").startswith(source) and x.get("kind") == "observed":
                return x.get("value")
        return None

    cadence = _observed("probe_db.window_rows cadence")
    census = _observed("probe_db.flat_one_census")
    counts = _observed("probe_db.interval_counts_day")
    integrity = _observed("probe_db.integrity_sber")

    cadence_fact = next((x for x in ft
                         if str(x.get("source", "")).startswith("probe_db.window_rows cadence")), None)
    if cadence_fact is None or cadence_fact.get("kind") != "observed":
        broken_cadence = "unknown (query not run)"
    elif "empty" in str(cadence_fact.get("fact", "")):
        broken_cadence = "queried selection is empty (0 rows) — cadence unknown, известная пустота"
    elif not cadence:
        broken_cadence = "insufficient: fewer than 2 timestamps in selection — cadence unknown"
    elif set(cadence.keys()) == {1}:
        broken_cadence = (f"continuous 1m cadence observed {cadence} — непрерывный 1m ряд в выборке; "
                          "разреженность не наблюдается")
    else:
        broken_cadence = (f"observed cadence {cadence} — разреженность выводится из рассчитанных "
                          "интервалов выборки; согласуется с внешней выгрузкой, но не доказывает "
                          "конкретный источник.")

    origin = {
        "status": "NOT_VERIFIED",
        "why": "candles не хранит source/created_at; писатели персистят внешние значения вербатим; "
               "по доступным исследованным источникам конкретный внешний банк/писатель не восстанавливается. "
               "Историческое состояние runtime (_prev_close) на момент записи неизвестно.",
    }
    if census is None and counts is None and integrity is None:
        origin["selection_evidence"] = "unknown (query not run)"

    return {
        "placeholder_origin_of_1_0_row": origin,
        "excluded": {
            "live_stream_persist": "условно: runtime._candle_ok отклоняет jump>0.40 при известном prev_close>0; "
                                    "историческое состояние prev_close на момент записи неизвестно (статус NOT_VERIFIED)",
            "structured_generator_in_code": "в текущем коде генератор 1.0 не воспроизведён; доказательства его отсутствия на момент записи ограничены",
            "lab_write": "текущие модули app/lab/* не пишут в candles",
        },
        "plausible_paths": [
            "fetch_candles/upsert_candles (ensure_candles|repair|backfill-tinvest) — внешний ответ, записан вербатим",
            "data_backfill._UP / moex: sync_moex_sync/sync_moex_daily/_upsert_imoex — внешний ответ MOEX ISS, записан вербатим (tz-преобразования в коде — наблюдение, не доказательство сдвига конкретной записи)",
            "load_history_csv — CSV-импорт (для SBER в UID_TO_FIGI не зафиксирован в committed-карте)",
        ],
        "broken_cadence": broken_cadence,
        "selection_evidence": {
            "census": census if census is not None else "unknown (query not run)",
            "interval_counts_day": counts if counts is not None else "unknown (query not run)",
            "integrity_sber": integrity if integrity is not None else "unknown (query not run)",
        },
    }


def _findings_from_db_probe(db_probe: dict) -> Findings:
    f = Findings()
    f.queried = True
    if not db_probe:
        f.queried = False
        return f
    f.window_rows = db_probe.get("window") or []
    f.interval5_count = db_probe.get("day_interval5_in_window")
    f.flat_one_census_count = db_probe.get("flat_one_census")
    f.interval_counts_day = db_probe.get("interval_counts_day") or {}
    f.single_5m_row_day = db_probe.get("single_5m_row_day") or []
    f.integrity_sber = db_probe.get("integrity_sber") or []
    return f


def _recompute_section_hashes(findings: dict) -> dict:
    out: dict = {}
    if findings.get("snapshot"):
        out["snapshot"] = section_sha256(findings["snapshot"])
    dp = findings.get("db_probe")
    if dp:
        out["window_1m"] = section_sha256(dp.get("window", []))
        out["interval_counts_day"] = section_sha256(dp.get("interval_counts_day", {}))
        out["single_5m_row_day"] = section_sha256(dp.get("single_5m_row_day", []))
        out["integrity_sber"] = section_sha256(dp.get("integrity_sber", []))
        out["cadence_minutes_window"] = section_sha256(dp.get("cadence_minutes_window", {}))
        out["day_interval5_in_window"] = section_sha256(dp.get("day_interval5_in_window", 0))
        out["flat_one_census"] = section_sha256(dp.get("flat_one_census", 0))
    return out


def transfer_from_facts_src(path: str | Path) -> dict:
    src_path = Path(path)
    src = json.loads(src_path.read_text())
    findings = {
        "snapshot": src.get("findings", {}).get("snapshot"),
        "db_probe": src.get("findings", {}).get("db_probe"),
    }
    dp = findings.get("db_probe") or {}
    f = _findings_from_db_probe(dp)
    facts = fact_table(f)
    meta = dict(src.get("meta") or {})
    meta["transfer"] = {
        "mode": "offline_transfer",
        "facts_src": str(src_path),
        "facts_src_sha256": hashlib.sha256(src_path.read_bytes()).hexdigest(),
        "note": "findings/db_probe перенесены дословно; section_hashes пересчитаны из перенесённого JSON; facts/inference пересчитаны из перенесённого db_probe",
    }
    return {
        "subject": src.get("subject", "sber 2026-09-08 12:47 ohlc=1.0 vol=0 (interval=1)"),
        "meta": meta,
        "queries": src.get("queries", SQL_QUERIES),
        "findings": findings,
        "facts": facts,
        "section_hashes": _recompute_section_hashes(findings),
        "inference": infer(facts),
        "facts_src_verbatim": {
            "facts": src.get("facts"),
            "inference": src.get("inference"),
            "section_hashes": src.get("section_hashes"),
        },
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default=None,
                    help="путь к A1 snapshot_candles.json (офлайн-анализ)")
    ap.add_argument("--facts-src", default=None,
                    help="путь к сохранённому facts.json предыдущего артефакта (offline-перенос)")
    ap.add_argument("--db-url", default=None,
                    help="ограниченный RO-проб (asyncpg); без него — офлайн")
    ap.add_argument("--save", default=None,
                    help="каталог артефакта reports/architect_20261008/b1/<unique>/")
    args = ap.parse_args(argv)

    script_path = Path(__file__)
    script_sha256 = hashlib.sha256(script_path.read_bytes()).hexdigest()

    if args.facts_src:
        blob = transfer_from_facts_src(args.facts_src)
        facts_src_sha256 = hashlib.sha256(Path(args.facts_src).read_bytes()).hexdigest()
        mode = "offline_transfer"
    else:
        findings: dict = {"snapshot": None, "db_probe": None}
        facts: list = []
        section_hashes: dict = {}

        if args.snapshot:
            findings["snapshot"] = snapshot_analysis(args.snapshot)
            section_hashes["snapshot"] = section_sha256(findings["snapshot"])

        async def _run():
            import asyncpg

            conn = await asyncpg.connect(args.db_url)
            try:
                async with conn.transaction(isolation="repeatable_read", readonly=True):
                    await conn.execute("SET statement_timeout = 30000")
                    f = await probe_db(conn, SBER_FIGI)
            finally:
                await conn.close()
            return f

        if args.db_url:
            f = asyncio.run(_run())
            db_probe = {
                "window": [{"ts": str(r["ts"]), "open": str(r["open"]), "high": str(r["high"]),
                            "low": str(r["low"]), "close": str(r["close"]), "volume": r["volume"]}
                           for r in f.window_rows],
                "day_interval5_in_window": len(f.interval5_in_window),
                "flat_one_census": len(f.flat_one_census),
                "interval_counts_day": f.interval_counts_day,
                "single_5m_row_day": [{"ts": str(r["ts"]), "volume": r["volume"]}
                                      for r in f.single_5m_row_day],
                "integrity_sber": [{k: (str(v) if hasattr(v, "isoformat") else v)
                                    for k, v in r.items()} for r in f.integrity_sber],
                "cadence_minutes_window": dict(sorted(cadence_hist(f.window_rows).items())),
            }
            findings["db_probe"] = db_probe
            section_hashes = {
                "window_1m": section_sha256(db_probe.get("window", [])),
                "interval_counts_day": section_sha256(db_probe.get("interval_counts_day", {})),
                "single_5m_row_day": section_sha256(db_probe.get("single_5m_row_day", [])),
                "integrity_sber": section_sha256(db_probe.get("integrity_sber", [])),
                "cadence_minutes_window": section_sha256(db_probe.get("cadence_minutes_window", {})),
            }
            facts = fact_table(f)

        blob = {
            "subject": "sber 2026-09-08 12:47 ohlc=1.0 vol=0 (interval=1)",
            "meta": {
                "figi": SBER_FIGI,
                "window_utc": [W0.isoformat(), W1.isoformat()],
                "intervals_probed": [1, 5],
                "days_for_counts": "окно 12:30–13:10 UTC 2026-09-08 (все candle-запросы в одном scope)",
                "integrity_span": "2026-09-08 (UTC, суточный scope integrity)",
                "db_scope": "READ ONLY, REPEATABLE READ, statement_timeout=30s, ограниченный scope; новые запросы вне окна не выполнялись",
            },
            "queries": SQL_QUERIES,
            "findings": findings,
            "facts": facts,
            "section_hashes": section_hashes,
            "inference": infer(facts),
        }
        facts_src_sha256 = hashlib.sha256(Path(args.snapshot).read_bytes()).hexdigest() if args.snapshot else None
        mode = "offline" if not args.db_url else "limited_ro_db"

    if args.save:
        d = Path(args.save)
        d.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()[:8]
        unique = f"20261008T{datetime.now(timezone.utc):%H%M%SZ}-{digest}"
        out = d / unique
        if out.exists():
            out = d / f"{unique}-r{int(datetime.now(timezone.utc).microsecond)}"
        out.mkdir(parents=True, exist_ok=False)
        (out / "facts.json").write_text(json.dumps(blob, indent=1, ensure_ascii=False, default=str))
        facts_b = (out / "facts.json").read_bytes()
        manifest = {
            "generator": "audit_sber_candle_lineage.py",
            "subject": blob["subject"],
            "mode": mode,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "sha256_facts": hashlib.sha256(facts_b).hexdigest(),
            "sha256_script": script_sha256,
            "facts_src_sha256": facts_src_sha256,
        }
        if mode == "offline_transfer" and args.facts_src:
            manifest["supersedes"] = Path(args.facts_src).resolve().parent.name
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
        print(f"saved: {out}")
    else:
        print(json.dumps(blob, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()