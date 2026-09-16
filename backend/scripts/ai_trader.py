"""AI-трейдер: big-pickle САМ торгует на sandbox-счёте бота.

Цикл: собрать контекст (портфель, позиции, срез движений, IMOEX, свечи, сделки) →
спросить модель → выполнить действия через API бота (/ai_trade, /positions/close).

Лимиты маржи/стресса бота действуют; AI-гейт пропускается (модель сама трейдер).

Запуск: python scripts/ai_trader.py --api http://127.0.0.1:8000 --interval 300
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SYSTEM = """Ты — автономный трейдер на MOEX (sandbox-счёт). Ты САМ решаешь, что делать:
открывать/закрывать позиции, какие ставить стоп/тейк. Никаких «рекомендаций» — только действия.

Вход: {portfolio: equity/cash/позиции/маржа, positions: открытые позиции с P&L и уровнями,
movers: движения по горизонтам (1д/1н/1м/3м, топ рост/падение), imoex: направление индекса,
universe: список доступных тикеров, recent_trades: последние сделки, now_msk}.

Правила:
- маржа: бот не даст превысить 80% equity; стресс ±5% IMOEX ≤ 10% equity;
- максимум 5 позиций одновременно;
- на каждый тикер — одно действие за цикл;
- не открывай больше 3 новых позиций за цикл;
- стоп обязателен (sl_pct 0.01-0.05), тейк по желанию (tp_pct, 0 = без тейка);
- закрывай позиции, если тезис сломан, и фиксируй прибыль при достижении цели.

Отвечай СТРОГО JSON-массивом действий (без текста вокруг):
[
  {"action":"open","ticker":"SBER","side":"SELL","notional_pct":1.0,"sl_pct":0.03,"tp_pct":0.06,"reason":"..."},
  {"action":"close","ticker":"GAZP","reason":"..."}
]
Если действий нет — верни []. notional_pct: 1.0 = стандартный слот (60% equity с плечом до ×2).
"""


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 60.0):
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url, json=payload)
        r.raise_for_status()
        return r.json()


def _ask(system: str, user: dict, model: str, url: str) -> list:
    url = url.rstrip("/")
    with httpx.Client(timeout=180.0) as c:
        sid = c.post(f"{url}/session", json={"title": "ai-trader"}).json().get("id")
        try:
            body = {
                "model": {"providerID": "opencode", "modelID": model},
                "system": system,
                "parts": [{"type": "text", "text": json.dumps(user, ensure_ascii=False, default=str)[:16000]}],
            }
            r = c.post(f"{url}/session/{sid}/message", json=body)
            r.raise_for_status()
            txt = "".join(p.get("text", "") for p in (r.json().get("parts") or [])
                          if p.get("type") == "text")
            m = re.search(r"\[.*\]", txt, re.S)
            return json.loads(m.group(0)) if m else []
        finally:
            try:
                c.delete(f"{url}/session/{sid}")
            except Exception:
                pass


def _context(api: str) -> dict:
    out: dict = {}
    try:
        st = _http("GET", f"{api}/api/v1/bot/status")
        out["portfolio"] = st.get("portfolio")
        out["long_short"] = st.get("long_short")
    except Exception:
        pass
    try:
        pos = _http("GET", f"{api}/api/v1/sandbox/positions")
        out["positions"] = [{k: p.get(k) for k in
                             ("ticker", "side", "qty", "entry_price", "current_price",
                              "net_pnl_est", "stop_loss", "take_profit", "dist_sl_atr")}
                            for p in (pos.get("positions") or [])]
    except Exception:
        out["positions"] = []
    try:
        mv = _http("GET", f"{api}/api/v1/screener/movers?top=5")
        out["movers"] = mv.get("horizons")
    except Exception:
        out["movers"] = {}
    try:
        out["imoex"] = (_http("GET", f"{api}/api/v1/bot/status").get("imoex_guard"))
    except Exception:
        pass
    try:
        scr = _http("GET", f"{api}/api/v1/screener")
        out["universe"] = [r["ticker"] for r in (scr.get("items") or [])[:60] if r.get("in_universe")]
    except Exception:
        out["universe"] = []
    try:
        tr = _http("GET", f"{api}/api/v1/sandbox/trades?limit=15")
        out["recent_trades"] = [{k: t.get(k) for k in
                                 ("ticker", "side", "entry_price", "exit_price", "net_pnl", "exit_reason")}
                                for t in (tr.get("trades") or [])]
    except Exception:
        out["recent_trades"] = []
    out["now_msk"] = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--opencode-url", default="http://192.168.1.3:4096")
    ap.add_argument("--model", default="big-pickle")
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    print(f"[ai-trader] api={args.api} model={args.model} interval={args.interval}s", flush=True)
    while True:
        t0 = time.monotonic()
        try:
            ctx = _context(args.api)
            n_pos = len(ctx.get("positions") or [])
            eq = (ctx.get("portfolio") or {}).get("equity")
            print(f"[ai-trader] контекст: позиций {n_pos}, equity {eq}, "
                  f"тикеров {len(ctx.get('universe') or [])}", flush=True)
            acts = _ask(SYSTEM, ctx, args.model, args.opencode_url)
            print(f"[ai-trader] действий: {len(acts)}", flush=True)
            for a in acts[:6]:
                try:
                    act = str(a.get("action") or "").lower()
                    tk = str(a.get("ticker") or "").upper()
                    if not tk:
                        continue
                    if act == "close":
                        r = _http("POST", f"{args.api}/api/v1/bot/ai_trade",
                                  {"ticker": tk, "action": "close", "reason": a.get("reason", "")})
                    elif act == "open":
                        r = _http("POST", f"{args.api}/api/v1/bot/ai_trade", {
                            "ticker": tk, "action": "open", "side": str(a.get("side") or "SELL"),
                            "notional_pct": a.get("notional_pct"), "sl_pct": a.get("sl_pct"),
                            "tp_pct": a.get("tp_pct"), "reason": a.get("reason", ""),
                        })
                    else:
                        continue
                    print(f"[ai-trader] {act} {tk}: {json.dumps(r, ensure_ascii=False)[:120]}", flush=True)
                except Exception as e:
                    print(f"[ai-trader] ошибка действия {a}: {type(e).__name__}: {str(e)[:100]}", flush=True)
        except Exception as e:
            print(f"[ai-trader] цикл: {type(e).__name__}: {str(e)[:120]}", flush=True)
        if args.once:
            return
        dt = float(args.interval) - (time.monotonic() - t0)
        time.sleep(max(15.0, dt))


if __name__ == "__main__":
    main()
