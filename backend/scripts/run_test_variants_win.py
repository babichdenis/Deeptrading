"""Windows-раннер вариантов теста (replay 1 день) — аналог run_test_variants.py.

Для каждого варианта: старт uvicorn с env (TEST_GATES/TEST_VARIANT) → POST /mode test
→ ждём завершения реплея → собираем метрики сделок из БД (общая с .3).

Запуск: .venv\\Scripts\\python.exe scripts\\run_test_variants_win.py
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PY = str(BACKEND / ".venv" / "Scripts" / "python.exe")
API = "http://127.0.0.1:8000"
LOG = BACKEND / "test_variants.log"
VARIANTS = [
    ("h1_10m_gates", "on", "base"),
    ("momentum_v1", "on", "momentum"),
]

sys.path.insert(0, str(BACKEND))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _stop_uvicorn() -> None:
    """Убить слушателя порта 8000 (targeted, с таймаутом — Get-NetTCPConnection висит)."""
    try:
        out = subprocess.run(["cmd", "/c", "netstat -ano | findstr :8000 | findstr LISTENING"],
                             capture_output=True, text=True, timeout=20)
        pids = set()
        for line in (out.stdout or "").splitlines():
            parts = line.split()
            if parts and parts[-1].isdigit():
                pids.add(parts[-1])
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, timeout=20)
    except Exception as e:
        log(f"  stop_uvicorn: {type(e).__name__}: {e}")
    time.sleep(3)


def _start_uvicorn(gates: str, variant: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["TEST_GATES"] = gates
    env["TEST_VARIANT"] = variant
    env["PYTHONIOENCODING"] = "utf-8"
    out = open(str(BACKEND / "uvicorn_test.out"), "a", encoding="utf-8", errors="replace")
    return subprocess.Popen([PY, "-m", "uvicorn", "app.main:app",
                             "--host", "0.0.0.0", "--port", "8000"],
                            cwd=str(BACKEND), env=env, stdout=out, stderr=subprocess.STDOUT)


async def _stats(test_name: str) -> dict:
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    row = await c.fetchrow("""
        SELECT count(*) n, coalesce(sum(net_pnl),0) net,
               sum(CASE WHEN net_pnl>0 THEN 1 ELSE 0 END) wins,
               coalesce(sum(CASE WHEN net_pnl>0 THEN net_pnl ELSE 0 END),0) gw,
               coalesce(sum(CASE WHEN net_pnl<0 THEN net_pnl ELSE 0 END),0) gl
        FROM sandbox_trades WHERE test_name=$1""", test_name)
    await c.close()
    n = int(row["n"] or 0)
    gw, gl = float(row["gw"] or 0), float(row["gl"] or 0)
    return {"n": n, "net": round(float(row["net"] or 0), 1),
            "wr": round(int(row["wins"] or 0) / n * 100, 1) if n else 0.0,
            "pf": round(gw / abs(gl), 2) if gl else 0.0,
            "gw": round(gw, 1), "gl": round(gl, 1)}


async def main() -> None:
    log("=== старт серии вариантов на .2 ===")
    for name, gates, variant in VARIANTS:
        log(f"--- {name}: gates={gates} variant={variant}")
        _stop_uvicorn()
        proc = _start_uvicorn(gates, variant)
        ok = False
        for _ in range(60):
            time.sleep(3)
            try:
                _http("GET", f"{API}/api/health", timeout=5)
                ok = True
                break
            except Exception:
                continue
        if not ok:
            log("  uvicorn не поднялся — пропуск")
            proc.terminate()
            continue
        try:
            r = _http("POST", f"{API}/api/v1/bot/mode",
                      {"mode": "test", "test_name": name,
                       "replay_start": "2026-09-15T07:00:00+00:00",
                       "replay_end": "2026-09-15T15:00:00+00:00",
                       "replay_pace": "fast"})
            log(f"  запуск: {r.get('mode')} {r.get('test_name')}")
        except Exception as e:
            log(f"  ОШИБКА запуска: {e}")
            proc.terminate()
            continue
        started = False
        for _ in range(40):
            await asyncio.sleep(10)
            try:
                st = _http("GET", f"{API}/api/v1/bot/status", timeout=10)
            except Exception:
                continue
            if st.get("running") or st.get("starting"):
                started = True
                break
        t0 = time.monotonic()
        last, still = -1, 0
        while time.monotonic() - t0 < 7200:
            await asyncio.sleep(30)
            try:
                st = _http("GET", f"{API}/api/v1/bot/status", timeout=10)
            except Exception:
                continue
            seen = int(st.get("candles_seen") or 0)
            if seen != last:
                last, still = seen, 0
                if int((time.monotonic() - t0)) % 180 < 31:
                    log(f"  ... свечей {seen}, сигналов {st.get('signals_seen')}, "
                        f"{(time.monotonic()-t0)/60:.0f} мин")
            else:
                still += 1
            if started and not st.get("running") and not st.get("starting"):
                if still >= 2:
                    log("  реплей завершён")
                    break
            else:
                still = 0
        s = await _stats(name)
        log(f"  ИТОГ {name}: N={s['n']} net={s['net']:+.1f}₽ WR={s['wr']}% PF={s['pf']} "
            f"gross {s['gw']:+.1f}/{s['gl']:+.1f}")
        proc.terminate()
        time.sleep(5)
    log("=== серия завершена ===")


if __name__ == "__main__":
    asyncio.run(main())
