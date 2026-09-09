#!/usr/bin/env python3
"""Reset sandbox account: close old, open new, pay in cash, update ACC, restart bot.

Usage:
    .venv/bin/python3 scripts/reset_sandbox_account.py            # default 10000 rub
    .venv/bin/python3 scripts/reset_sandbox_account.py --cash 20000
    .venv/bin/python3 scripts/reset_sandbox_account.py --name V4_Bot_10k

Steps:
  1. stop bot (POST /api/v1/bot/stop)
  2. read current ACC from app/api/routes/sandbox.py + app/bot/live_broker.py (must match)
  3. close old sandbox account (positions are wiped with it)
  4. open new sandbox account
  5. pay in --cash rubles
  6. rewrite ACC in both files
  7. restart uvicorn (kill + nohup), wait healthy
  8. verify: /api/v1/sandbox/status shows new account cash
"""
import os
import re
import sys
import time
import signal
import argparse
import subprocess
from t_tech.invest import Client
from t_tech.invest.schemas import MoneyValue

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import get_settings

SB = "sandbox-invest-public-api.tbank.ru"
ACC_PAT = re.compile(r'ACC = "([0-9a-f-]{36})"')
ACC_FILES = [
    "app/api/routes/sandbox.py",
    "app/bot/live_broker.py",
]
BOT_URL = "http://127.0.0.1:8000"


def _get_client():
    settings = get_settings()
    token = settings.sandbox or settings.tinkoff_token
    return Client(token, target=SB).__enter__()


def _read_acc():
    accs = set()
    for f in ACC_FILES:
        s = open(f, encoding="utf-8").read()
        m = ACC_PAT.search(s)
        if not m:
            raise SystemExit(f"ACC not found in {f}")
        accs.add(m.group(1))
    if len(accs) != 1:
        raise SystemExit(f"ACC mismatch between files: {accs}")
    return accs.pop()


def _write_acc(new_acc):
    for f in ACC_FILES:
        s = open(f, encoding="utf-8").read()
        s2 = ACC_PAT.sub(f'ACC = "{new_acc}"', s, count=1)
        if s2 != s:
            open(f, "w", encoding="utf-8").write(s2)
            print(f"  ACC updated in {f}")
        else:
            print(f"  (no change in {f})")


def _http(url, method="GET", body=None):
    import urllib.request, json

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"null")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--name", default="V4_Bot_10k")
    args = ap.parse_args()

    svc = _get_client()
    old = _read_acc()
    print(f"current ACC: {old}")

    # 1) stop bot
    try:
        _http(f"{BOT_URL}/api/v1/bot/stop", "POST")
        print("bot stopped")
    except Exception as e:
        print(f"  (bot stop warn: {e})")

    # 2) close old account
    try:
        svc.sandbox.close_sandbox_account(account_id=old)
        print(f"closed old account: {old}")
    except Exception as e:
        print(f"  (close old warn: {e})")

    # 3) open new + pay in
    try:
        r = svc.sandbox.open_sandbox_account(name=args.name)
        new_acc = r.account_id
    except Exception as e:
        raise SystemExit(f"open new failed: {e}")
    print(f"opened new account: {new_acc}")

    try:
        svc.sandbox.sandbox_pay_in(account_id=new_acc, amount=MoneyValue(currency="rub", units=int(args.cash), nano=0))
        print(f"paid in {args.cash:.0f} rub")
    except Exception as e:
        raise SystemExit(f"pay_in failed: {e}")

    _write_acc(new_acc)

    # 4) restart uvicorn (keep vite alive)
    pids = subprocess.run(
        ["pgrep", "-f", "uvicorn app.main"], capture_output=True, text=True
    ).stdout.split()
    for pid in pids:
        try:
            os.kill(int(pid), signal.SIGKILL)
            print(f"killed uvicorn {pid}")
        except Exception as e:
            print(f"  (kill {pid} warn: {e})")
    time.sleep(2)

    env = dict(os.environ)
    logf = open("/tmp/uvicorn_reset.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        stdout=logf,
        stderr=logf,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        start_new_session=True,
    )
    print(f"uvicorn restarted, pid={proc.pid}")

    # 5) wait healthy + bot auto-start
    ok = False
    for _ in range(30):
        time.sleep(3)
        try:
            st = _http(f"{BOT_URL}/api/v1/bot/status")
            if st.get("running"):
                ok = True
            elif st.get("error"):
                print(f"  status error: {st['error']}")
            if ok:
                print(f"bot running (mode={st.get('mode')})")
                break
        except Exception:
            pass
    if not ok:
        raise SystemExit("uvicorn did not become healthy / bot not running; see /tmp/uvicorn_reset.log")

    # 6) verify sandbox
    try:
        st = _http(f"{BOT_URL}/api/v1/sandbox/status")
        pf = st.get("portfolio", {})
        print(f"sandbox verified: cash={pf.get('cash')} positions_open={pf.get('positions_open')}")
    except Exception as e:
        print(f"  (verify warn: {e})")

    print(f"\nDONE. new account id: {new_acc}")


if __name__ == "__main__":
    main()