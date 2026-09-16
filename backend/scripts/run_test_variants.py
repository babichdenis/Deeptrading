"""Прогон вариантов теста (replay 1 день) с разными гейтами/якорем кворума.

Для каждого варианта: рестарт uvicorn с env (TEST_GATES/TEST_VARIANT) →
POST /mode test → ждём завершения реплея → собираем метрики сделок.

Запуск: .venv/bin/python3 scripts/run_test_variants.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import urllib.request
import json

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

API = "http://127.0.0.1:8000"
BACKEND = os.path.expanduser("~/Dev/Deeptrading/backend")
VARIANTS = [
    # (test_name, gates, variant)
    ("h1_10m_gates", "on", "base"),
    ("h1_10m_nogates", "off", "base"),
    ("h1_10m_macd1", "on", "macd1"),
    ("h1_10m_macd2", "on", "macd2"),
]


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 30.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _restart(gates: str, variant: str) -> None:
    subprocess.run(["bash", "-lc", "pkill -f 'uvicorn app.main' || true"], check=False)
    time.sleep(4)
    env = dict(os.environ, TEST_GATES=gates, TEST_VARIANT=variant)
    subprocess.Popen(
        ["bash", "-lc",
         f"cd {BACKEND} && nohup .venv/bin/python3 -m uvicorn app.main:app "
         f"--host 0.0.0.0 --port 8000 > /tmp/uvicorn_imoex.log 2>&1 &"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(3)
        try:
            _http("GET", f"{API}/api/health", timeout=5)
            return
        except Exception:
            continue
    raise RuntimeError("uvicorn не поднялся")


async def _stats(test_name: str) -> dict:
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    row = await c.fetchrow("""
        SELECT count(*) n,
               coalesce(sum(net_pnl), 0) net,
               sum(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) wins,
               coalesce(sum(CASE WHEN net_pnl > 0 THEN net_pnl ELSE 0 END), 0) gw,
               coalesce(sum(CASE WHEN net_pnl < 0 THEN net_pnl ELSE 0 END), 0) gl
        FROM sandbox_trades WHERE test_name = $1""", test_name)
    await c.close()
    n = int(row["n"] or 0)
    gw, gl = float(row["gw"] or 0), float(row["gl"] or 0)
    return {"n": n, "net": round(float(row["net"] or 0), 1),
            "wr": round(int(row["wins"] or 0) / n * 100, 1) if n else 0.0,
            "pf": round(gw / abs(gl), 2) if gl else 0.0,
            "gw": round(gw, 1), "gl": round(gl, 1)}


async def main() -> None:
    for name, gates, variant in VARIANTS:
        print(f"\n=== {name}: gates={gates} variant={variant}", flush=True)
        _restart(gates, variant)
        try:
            r = _http("POST", f"{API}/api/v1/bot/mode",
                      {"mode": "test", "test_name": name,
                       "replay_start": "2026-09-15T07:00:00+00:00", "replay_end": "2026-09-15T15:00:00+00:00",
                       "replay_pace": "fast"})
            print("  запуск:", r.get("mode"), r.get("test_name"), flush=True)
        except Exception as e:
            print("  ОШИБКА запуска:", e, flush=True)
            continue
        # ждём старта рантайма (POST /mode перезапускает его внутри uvicorn)
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
        last = -1
        still = 0
        while time.monotonic() - t0 < 7200:
            await asyncio.sleep(30)
            try:
                st = _http("GET", f"{API}/api/v1/bot/status", timeout=10)
            except Exception:
                continue
            seen = int(st.get("candles_seen") or 0)
            if seen != last:
                still = 0
                last = seen
                if (time.monotonic() - t0) % 180 < 31:
                    print(f"  ... свечей {seen}, сигналов {st.get('signals_seen')}, "
                          f"{(time.monotonic()-t0)/60:.0f} мин", flush=True)
            else:
                still += 1
            if started and not st.get("running") and not st.get("starting"):
                if still >= 2:
                    print("  реплей завершён", flush=True)
                    break
            else:
                still = 0
        s = await _stats(name)
        print(f"  ИТОГ {name}: N={s['n']} net={s['net']:+.1f}₽ WR={s['wr']}% PF={s['pf']} "
              f"gross {s['gw']:+.1f}/{s['gl']:+.1f}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
