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

Вход:
- portfolio: equity/cash/маржа; long_short: баланс L/S;
- positions[]: открытые позиции (entry/last/pnl/sl/tp/dist_*_atr/regime) + orderbook каждой;
- orderbooks{ticker}: ЖИВОЙ СТАКАН — last, best_bid/best_ask, spread_bps, bid_qty/ask_qty,
  imbalance (-1..+1, >0 = перевес покупок), depth_rub (глубина в рублях);
- positions[].m5/h1 и candles{ticker}.m5/h1: ЖИВЫЕ СВЕЧИ (o/h/l/c/v, время МСК) —
  5м (10 баров) и час (6 баров); v — объём;
- movers: движения по горизонтам 1д/1н/1м/3м (up/down + streak = дней в группе);
- universe[]: доступные тикеры (price/turnover/rng_pct);
- imoex: направление индекса (dir/pct_20m/pct_60m/pct_day, breadth_up_pct);
- risk: дневной P&L/лимит; recent_trades: последние сделки; now_msk.

Правила:
- маржа: бот не даст превысить 80% equity; стресс ±5% IMOEX ≤ 10% equity;
- максимум 6 позиций одновременно;
- на каждый тикер — одно действие за цикл;
- не открывай больше 3 новых позиций за цикл;
- стоп обязателен (sl_pct 0.01-0.05), тейк по желанию (tp_pct, 0 = без тейка);
- закрывай позиции, если тезис сломан, и фиксируй прибыль при достижении цели.
- ВЫЖИМАЙ МАКСИМУМ: если позиция в плюсе и прибыль начала угасать (цена развернулась от
  максимума, импульс/стакан против, momentum слабеет) — ЗАКРЫВАЙ или подтяни TP, не отдавай
  нажитое. Лучше зафиксировать меньше, чем отдать всё. Это твоя главная работа по позициям.
- ДОЛГИЕ СДЕЛКИ: если видишь, что падение/рост надолго (дни, а не часы: 3м-движение, стаж
  в группе, дневной тренд) — можешь открывать swing-сделку: в action open добавь
  "hold": "swing" (по умолчанию "intraday" — закроется в конце дня).

