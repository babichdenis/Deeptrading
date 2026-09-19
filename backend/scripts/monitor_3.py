"""Монитор .3: следит за ботом/стримом/маржой/ошибками AI. Пишет /tmp/monitor3.log.

Если uvicorn упал — поднимает его сам (nohup, с cd — иначе nohup падает).
Запуск: screen -dmS monitor3 bash -lc "cd ~/Dev/Deeptrading/backend && exec .venv/bin/python3 scripts/monitor_3.py"
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx

API = "http://127.0.0.1:8000"
LOG = "/tmp/monitor3.log"
BACKEND = "/Users/Denis/Dev/Deeptrading/backend"
INTERVAL = 300  # 5 мин
MSK = timezone(timedelta(hours=3))


def log(msg: str) -> None:
    line = f"[{datetime.now(MSK).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get(path: str, timeout: float = 15.0) -> dict:
    try:
        r = httpx.get(f"{API}{path}", timeout=timeout)
        return r.json() if r.content else {}
    except Exception:
        return {}


def uvicorn_alive() -> bool:
    try:
        out = subprocess.run(["pgrep", "-f", "uvicorn app.main"],
                             capture_output=True, text=True, timeout=10)
        return bool(out.stdout.strip())
    except Exception:
        return True  # не смогли проверить — не трогаем


def start_uvicorn() -> None:
    log("⚠️ uvicorn не отвечает — поднимаю заново")
    subprocess.Popen(
        f"cd {BACKEND} && nohup .venv/bin/python3 -m uvicorn app.main:app "
        f"--host 0.0.0.0 --port 8000 --timeout-graceful-shutdown 10 "
        f"> /tmp/uvicorn_imoex.log 2>&1 &",
        shell=True)


_ai_log_pos: dict[str, int] = {}


def check_ai_log(path: str, name: str) -> int:
    """Свежие ошибки в логе AI — только НОВЫЕ строки с прошлой проверки.

    Раньше читались последние 200 строк целиком, и старые ошибки (до фикса)
    всплывали в мониторе каждый цикл как «свежие».
    """
    global _ai_log_pos
    try:
        size = os.path.getsize(path)
    except Exception:
        return 0
    if path not in _ai_log_pos:
        # Первый запуск: старые ошибки не считаем свежими — начинаем с конца файла.
        _ai_log_pos[path] = size
        return 0
    start = _ai_log_pos.get(path, 0)
    if start > size:  # лог перезаписан/ротирован
        start = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            lines = f.readlines()
            _ai_log_pos[path] = f.tell()
    except Exception:
        return 0
    errs = [l for l in lines if ("цикл:" in l or "Traceback" in l or "NameError" in l)]
    if errs:
        log(f"❌ {name}: свежие ошибки в логе ({len(errs)}), последняя: {errs[-1].strip()[:140]}")
    return len(errs)


def _env_bot_params() -> dict:
    """Параметры контура из backend/.env — чтобы авто-рестарт не запускал paper."""
    out = {"mode": "sandbox", "test_name": "", "replay_start": "", "replay_end": "",
           "replay_pace": "fast"}
    try:
        for ln in open(f"{BACKEND}/.env", encoding="utf-8"):
            ln = ln.strip()
            if "=" not in ln or ln.startswith("#"):
                continue
            k, v = ln.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "BOT_MODE" and v in ("sandbox", "live", "test"):
                out["mode"] = v
            elif k == "BOT_TEST_NAME":
                out["test_name"] = v
            elif k == "BOT_TEST_START":
                out["replay_start"] = v
            elif k == "BOT_TEST_END":
                out["replay_end"] = v
            elif k == "BOT_TEST_PACE" and v in ("fast", "wall"):
                out["replay_pace"] = v
    except Exception:
        pass
    return out


def _restart_bot() -> None:
    """Перезапустить бота в контуре из .env (POST /bot/mode с сохранёнными настройками).

    Раньше был POST /bot/start с пустым телом — он брал StartRequest.mode="paper"
    и бот молча уходил в бумажный режим (mode=paper), теряя live/sandbox.
    """
    p = _env_bot_params()
    try:
        httpx.post(f"{API}/api/v1/bot/mode", json=p, timeout=60)
        log(f"⚠️ бот НЕ запущен — перезапущен в контуре {p['mode']}")
    except Exception as e:
        log(f"⚠️ рестарт бота не удался: {type(e).__name__}: {str(e)[:80]}")


def main() -> None:
    log("монитор .3 запущен")
    _last_ok = True
    _bad = 0
    while True:
        try:
            st = get("/api/v1/bot/status")
            if not st:
                _bad += 1
                if not uvicorn_alive() or _bad >= 2:
                    start_uvicorn()
                    _bad = 0
                    time.sleep(60)
                else:
                    log("⚠️ API не отвечает, но процесс жив — жду")
                time.sleep(INTERVAL)
                continue
            _bad = 0
            running = bool(st.get("running"))
            starting = bool(st.get("starting"))
            if starting and not running:
                # Идёт прогрев (загрузка 32 тикеров из БД) — НЕ трогаем, иначе цикл рестартов.
                log("⏳ бот стартует (warmup) — жду")
                time.sleep(INTERVAL)
                continue
            health = (st.get("data") or {}).get("health")
            candles = st.get("candles_seen") or 0
            pos = (st.get("long_short") or {}).get("total")
            pf = get("/api/v1/bot/portfolio_summary")
            eq = pf.get("equity")
            mu = pf.get("margin_use_pct")
            if not running:
                log(f"⚠️ бот НЕ запущен (health={health}) — перезапускаю в контуре .env")
                _restart_bot()
            elif health not in ("HEALTHY", None):
                log(f"⚠️ стрим: health={health}, свечей={candles}")
            if mu is not None and float(mu) > 0.85:
                log(f"⚠️ МАРЖА {float(mu):.0%} — близко к лимиту (позиций {pos})")
            check_ai_log("/tmp/ai_trader_3.log", "ai_trader")
            check_ai_log("/tmp/ai_watch.log", "watch")
            if _last_ok is not True:
                log(f"✅ бот снова работает (equity {eq}, позиций {pos})")
            _last_ok = running
            log(f"ok: running={running} health={health} свечей={candles} "
                f"позиций={pos} equity={eq} маржа={mu}")
        except Exception as e:
            log(f"монитор: ошибка {type(e).__name__}: {str(e)[:100]}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
