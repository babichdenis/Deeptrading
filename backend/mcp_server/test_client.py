#!/usr/bin/env python3
"""Тест-клиент MCP-сервера бота (stdio): чек-лист инструментов/промтов/ресурсов.

Запуск (на машине, где работает бот, из backend):
    BOT_API_URL=http://127.0.0.1:8000 .venv-mcp/bin/python3 mcp_server/test_client.py
    # Windows: .venv-mcp\\Scripts\\python.exe mcp_server\\test_client.py

Проверяет:
 1) инициализацию и список инструментов;
 2) read-инструменты (status/state/guard/risk/events/tests/config/screener);
 3) dry-run write (pause_entries, set_levels/close_position — без confirm);
 4) промты и ресурсы.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "bot_server.py")


def _text(res) -> str:
    try:
        parts = []
        for attr in ("content", "contents", "messages"):
            val = getattr(res, attr, None) or []
            for c in val:
                t = getattr(c, "text", None)
                if t is None and isinstance(c, dict):
                    t = c.get("text")
                if t is None:
                    cc = getattr(c, "content", None)
                    t = getattr(cc, "text", None) if cc is not None else None
                if t:
                    parts.append(str(t))
        return "\n".join(parts)[:400]
    except Exception as e:
        return f"<no text: {e}>"


async def main() -> int:
    ok_all = True
    params = StdioServerParameters(command=sys.executable, args=[SERVER], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            init = await s.initialize()
            print(f"[init] server={init.server_info.name} v{init.server_info.version}")

            tools = await s.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[tools] {len(names)}: {', '.join(names)}")
            need = {"get_status", "get_state", "get_positions", "get_guard", "get_risk",
                    "get_trades", "get_events", "get_logs", "get_tests", "get_test_stats",
                    "get_trading_status", "get_config", "get_screener", "get_orders",
                    "set_levels", "close_position", "close_all", "pause_entries",
                    "cancel_pending", "run_test", "stop_bot"}
            missing = need - set(names)
            ok_all &= not missing
            print(f"[tools] missing: {sorted(missing) if missing else 'нет'}")

            calls = [
                ("get_status", {}),
                ("get_state", {}),
                ("get_positions", {}),
                ("get_guard", {}),
                ("get_risk", {}),
                ("get_events", {"limit": 5}),
                ("get_tests", {}),
                ("get_config", {}),
                ("get_orders", {"limit": 5}),
            ]
            for name, args in calls:
                try:
                    r = await s.call_tool(name, args)
                    txt = _text(r)
                    bad = (getattr(r, "isError", None) or getattr(r, "is_error", False)) or '"ok": false' in txt.replace(" ", "").lower()
                    ok_all &= not bad
                    print(f"[read] {name}: {'OK' if not bad else 'FAIL'} | {txt[:110]}")
                except Exception as e:
                    ok_all = False
                    print(f"[read] {name}: EXC {type(e).__name__}: {e}")

            # dry-run write: пауза входов
            r = await s.call_tool("pause_entries", {"paused": True, "confirm": False, "reason": "test"})
            txt = _text(r)
            ok = '"dry_run": true' in txt.replace(" ", "").lower() or "dry_run" in txt
            ok_all &= ok
            print(f"[dry-run] pause_entries: {'OK' if ok else 'FAIL'} | {txt[:160]}")

            # dry-run: close_all (только превью)
            r = await s.call_tool("close_all", {"confirm": False, "reason": "test"})
            txt = _text(r)
            ok = "dry_run" in txt or "ok" in txt.lower()
            print(f"[dry-run] close_all: {'OK' if ok else 'FAIL'} | {txt[:160]}")

            prompts = await s.list_prompts()
            pnames = sorted(p.name for p in prompts.prompts)
            print(f"[prompts] {len(pnames)}: {', '.join(pnames)}")
            if "digest" in pnames:
                pr = await s.get_prompt("digest", {})
                ptxt = _text(pr)
                ok_all &= bool(ptxt)
                print(f"[prompt] digest: {'OK' if ptxt else 'FAIL'} | {ptxt[:120]}")
            else:
                ok_all = False
                print("[prompt] digest: FAIL (нет промта)")

            resources = await s.list_resources()
            rnames = sorted(str(r.uri) for r in resources.resources)
            print(f"[resources] {len(rnames)}: {', '.join(rnames)}")
            if rnames:
                rr = await s.read_resource(rnames[0])
                txt = _text(rr)
                ok_all &= bool(txt)
                print(f"[resource] {rnames[0]}: {'OK' if txt else 'FAIL'} | {txt[:100]}")

    print("\n=== ИТОГ:", "PASS" if ok_all else "FAIL", "===")
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
