#!/usr/bin/env python3
"""MCP-сервер торгового бота Deeptrading v2 — инструменты, промты и ресурсы для ИИ.

Тонкий stdio-сервер над HTTP API бота (по умолчанию http://127.0.0.1:8000).
Даёт нейросети (opencode/Claude/локальная LLM) безопасный доступ к состоянию
бота, позициям, сделкам, логам, тестам и guard'у IMOEX, а также ограниченные
write-действия (SL/TP, закрытие, пауза, запуск теста).

Безопасность:
- Все write-инструменты требуют `confirm=True`; без него возвращается dry-run
  превью (что именно изменится) — модель обязана показать его человеку.
- В режиме `live` (реальные деньги) write-инструменты запрещены, пока в
  окружении сервера не выставлено `MCP_ALLOW_LIVE=1`.
- Каждый write пишется в audit.jsonl (кто/что/когда/причина/результат).
- Переключение в live, изменение математики движка и лимитов риска через MCP
  НЕ доступны (см. docs/MCP_idea.md) — только вручную человеком.

Запуск (stdio):
    BOT_API_URL=http://127.0.0.1:8000 .venv-mcp/bin/python3 mcp_server/bot_server.py

Конфиг opencode (~/.config/opencode/opencode.jsonc):
    {
      "$schema": "https://opencode.ai/config.json",
      "mcp": {
        "deeptrading-bot": {
          "type": "local",
          "command": ["C:\\\\Users\\\\nadts\\\\Dev\\\\Deeptrading\\\\backend\\\\.venv-mcp\\\\Scripts\\\\python.exe",
                      "C:\\\\Users\\\\nadts\\\\Dev\\\\Deeptrading\\\\backend\\\\mcp_server\\\\bot_server.py"],
          "environment": {"BOT_API_URL": "http://127.0.0.1:8000"},
          "enabled": true
        }
      }
    }
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

API = os.environ.get("BOT_API_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("BOT_API_TOKEN", "")
ALLOW_LIVE = os.environ.get("MCP_ALLOW_LIVE", "0") == "1"
AUDIT_FILE = os.environ.get("MCP_AUDIT_FILE", str(Path(__file__).with_name("audit.jsonl")))
TIMEOUT = float(os.environ.get("MCP_HTTP_TIMEOUT", "20"))

INSTRUCTIONS = """Ты — ассистент торгового бота Deeptrading (T-Invest, MOEX).
Через этот MCP-сервер ты видишь состояние бота и можешь ограниченно им управлять.

ПРАВИЛА (обязательны):
1. Сначала читай: get_status → get_state → get_guard → get_events. Только потом делай выводы.
2. Любое write-действие (set_levels, close_position, close_all, pause_entries,
   cancel_pending, run_test, stop_bot) вызывай СНАЧАЛА с confirm=false — получишь
   dry-run превью. Покажи превью человеку и выполни confirm=true только после
   его явного согласия. Причину указывай в параметре reason.
3. Никогда не предлагай переключение в live, изменение кода движка, лимитов риска
   или параметров стратегий через MCP — это только вручную.
4. IMOEX guard: при всплеске индекса бот запрещает входы против направления.
   Блокировки — это защита, а не ошибка. Если guard показывает stale/НЕТ СВЕЧЕЙ —
   сообщи человеку как инцидент (данные MOEX не обновляются, защита не работает).
5. Отвечай кратко и по делу: цифры, факты, рекомендации. Позиции и P&L — в ₽.
6. Если инструмент вернул {"ok": false, "error": ...} — не повторяй вызов вслепую,
   объясни проблему и предложи шаг диагностики.
