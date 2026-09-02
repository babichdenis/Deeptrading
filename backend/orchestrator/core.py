import hashlib
import os
import time

from . import config, agents, state, notifier
from . import opencode_client


def _file_hash(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _dispatch_executor(s, n):
    """Записать задачу для executor-сессии (relay/deepseek) и уведомить."""
    agents.dispatch_executor_task(s["next_action"])


def _next_role(role):
    """Простая политика перехода между автоматизированными агентами.

    planner (architect) -> исполнитель (executor/builder) -> гейт владельца.
    """
    if role == "architect":
        return "executor"
    return "owner"  # после исполнения — ревью/апрув владельца


def run_once(n, s):
    """Один шаг конечного автомата. Возвращает True, если состояние изменилось."""
    owner = s.get("next_action_owner")

    # ---- Режим opencode: универсальный драйв агентов через Server API ----
    if config.EXECUTOR_MODE == "opencode" and owner not in (None, "owner", "My3"):
        if not s.get("next_action"):
            return False  # ждём, пока владелец задаст задачу (CLI task / решение)
        client, sid = opencode_client.resolve_agent(owner)
        if client is None:
            notifier.send(
                f"⚠️ opencode: нет сессии для роли '{owner}'. "
                f"Проверь agents_registry.json / OPENCODE_SERVER_PASSWORD.",
                category="decision",
            )
            return False
        out = client.ask(sid, s["next_action"])
        if out is None:
            return False  # ждём ответа агента
        state.record(s, owner, s["next_action"])
        n.send(
            f"[{owner}] завершил: {s['next_action'][:80]} -> {out[:80]}",
            category="done",
        )
        s["last_completed_by"] = owner
        s["last_task"] = s["next_action"]
        nxt = _next_role(owner)
        # результат планировщика передаём исполнителю; после исполнения — на ревью
        s["next_action"] = out if nxt != "owner" else None
        s["next_action_owner"] = nxt
        return True

    # ---- Режим relay/deepseek: архитектор ставит задачу и передаёт executor ----
    if owner == "architect":
        if not s.get("next_action"):
            return False
        s["next_action_owner"] = "executor"
        _dispatch_executor(s, n)
        return True

    if owner == "executor":
        out = agents.call_executor(s["next_action"])
        if out is None:
            return False
        state.record(s, "executor", s["next_action"])
        n.send(
            f"[executor] завершил: {s['next_action'][:80]} -> {out[:80]}",
            category="done",
        )
        s["last_completed_by"] = "executor"
        s["last_task"] = s["next_action"]
        s["next_action"] = None
        s["next_action_owner"] = "architect"
        return True

    # ---- Гейт согласования (owner / My3 / любая человеко-роль) ----
    if not isinstance(s.get("pending_decision"), dict):
        s["pending_decision"] = {
            "run_id": s.get("active_running_experiment") or "?",
            "question": "Review / утверждение: проверьте результат и одобрите следующий шаг.",
            "options": ["approve", "reject"],
        }
    n.notify_decision_needed(
        s["pending_decision"]["run_id"],
        s["pending_decision"]["question"],
        s["pending_decision"]["options"],
    )
    choice = notifier.read_decision()
    if choice is None:
        return False
    n.clear_decision_notify()
    s["last_completed_by"] = owner
    s["pending_decision"] = None
    if choice in ("approve", "y", "yes"):
        if s.get("next_action"):
            s["next_action_owner"] = "executor"
            _dispatch_executor(s, n)
        else:
            s["next_action_owner"] = "architect"
    else:
        s["next_action_owner"] = "architect"
    return True


def main():
    n = notifier.Notifier()
    mode = config.EXECUTOR_MODE
    mode_msg = "relay (Hy3)" if mode == "relay" else ("deepseek" if mode == "deepseek" else "opencode")
    n.send(
        f"Orchestrator запущен. Канал: REST-мост (event_bus -> WS). Режим executor: {mode_msg}.\n"
        f"Архитектор: {config.SESSION_BASE}{config.SESSION_ARCHITECT}\n"
        f"Executor:   {config.SESSION_BASE}{config.SESSION_EXECUTOR}",
        category="info",
    )

    if config.TELEGRAM_BOT_TOKEN:
        if not notifier.ensure_bot_running():
            n.send("Telegram-бот не запущен. Решения — через decisions.json.", category="info")

    s = state.load()
    watched = {
        config.EXECUTOR_TASK_PATH: "📤 executor_task.md изменён",
        config.EXECUTOR_RESPONSE_PATH: "📥 executor_response.md изменён",
        config.STATUS_PATH: "📊 STATUS.md изменён",
    }
    hashes = {}
    for p in watched:
        hashes[p] = _file_hash(p)
    try:
        while True:
            ctrl = state.load_control()
            if not ctrl.get("paused"):
                if run_once(n, s):
                    state.save(s)
            for p, label in watched.items():
                h = _file_hash(p)
                if h is not None and hashes.get(p) is not None and h != hashes[p]:
                    n.send(f"{label} → {p}", category="info")
                hashes[p] = h
            time.sleep(config.POLL_INTERVAL_SEC)
    except KeyboardInterrupt:
        n.send("Orchestrator остановлен.", category="info")
