"""Чистый снапшот макро-рядов: дедупликация и порядок меток.

Исходные файлы не изменяются. Результат — новый каталог `data/macro_snapshot_<дата>`
с `manifest.json` (провенанс: хэши и размеры источников и результата) и
`conflicts.csv` (метки, где OHLCV расходятся, — отдельно от точных дублей).
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BACKEND, "data")
FILES = ("brent_1m.csv", "brent_5m.csv", "gold_1m.csv", "gold_5m.csv",
         "usdrub_1m.csv", "usdrub_5m.csv", "imoex_5m.csv")


def _sha(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split(line: str) -> tuple[str, list[str], bool]:
    parts = line.rstrip("\n").split(";")
    first = parts[0].strip()
    if not first or first.startswith("ts") or first.startswith("figi"):
        return "", parts, True
    if "T" in first and "-" in first:
        return first, parts, True
    stamp = parts[1] if len(parts) > 1 else ""
    return stamp, parts, False


def _volume(parts: list[str], timestamp_first: bool) -> float:
    try:
        return float(parts[-2] if len(parts) >= 2 else 0)
    except ValueError:
        return 0.0


def process(path: str, out_path: str, conflict_rows: list[dict]) -> dict:
    source_sha = _sha(path)
    exact = 0
    conflicts = 0
    kept: dict[str, list[str]] = {}
    timestamp_first = False
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            stamp, parts, timestamp_first = _split(line)
            if not stamp or stamp.startswith("ts") or stamp.startswith("figi"):
                continue
            known = kept.get(stamp)
            if known is None:
                kept[stamp] = parts
                continue
            if known == parts:
                exact += 1
                continue
            conflicts += 1
            keep_new = _volume(parts, timestamp_first) > _volume(known, timestamp_first)
            conflict_rows.append({
                "file": os.path.basename(path),
                "ts": stamp,
                "kept": ";".join(parts if keep_new else known),
                "dropped": ";".join(known if keep_new else parts),
                "rule": "larger_volume" if keep_new else "first_kept",
            })
            if keep_new:
                kept[stamp] = parts
    stamps = sorted(kept)
    with open(out_path, "w", encoding="utf-8", newline="") as handle:
        for stamp in stamps:
            handle.write(";".join(kept[stamp]) + "\n")
    return {
        "source": os.path.basename(path),
        "source_sha256": source_sha,
        "source_size": os.path.getsize(path),
        "result": os.path.basename(out_path),
        "result_sha256": _sha(out_path),
        "result_size": os.path.getsize(out_path),
        "rows_in": sum(1 for _ in open(path, "r", encoding="utf-8", errors="replace")),
        "rows_out": len(stamps),
        "exact_duplicates_dropped": exact,
        "conflicts_resolved": conflicts,
        "first_ts": stamps[0] if stamps else None,
        "last_ts": stamps[-1] if stamps else None,
        "timestamp_first": timestamp_first,
    }


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_dir = os.path.join(DATA, f"macro_snapshot_{stamp}")
    os.makedirs(out_dir, exist_ok=True)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": DATA,
        "note": "исходные файлы не изменялись; дедупликация по метке, сортировка по метке",
        "files": [],
    }
    conflict_rows: list[dict] = []
    for name in FILES:
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            manifest["files"].append({"source": name, "missing": True})
            continue
        manifest["files"].append(process(path, os.path.join(out_dir, name), conflict_rows))
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
    with open(os.path.join(out_dir, "conflicts.csv"), "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["file", "ts", "kept", "dropped", "rule"])
        writer.writeheader()
        writer.writerows(conflict_rows)
    for item in manifest["files"]:
        if item.get("missing"):
            print(f"{item['source']}: нет файла")
            continue
        print(f"{item['source']}: {item['rows_in']} -> {item['rows_out']} "
              f"(точных дублей {item['exact_duplicates_dropped']}, "
              f"конфликтов {item['conflicts_resolved']}), "
              f"{item['first_ts']} .. {item['last_ts']}")
    print(f"снапшот: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
