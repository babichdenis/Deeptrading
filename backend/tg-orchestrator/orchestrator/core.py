"""Ядро оркестратора: конечный автомат прогона одной итерации.

Поток ролей: architect (план) -> executor (исполнение) -> owner (апрув).
Владелец вмешивается через Telegram-кнопки, когда автомат ждёт решения.
"""
import json
import os
import time

from . import config
from . import state as _st
from . import agents as _agents
from . import notifier as _notif


def _next_role(role):
    return {"architect": "executor", "executor": "owner"}.get(role, "architect")


def _dispatch_executor(s, n):
    task = s.get("task") or "Реализуй следующий шаг проекта."
    _agents.dispatch_executor_task(task, n)
    _notif.ensure_bot_running()
    _notif.Notifier().send(
        "executor получил задачу; жду результат в reports/.", category="new")


def _wait_owner_decision(run_id, question, options, document_path=None, timeout=600):
    n = _notif.Notifier()
    n.notify_decision_needed(run_id, question, options, document_path)
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = n.read_decision()
        if d:
            n.clear_decision_notify()
            return d
        time.sleep(config.POLL_INTERVAL_SEC)
    return None


def run_once(n, s):
    role = s.get("role", "architect")
    if role == "architect":
        _notif.Notifier().send(f"#{n} architect формирует план…", category="new")
        _dispatch_executor(s, n)
        s["role"] = _next_role(role)
    elif role == "executor":
        _notif.Notifier().send(f"#{n} executor исполняет…", category="new")
        _dispatch_executor(s, n)
        s["role"] = _next_role(role)
    else:  # owner gate
        decision = _wait_owner_decision(
            f"run-{n}", "Одобрить текущий шаг?", ["approve", "reject"])
        if decision == "approve":
            _notif.Notifier().send(f"#{n} владелец одобрил ✅", category="decision")
            s["role"] = "architect"
            s["n"] = n + 1
        else:
            _notif.Notifier().send(f"#{n} владелец отклонил ❌ / нет решения", category="decision")
            s["role"] = "architect"
    return s
