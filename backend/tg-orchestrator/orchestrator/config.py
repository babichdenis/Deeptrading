"""Конфигурация оркестратора.

Все пути резолвятся относительно папки пакета, чтобы пакет можно было
перенести в любой проект. .env ищется в папке пакета, затем в корне проекта
(родитель пакета) и в текущей директории.
"""
import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_PKG_DIR)


def _load_env():
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    for p in (
        os.path.join(_PKG_DIR, ".env"),
        os.path.join(_PROJECT_ROOT, ".env"),
        os.path.join(os.getcwd(), ".env"),
    ):
        if os.path.exists(p):
            try:
                load_dotenv(p)
            except Exception:
                pass


_load_env()

# ---- Telegram ----
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
# Базовый URL Bot API. В РФ api.telegram.org заблокирован — укажи Cloudflare-прокси
# СО СУФФИКСОМ /bot, напр. https://<worker>.workers.dev/bot
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE") or None

# Режим получения входящих от Telegram:
#   "relay"   — store-and-forward через Cloudflare-воркер (нужен TELEGRAM_RELAY_*).
#   "polling" — долгий опрос напрямую (работает только вне РФ / через VPN).
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "relay")

# ---- Store-and-forward релей (Cloudflare Worker) ----
# Воркер принимает webhook -> кладёт апдейт в KV `relay:latest`,
# а локальный бот забирает его по GET /pull/<secret> (outbound-HTTPS, доступно из РФ).
TELEGRAM_RELAY_BASE = os.getenv("TELEGRAM_RELAY_BASE")   # https://<worker>.workers.dev
TELEGRAM_RELAY_SECRET = os.getenv("TELEGRAM_RELAY_SECRET")  # любой длинный секрет

# ---- opencode Server API (драйв агентских TUI-сессий) ----
OPENCODE_SERVER_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD")  # user=opencode
# Реестр: роль -> id сессии opencode. Можно задать здесь, либо в agents_registry.json.
SESSION_ARCHITECT = os.getenv("SESSION_ARCHITECT")
SESSION_EXECUTOR = os.getenv("SESSION_EXECUTOR")
SESSION_REVIEWER = os.getenv("SESSION_REVIEWER")
SESSION_BASE = os.getenv("SESSION_BASE", "oc://renderer/server/c2lkZWNhcg/session/")

# ---- Транспорт исполнителя ----
# "relay"     — оркестратор пишет задачу в файл и ждёт ответа от внешней сессии
# "deepseek"  — вызов DeepSeek API напрямую
# "opencode"  — оркестратор сам будит opencode-сессию через Server API
EXECUTOR_MODE = os.getenv("EXECUTOR_MODE", "relay")

# ---- Файлы и папки ----
ORCH_DIR = _PKG_DIR
PROJECT_ROOT = _PROJECT_ROOT

REPORTS_DIR = os.getenv("REPORTS_DIR", os.path.join(_PKG_DIR, "reports"))
os.makedirs(REPORTS_DIR, exist_ok=True)

STATE_PATH = os.getenv("STATE_PATH", os.path.join(_PKG_DIR, "state.json"))
CONTROL_PATH = os.getenv("CONTROL_PATH", os.path.join(_PKG_DIR, "control.json"))

# Изолированный канал апрува, запрошенного агентом через `orchestrator request`.
AGENT_PENDING_PATH = os.getenv("AGENT_PENDING_PATH", os.path.join(_PKG_DIR, "agent_pending.json"))
AGENT_DECISION_PATH = os.getenv("AGENT_DECISION_PATH", os.path.join(_PKG_DIR, "agent_decision.json"))
# Решение, которое пишет демон (owner-гейт) и внешние агенты.
DECISIONS_PATH = os.getenv("DECISIONS_PATH", os.path.join(_PKG_DIR, "decisions.json"))

# Файлы обмена с executor-сессией (relay-режим).
EXECUTOR_TASK_PATH = os.getenv("EXECUTOR_TASK_PATH", os.path.join(_PKG_DIR, "executor_task.md"))
EXECUTOR_RESPONSE_PATH = os.getenv("EXECUTOR_RESPONSE_PATH", os.path.join(_PKG_DIR, "executor_response.md"))

# pid/лог единого процесса бота.
BOT_PID_PATH = os.getenv("BOT_PID_PATH", os.path.join(_PKG_DIR, "bot.pid"))
BOT_LOG_PATH = os.getenv("BOT_LOG_PATH", os.path.join(_PKG_DIR, "bot.log"))

# Выбранный агент-адресат для текущего чата (главное меню в Telegram).
AGENT_TARGET_PATH = os.getenv("AGENT_TARGET_PATH", os.path.join(_PKG_DIR, "agent_target.json"))

POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "5"))

# Локальный REST/WS-мост оркестратора (опционален; если не запущен — игнорируется).
ORCHESTRATOR_API_BASE = os.getenv("ORCHESTRATOR_API_BASE", "http://127.0.0.1:8000")
