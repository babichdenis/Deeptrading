"""Клиент к opencode Server API (opencode serve / открытые TUI-сессии).

Позволяет оркестратору «будить» сессии агентов напрямую по HTTP, без
ручного копирования файлов между окнами. Каждая открытая TUI-сессия
поднимает свой HTTP-сервер на 127.0.0.1:<случайный порт>; оркестратор
находит их через `discover_opencode_servers()` и обращается по API:

  POST /session/:id/prompt_async  — разбудить агента (fire-and-forget)
  POST /session/:id/message       — отправить и дождаться ответа
  GET  /session                   — список сессий на сервере
  GET  /global/health             — проверка живости

Авторизация: HTTP Basic, user=`opencode`, password=`OPENCODE_SERVER_PASSWORD`.
"""
import base64
import json
import os
import subprocess
import urllib.error
import urllib.request

from . import config


class OpenCodeClient:
    def __init__(self, base_url, password=None):
        self.base = base_url.rstrip("/")
        self.pw = password

    def _req(self, method, path, body=None):
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.pw:
            tok = base64.b64encode(f"opencode:{self.pw}".encode()).decode()
            headers["Authorization"] = f"Basic {tok}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                raw = r.read().decode()
                ct = r.headers.get("Content-Type", "")
                return json.loads(raw) if ct.startswith("application/json") else raw
        except urllib.error.HTTPError as e:
            raise RuntimeError(
                f"opencode API {method} {path} -> {e.code}: {e.read().decode()[:200]}"
            )

    def health(self):
        return self._req("GET", "/global/health")

    def sessions(self):
        return self._req("GET", "/session")

    def wake(self, session_id, text):
        """Разбудить сессию без ожидания ответа (огонь-и-забудь)."""
        return self._req(
            "POST",
            f"/session/{session_id}/prompt_async",
            {"parts": [{"type": "text", "text": text}]},
        )

    def ask(self, session_id, text, agent=None, model=None, no_reply=False):
        """Отправить задачу и (по умолчанию) дождаться ответа агента."""
        body = {"parts": [{"type": "text", "text": text}], "noReply": no_reply}
        if agent:
            body["agent"] = agent
        if model:
            body["model"] = model
        return self._req("POST", f"/session/{session_id}/message", body)


def _opencode_listen_ports():
    try:
        out = subprocess.run(
            ["lsof", "-i", "-P", "-sTCP:LISTEN", "-n"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except Exception:
        return []
    ports = set()
    for line in out.splitlines():
        if not line.startswith("OpenCode"):
            continue
        if "127.0.0.1:" in line and "(LISTEN)" in line:
            port = line.split("127.0.0.1:")[1].split()[0]
            ports.add(port)
    return sorted(ports)


def discover_opencode_servers(password=None):
    """Вернуть список base_url открытых opencode-серверов.

    Сервер считается opencode, если /global/health, /session или /doc
    отвечают 200 или 401 (401 = защищён basic-auth, значит API есть).
    """
    servers = []
    for port in _opencode_listen_ports():
        url = f"http://127.0.0.1:{port}"
        found = False
        for path in ("/global/health", "/session", "/doc"):
            try:
                req = urllib.request.Request(f"{url}{path}", method="GET")
                with urllib.request.urlopen(req, timeout=3) as r:
                    found = True
                    break
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    found = True
                    break
            except Exception:
                continue
        if found:
            servers.append(url)
    return servers


def find_session_url(session_id, password=None, servers=None):
    """Найти base_url сервера, на котором есть сессия session_id."""
    servers = servers or discover_opencode_servers(password)
    for base in servers:
        client = OpenCodeClient(base, password)
        try:
            sessions = client.sessions()
        except Exception:
            continue
        items = sessions.values() if isinstance(sessions, dict) else sessions
        for s in items:
            if s.get("id") == session_id:
                return base
    return None


def load_registry():
    """Загрузить реестр роль -> [session_id].

    Приоритет: agents_registry.json (существующий файл в папке оркестратора),
    иначе одиночные SESSION_<ROLE> из config/.env.
    Формат agents_registry.json:
        {"architect": ["ses_..."], "executor": ["ses_..."], ...}
    """
    reg_path = os.path.join(config._ORCH_DIR, "agents_registry.json")
    if os.path.exists(reg_path):
        try:
            with open(reg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    # фолбэк: по одной сессии на роль из config
    reg = {}
    for role in ("architect", "executor", "builder", "reviewer", "owner"):
        sid = getattr(config, f"SESSION_{role.upper()}", None)
        if sid:
            reg[role] = [sid]
    return reg


_rr = {}  # round-robin индекс по ролям (в рамках процесса демона)


def resolve_agent(role, password=None, registry=None):
    """Вернуть (OpenCodeClient, session_id) для роли, либо (None, None).

    - если задан OPENCODE_AGENT_<ROLE>_URL — берём его и session_id из реестра/конфига;
    - иначе перебираем серверы и ищем сессию по id из реестра;
    - при нескольких id одной роли — round-robin.
    """
    password = password or config.OPENCODE_SERVER_PASSWORD
    registry = registry or load_registry()
    ids = registry.get(role, [])
    if not ids:
        sid = getattr(config, f"SESSION_{role.upper()}", None)
        if sid:
            ids = [sid]
    if not ids:
        return None, None

    idx = _rr.get(role, 0) % len(ids)
    _rr[role] = idx + 1
    session_id = ids[idx]

    base = None
    explicit = getattr(config, f"OPENCODE_AGENT_{role.upper()}_URL", None)
    if explicit:
        base = explicit
    else:
        base = find_session_url(session_id, password)
    if not base:
        return None, session_id  # id известен, но сервер не найден
    return OpenCodeClient(base, password), session_id
