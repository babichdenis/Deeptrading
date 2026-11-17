"""Аудит макро-рядов: точные дубли, конфликты по метке, нарушения порядка.

Ничего не перезаписывает: только читает `backend/data/*.csv` и пишет отчёт в
`backend/reports/macro_audit_<дата>/`.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
REPORTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
FILES = ("brent_1m.csv", "brent_5m.csv", "gold_1m.csv", "gold_5m.csv",
         "usdrub_1m.csv", "usdrub_5m.csv", "imoex_5m.csv")


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _audit_file(path: str) -> dict:
    rows = 0
    seen_stamp: dict[str, tuple[str, ...]] = {}
    exact_dupes = 0
    conflicts: list[dict] = []
    out_of_order = 0
    prev = None
    blank = 0
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                blank += 1
                continue
            parts = text.split(";")
            first = parts[0].strip()
            if not first:
                blank += 1
                continue
            if first.startswith("ts") or first.startswith("figi"):
                continue
            if "T" in first and "-" in first:
                stamp = first
            else:
                stamp = parts[1] if len(parts) > 1 else ""
            payload = tuple(parts)
            rows += 1
            if prev is not None and stamp < prev:
                out_of_order += 1
            prev = stamp if prev is None or stamp > prev else prev
            known = seen_stamp.get(stamp)
            if known is None:
                seen_stamp[stamp] = payload
                continue
            if known == payload:
                exact_dupes += 1
            else:
                conflicts.append({"ts": stamp, "first": list(known), "second": list(payload)})
    stamps = sorted(seen_stamp)
    return {
        "file": os.path.basename(path),
        "size": os.path.getsize(path),
        "sha256": _sha(path),
        "mtime": datetime.fromtimestamp(os.path.getmtime(path), timezone.utc).isoformat(),
        "lines": rows,
        "blank_lines": blank,
        "unique_stamps": len(stamps),
        "exact_duplicates": exact_dupes,
        "conflicting_stamps": len(conflicts),
        "conflict_examples": conflicts[:5],
        "out_of_order_lines": out_of_order,
        "first_ts": stamps[0] if stamps else None,
        "last_ts": stamps[-1] if stamps else None,
    }


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def main() -> int:
    data_dir = DATA
    if len(sys.argv) > 1:
        candidate = sys.argv[1] if os.path.isabs(sys.argv[1]) else os.path.join(DATA, sys.argv[1])
        if os.path.isdir(candidate):
            data_dir = candidate
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    is_snapshot = os.path.exists(os.path.join(data_dir, "manifest.json"))
    out_dir = os.path.join(REPORTS, f"macro_audit_{stamp}" + ("_snapshot" if is_snapshot else ""))
    os.makedirs(out_dir, exist_ok=True)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "data_dir": data_dir, "files": []}
    for name in FILES:
        path = os.path.join(data_dir, name)
        if not os.path.exists(path):
            report["files"].append({"file": name, "missing": True})
            continue
        report["files"].append(_audit_file(path))
    target = os.path.join(out_dir, "audit.json")
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    lines = ["# Аудит макро-рядов", "", f"Создан: {report['generated_at']}", "",
             "| файл | строк | уникальных меток | точных дублей | конфликтов | вне порядка | последняя метка |",
             "|---|---|---|---|---|---|---|"]
    for item in report["files"]:
        if item.get("missing"):
            lines.append(f"| {item['file']} | нет файла | | | | | |")
            continue
        lines.append(
            f"| {item['file']} | {item['lines']} | {item['unique_stamps']} | "
            f"{item['exact_duplicates']} | {item['conflicting_stamps']} | "
            f"{item['out_of_order_lines']} | {item['last_ts']} |")
    with open(os.path.join(out_dir, "audit.md"), "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nотчёт: {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