"""

mcp = MCPServer("deeptrading-bot", instructions=INSTRUCTIONS)


# ---------------------------------------------------------------- HTTP helpers

def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def _clean(params: dict | None) -> dict:
    return {k: v for k, v in (params or {}).items() if v not in (None, "")}


def _get(path: str, timeout: float | None = None, **params) -> Any:
    try:
        with httpx.Client(timeout=timeout or TIMEOUT, headers=_headers()) as c:
            r = c.get(f"{API}{path}", params=_clean(params))
            if r.status_code >= 400:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
            return r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _post(path: str, payload: dict | None = None, timeout: float | None = None, **params) -> Any:
    try:
        with httpx.Client(timeout=timeout or TIMEOUT, headers=_headers()) as c:
            r = c.post(f"{API}{path}", json=payload, params=_clean(params))
            if r.status_code >= 400:
                return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:300]}"}
            return r.json()
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _audit(tool: str, args: dict, result: Any, dry: bool) -> None:
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "args": args,
            "dry_run": dry,
            "result": (result if isinstance(result, (dict, list)) else str(result)) if not isinstance(result, str) else result[:500],
        }
        with open(AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _mode() -> str:
    d = _get("/api/v1/bot/status")
    return str(d.get("mode") or "") if isinstance(d, dict) else ""


def _live_blocked() -> str | None:
    if _mode().startswith("live") and not ALLOW_LIVE:
        return ("Бот в режиме live (реальные деньги): write через MCP запрещён. "
                "Если это осознанное решение — выставьте MCP_ALLOW_LIVE=1 в окружении MCP-сервера.")
    return None


def _guarded(tool: str, args: dict, confirm: bool, reason: str, preview: dict, action) -> dict:
    """Единый гард write-действий: live-блок → dry-run → confirm → аудит."""
    blocked = _live_blocked()
    if blocked:
        _audit(tool, {**args, "reason": reason}, {"blocked": blocked}, True)
        return {"ok": False, "blocked": True, "error": blocked}
    if not confirm:
        _audit(tool, {**args, "reason": reason}, {"preview": preview}, True)
        return {
            "dry_run": True,
            "tool": tool,
            "reason": reason,
            "preview": preview,
            "message": "Это превью. Покажи его человеку; для выполнения вызови с confirm=true.",
        }
    res = action()
    _audit(tool, {**args, "reason": reason}, res, False)
    return res if isinstance(res, dict) else {"ok": True, "result": res}


# ------------------------------------------------------------------- READ tools

@mcp.tool()
def get_status() -> dict:
    """Краткий статус бота: running, mode, error, метрики (cps, ensemble_ms, persist),
    портфель (equity/pnl/positions_open), риск (daily_pnl, лимит), конфиг."""
    d = _get("/api/v1/bot/status")
    if not isinstance(d, dict) or d.get("ok") is False:
        return d if isinstance(d, dict) else {"ok": False, "error": "no data"}
    return {k: d.get(k) for k in ("running", "starting", "mode", "error", "risk",
                                  "portfolio", "metrics", "entries_paused", "config")}


@mcp.tool()
def get_state() -> dict:
    """Снапшот состояния: режим, equity, открытые позиции (тикер, сторона, qty, вход,
    текущая цена, P&L, SL, TP, дистанции до уровней, режим рынка, трейлинг) и алерты.
    Плюс imoex_guard — состояние защиты от всплесков индекса."""
    return _get("/api/v1/bot/state")


@mcp.tool()
def get_positions() -> dict:
    """Компактный список открытых позиций (без equity/alerts/конфига)."""
    st = _get("/api/v1/bot/state")
    if not isinstance(st, dict) or st.get("ok") is False:
        return st if isinstance(st, dict) else {"ok": False, "error": "no data"}
    return {"count": len(st.get("positions") or []), "positions": st.get("positions") or [],
            "equity": st.get("equity")}


@mcp.tool()
def get_guard() -> dict:
    """IMOEX guard: защита от всплесков индекса.

    Поля: active (1=всплеск вверх — блок SELL, -1=вниз — блок BUY, 0=нет),
    pct/move (ход индекса за окно), blocks (сколько входов заблокировано),
    stale (свечи MOEX не обновляются в торговую сессию — защита не работает!),
    trading, last_candle, пороги on_pct/chase_pct/min_beta.
    """
    st = _get("/api/v1/bot/status")
    if not isinstance(st, dict) or st.get("ok") is False:
        return st if isinstance(st, dict) else {"ok": False, "error": "no data"}
    g = st.get("imoex_guard") or {}
    hint = ("всплеск ВВЕРХ: SELL-входы против индекса запрещены" if g.get("active", 0) > 0
            else "всплеск ВНИЗ: BUY-входы против индекса запрещены" if g.get("active", 0) < 0
            else "нет всплеска")
    if g.get("stale"):
        hint = "СВЕЧИ IMOEX НЕ ОБНОВЛЯЮТСЯ — guard не защищает входы, это инцидент"
    return {**g, "hint": hint}


@mcp.tool()
def get_risk() -> dict:
    """Риск и деньги: risk.state (NORMAL/REDUCED/HALTED), daily_pnl, лимит дня,
    equity, свободные средства, размер портфеля."""
    st = _get("/api/v1/bot/status")
    if not isinstance(st, dict) or st.get("ok") is False:
        return st if isinstance(st, dict) else {"ok": False, "error": "no data"}
    return {"risk": st.get("risk"), "portfolio": st.get("portfolio"),
            "equity": (st.get("portfolio") or {}).get("equity")}


@mcp.tool()
def get_trades(limit: int = 20) -> dict:
    """Последние сделки: тикер, сторона, qty, вход/выход, P&L, комиссия, причина выхода."""
    d = _get("/api/v1/bot/trades", limit=limit)
    if not isinstance(d, dict) or d.get("ok") is False:
        return d if isinstance(d, dict) else {"ok": False, "error": "no data"}
    return d


@mcp.tool()
def get_events(limit: int = 100, kind: str = "") -> dict:
    """Лента событий бота (структурно): ORDER_FILLED, SIGNAL_REJECTED, IMOEX_GUARD,
    IMOEX_STALE, POSITION_OPENED, METRICS_ALERT и др.
    kind — фильтр по подстроке reason/type (например 'IMOEX', 'ORDER')."""
    d = _get("/api/v1/bot/events", limit=limit)
    if not isinstance(d, dict) or d.get("ok") is False:
        return d if isinstance(d, dict) else {"ok": False, "error": "no data"}
    ev = d.get("events") or []
    if kind:
        k = kind.upper()
        ev = [e for e in ev if k in json.dumps(e, ensure_ascii=False).upper()]
    return {"count": len(ev), "events": ev}


@mcp.tool()
def get_logs(limit: int = 100, grep: str = "") -> list:
    """Последние строки лога бота (сигналы, входы/выходы, MARGIN, TECHINFO).
    grep — подстрока для фильтра (например 'IMOEX', 'ПРОПУСК', 'ERROR')."""
    d = _get("/api/v1/bot/logs", limit=limit)
    if not isinstance(d, dict) or d.get("ok") is False:
        return [str(d)]
    logs = d.get("logs") or []
    if grep:
        logs = [x for x in logs if grep.lower() in str(x).lower()]
    return logs


@mcp.tool()
def get_tests() -> dict:
    """Список прогонов тестов (replay) со статистикой: trades, net, PF, WR%, открытые позиции."""
    return _get("/api/v1/bot/tests")


@mcp.tool()
def get_test_stats(test_name: str = "", date_from: str = "", date_to: str = "") -> dict:
    """Статистика прогона/периода по срезам: overall, side, regime, ticker, exit_reason,
    session, quorum. test_name — имя теста; date_from/date_to — ISO (UTC)."""
    return _get("/api/v1/bot/test_stats", test_name=test_name,
                date_from=date_from, date_to=date_to, timeout=60)


@mcp.tool()
def get_trading_status() -> dict:
    """Торговый статус MOEX по инструментам (NORMAL_TRADING / DISCRETE_AUCTION / закрыто).
    Может отвечать ~10-30с (запрос в T-Invest по всем figi)."""
    return _get("/api/v1/bot/trading_status", timeout=60)


@mcp.tool()
def get_config() -> dict:
    """Текущая конфигурация бота: сессии, направления, режимы, маржа, SL/TP, guard и т.д."""
    return _get("/api/v1/bot/config")


@mcp.tool()
def get_screener(limit: int = 25, sort_by: str = "rng_pct") -> dict:
    """Рынок TQBR: цена, оборот, волатильность (RNG%) по акциям. sort_by: ticker|price|turnover|rng_pct."""
    d = _get("/api/v1/screener", timeout=60)
    if not isinstance(d, dict) or d.get("ok") is False:
        return d if isinstance(d, dict) else {"ok": False, "error": "no data"}
    rows = d.get("rows") or d.get("items") or []
    try:
        rows = sorted(rows, key=lambda r: r.get(sort_by) or 0, reverse=(sort_by != "ticker"))
    except Exception:
        pass
    return {"count": len(rows), "rows": rows[:max(1, min(limit, 200))]}


@mcp.tool()
def get_orders(limit: int = 20) -> dict:
    """Заявки/ордера бота (pending и история lifecycle)."""
    return _get("/api/v1/bot/orders", limit=limit)


# ------------------------------------------------------------------ WRITE tools

@mcp.tool()
def set_levels(ticker: str, sl: float | None = None, tp: float | None = None,
               confirm: bool = False, reason: str = "") -> dict:
    """Сменить SL/TP позиции (защита прибыли / сдвиг цели).

    ticker — тикер ("SMLT"); sl/tp — абсолютные цены (можно только одно).
    Для LONG: SL ниже цены, TP выше; для SHORT наоборот.
    Сначала вызови с confirm=false (превью), выполни с confirm=true после согласия человека.
    """
    st = _get("/api/v1/bot/state")
    pos = None
    if isinstance(st, dict):
        pos = next((p for p in (st.get("positions") or [])
                    if str(p.get("ticker", "")).upper() == ticker.upper()), None)
    if not pos:
        return {"ok": False, "error": f"позиция {ticker} не найдена"}
    preview = {
        "position": {k: pos.get(k) for k in ("ticker", "side", "qty", "entry", "last", "pnl",
                                             "sl", "tp", "dist_sl_pct", "dist_tp_pct")},
        "new": {"sl": sl if sl is not None else pos.get("sl"),
                "tp": tp if tp is not None else pos.get("tp")},
    }
    if sl is None and tp is None:
        return {"ok": False, "error": "нужен хотя бы один из sl/tp"}
    return _guarded("set_levels", {"ticker": ticker.upper(), "sl": sl, "tp": tp},
                    confirm, reason, preview,
                    lambda: _post("/api/v1/bot/positions/levels",
                                  {"ticker": ticker.upper(), "sl": sl, "tp": tp}))


@mcp.tool()
def close_position(ticker: str, confirm: bool = False, reason: str = "") -> dict:
    """Закрыть позицию по тикеру рыночно (по последней цене).

    Сначала confirm=false (превью: позиция и её P&L), затем confirm=true после согласия.
    """
    st = _get("/api/v1/bot/state")
    pos = None
    if isinstance(st, dict):
        pos = next((p for p in (st.get("positions") or [])
                    if str(p.get("ticker", "")).upper() == ticker.upper()), None)
    if not pos:
        return {"ok": False, "error": f"позиция {ticker} не найдена"}
    preview = {"position": {k: pos.get(k) for k in ("ticker", "side", "qty", "entry", "last",
                                                    "pnl", "sl", "tp")}}
    return _guarded("close_position", {"ticker": ticker.upper()}, confirm, reason, preview,
                    lambda: _post("/api/v1/bot/positions/close", figi=pos["figi"]))


@mcp.tool()
def close_all(confirm: bool = False, reason: str = "") -> dict:
    """Закрыть ВСЕ позиции рыночно. Опасная операция: сначала превью (confirm=false)."""
    st = _get("/api/v1/bot/state")
    positions = (st.get("positions") or []) if isinstance(st, dict) else []
    preview = {"positions": [{k: p.get(k) for k in ("ticker", "side", "qty", "pnl")}
                             for p in positions], "count": len(positions)}
    return _guarded("close_all", {}, confirm, reason, preview,
                    lambda: _post("/api/v1/bot/positions/close-all"))


@mcp.tool()
def pause_entries(paused: bool, confirm: bool = False, reason: str = "") -> dict:
    """Пауза/возобновление НОВЫХ входов (выходы и управление позициями работают).
    paused=true — пауза, false — возобновить."""
    st = _get("/api/v1/bot/status")
    preview = {"entries_paused_now": (st or {}).get("entries_paused"), "set_paused": paused}
    return _guarded("pause_entries", {"paused": paused}, confirm, reason, preview,
                    lambda: _post("/api/v1/bot/pause", {"paused": paused}))


@mcp.tool()
def cancel_pending(confirm: bool = False, reason: str = "") -> dict:
    """Снять все неисполненные (pending) заявки бота."""
    d = _get("/api/v1/bot/orders")
    orders = (d.get("orders") or []) if isinstance(d, dict) else []
    pending = [o for o in orders if str(o.get("status", "")).upper() in ("PENDING", "NEW", "QUEUED")]
    preview = {"pending": len(pending), "orders": pending[:10]}
    return _guarded("cancel_pending", {}, confirm, reason, preview,
                    lambda: _post("/api/v1/bot/orders/cancel-pending"))


@mcp.tool()
def run_test(name: str, replay_start: str, replay_end: str = "",
             confirm: bool = False, reason: str = "") -> dict:
    """Запустить replay-тест (paper): переключает бота в mode=test и проигрывает период.

    name — имя теста (в него помечаются сделки), replay_start/replay_end — ISO UTC.
    Бот будет остановлен и перезапущен в тестовом режиме. Сначала превью.
    """
    preview = {"test_name": name, "replay_start": replay_start,
               "replay_end": replay_end or "(до конца данных)",
               "current_mode": _mode()}
    return _guarded("run_test", {"name": name, "replay_start": replay_start,
                                 "replay_end": replay_end}, confirm, reason, preview,
                    lambda: _post("/api/v1/bot/mode", {
                        "mode": "test", "test_name": name,
                        "replay_start": replay_start, "replay_end": replay_end}, timeout=120))


@mcp.tool()
def stop_bot(confirm: bool = False, reason: str = "") -> dict:
    """Остановить бота (позиции НЕ закрываются, остаются у брокера)."""
    st = _get("/api/v1/bot/state")
    positions = (st.get("positions") or []) if isinstance(st, dict) else []
    preview = {"running": (st or {}).get("running"), "mode": (st or {}).get("mode"),
               "open_positions": len(positions),
               "warning": "позиции останутся открытыми (SL/TP у брокера не выставляются)"}
    return _guarded("stop_bot", {}, confirm, reason, preview,
                    lambda: _post("/api/v1/bot/stop", timeout=120))


# --------------------------------------------------------------------- PROMPTS

@mcp.prompt()
def digest() -> str:
    """Сводка состояния бота за сейчас (для утреннего/периодического отчёта)."""
    return ("Сделай сводку по боту: вызови get_status, get_positions, get_guard, get_events(limit=30). "
            "Отрази: running/mode, equity и P&L, открытые позиции с дистанциями до SL/TP, "
            "активные блокировки IMOEX guard (если есть — что именно блокируется), "
            "свежесть свечей MOEX (stale?), риск дня (daily_pnl/лимит). "
            "В конце — 1-2 рекомендации (что требует внимания человека). Кратко, цифрами.")


@mcp.prompt()
def positions_review() -> str:
    """Разбор открытых позиций: риски, уровни, что делать."""
    return ("Вызови get_state и get_risk. По каждой позиции: тикер, сторона, qty, P&L, "
            "дистанция до SL и TP в %, режим рынка и трейлинг. Отметь позиции, где "
            "цена близко к SL (<0.3%) или где трейлинг подтянут. Проверь, нет ли позиций "
            "против свежего всплеска IMOEX (get_guard). Дай список действий: держать / "
            "подтянуть стоп (set_levels, только превью!) / закрыть (close_position, превью).")


@mcp.prompt()
def incident() -> str:
    """Диагностика инцидента: guard, свечи, ошибки, отклонённые сигналы."""
    return ("Диагностируй состояние: get_guard (нет ли stale — свечи MOEX не идут?), "
            "get_status.error, get_events(limit=100, kind='IMOEX') и get_logs(grep='ERROR'), "
            "get_events(kind='SIGNAL_REJECTED'). Найди: (1) ошибки/сбои, (2) почему бот не "
            "входит или не выходит (какие фильтры режут), (3) состояние защиты от всплесков. "
            "Дай вывод: инцидент / норма, и что сделать.")


@mcp.prompt()
def guard_help() -> str:
    """Объяснение IMOEX guard простыми словами (для человека)."""
    return ("Вызови get_guard и объясни простыми словами: что такое всплеск IMOEX "
            "(ход индекса за 20 минут ≥ порога), почему бот запрещает входы против него, "
            "что значит каждый счётчик (blocks/activations/releases), что такое chase-блок "
            "(при очень сильном ходе ≥1.5% блокируются и входы ПО индексу) и per-ticker режим "
            "(блок только бумаг с beta ≥ порога). Если stale — объясни, что защита не работает.")


@mcp.prompt()
def research_loop() -> str:
    """Как вести исследования через API (lab/warehouse/сигналы) с ограничениями."""
    return ("Ты можешь помогать с исследованиями, но с ограничениями (docs/MCP_idea.md): "
            "нельзя менять движок, CostModel, лимиты риска и переключать в live. "
            "Доступные шаги: посмотреть каталог стратегий (GET /api/v1/strategies/catalog), "
            "создать конфигурацию (POST /api/v1/warehouse/configurations), поставить тест "
            "(POST /api/v1/lab/queue), дождаться и снять метрики (GET /api/v1/lab/queue), "
            "сравнить прогоны. Для бота используй run_test (replay, paper). "
            "Перед запуском большого перебора — покажи план человеку и спроси подтверждение.")


@mcp.prompt()
def safe_writes() -> str:
    """Правила безопасных write-действий через MCP."""
    return ("Все write-действия: (1) сначала вызови с confirm=false и покажи превью человеку; "
            "(2) выполняй confirm=true только после явного 'да'; (3) всегда заполняй reason "
            "(зачем это делается); (4) в live-режиме записи запрещены сервером; "
            "(5) после выполнения покажи результат и новое состояние (get_state). "
            "Никогда не выполняй серию write-действий без подтверждения каждого шага.")


# ------------------------------------------------------------------- RESOURCES

@mcp.resource("bot://state")
def res_state() -> str:
    """Текущее состояние бота (позиции, P&L, guard) — JSON."""
    return json.dumps(_get("/api/v1/bot/state"), ensure_ascii=False, indent=2, default=str)


@mcp.resource("bot://status")
def res_status() -> str:
    """Статус и метрики бота — JSON."""
    return json.dumps(_get("/api/v1/bot/status"), ensure_ascii=False, indent=2, default=str)


@mcp.resource("bot://guard")
def res_guard() -> str:
    """Состояние IMOEX guard — JSON."""
    st = _get("/api/v1/bot/status")
    g = (st or {}).get("imoex_guard") if isinstance(st, dict) else None
    return json.dumps(g or {"ok": False}, ensure_ascii=False, indent=2, default=str)


@mcp.resource("bot://config")
def res_config() -> str:
    """Конфигурация бота — JSON."""
    return json.dumps(_get("/api/v1/bot/config"), ensure_ascii=False, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
