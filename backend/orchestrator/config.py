import os

_ORCH_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_ORCH_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(os.path.join(_PROJECT_ROOT, ".env"))
    load_dotenv(os.path.join(_PROJECT_ROOT, "backend", ".env"))
except Exception:
    pass

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE") or None

# Режим получения входящих от Telegram: "polling" (долгий опрос через прокси)
# или "webhook" (нужен публичный endpoint/туннель — ngrok оказался заблокирован
# по IP, поэтому по умолчанию polling).
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "polling")
TELEGRAM_WEBHOOK_PORT = int(os.getenv("TELEGRAM_WEBHOOK_PORT", "8443"))

# Store-and-forward релей на Cloudflare-воркере (всегда доступен Telegram'у).
# Воркер принимает webhook -> кладёт апдейт в KV, а локальный бот сам забирает
# его по outbound-HTTPS (GET /pull/<secret>). Не требует публичного IP/туннеля.
TELEGRAM_RELAY_BASE = os.getenv("TELEGRAM_RELAY_BASE")  # напр. https://tg-relay-xxxx.workers.dev
TELEGRAM_RELAY_SECRET = os.getenv("TELEGRAM_RELAY_SECRET")  # общий секрет для /webhook и /pull

STATE_PATH = os.getenv("STATE_PATH", os.path.join(_ORCH_DIR, "state.json"))
STATUS_PATH = os.getenv("STATUS_PATH", os.path.join(_PROJECT_ROOT, "textdocs", "STATUS.md"))
DECISIONS_PATH = os.getenv("DECISIONS_PATH", os.path.join(_ORCH_DIR, "decisions.json"))
CONTROL_PATH = os.getenv("CONTROL_PATH", os.path.join(_ORCH_DIR, "control.json"))

# Каталоги и pid-файл единого процесса Telegram-бота. Бот обязан крутить
# polling в ГЛАВНОМ потоке своего процесса (иначе падает add_signal_handler),
# поэтому он запускается отдельным процессом `python3 -m orchestrator bot`,
# а не фоновым потоком.
ORCH_DIR = _ORCH_DIR
BACKEND_DIR = os.path.dirname(_ORCH_DIR)
BOT_PID_PATH = os.getenv("BOT_PID_PATH", os.path.join(_ORCH_DIR, "bot.pid"))
BOT_LOG_PATH = os.getenv("BOT_LOG_PATH", os.path.join(_ORCH_DIR, "bot.log"))

# Отдельный канал для апрува, запрошенного агентом через `orchestrator request`.
# Изолирован от DECISIONS_PATH, которым пользуется демон в owner-гейте, чтобы
# они не "съедали" друг у друга решение владельца.
AGENT_PENDING_PATH = os.getenv("AGENT_PENDING_PATH", os.path.join(_ORCH_DIR, "agent_pending.json"))
AGENT_DECISION_PATH = os.getenv("AGENT_DECISION_PATH", os.path.join(_ORCH_DIR, "agent_decision.json"))

EXECUTOR_MODE = os.getenv("EXECUTOR_MODE", "relay")
EXECUTOR_TASK_PATH = os.getenv("EXECUTOR_TASK_PATH", os.path.join(_ORCH_DIR, "executor_task.md"))
EXECUTOR_RESPONSE_PATH = os.getenv("EXECUTOR_RESPONSE_PATH", os.path.join(_ORCH_DIR, "executor_response.md"))

# opencode Server API: оркестратор драйвит открытые TUI-сессии напрямую.
# Каждая открытая сессия слушает свой HTTP-сервер на 127.0.0.1:<порт>;
# оркестратор находит их через lsof и обращается по API.
OPENCODE_SERVER_PASSWORD = os.getenv("OPENCODE_SERVER_PASSWORD")  # user=opencode
OPENCODE_AGENT_ARCHITECT_URL = os.getenv("OPENCODE_AGENT_ARCHITECT_URL") or None
OPENCODE_AGENT_EXECUTOR_URL = os.getenv("OPENCODE_AGENT_EXECUTOR_URL") or None

# Реестр сессий (роль -> id сессии opencode). Можно переопределить в .env
# через SESSION_<ROLE>, либо вести в agents_registry.json (см. opencode_client).
# По умолчанию — твои открытые сессии:
#   architect (планировщик) = ses_fc29...
#   executor  (builder)      = ses_fcd7...
SESSION_ARCHITECT = os.getenv("SESSION_ARCHITECT", "ses_fc29d2d4cffeTzqf5kwP1x1DAy")
SESSION_EXECUTOR = os.getenv("SESSION_EXECUTOR", "ses_fcd7bad25ffeBb5SMJbbmPD4bd")
SESSION_BASE = os.getenv("SESSION_BASE", "oc://renderer/server/c2lkZWNhcg/session/")

POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "5"))
ORCHESTRATOR_API_BASE = os.getenv("ORCHESTRATOR_API_BASE", "http://127.0.0.1:8000")
