"""run_state.py — единственное место, где лежит состояние «что должно работать
после перезапуска»: контур и параметры последнего тестового прогона.

Почему не .env и не os.environ:
- .env — деплой (токены, порты, БД). Туда положили BOT_TEST_* по исторической
  случайности, и рядом с ними не оказалось TEST_ENGINE/TEST_INTERVAL.
- os.environ живёт только в процессе: после рестарта TEST_ENGINE исчезал, и
  контур молча возвращался с rsi_trade_hub на ensemble_v4 (реплей на порядок
  медленнее, конфиг UI врёт).
- data/bot_config.json — состояние настроек бота (там уже всё, что правит UI),
  логично держать рядом и «что запускать», в одном файле, а не в четырёх.

Формат data/run_state.json (пишет POST /api/v1/bot/mode, читает автостарт):

    {"mode": "test", "test_name": "...", "replay_start": "...", "replay_end": "...",
     "replay_pace": "fast", "replay_log_persist": false, "test_engine": "rsi_trade_hub",
     "test_interval": "10min", "test_params": {}, "preset": {}, "updated_at": "..."}

Если файла нет (первый запуск, старая установка) — resolve_boot_state() отдаёт
легаси-фолбэк из .env и честно сообщает источник, чтобы это было видно в логе.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

RUN_STATE_FILE = Path(__file__).resolve().parents[2] / "data" / "run_state.json"

# Ключи состояния прогона. Всё, что здесь не перечислено, в файл не пишется:
# мусор из старых версий не должен накапливаться.
RUN_STATE_KEYS = (
    "mode",
    "test_name",
    "replay_start",
    "replay_end",
    "replay_pace",
    "replay_log_persist",
    "test_engine",
    "test_interval",
    "test_params",
    "preset",
    # Решение владельца в модалке: "preset" (едем по пресету) | "ui" (по ползункам).
    # Пусто = пресет не выбирали. Сохраняется, чтобы рестарт не поменял контур
    # тихо: apply_test_overrides смотрит TEST_PRESET_MODE.
    "preset_mode",
)

# Ключи os.environ, которые apply_test_overrides читает как оверрайды теста.
# Файл — источник истины, env — кэш внутри процесса (см. sync_env).
_ENV_KEYS = {
    "test_engine": "TEST_ENGINE",
    "test_interval": "TEST_INTERVAL",
    "test_params": "TEST_PARAMS",
    "preset": "TEST_PRESET",
    "preset_mode": "TEST_PRESET_MODE",
}


def _json_str(value: Any) -> str:
    """Компактный JSON без пробелов — иначе .env/парсер режет значение."""
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load_run_state(path: Path | None = None) -> dict:
    """Прочитать состояние прогона. Нет файла/битый JSON — пустой dict, без исключений."""
    p = Path(path) if path else RUN_STATE_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: raw[k] for k in RUN_STATE_KEYS if k in raw}


def save_run_state(state: dict, path: Path | None = None) -> bool:
    """Атомарно записать состояние прогона. Только RUN_STATE_KEYS + updated_at."""
    p = Path(path) if path else RUN_STATE_FILE
    clean = {k: state[k] for k in RUN_STATE_KEYS if k in state}
    clean["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        return False
    return True


def sync_env(state: dict) -> None:
    """Обновить process-env кэш оверрайдов под apply_test_overrides.

    Среда — производная от файла, а не самостоятельный источник: на старте
    сервера она пересобирается из run_state.json (main.py → sync_env).
    Пустое значение = убрать ключ, чтобы прошлый движок не всплыл.
    """
    for key, env_key in _ENV_KEYS.items():
        raw = state.get(key)
        val = raw if isinstance(raw, str) else _json_str(raw)
        if val:
            os.environ[env_key] = val
        else:
            os.environ.pop(env_key, None)


def _legacy_state(settings: Any = None) -> dict:
    """Состояние из .env/окружения — для установок без run_state.json."""
    def _s(name: str, default: str = "") -> str:
        if settings is None:
            return default
        val = getattr(settings, name, default)
        return val if isinstance(val, str) else default

    def _env(name: str, default: str = "") -> str:
        return str(os.environ.get(name, default) or default).strip()

    state = {
        "mode": _s("bot_mode") or _env("BOT_MODE", "sandbox"),
        "test_name": _s("bot_test_name") or _env("BOT_TEST_NAME"),
        "replay_start": _s("bot_test_start") or _env("BOT_TEST_START"),
        "replay_end": _s("bot_test_end") or _env("BOT_TEST_END"),
        "replay_pace": _s("bot_test_pace") or _env("BOT_TEST_PACE", "fast"),
        "replay_log_persist": _env("BOT_TEST_LOG_PERSIST", "0") in ("1", "true", "on", "yes"),
        "test_engine": _env("TEST_ENGINE"),
        "test_interval": _env("TEST_INTERVAL"),
        "test_params": {},
        "preset": {},
    }
    if state["mode"] not in ("sandbox", "live", "test"):
        state["mode"] = "sandbox"
    return state


def resolve_boot_state(settings: Any = None, path: Path | None = None) -> tuple[dict, str]:
    """Состояние для автостарта: (state, source).

    source — "run_state.json" или ".env (legacy)"; вызывающий логирует, чтобы
    молчаливая подмена источника не повторилась.
    """
    state = load_run_state(path)
    if state:
        if not state.get("mode"):
            state["mode"] = "test" if state.get("test_name") else "sandbox"
        return state, "run_state.json"
    return _legacy_state(settings), ".env (legacy, run_state.json нет)"
