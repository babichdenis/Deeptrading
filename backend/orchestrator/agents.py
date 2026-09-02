import os
import time
from datetime import datetime

from . import config, notifier

ROLE_EXECUTOR = (
    "Ты executor-агент. Ты меняешь ТОЛЬКО утверждённый RUNNING_EXPERIMENT. "
    "Выполняй техническую задачу, не меняй baseline, не запускай новые experiments без "
    "утверждения owner. Возвращай что сделал и кто следующий (architect / owner)."
)


def _task_header():
    return (
        "# Задача для executor-сессии (Hy3)\n"
        f"# Сессия executor:  {config.SESSION_BASE}{config.SESSION_EXECUTOR}\n"
        f"# Сессия архитектора: {config.SESSION_BASE}{config.SESSION_ARCHITECT}\n"
        f"# Время: {datetime.now().isoformat()}\n\n"
        "Вставь содержимое ниже в сессию executor (Hy3) и попроси выполнить задачу.\n"
        "Результат верни командой (из папки backend/):\n"
        "    python -m orchestrator submit \"@<файл_ответа>.md\"\n"
        "либо запиши ответ в executor_response.md.\n\n"
        "---\n\n"
    )


def write_executor_task(task):
    with open(config.EXECUTOR_TASK_PATH, "w") as f:
        f.write(_task_header() + task + "\n")
    return config.EXECUTOR_TASK_PATH


def dispatch_executor_task(task):
    """Записать задачу для executor-сессии и уведомить её (единоразово)."""
    path = write_executor_task(task)
    notifier.send(
        f"📤 Задача для executor готова → {path}\n"
        f"Сессия executor: {config.SESSION_BASE}{config.SESSION_EXECUTOR}\n"
        "Вставьте содержимое файла в сессию executor (Hy3) и верните ответ командой:\n"
        "    python -m orchestrator submit \"@<ответ>.md\"",
        category="info",
    )
    return path


def write_executor_response(text):
    with open(config.EXECUTOR_RESPONSE_PATH, "w") as f:
        f.write(text)
    return config.EXECUTOR_RESPONSE_PATH


def call_executor(task):
    if config.EXECUTOR_MODE == "deepseek":
        return _call_deepseek(task)
    if config.EXECUTOR_MODE == "opencode":
        return _call_opencode(task)
    return _call_relay(task)


def _extract_text(resp):
    """Вытянуть текстовый ответ из структуры /session/:id/message -> {info, parts}."""
    if isinstance(resp, str):
        return resp
    parts = (resp or {}).get("parts") or []
    chunks = []
    for p in parts:
        if isinstance(p, dict):
            if p.get("text"):
                chunks.append(p["text"])
            elif p.get("content"):
                for c in p["content"]:
                    if isinstance(c, dict) and c.get("text"):
                        chunks.append(c["text"])
    return "\n".join(chunks).strip() or str(resp)


def _call_opencode(task):
    """Executor = открытая TUI-сессия opencode, драйвер через Server API."""
    from . import opencode_client

    client, sid = opencode_client.resolve_agent("executor")
    if client is None or sid is None:
        notifier.send(
            "⚠️ opencode: executor-сессия не найдена. Проверь OPENCODE_SERVER_PASSWORD, "
            "agents_registry.json и что сессия запущена.",
            category="decision",
        )
        return None
    try:
        resp = client.ask(sid, task)
    except Exception as e:
        notifier.send(f"⚠️ opencode executor error: {e}", category="decision")
        return None
    text = _extract_text(resp)
    notifier.send(f"📥 Ответ executor (opencode) получен.", category="done")
    return text


def _call_deepseek(task):
    if not config.DEEPSEEK_API_KEY:
        return f"[STUB DeepSeek] received task: {task[:120]}"
    from openai import OpenAI

    client = OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
    resp = client.chat.completions.create(
        model=config.DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": ROLE_EXECUTOR},
            {"role": "user", "content": task},
        ],
    )
    return resp.choices[0].message.content


def _call_relay(task):
    # В relay-режиме задачу пишет оркестратор единожды при диспетчеризации
    # (см. core._dispatch_executor / CLI dispatch). Здесь только забираем ответ,
    # который внешняя сессия Hy3 кладёт в executor_response.md.
    if os.path.exists(config.EXECUTOR_RESPONSE_PATH):
        with open(config.EXECUTOR_RESPONSE_PATH) as f:
            resp = f.read()
        os.remove(config.EXECUTOR_RESPONSE_PATH)
        notifier.send("📥 Ответ executor получен.", category="done")
        return resp
    return None


def call_agent(role, task):
    if role in ("executor", "DeepSeek"):
        return call_executor(task)
    return "[architect/My3] выполняется владельцем или Hy3-архитектором вручную."
