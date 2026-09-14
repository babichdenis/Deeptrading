#!/usr/bin/env python3
"""MCP-сервер торгового бота Deeptrading: инструменты для ИИ-управления.

Тонкий клиент над HTTP API бота (по умолчанию http://127.0.0.1:8000).
Позволяет нейросети: смотреть состояние, менять SL/TP, закрывать позиции,
читать сделки/логи — без ручных curl.

Запуск (stdio):
    BOT_API_URL=http://192.168.1.3:8000 .venv-mcp/bin/python3 mcp/bot_server.py

Пример конфигурации MCP-клиента (opencode.json / claude_desktop_config.json):
    {
      "mcpServers": {
        "deeptrading": {
          "command": "/Users/Denis/Dev/Deeptrading/backend/.venv-mcp/bin/python3",
          "args": ["/Users/Denis/Dev/Deeptrading/backend/mcp/bot_server.py"],
          "env": {"BOT_API_URL": "http://192.168.1.3:8000"}
        }
      }
    }
"""
from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

API = os.environ.get("BOT_API_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("BOT_API_TOKEN", "")

mcp = MCPServer("deeptrading-bot")


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def _get(path: str, **params) -> Any:
    with httpx.Client(timeout=20.0, headers=_headers()) as c:
        r = c.get(f"{API}{path}", params=params)
        r.raise_for_status()
        return r.json()


def _post(path: str, payload: dict | None = None, **params) -> Any:
    with httpx.Client(timeout=30.0, headers=_headers()) as c:
        r = c.post(f"{API}{path}", json=payload, params=params)
        r.raise_for_status()
        return r.json()


@mcp.tool()
def get_state() -> dict:
    """Снапшот состояния: режим, equity, открытые позиции (тикер, сторона, qty, вход,
    цена, P&L, SL, TP, дистанции до уровней, режим рынка, трейлинг) и алерты."""
    return _get("/api/v1/bot/state")


@mcp.tool()
def get_status() -> dict:
    """Краткий статус бота: running, mode, error, метрики (cps, ensemble_ms), портфель."""
    d = _get("/api/v1/bot/status")
    return {k: d.get(k) for k in ("running", "mode", "error", "metrics", "portfolio", "config")}


@mcp.tool()
def set_levels(ticker: str, sl: float | None = None, tp: float | None = None) -> dict:
    """Сменить SL/TP позиции (защита прибыли / сдвиг цели).

    ticker — тикер (напр. "SMLT"); sl/tp — абсолютные цены.
    Для LONG: SL ниже цены, TP выше. Для SHORT: наоборот.
    """
    return _post("/api/v1/bot/positions/levels",
                 {"ticker": ticker.upper(), "sl": sl, "tp": tp})


@mcp.tool()
def close_position(ticker: str) -> dict:
    """Закрыть позицию по тикеру (рыночно, по последней цене)."""
    st = _get("/api/v1/bot/state")
    pos = next((p for p in st.get("positions", [])
                if str(p.get("ticker", "")).upper() == ticker.upper()), None)
    if not pos:
        return {"ok": False, "error": f"позиция {ticker} не найдена"}
    return _post("/api/v1/bot/positions/close", figi=pos["figi"])


@mcp.tool()
def get_trades(limit: int = 20) -> list:
    """Последние сделки: тикер, сторона, qty, вход/выход, P&L, причина выхода."""
    d = _get("/api/v1/bot/trades", limit=limit)
    return d.get("trades", []) if isinstance(d, dict) else d


@mcp.tool()
def get_logs(limit: int = 100) -> list:
    """Последние строки лога бота (сигналы, входы/выходы, MARGIN, TECHINFO)."""
    d = _get("/api/v1/bot/logs", limit=limit)
    return d.get("logs", []) if isinstance(d, dict) else d


if __name__ == "__main__":
    mcp.run()
