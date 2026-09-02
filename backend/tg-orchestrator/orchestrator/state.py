"""Простое состояние оркестратора (JSON-файл)."""
import json
import os

from . import config


def load():
    try:
        with open(config.STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(s):
    with open(config.STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def load_control():
    try:
        with open(config.CONTROL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def record(s, role, task):
    s.setdefault("log", []).append({"role": role, "task": task[:200]})
