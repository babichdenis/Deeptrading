#!/usr/bin/env python3
"""Reset sandbox account одной командой — через эндпойнт работающего бэкенда.

Сам сброс выполняет сам бэкенд (POST /api/v1/sandbox/reset):
  1. останавливает бота;
  2. закрывает текущий sandbox-счёт (вместе со всеми его позициями);
  3. заводит новый с --name и пополняет на --cash;
  4. стирает сделки/позиции/историю этой сессии из БД;
  5. перезапускает бота на новом счёте.

Скрипт только зовёт эндпойнт на том хосте, где крутится бот (по умолчанию
localhost; для Windows-хоста: --host http://192.168.1.2:8000), синхронизирует
локальный backend/.env и проверяет результат.

Usage:
    .venv/bin/python3 scripts/reset_sandbox_account.py
    .venv/bin/python3 scripts/reset_sandbox_account.py --host http://192.168.1.2:8000
    .venv/bin/python3 scripts/reset_sandbox_account.py --cash 20000 --name NewBot
    .venv/bin/python3 scripts/reset_sandbox_account.py --keep-history   # счёт без чистки сделок
"""
import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ENV_PATH = BACKEND / ".env"


def _http(base, path, method="GET", body=None, timeout=180):
    req = urllib.request.Request(
        f"{base.rstrip('/')}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as he:
        try:
            detail = json.loads(he.read() or b"null").get("detail")
        except Exception:
            detail = None
        raise RuntimeError(f"HTTP {he.code}: {he.reason}"
                           + (f" — {detail}" if detail else "")) from None


def _sync_local_env(new_acc: str) -> str:
    """Best-effort: обновить SANDBOX_ACCOUNT в локальном backend/.env (источник-репо)."""
    if not ENV_PATH.exists():
        return "нет local .env"
    txt = ENV_PATH.read_text(encoding="utf-8")
    if re.search(r"(?m)^SANDBOX_ACCOUNT=.*$", txt):
        txt2 = re.sub(r"(?m)^SANDBOX_ACCOUNT=.*$", f"SANDBOX_ACCOUNT={new_acc}", txt, count=1)
    else:
        txt2 = txt.rstrip() + f"\nSANDBOX_ACCOUNT={new_acc}\n"
    if txt2 == txt:
        return "уже актуально"
    ENV_PATH.write_text(txt2, encoding="utf-8")
    return "обновлён"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="http://127.0.0.1:8000",
                    help="адрес работающего бот-бэкенда (для Windows: http://192.168.1.2:8000)")
    ap.add_argument("--cash", type=float, default=10_000.0)
    ap.add_argument("--name", default="V4_Bot_10k")
    ap.add_argument("--keep-history", action="store_true",
                    help="не чистить сделки/историю (только счёт)")
    args = ap.parse_args()

    print(f"сброс sandbox → {args.host} (cash={args.cash:.0f}, name={args.name})")
    res = _http(args.host, "/api/v1/sandbox/reset", "POST", {
        "cash": args.cash, "name": args.name, "keep_history": args.keep_history,
    })
    print(f"  новый счёт:        {res.get('account_id')} ({res.get('account_name')})")
    if res.get("closed_old"):
        print(f"  закрыт старый:     {res['closed_old']}")
    else:
        print("  старый счёт:       уже отсутствовал")
    print(f"  бот перезапущен:   {res.get('restarted')}")
    if res.get("restart_error"):
        print(f"  ⚠ restart error:   {res['restart_error']}")
    print(f"  очищено сделок/БД: {res.get('wiped')}")
    if not args.keep_history:
        print(f"  локальный .env:    {_sync_local_env(res['account_id'])}")

    # Проверка на живом бэкенде.
    time.sleep(2)
    try:
        st = _http(args.host, "/api/v1/sandbox/status")
        pf = st.get("portfolio", {})
        print(f"  статус:            cash={pf.get('cash')} equity={pf.get('equity')} "
              f"positions={pf.get('positions_open')} reconcile_ok={pf.get('reconcile', {}).get('ok')}")
        st_bot = _http(args.host, "/api/v1/bot/status")
        print(f"  бот:               running={st_bot.get('running')} mode={st_bot.get('broker_mode')}")
        tr = _http(args.host, "/api/v1/bot/trades?limit=5")
        print(f"  сделок в истории:  {tr.get('count')}")
    except Exception as e:
        print(f"  (проверка: {type(e).__name__}: {e})")

    print("\nГОТОВО. Sandbox полностью обнулён; account_id записан в .env бэкенда.")
    print("Замените SANDBOX_ACCOUNT в backend/.env других копий репозитория, если они есть.")


if __name__ == "__main__":
    main()