"""Реестр невалидных данных: диапазоны, где ряд записан в других единицах и с переставленными полями.

Пометка нужна, чтобы код и тесты не строили на испорченном участке: цена меньше
биржевой примерно в 4.27 раза, объём больше примерно в 200 раз, минимум и закрытие
перепутаны. Перестановкой полей такой участок не чинится, пока не перекачан.

Источник пометки — `configs/data_invalidity.json`, собранный
`scripts/audit_data_invalidity.py` по фактическим нарушениям вложения OHLC.
"""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

MANIFEST_PATH = Path(__file__).resolve().parents[2] / "configs" / "data_invalidity.json"


@lru_cache(maxsize=1)
def _manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"ranges": []}
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def cache_clear() -> None:
    _manifest.cache_clear()


def ranges_for(figi: str | None = None, interval: int | None = None) -> list[dict]:
    out = []
    for r in _manifest().get("ranges", []):
        if figi is not None and r["figi"] != figi:
            continue
        if interval is not None and int(r["interval"]) != int(interval):
            continue
        out.append(r)
    return out


def is_invalid(figi: str, ts: datetime, interval: int | None = None) -> bool:
    for r in ranges_for(figi, interval):
        start = datetime.fromisoformat(r["ts_from"])
        end = datetime.fromisoformat(r["ts_to"])
        if start <= ts <= end:
            return True
    return False


def overlaps(figi: str, ts_from: datetime, ts_to: datetime, interval: int | None = None) -> list[dict]:
    out = []
    for r in ranges_for(figi, interval):
        start = datetime.fromisoformat(r["ts_from"])
        end = datetime.fromisoformat(r["ts_to"])
        if start <= ts_to and ts_from <= end:
            out.append(r)
    return out


def affected_figis() -> set[str]:
    return {r["figi"] for r in _manifest().get("ranges", [])}


def summary() -> dict:
    ranges = _manifest().get("ranges", [])
    per_interval: dict[int, int] = {}
    for r in ranges:
        per_interval[int(r["interval"])] = per_interval.get(int(r["interval"]), 0) + 1
    return {
        "pairs": len(ranges),
        "figis": len(affected_figis()),
        "bad_bars": sum(int(r["bad_bars"]) for r in ranges),
        "per_interval": dict(sorted(per_interval.items())),
        "generated_at": _manifest().get("generated_at"),
    }
