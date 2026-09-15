#!/usr/bin/env python3
"""AI-гейт: воркер подтверждения входов бота через DeepSeek.

Опрашивает `/api/v1/bot/approvals`, для каждой заявки собирает контекст (позиции,
IMOEX guard, риск дня, последние сделки), спрашивает DeepSeek (chat completions)
и применяет решение approve/reject через API бота. Все решения — в журнал.

Запуск (на машине бота, из backend):
    .venv/bin/python scripts/ai_approval_worker.py --api http://127.0.0.1:8000
    .venv/bin/python scripts/ai_approval_worker.py --dry-run       # только логировать
    .venv/bin/python scripts/ai_approval_worker.py --interval 3

Ключ: DEEPSEEK_API_KEY из окружения или backend/.env (также DEEPSEEK_BASE_URL,
DEEPSEEK_MODEL). Включается на боте через PATCH /config {"ai_approval": true}.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
LOG_FILE = BACKEND / "reports" / "ai_approval_log.jsonl"

SYSTEM = """Ты — риск-менеджер торгового бота (MOEX, T-Invest). Бот прислал заявку на вход,
нужно решить: approve (одобрить) или reject (отклонить) и дать короткий совет.
Отвечай СТРОГО JSON:
{"decision": "approve"|"reject"|"skip", "reason": "коротко по-русски",
 "advice": "1 короткая рекомендация по ситуации (что сделать боту/человеку)", "confidence": 0.0-1.0}

Правила (по приоритету):
0. IMOEX (индекс) рассчитывается ТОЛЬКО в основную сессию 09:50–19:00 МСК (пн-пт).
   Если guard.trading=false (утро 06:50–09:50, вечер 19:00–23:50, выходной) — данных индекса
   НЕТ, и это нормально: НЕ отклоняй заявку из-за отсутствия/устаревания индекса. Оценивай
   по риску, концентрации, серии стопов и времени суток. Правило 1 действует только при
   guard.trading=true.
1. REJECT, если guard.trading=true и вход идёт ПРОТИВ направления свежего всплеска IMOEX
   (guard.active=1 и сторона SELL, или guard.active=-1 и сторона BUY).
2. REJECT, если guard.stale=true (индекс не обновляется в основную сессию — инцидент)
   и заявка идёт против рынка/крупная.
3. REJECT, если risk.state != NORMAL или daily_pnl близок к лимиту дня.
4. REJECT при явно негативном контексте: серия убытков по этому тикеру (recent_trades_ticker),
   низкая ликвидность, вход против режима.
5. APPROVE, если противопоказаний нет: бот уже прошёл свои фильтры (кворум, режим, guard),
   а вход согласован с направлением индекса/трендом.
6. skip — если данных мало или случай спорный (пусть решит таймаут/человек).

СОВЕТ (advice) — всегда заполняй, 1 короткая фраза, конкретное действие, например:
- "пауза по тикеру 1ч — 3 убытка подряд" (после серии убытков);
- "уменьшить размер вдвое — низкая ликвидность (объём N)";
- "подтянуть SL к безубытку — позиция в плюсе";
- "не входить до стабилизации IMOEX" (при всплеске);
- "проверить данные MOEX — индекс не обновляется";
- "держим курс — вход по правилам, противопоказаний нет".
Совет не исполняется автоматически — его видит человек в дашборде; пиши так, чтобы его можно
было однажды превратить в правило.

