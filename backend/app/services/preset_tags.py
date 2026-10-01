"""Сайдкар пресета и теги настроек теста — источник правды для Analytics.

Сайдкар `reports/presets/<test_name>.json` = {preset, payload, created_utc}
кладётся при запуске теста (POST /bot/mode с payload.preset или scripts/preset.py).
Единственный источник правды для тегов: CLI (`preset.py tags`), API аналитики,
UI карточки прогона. Схема пресета и контракт — docs/CONFIG_PRESETS.md.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_DIR = ROOT / "reports" / "presets"

BAD = set("\\/?%*:|\"<>")
NAME_LIMIT = 48  # сервер нормализует test_name до 48 символов в POST /bot/mode

SESS_RU = {"morning": "утро", "day": "день", "evening": "вечер"}
REGIME_RU = {
    "NEUTRAL": "нейтральный", "TREND_UP": "тренд вверх", "TREND_DOWN": "тренд вниз",
    "HIGH_VOLATILITY": "высокая волатильность", "RANGE": "диапазон",
}


def sanitize(name: str, limit: int = NAME_LIMIT) -> str:
    return "".join(c if c not in BAD else "_" for c in str(name)).strip()[:limit]


def sidecar_path(test_name: str) -> Path:
    return SIDECAR_DIR / f"{sanitize(test_name)}.json"


def load_sidecar(test_name: str) -> dict | None:
    path = sidecar_path(test_name)
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def save_sidecar(test_name: str, preset: dict, payload: dict) -> Path:
    path = sidecar_path(test_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"preset": preset, "payload": payload,
         "created_utc": datetime.now(timezone.utc).isoformat()},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def remove_sidecar(test_name: str) -> bool:
    path = sidecar_path(test_name)
    try:
        if path.is_file():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def load_all_sidecars() -> dict[str, dict]:
    """Все сайдкары, ключ — имя теста из payload.test_name (фолбэк — имя файла)."""
    out: dict[str, dict] = {}
    try:
        files = sorted(SIDECAR_DIR.glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = str(((data.get("payload") or {}).get("test_name")) or f.stem)
        out[name] = data
    return out


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "вкл" if value else "выкл"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _robots(p: dict) -> list[dict[str, str]]:
    h = p.get("harness") or {}
    items: list[dict[str, str]] = []
    for b in h.get("robots") or []:
        if not isinstance(b, dict):
            continue
        params = b.get("params") or {}
        items.append({"k": str(b.get("robot") or "?"),
                      "v": json.dumps(params, ensure_ascii=False) if params else "параметры по умолчанию"})
    return items


def tag_groups(p: dict) -> list[dict]:
    """Теги настроек, сгруппированные по смыслу (то, что рисует Analytics)."""
    h = p.get("harness") or {}
    r = p.get("runtime") or {}
    pr = p.get("preset") or {}
    t = (p.get("targets") or {}).get("replay") or {}
    money = r.get("money") or {}
    entry = r.get("entry") or {}
    rt_ex = r.get("exits") or {}
    bias = r.get("bias") or {}
    costs = h.get("costs") or {}
    period = h.get("period") or []
    sessions = "/".join(SESS_RU.get(s, str(s)) for s in (r.get("sessions") or [])) or "—"
    regimes = r.get("regimes")
    if isinstance(regimes, list):
        regimes = "/".join(REGIME_RU.get(str(x), str(x)) for x in regimes) or "—"

    groups: list[dict] = [
        {"group": "Пресет", "items": [
            {"k": "id", "v": _fmt(pr.get("id"))},
            {"k": "имя", "v": _fmt(pr.get("name"))},
        ]},
    ]
    robots = _robots(p)
    engine = str(t.get("engine") or "")
    if engine and (not robots or engine not in {r_["k"] for r_ in robots}):
        robots = [{"k": "движок", "v": engine}] + robots
    if robots:
        groups.append({"group": "Роботы", "items": robots})
    groups.append({"group": "Период", "items": [
        {"k": "период", "v": "..".join(str(x) for x in period) if period else "—"},
        {"k": "ТФ", "v": _fmt(h.get("timeframe") or t.get("interval"))},
        {"k": "бумаг", "v": str(len(h.get("universe") or []))},
        {"k": "темп", "v": _fmt(t.get("pace"))},
    ]})
    groups.append({"group": "Сессии", "items": [
        {"k": "вход", "v": sessions},
        {"k": "overnight", "v": _fmt(r.get("overnight"))},
    ]})
    ex_items = [
        {"k": "режим SL", "v": _fmt(rt_ex.get("sl_mode"))},
        {"k": "SL", "v": f"×{_fmt(rt_ex.get('initial_sl_atr'))} ATR"},
        {"k": "трейл", "v": _fmt(rt_ex.get("trail_activation_comm_mult"))},
        {"k": "выходы", "v": ",".join(str(x) for x in (h.get("exits") or [])) or "—"},
    ]
    groups.append({"group": "Выходы", "items": ex_items})
    groups.append({"group": "Деньги", "items": [
        {"k": "капитал", "v": _fmt(money.get("initial_cash"))},
        {"k": "qty/сделку", "v": _fmt(money.get("qty_per_trade"))},
        {"k": "позиция", "v": _fmt(money.get("pos_pct"))},
        {"k": "макс позиций", "v": _fmt(money.get("max_positions"))},
    ]})
    limits = [{"k": k, "v": _fmt(v)} for k, v in money.items()
              if k not in ("initial_cash", "qty_per_trade", "pos_pct", "max_positions")]
    if limits:
        groups.append({"group": "Лимиты", "items": limits})
    groups.append({"group": "Вход", "items": [
        {"k": "кворум", "v": _fmt(entry.get("quorum"))},
        {"k": "confirm", "v": _fmt(entry.get("confirm_flip"))},
        {"k": "cooldown", "v": _fmt(entry.get("cooldown_bars"))},
        {"k": "регимы", "v": _fmt(regimes) if regimes is not None else "—"},
    ]})
    groups.append({"group": "Bias", "items": [
        {"k": "bias", "v": _fmt(bias.get("enabled"))},
        {"k": "ТФ/период", "v": f"{_fmt(bias.get('tf'))}/{_fmt(bias.get('period'))}"},
    ]})
    groups.append({"group": "Гейты", "items": [
        {"k": "включены", "v": ", ".join(str(x) for x in (entry.get("gates") or [])) or "—"},
    ]})
    groups.append({"group": "Издержки", "items": [
        {"k": "комиссия", "v": _fmt(costs.get("commission"))},
        {"k": "слиппедж", "v": f"{_fmt(costs.get('slippage_bps'))} bps"},
    ]})
    return groups


def tags(p: dict) -> list[str]:
    """Плоский список для CLI (`preset.py tags`)."""
    out: list[str] = []
    for g in tag_groups(p):
        for item in g["items"]:
            out.append(f"{item['k']}: {item['v']}")
    return out


def preset_id(sidecar: dict | None) -> str | None:
    if not sidecar:
        return None
    return str(((sidecar.get("preset") or {}).get("preset") or {}).get("id") or "") or None


def tags_for_names(names: Iterable[str]) -> dict[str, dict]:
    """Имя теста -> {groups, preset_id} для списка тестов (только у кого есть сайдкар)."""
    all_sc = load_all_sidecars()
    out: dict[str, dict] = {}
    for name in names:
        sc = all_sc.get(name) or load_sidecar(name)
        if not sc:
            continue
        p = sc.get("preset") or {}
        out[name] = {"groups": tag_groups(p), "preset_id": preset_id(sc),
                     "notes": ((p.get("preset") or {}).get("notes") or "")}
    return out


__all__ = [
    "SIDECAR_DIR", "NAME_LIMIT", "SESS_RU", "REGIME_RU",
    "sanitize", "sidecar_path", "load_sidecar", "save_sidecar", "remove_sidecar",
    "load_all_sidecars", "tag_groups", "tags", "preset_id", "tags_for_names",
]
