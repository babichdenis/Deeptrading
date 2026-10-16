#!/usr/bin/env python3
"""A21 r2: локальный журнал соединения базовых сигналов с news-shadow (offline, без сети).

Читает явно заданный candidate JSONL и локальную КОПИЮ архива A6; пишет ТОЛЬКО в новый
output в каталоге запуска. Исходный архив read-only.

r2-контракт:
  * Идентичность: id/ticker/версии — непустые строки (dict/list отклоняются), id≤200,
    ticker по шаблону, версии ≤80. A6 external_ref = стабильный "a21:"+sha256(id+payload)
    (без обрезки) → разные допустимые id не коллизируют. id+payload идемпотентен;
    конфликт id (иной payload) — reject.
  * Копия данных: immutable snapshot.sqlite (SQLite backup исходника) ДО любых изменений;
    рабочая journal.sqlite создаётся из неё; в НОВОЙ копии очищаются только производные
    candidates/shadow_decisions; новости/версии/анализы/coverage не меняются. Исходник
    не мутируется.
  * Lineage: snapshot_sha256 (точная идентичность данных), candidates_sha256 (те же байты,
    что разобраны), candidate_line_sha256, core_sha256, output_db_sha256 (после закрытия).
  * Ошибки: непустые версии обязательны; битый JSON/типы/UTC → rejects; явно заданный
    несуществующий/пустой/повреждённый archive → source_unavailable/invalid_archive
    (exit 2, без ложного allow); пустой режим только без --archive. Ошибки чтения →
    безопасный error manifest без traceback, источник не изменяется.

analyze_item/API/collector/runtime НЕ вызываются; для C читаются только сохранённые анализы.
Outcome/PnL в решение не подаются.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import news_observer_core as core  # noqa: E402

BRANCHES = ("A", "B", "C")
REQUIRED_CAND = ("id", "ticker", "side", "decision_time",
                 "price_strategy_version", "data_version")
TICKER_RE = re.compile(r"^[A-Za-z0-9._-]{1,20}$")
ID_MAX = 200
VER_MAX = 80


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha_file(p: Path) -> str:
    return _sha_bytes(Path(p).read_bytes())


def _is_str(v) -> bool:
    return isinstance(v, str)


def _external_ref(cid: str, payload: str) -> str:
    return "a21:" + hashlib.sha256((cid + "\x1f" + payload).encode("utf-8")).hexdigest()[:40]


def _load_candidates(data: bytes):
    valid, rejects, dup_ok, seen = [], [], 0, {}
    for lineno, line in enumerate(data.decode("utf-8").splitlines(), start=1):
        if line.strip() == "":
            continue
        line_hash = _sha_bytes(line.encode("utf-8"))
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            rejects.append({"line": lineno, "code": "malformed_json", "detail": "не JSON"})
            continue
        if not isinstance(obj, dict):
            rejects.append({"line": lineno, "code": "not_object", "detail": "не объект"})
            continue
        bad = [k for k in REQUIRED_CAND if k not in obj]
        for k in ("id", "ticker", "price_strategy_version", "data_version", "side", "decision_time"):
            if k in obj and not _is_str(obj[k]):
                bad.append(k)
        if bad:
            rejects.append({"line": lineno, "code": "bad_field_type_or_missing",
                            "detail": ",".join(sorted(set(bad)))})
            continue
        if any(obj[k] != obj[k].strip() or any(ord(ch) < 32 for ch in obj[k])
               for k in ("id", "ticker", "price_strategy_version", "data_version")):
            rejects.append({"line": lineno, "code": "noncanonical_identity",
                            "detail": "identity fields must not require whitespace normalization"})
            continue
        cid = obj["id"].strip()
        ticker = obj["ticker"].strip()
        side = obj["side"].strip().lower()
        pv = obj["price_strategy_version"].strip()
        dv = obj["data_version"].strip()
        if not cid or len(cid) > ID_MAX:
            rejects.append({"line": lineno, "code": "bad_id", "detail": f"len={len(cid)}"})
            continue
        if not TICKER_RE.match(ticker):
            rejects.append({"line": lineno, "code": "bad_ticker", "detail": ticker})
            continue
        if side not in ("long", "short"):
            rejects.append({"line": lineno, "code": "bad_side", "detail": obj["side"]})
            continue
        if not pv or len(pv) > VER_MAX or not dv or len(dv) > VER_MAX:
            rejects.append({"line": lineno, "code": "missing_versions",
                            "detail": "нужны непустые price_strategy_version/data_version"})
            continue
        dt = core.parse_utc(obj["decision_time"])
        if dt is None:
            rejects.append({"line": lineno, "code": "bad_decision_time",
                            "detail": str(obj["decision_time"])})
            continue
        payload = json.dumps([ticker, side, core.datetime_utc_iso(dt), pv, dv], ensure_ascii=False)
        prev = seen.get(cid)
        if prev is None:
            seen[cid] = payload
            valid.append({"line": lineno, "line_sha256": line_hash, "id": cid, "ticker": ticker,
                          "side": side, "decision_time": core.datetime_utc_iso(dt),
                          "price_strategy_version": pv, "data_version": dv})
        elif prev == payload:
            dup_ok += 1
        else:
            rejects.append({"line": lineno, "code": "id_conflict",
                            "detail": f"id={cid} другой payload"})
    return valid, rejects, dup_ok


def _write_error(out: Path, code: str, detail: str = "") -> int:
    (out / "error_manifest.json").write_text(json.dumps(
        {"ok": False, "error_code": code, "detail": detail[:300]}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(json.dumps({"ok": False, "error_code": code}, ensure_ascii=False))
    return 2


def _backup_readonly(src: Path, dst: Path) -> None:
    s = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True, timeout=10.0)
    try:
        d = sqlite3.connect(str(dst), timeout=10.0)
        with d:
            s.backup(d)
        d.close()
    finally:
        s.close()


def run(candidates_path: Path, archive: Path | None, out: Path, provider: str = "deepseek") -> int:
    out = Path(out)
    if out.exists() and any(out.iterdir()):
        print(json.dumps({"ok": False, "error": "OUT_DIR_NOT_EMPTY"}, ensure_ascii=False))
        return 2
    out.mkdir(parents=True, exist_ok=True)

    try:
        data = Path(candidates_path).read_bytes()
    except OSError as exc:
        return _write_error(out, "candidates_unreadable", str(exc))
    cand_sha = _sha_bytes(data)
    try:
        valid, rejects, dup_ok = _load_candidates(data)
    except UnicodeDecodeError as exc:
        return _write_error(out, "candidates_invalid_utf8", str(exc))
    except Exception as exc:  # безопасная диагностика без traceback
        return _write_error(out, "candidates_parse_error", type(exc).__name__)

    snapshot = out / "snapshot.sqlite"
    work = out / "journal.sqlite"
    snapshot_sha = None
    archive_sha_main = None
    mode = "empty"
    explicit_archive = archive is not None
    if explicit_archive:
        arch = Path(archive)
        if not arch.exists() or arch.stat().st_size == 0:
            return _write_error(out, "source_unavailable",
                                f"archive отсутствует/пуст: {arch}")
        archive_sha_main = _sha_file(arch)
        try:
            _backup_readonly(arch, snapshot)
        except sqlite3.DatabaseError:
            return _write_error(out, "invalid_archive", "не SQLite / повреждён")
        except OSError as exc:
            return _write_error(out, "archive_read_error", str(exc))
        snapshot_sha = _sha_file(snapshot)
        shutil.copyfile(snapshot, work)
        mode = "copied"
    else:
        core.connect(work).close()

    conn = None
    try:
        conn = core.connect(work)
    except ValueError as exc:
        return _write_error(out, "invalid_archive", str(exc))
    except Exception as exc:
        return _write_error(out, "archive_open_error", type(exc).__name__)

    core_sha = _sha_file(Path(core.__file__))
    journal = []
    try:
        if mode == "copied":
            conn.execute("DELETE FROM shadow_decisions")
            conn.execute("DELETE FROM candidates")
            conn.commit()
        for c in valid:
            ext = _external_ref(c["id"], json.dumps(
                [c["ticker"], c["side"], c["decision_time"],
                 c["price_strategy_version"], c["data_version"]], ensure_ascii=False))
            res = core.add_candidate(conn, c["ticker"], c["side"], c["decision_time"],
                                     c["price_strategy_version"], c["data_version"], external_ref=ext)
            if not res.get("ok"):
                rejects.append({"line": c["line"], "code": "add_candidate_failed",
                                "detail": str(res.get("error_code"))})
                continue
            cid = res["candidate_id"]
            for branch in BRANCHES:
                dec = core.shadow_decide(conn, cid, branch, provider=provider)
                if not dec.get("ok"):
                    rejects.append({"line": c["line"], "code": "shadow_failed",
                                    "detail": f"{branch}:{dec.get('error_code')}"})
                    continue
                ev = dec.get("evidence") if isinstance(dec.get("evidence"), dict) else {}
                coverage = {k: ev.get(k) for k in
                            ("visible_events", "mandatory_sources", "ok_sources", "stale_ok_sources",
                             "error_sources", "missing_sources", "never_seen_sources", "freshness_min")
                            if k in ev} or None
                journal.append({
                    "candidate_id": c["id"], "external_ref": ext, "db_candidate_pk": cid,
                    "ticker": c["ticker"], "side": c["side"], "decision_time": c["decision_time"],
                    "price_strategy_version": c["price_strategy_version"],
                    "data_version": c["data_version"], "branch": branch,
                    "decision": dec["decision"], "reason_code": dec["reason_code"],
                    "rule_version": dec["rule_version"], "provider": dec["provider"],
                    "cutoff": c["decision_time"], "coverage": coverage, "evidence": ev,
                    "created": bool(dec.get("created"))})
    except Exception as exc:
        if conn is not None:
            conn.close()
        return _write_error(out, "journal_run_error", type(exc).__name__)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    out_db_sha = _sha_file(work) if work.exists() else None
    for row in journal:
        row["lineage"] = {"snapshot_sha256": snapshot_sha, "archive_main_sha256": archive_sha_main,
                          "candidates_sha256": cand_sha, "candidate_line_sha256":
                          next((c["line_sha256"] for c in valid
                                if c["id"] == row["candidate_id"]), None),
                          "core_sha256": core_sha, "output_db_sha256": out_db_sha}

    with (out / "journal.jsonl").open("w", encoding="utf-8") as f:
        for row in journal:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (out / "rejects.jsonl").open("w", encoding="utf-8") as f:
        for row in rejects:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    manifest = {
        "run_id": out.name, "tool": "news_candidate_journal.py", "provider": provider,
        "archive_mode": mode, "archive_input": str(archive) if archive else None,
        "snapshot_sha256": snapshot_sha, "archive_main_sha256": archive_sha_main,
        "snapshot_sha256_note": "точная идентичность данных (immutable backup до изменений)",
        "archive_main_sha256_note": "НЕАВТОРИТЕТНО при WAL/параллельном writer",
        "candidates_sha256": cand_sha, "core_sha256": core_sha, "output_db_sha256": out_db_sha,
        "candidates_valid": len(valid), "duplicates_idempotent": dup_ok,
        "journal_rows": len(journal), "rejects": len(rejects), "branches": list(BRANCHES),
        "outputs_sha256": {n: (_sha_file(out / n) if (out / n).exists() else None)
                           for n in ("journal.jsonl", "rejects.jsonl")},
        "note": "offline only; no analyze_item/API/collector/runtime; outcome/PnL not read",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    print(json.dumps({"ok": not rejects, "valid": len(valid), "journal_rows": len(journal),
                      "rejects": len(rejects), "mode": mode}, ensure_ascii=False))
    return 2 if rejects else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="A21 candidate↔news-shadow journal (offline)")
    ap.add_argument("--candidates", required=True, type=Path)
    ap.add_argument("--archive", type=Path, default=None)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--provider", default="deepseek")
    args = ap.parse_args(argv)
    try:
        return run(args.candidates, args.archive, args.out, args.provider)
    except Exception as exc:  # последний рубеж: без traceback
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        return _write_error(out, "unhandled_error", type(exc).__name__)


if __name__ == "__main__":
    raise SystemExit(main())