Не выдумывай данные, опирайся только на переданный JSON. Учитывай сессию (МСК): утро/вечер
менее ликвидны, вечером движения чаще ложные."""


def _load_env() -> None:
    """Подтянуть backend/.env (без внешних зависимостей)."""
    p = BACKEND / ".env"
    if not p.exists():
        return
    for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 20.0) -> dict:
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url, json=payload)
        r.raise_for_status()
        return r.json() if r.content else {}


def _ctx(api: str) -> dict:
    out: dict = {}
    try:
        out["state"] = _http("GET", f"{api}/api/v1/bot/state")
    except Exception as e:
        out["state"] = {"error": str(e)[:120]}
    try:
        st = _http("GET", f"{api}/api/v1/bot/status")
        out["risk"] = st.get("risk")
        out["portfolio"] = st.get("portfolio")
        out["guard"] = st.get("imoex_guard")
    except Exception as e:
        out["risk"] = {"error": str(e)[:120]}
    try:
        tr = _http("GET", f"{api}/api/v1/bot/trades", timeout=20)
        out["recent_trades"] = [
            {k: t.get(k) for k in ("ticker", "side", "net_pnl", "exit_reason", "entry_time")}
            for t in (tr.get("trades") or [])[:8]
        ]
    except Exception:
        out["recent_trades"] = []
    return out


def _ctx_compact(api: str, order: dict) -> dict:
    """Компактный контекст: только то, что нужно для решения (меньше — быстрее и точнее)."""
    tk = str(order.get("ticker") or "").upper()
    side = str(order.get("side") or "").upper()
    meta = order.get("meta") or {}
    out: dict = {
        "order": {
            "ticker": tk, "side": side, "qty": order.get("qty"),
            "price": order.get("price"),
            "reason": meta.get("reason") or meta.get("entry_reason") or meta.get("signal_note"),
        },
    }
    try:
        st = _http("GET", f"{api}/api/v1/bot/state")
        pos = st.get("positions") or []
        want_long = side == "BUY"
        same = [p for p in pos
                if str(p.get("side", "")).upper() in (("LONG", "BUY") if want_long else ("SHORT", "SELL"))]
        mine = next((p for p in pos if str(p.get("ticker", "")).upper() == tk), None)
        out["positions"] = {
            "count": len(pos),
            "same_side": len(same),
            "this_ticker": ({k: mine.get(k) for k in ("side", "qty", "pnl", "sl", "tp")} if mine else None),
        }
        out["equity"] = st.get("equity")
        out["guard"] = st.get("imoex_guard")
    except Exception as e:
        out["state_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    try:
        r = _http("GET", f"{api}/api/v1/bot/status")
        out["risk"] = r.get("risk")
        _sess = r.get("session")
        out["session"] = (_sess.get("state") if isinstance(_sess, dict) else _sess)
    except Exception:
        pass
    try:
        tr = _http("GET", f"{api}/api/v1/bot/trades", limit=60, timeout=20)
        rows = [t for t in (tr.get("trades") or []) if str(t.get("ticker", "")).upper() == tk][:5]
        out["recent_trades_ticker"] = [
            {k: t.get(k) for k in ("entry_time", "side", "net_pnl", "exit_reason")} for t in rows]
    except Exception:
        out["recent_trades_ticker"] = []
    return out


def _ask_deepseek(order: dict, ctx: dict, model: str, base: str, key: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps({"order": order, "context": ctx},
                                                   ensure_ascii=False, default=str)[:12000]},
        ],
        "temperature": 0.1,
        "max_tokens": 300,
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=60.0) as c:
        r = c.post(f"{base.rstrip('/')}/chat/completions",
                   headers={"Authorization": f"Bearer {key}"}, json=payload)
        r.raise_for_status()
        data = r.json()
    txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "{}"
    return _parse_decision(txt)


def _extract_json(txt: str) -> str:
    """Достать JSON из ответа модели (может быть в ```json ... ```)."""
    t = txt.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t


def _parse_decision(txt: str) -> dict:
    try:
        d = json.loads(_extract_json(txt))
    except Exception:
        d = {"decision": "skip", "reason": f"parse_error: {txt[:80]}", "confidence": 0.0}
    d["decision"] = str(d.get("decision", "skip")).lower()
    if d["decision"] not in ("approve", "reject", "skip"):
        d = {"decision": "skip", "reason": f"bad_decision: {d.get('decision')}", "confidence": 0.0}
    d["advice"] = str(d.get("advice") or "")[:300]
    return d


def _ask_opencode(order: dict, ctx: dict, model: str, url: str) -> dict:
    """Big Pickle и другие модели opencode zen — через локальный `opencode serve`
    (бесплатный tier Zen работает только внутри opencode)."""
    url = url.rstrip("/")
    with httpx.Client(timeout=120.0) as c:
        sid = c.post(f"{url}/session", json={"title": "ai-gate"}).json().get("id")
        try:
            body = {
                "model": {"providerID": "opencode", "modelID": model},
                "system": SYSTEM,
                "parts": [{"type": "text", "text": json.dumps(
                    {"order": order, "context": ctx}, ensure_ascii=False, default=str)[:12000]}],
            }
            r = c.post(f"{url}/session/{sid}/message", json=body)
            r.raise_for_status()
            d = r.json()
            txt = "".join(p.get("text", "") for p in (d.get("parts") or [])
                          if p.get("type") == "text")
            return _parse_decision(txt)
        finally:
            try:
                c.delete(f"{url}/session/{sid}")
            except Exception:
                pass


def _log(rec: dict) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass
    print(json.dumps(rec, ensure_ascii=False, default=str), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("BOT_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="shadow: решения НЕ применяются, только логируются и уходят в UI")
    ap.add_argument("--provider", default="opencode", choices=("opencode", "deepseek"))
    ap.add_argument("--opencode-url", default=os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096"))
    ap.add_argument("--model", default="")
    ap.add_argument("--selftest", action="store_true", help="проверить провайдера синтетической заявкой и выйти")
    ap.add_argument("--full-context", action="store_true",
                    help="слать полный контекст (state+portfolio+8 сделок); по умолчанию — компактный")
    args = ap.parse_args()

    _load_env()
    if args.provider == "opencode":
        model = args.model or "big-pickle"
        print(f"[ai-gate] provider=opencode model={model} url={args.opencode_url} "
              f"api={args.api} dry_run={args.dry_run}", flush=True)
    else:
        model = args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            print("НЕТ DEEPSEEK_API_KEY (env или backend/.env) — выход", file=sys.stderr)
            raise SystemExit(2)
        print(f"[ai-gate] provider=deepseek model={model} base={base} "
              f"api={args.api} dry_run={args.dry_run}", flush=True)

    def decide(order: dict, ctx: dict) -> dict:
        if args.provider == "opencode":
            return _ask_opencode(order, ctx, model, args.opencode_url)
        return _ask_deepseek(order, ctx, model, os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                             os.environ.get("DEEPSEEK_API_KEY", ""))

    # Отдаём текущий промпт/конфиг в бота — виден в UI (вкладка AI-гейт).
    def post_prompt() -> bool:
        try:
            _http("POST", f"{args.api}/api/v1/bot/ai_prompt", {
                "provider": args.provider, "model": model, "shadow": args.dry_run,
                "system": SYSTEM,
                "context_schema": {
                    "order": "id, ticker, side, qty, price, waiting_sec",
                    "context": "state(позиции/equity/guard), risk, portfolio, recent_trades",
                },
            })
            return True
        except Exception as e:
            print(f"[ai-gate] prompt post failed: {type(e).__name__}: {str(e)[:80]}", flush=True)
            return False

    _prompt_sent = post_prompt()

    if args.selftest:
        sample = {"id": "selftest-1", "ticker": "SMLT", "side": "SELL", "qty": 10,
                  "price": 339.2, "waiting_sec": 1.0}
        print("SELFTEST:", json.dumps(decide(sample, {"note": "synthetic"}), ensure_ascii=False), flush=True)
        return

    while True:
        try:
            if not _prompt_sent:
                _prompt_sent = post_prompt()
            d = _http("GET", f"{args.api}/api/v1/bot/approvals")
            pending = d.get("pending") or []
            if not d.get("enabled"):
                time.sleep(args.interval)
                continue
            for order in pending:
                oid = order.get("id")
                wait = float(order.get("waiting_sec") or 0)
                tmo = float(d.get("timeout_sec") or 45.0)
                if wait > tmo * 0.75:
                    continue  # поздно решать — пусть сработает таймаут/default
                ctx = _ctx(args.api) if args.full_context else _ctx_compact(args.api, order)
                t0 = time.monotonic()
                try:
                    dec = decide(order, ctx)
                except Exception as e:
                    _log({"ts": datetime.now(timezone.utc).isoformat(), "order_id": oid,
                          "ticker": order.get("ticker"), "decision": "skip",
                          "reason": f"deepseek_error: {type(e).__name__}: {str(e)[:120]}",
                          "latency_ms": int((time.monotonic() - t0) * 1000), "model": model,
                          "dry_run": args.dry_run})
                    continue
                rec = {"ts": datetime.now(timezone.utc).isoformat(), "order_id": oid,
                       "ticker": order.get("ticker"), "side": order.get("side"),
                       "qty": order.get("qty"), "figi": order.get("figi"),
                       "decision": dec["decision"], "reason": dec.get("reason", ""),
                       "advice": dec.get("advice", ""),
                       "confidence": dec.get("confidence"),
                       "latency_ms": int((time.monotonic() - t0) * 1000),
                       "model": model, "dry_run": args.dry_run, "shadow": args.dry_run}
                if not args.dry_run and dec["decision"] in ("approve", "reject"):
                    try:
                        res = _http("POST", f"{args.api}/api/v1/bot/approvals/{oid}/{dec['decision']}",
                                    {"reason": f"AI: {dec.get('reason', '')[:200]}"})
                        rec["applied"] = res
                    except Exception as e:
                        rec["applied"] = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
                # Отдать решение в бота — для поля AI-гейта в UI (работает и в shadow).
                try:
                    _http("POST", f"{args.api}/api/v1/bot/ai_decisions", rec)
                except Exception as e:
                    rec["ui_post_error"] = f"{type(e).__name__}: {str(e)[:80]}"
                _log(rec)
        except KeyboardInterrupt:
            print("stop", flush=True)
            return
        except Exception as e:
            print(f"[ai-gate] cycle error: {type(e).__name__}: {str(e)[:120]}", flush=True)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