Отвечай СТРОГО JSON-объектом (без текста вокруг):
{
  "analysis": "разбор рынка и портфеля: что вижу в данных (IMOEX, движения, стаканы, свечи),
               как это связано с текущими позициями, общий план на цикл (5-10 предложений)",
  "actions": [
    {"action":"open","ticker":"SBER","side":"SELL","notional_pct":1.0,"sl_pct":0.03,"tp_pct":0.06,
     "hold":"intraday|swing",
     "reason":"почему именно эта сделка: что в данных говорит за вход, почему эта сторона,
               где стоп/тейк и что подтверждает/опровергает тезис (2-4 предложения)"},
    {"action":"close","ticker":"GAZP",
     "reason":"почему закрываю: что изменилось в данных/тезисе (2-4 предложения)"}
  ],
  "suggestions": [
    "предложения по механизму бота: что мешает зарабатывать, какие гейты/лимиты поправить,
     что добавить (0-5 конкретных пунктов, каждый 1-2 предложения)"
  ]
}
Если действий нет — "actions": []. notional_pct: 1.0 = стандартный слот (60% equity с плечом до ×2).
Пиши содержательно: по твоему analysis, reason и suggestions мы правим логику бота.
"""


def _http(method: str, url: str, payload: dict | None = None, timeout: float = 60.0):
    with httpx.Client(timeout=timeout) as c:
        r = c.request(method, url, json=payload)
        r.raise_for_status()
        return r.json()


def _ask(system: str, user: dict, model: str, url: str) -> tuple:
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
            m = re.search(r"\{.*\}", txt, re.S)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    if isinstance(obj, dict) and "actions" in obj:
                        return [obj.get("analysis") or "", obj.get("actions") or [],
                                obj.get("suggestions") or []]
                except Exception:
                    pass
            m2 = re.search(r"\[.*\]", txt, re.S)
            return ["", json.loads(m2.group(0)), []] if m2 else ["", [], []]
        finally:
            try:
                c.delete(f"{url}/session/{sid}")
            except Exception:
                pass


def _orderbook(api: str, figi: str) -> dict:
    """Живой стакан: последняя цена, спред, перевес бидов, глубина в ₽."""
    try:
        ob = _http("GET", f"{api}/api/v1/bot/orderbook/{figi}?depth=5", timeout=20)
        return {k: ob.get(k) for k in ("last", "best_bid", "best_ask", "spread_bps",
                                       "bid_qty", "ask_qty", "imbalance", "depth_rub")}
    except Exception:
        return {}


def _bars(api: str, figi: str, tf: str = "5min", limit: int = 10) -> list:
    """Живые свечи из буфера рантайма (последние N баров)."""
    try:
        r = _http("GET", f"{api}/api/v1/bot/bars/{figi}?tf={tf}&limit={limit}", timeout=20)
        return [{"t": b["ts"][11:16], "o": b["o"], "h": b["h"], "l": b["l"],
                 "c": b["c"], "v": b["v"]} for b in (r.get("bars") or [])]
    except Exception:
        return []


def _context(api: str) -> dict:
    out: dict = {}
    st: dict = {}
    try:
        st = _http("GET", f"{api}/api/v1/bot/status")
        out["portfolio"] = st.get("portfolio")
        out["long_short"] = st.get("long_short")
        out["imoex"] = st.get("imoex_guard")
        out["risk"] = st.get("risk")
    except Exception:
        pass
    # Позиции с деталями + их стаканы
    try:
        pos = _http("GET", f"{api}/api/v1/bot/state")
        items = pos.get("positions") or []
        out["positions"] = []
        for p in items[:8]:
            d = {k: p.get(k) for k in
                 ("ticker", "side", "qty", "entry", "last", "pnl", "sl", "tp",
                  "dist_sl_atr", "dist_tp_atr", "atr", "regime", "trail_active")}
            d["orderbook"] = _orderbook(api, p.get("figi"))
            d["m5"] = _bars(api, p.get("figi"), "5min", 10)
            d["h1"] = _bars(api, p.get("figi"), "hour", 6)
            out["positions"].append(d)
    except Exception:
        out["positions"] = []
    # Движения по горизонтам (+ стаж в группе)
    try:
        mv = _http("GET", f"{api}/api/v1/screener/movers?top=6")
        out["movers"] = mv.get("horizons")
    except Exception:
        out["movers"] = {}
    # Вселенная: цена/оборот/волатильность + стаканы кандидатов (топ рост/падение 1м)
    try:
        scr = _http("GET", f"{api}/api/v1/screener")
        rows = [r for r in (scr.get("items") or []) if r.get("in_universe")]
        out["universe"] = [{"ticker": r["ticker"], "price": r.get("price"),
                            "turnover": r.get("turnover"), "rng_pct": r.get("rng_pct")}
                           for r in rows[:40]]
        _cand: list[tuple[str, str]] = []
        for lbl in ("1м",):
            h = (out.get("movers") or {}).get(lbl) or {}
            for x in (h.get("up") or [])[:3] + (h.get("down") or [])[:3]:
                _cand.append((x.get("ticker"), x.get("ticker")))
        _by_tk = {r["ticker"]: r["figi"] for r in rows}
        out["orderbooks"] = {}
        for tk, _ in _cand[:6]:
            fg = _by_tk.get(tk)
            if fg:
                out["orderbooks"][tk] = _orderbook(api, fg)
                out.setdefault("candles", {})[tk] = {
                    "m5": _bars(api, fg, "5min", 10),
                    "h1": _bars(api, fg, "hour", 6),
                }
    except Exception:
        out["universe"] = []
    # Сделки
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
    ap.add_argument("--report-only", action="store_true",
                    help="только отчёт/предложения и закрытия, без открытия позиций")
    args = ap.parse_args()

    def post_prompt() -> None:
        try:
            _http("POST", f"{args.api}/api/v1/bot/ai_prompt", {
                "provider": "opencode", "model": args.model, "shadow": False,
                "system": SYSTEM,
                "context_schema": {
                    "portfolio": "equity/cash/маржа, long_short",
                    "positions[]": "entry/last/pnl/sl/tp/dist_*_atr/regime + orderbook + m5/h1 свечи",
                    "orderbooks{}": "стакан кандидатов (imbalance, depth_rub)",
                    "movers": "1д/1н/1м/3м + streak",
                    "universe[]": "price/turnover/rng_pct",
                    "imoex": "dir/pct_20m/pct_60m/pct_day, breadth_up_pct",
                    "recent_trades": "последние сделки",
                },
            }, timeout=20)
        except Exception:
            pass

    print(f"[ai-trader] api={args.api} model={args.model} interval={args.interval}s", flush=True)
    post_prompt()
    _cycle = 0
    while True:
        _cycle += 1
        if _cycle % 100 == 0:
            post_prompt()
        t0 = time.monotonic()
        try:
            ctx = _context(args.api)
            n_pos = len(ctx.get("positions") or [])
            eq = (ctx.get("portfolio") or {}).get("equity")
            print(f"[ai-trader] контекст: позиций {n_pos}, equity {eq}, "
                  f"тикеров {len(ctx.get('universe') or [])}", flush=True)
            analysis, acts, sugg = _ask(SYSTEM, ctx, args.model, args.opencode_url)
            if analysis:
                print(f"[ai-trader] РАЗБОР: {analysis}", flush=True)
                try:
                    _http("POST", f"{args.api}/api/v1/bot/ai_notes", {
                        "ticker": "ПОРТФЕЛЬ", "side": "", "action": "analysis",
                        "note": str(analysis)[:900], "advice": "", "model": args.model,
                        "provider": "opencode",
                    }, timeout=20)
                except Exception:
                    pass
                try:
                    _http("POST", f"{args.api}/api/v1/bot/ai_report", {
                        "model": args.model, "analysis": str(analysis)[:6000],
                        "suggestions": sugg[:8], "actions": acts[:10],
                        "now_msk": ctx.get("now_msk"),
                    }, timeout=20)
                except Exception:
                    pass
            print(f"[ai-trader] действий: {len(acts)}", flush=True)
            for a in acts[:6]:
                try:
                    act = str(a.get("action") or "").lower()
                    tk = str(a.get("ticker") or "").upper()
                    if not tk:
                        continue
                    if args.report_only and act == "open":
                        print(f"[ai-trader] open {tk} пропущен (--report-only)", flush=True)
                        continue
                    if act == "close":
                        r = _http("POST", f"{args.api}/api/v1/bot/ai_trade",
                                  {"ticker": tk, "action": "close", "reason": a.get("reason", "")})
                    elif act == "open":
                        r = _http("POST", f"{args.api}/api/v1/bot/ai_trade", {
                            "ticker": tk, "action": "open", "side": str(a.get("side") or "SELL"),
                            "notional_pct": a.get("notional_pct"), "sl_pct": a.get("sl_pct"),
                            "tp_pct": a.get("tp_pct"), "reason": a.get("reason", ""),
                            "hold": str(a.get("hold") or "intraday"),
                        })
                    else:
                        continue
                    _why = str(a.get("reason") or "")[:600]
                    print(f"[ai-trader] {act} {tk}: {json.dumps(r, ensure_ascii=False)[:100]}", flush=True)
                    if _why:
                        print(f"[ai-trader]   причина: {_why}", flush=True)
                    try:
                        _http("POST", f"{args.api}/api/v1/bot/ai_notes", {
                            "ticker": tk, "side": str(a.get("side") or ""), "action": act,
                            "note": _why, "advice": "", "model": args.model,
                            "provider": "opencode",
                        }, timeout=20)
                    except Exception:
                        pass
                    try:
                        with open("ai_trader_cycles.jsonl", "a", encoding="utf-8") as f:
                            f.write(json.dumps({"ts": ctx.get("now_msk"), "analysis": analysis,
                                                "action": a, "result": r},
                                               ensure_ascii=False, default=str) + "\n")
                    except Exception:
                        pass
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
