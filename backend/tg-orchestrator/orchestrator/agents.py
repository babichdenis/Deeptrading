"""Роли агентов и передача им задач.

Агенты общаются между собой только через оркестратор:
  - fire-and-forget:  `python -m orchestrator send <role> <text>`
  - запрос-ответ:     `python -m orchestrator ask <role> <text>`
  - просить апрув у владельца: `python -m orchestrator request "<вопрос>"`
  - сообщить владельцу:        `python -m orchestrator notify "<текст>"`

Все команды — это тонкие обёртки над CLI; сами агенты просто пишут
нужную команду в свою opencode-сессию, и оркестратор отрабатывает её.
"""
import os
import time

from . import config
from . import opencode_client


ROLE_EXECUTOR = """Ты — исполнитель (executor). Твоя задача — выполнить конкретную
работу, переданную оркестратором, и вернуть результат владельцу.

Как общаться с другими агентами (через оркестратор):
  - Спросить другого агента без блокировки:
      python -m orchestrator send <role> <текст>
  - Спросить и дождаться ответа:
      python -m orchestrator ask <role> <текст>
  - Попросить владельца согласовать (апрув) что-то важное:
      python -m orchestrator request "Нужно ли одобрить <X>?"
    (владелец получит в Telegram кнопки ✅/❌; команда вернёт 'approve'/'reject')
  - Отправить сообщение владельцу в Telegram:
      python -m orchestrator notify "<текст>"

Контекст источника запроса:
  - Если сообщение помечено как пришедшее из Telegram (обёртка 📨), отвечай
    лаконично — ответ покажут владельцу в Telegram. Не вызывай оркестратор
    в ответ, просто дай ответ.
  - Если ты запросил апрув через `request`, не нужно отвечать текстом в Telegram —
    владелец нажмёт кнопку, и твоя команда вернёт решение.

Результат своей работы сохраняй в папку reports/ и сообщай путь.
"""


def _task_header():
    return (
        "=== ЗАДАЧА ОТ ОРКЕСТРАТОРА ===\n"
        f"Сессия architect: {config.SESSION_BASE}{config.SESSION_ARCHITECT}\n"
        f"Сессия executor: {config.SESSION_BASE}{config.SESSION_EXECUTOR}\n"
        "=============================="
    )


def wrap_inbound(text, source, role=None):
    """Единый заголовок-маркер источника для любого вопроса агенту.

    source:
      'telegram'        — сообщение от владельца через Telegram
      'agent:<role>'    — запрос от другого агента (бота)
      'orchestrator'    — системный шаг оркестратора (план/исполнение)
    """
    if source == "telegram":
        head = "📨 ИСТОЧНИК: Telegram — сообщение от владельца."
        note = ("Ответь лаконично и по делу: твой ответ перешлют владельцу в "
                "Telegram. Не вызывай оркестратор в ответ — просто дай ответ.")
    elif isinstance(source, str) and source.startswith("agent:"):
        r = role or source.split(":", 1)[1]
        head = f"🤖 ИСТОЧНИК: другой агент — роль '{r}' (через оркестратор)."
        note = ("Это запрос от коллеги-агента. Ответь по существу. Если нужно "
                "решение владельца — сам вызови `orchestrator request \"...\"`.")
    else:
        head = "⚙️ ИСТОЧНИК: оркестратор (системный шаг)."
        note = "Это задача оркестратора по плану прогона."
    block = head
    if note:
        block += "\n" + note
    return f"{block}\n\n{text}"


def dispatch_executor_task(task, n, run_dir=None):
    """Записать задачу для executor и (если возможно) будить его сессию."""
    header = _task_header()
    body = (
        f"{header}\n\n"
        f"#{n} Задача:\n{task}\n\n"
        "Выполни задачу, сохрани результат в reports/ и сообщи кратко."
    )
    text = wrap_inbound(body, "orchestrator")
    if config.EXECUTOR_MODE == "opencode":
        client, sid = opencode_client.resolve_agent("executor")
        if client and sid:
            try:
                client.send_message(sid, text)
                return
            except Exception as e:
                print(f"[executor] не удалось будить сессию: {e}")
    with open(config.EXECUTOR_TASK_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def write_executor_response(text, n):
    with open(config.EXECUTOR_RESPONSE_PATH, "w", encoding="utf-8") as f:
        f.write(text)


def read_executor_response():
    try:
        with open(config.EXECUTOR_RESPONSE_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
