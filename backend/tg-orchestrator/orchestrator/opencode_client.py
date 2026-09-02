"""Клиент к opencode Server API для драйва агентских TUI-сессий.

Агенты (architect/executor/...) живут в opencode-сессиях на sidecar-сервере.
Оркестратор будит их, отправляет вопрос/задачу и забирает ответ.
Аутентификация: HTTP Basic, user=`opencode`, password=`OPENCODE_SERVER_PASSWORD`.
"""
import json
import os
import time

import httpx

from . import config

REGISTRY_PATH = os.path.join(config._PKG_DIR, "agents_registry.json")
DEFAULT_REGISTRY = {
    "architect": config.SESSION_ARCHITECT,
    "executor": config.SESSION_EXECUTOR,
    "reviewer": config.SESSION_REVIEWER,
}


def load_registry():
    reg = dict(DEFAULT_REGISTRY)
    if os.path.exists(REGISTRY_PATH):
        try:
            with open(REGISTRY_PATH, "r", encoding="utf-8") as f:
                reg.update(json.load(f))
        except (OSError, ValueError):
            pass
    # нормализуем: допускаем список id -> берём первый
    out = {}
    for k, v in reg.items():
        if not v:
            continue
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            out[k] = v
    return out


def resolve_agent(role):
    """Вернуть (OpenCodeClient, sid) для роли или (None, None)."""
    reg = load_registry()
    sid = reg.get(role)
    if not sid:
        return None, None
    return OpenCodeClient(), sid


class OpenCodeClient:
    def __init__(self, host=None, password=None):
        self.host = (host or "127.0.0.1:49300")
        if ":" not in self.host:
            self.host = f"{self.host}:49300"
        self.password = password or config.OPENCODE_SERVER_PASSWORD
        self.base = f"http://{self.host}"

    def _req(self, method, path, json_body=None, timeout=240):
        url = f"{self.base}{path}"
        with httpx.Client(timeout=timeout) as client:
            r = client.request(
                method, url, json=json_body,
                auth=("opencode", self.password or ""),
            )
        if r.status_code == 401:
            raise RuntimeError("401 от opencode: неверный OPENCODE_SERVER_PASSWORD")
        r.raise_for_status()
        if r.text:
            try:
                return r.json()
            except ValueError:
                return {"raw": r.text}
        return {}

    def list_sessions(self):
        return self._req("GET", "/session")

    def send_message(self, sid, message):
        return self._req("POST", f"/session/{sid}/message", {"message": message})

    def session_messages(self, sid, limit=200):
        return self._req("GET", f"/session/{sid}/messages?limit={limit}")

    def ask(self, sid, message, timeout=240, poll=2.0):
        """Отправить сообщение и дождаться завершения работы агента (статус idle)."""
        self.send_message(sid, message)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msgs = self.session_messages(sid, limit=10)
            except Exception:
                msgs = {}
            items = msgs.get("messages", []) if isinstance(msgs, dict) else msgs
            if items:
                last = items[-1]
                parts = last.get("parts", []) if isinstance(last, dict) else []
                status = None
                for p in parts:
                    if isinstance(p, dict) and p.get("type") == "status":
                        status = p.get("status")
                if status in ("idle", "error"):
                    return msgs
            time.sleep(poll)
        return self.session_messages(sid, limit=10)


def _extract_text(resp):
    """Извлечь текстовый ответ агента из ответа session_messages."""
    if isinstance(resp, dict):
        items = resp.get("messages", [])
    elif isinstance(resp, list):
        items = resp
    else:
        return str(resp)
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        for p in m.get("parts", []):
            if isinstance(p, dict) and p.get("type") == "text":
                out.append(p.get("text", ""))
    return "\n".join(out).strip() or "(пустой ответ агента)"
