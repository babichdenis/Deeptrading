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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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

ДАННЫЕ О ЦЕНЕ И ОБЪЁМЕ (используй их, не выдумывай):
- candles_1m: последние 5 закрытых 1м свечей (время t — МСК; o/h/l/c/v) — видно импульс и разворот;
- volume: {last, mean50, ratio} — объём последнего бара, средний за 50 и их отношение;
- signal_features — фичи сигнала (голоса, объёмные фичи), если переданы.
Правила по ним:
- вход SELL, а последние 3+ свечи растут (каждая close выше предыдущей) → вход против
  импульса: REJECT или совет подождать разворот/подтверждение;
- вход BUY, а последние 3+ свечи падают → то же зеркально;
- volume.ratio < 0.5 → низкая ликвидность: совет уменьшить размер или подождать;
- volume.ratio > 3 и сторона против бара → возможен выброс: осторожно, совет подождать.
- orderbook (если есть): spread_bps — ширина спреда (широкий > 15–20 б.п. → плохая точка входа,
  совет подождать/лимитником); imbalance — перевес бидов (+ покупатели, − продавцы):
  SELL при imbalance > +0.3 или BUY при imbalance < −0.3 → вход против потока заявок,
  осторожно/отклонить; depth_rub — плотность стакана (мало, < ~100 тыс ₽ → совет уменьшить размер).

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
        "now_msk": datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M"),
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
    # Последние 1м свечи (3-5) + объём (last / mean50 / ratio) — цена и ликвидность.
    try:
        figi = str(order.get("figi") or "")
        a = _http("GET", f"{api}/api/analysis/{figi}?interval_name=1min&limit=60", timeout=25)
        cs = a.get("candles") or []
        def _num(v):
            try:
                return float(v)
            except Exception:
                return None

        def _t_msk(v):
            try:
                s = str(v).replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone(timedelta(hours=3))).strftime("%H:%M")
            except Exception:
                return str(v)[11:16]

        out["candles_1m"] = [
            {"t": _t_msk(c.get("ts")),
             "o": _num(c.get("open")), "h": _num(c.get("high")),
             "l": _num(c.get("low")), "c": _num(c.get("close")),
             "v": int(_num(c.get("volume")) or 0)}
            for c in cs[-5:]
        ]
        vols = [(_num(c.get("volume")) or 0.0) for c in cs[-51:]]
        if vols:
            _last = vols[-1]
            _prev = vols[:-1] or vols
            _mean = sum(_prev) / len(_prev) if _prev else 0.0
            out["volume"] = {"last": int(_last), "mean50": int(_mean),
                             "ratio": (round(_last / _mean, 2) if _mean > 0 else None)}
    except Exception as e:
        out["candles_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    # Стакан (order book) — ликвидность и перевес заявок на момент входа.
    try:
        _figi_ob = str(order.get("figi") or "")
        ob = _http("GET", f"{api}/api/v1/bot/orderbook/{_figi_ob}?depth=10", timeout=30)
        if isinstance(ob, dict) and ob.get("spread_bps") is not None:
            out["orderbook"] = {k: ob.get(k) for k in
                                ("last", "best_bid", "best_ask", "spread_bps",
                                 "bid_qty", "ask_qty", "imbalance", "depth_rub",
                                 "top_bids", "top_asks")}
    except Exception as e:
        out["orderbook_error"] = f"{type(e).__name__}: {str(e)[:80]}"
    # Фичи сигнала (голоса/объёмные фичи), если есть в meta заявки.
    try:
        _feats = {k: meta.get(k) for k in
                  ("votes", "volume_features", "quorum", "atr_pct", "regime", "entry_tf")
                  if meta.get(k) is not None}
        if _feats:
            out["signal_features"] = _feats
    except Exception:
        pass
    return out


def _ask_deepseek(order: dict, ctx: dict, model: str, base: str, key: str,
                  system: str = SYSTEM, parser=None) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
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
    return (parser or _parse_decision)(txt)


def _extract_json(txt: str) -> str:
    """Достать JSON из ответа модели (может быть в ```json ... ```)."""
    t = txt.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1] if t.count("```") >= 2 else t.strip("`")
        if t.startswith("json"):
            t = t[4:]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t


SYSTEM_WATCH = """Ты — вахтёр ОТКРЫТЫХ ПОЗИЦИЙ торгового бота (MOEX). По позиции реши:
hold (держать), tighten (подтянуть стоп), close (закрыть), watch (наблюдать).
Отвечай СТРОГО JSON:
{"action": "hold"|"tighten"|"close"|"watch", "sl": <число или null>, "tp": <число или null>,
 "reason": "коротко по-русски", "advice": "что сделать", "confidence": 0.0-1.0}

В контексте есть levels.sl_suggest / levels.tp_suggest — готовые безопасные уровни.
Если решаешь tighten — просто подставь их в поля sl/tp (или свой более осторожный уровень).
Поле "sl" — НОВАЯ цена стопа, если action=tighten (иначе null):
- LONG: новая цена ВЫШЕ текущего стопа и НИЖЕ текущей цены (подтягиваем вверх);
- SHORT: новая цена НИЖЕ текущего стопа и ВЫШЕ текущей цены (подтягиваем вниз);
- не дальше ~2 ATR от текущего стопа за один шаг; не ставь стоп вплотную к цене
  (оставляй запас ~0.3 ATR), иначе выбьет шумом.
Поле "tp" — НОВАЯ цена цели (только ПОДТЯНУТЬ к цене, если позиция в плюсе, но цена
развернулась против неё — «забрать прибыль»):
- LONG: новая цена НИЖЕ текущей цели и ВЫШЕ текущей цены (+запас ~0.3 ATR);
- SHORT: новая цена ВЫШЕ текущей цели и НИЖЕ текущей цены (−запас ~0.3 ATR);
- не двигай TP дальше от цены и не ставь его, если позиция в минусе;
- за один шаг — не более ~2 ATR.

Правила (по приоритету):
1. dist_sl_atr <= 1.0 → позиция почти у стопа: close или tighten (защитить остаток).
2. dist_tp_atr <= 1.0 → цель близко: hold или tighten (зафиксировать прибыль).
3. pnl < 0 и цена идёт против позиции (last хуже entry) → tighten или close.
4. pnl > 0 и dist_tp_atr > 2 → hold (пусть работает).
5. Плохой контекст (серия убытков, вход против IMOEX-всплеска, риск не NORMAL) → close/tighten.
6. Нет причин → hold.

Только слова: ты НЕ управляешь ботом, твой ответ — совет человеку. Не выдумывай данные."""


def _parse_watch(txt: str) -> dict:
    try:
        d = json.loads(_extract_json(txt))
    except Exception:
        d = {"action": "watch", "reason": f"parse_error: {txt[:80]}", "confidence": 0.0}
    a = str(d.get("action", "watch")).lower()
    if a not in ("hold", "tighten", "close", "watch"):
        a = "watch"
    d["action"] = a
    for _k in ("sl", "tp"):
        _v = d.get(_k)
        try:
            d[_k] = float(_v) if _v not in (None, "", "null") else None
        except Exception:
            d[_k] = None
    _r = str(d.get("reason") or "").strip()
    for _junk in ("коротко по-русски,", "коротко по-русски", "коротко,"):
        if _r.lower().startswith(_junk):
            _r = _r[len(_junk):].strip()
    d["reason"] = _r[:300]
    d["advice"] = str(d.get("advice") or "")[:300]
    return d


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


def _ask_ollama(order: dict, ctx: dict, model: str, base: str,
                system: str = SYSTEM, parser=None) -> dict:
    """Локальный Ollama на .2 (OpenAI-совместимый /v1). Бесплатно, без лимитов."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps({"order": order, "context": ctx},
                                                   ensure_ascii=False, default=str)[:12000]},
        ],
        "temperature": 0.1,
        "max_tokens": 400,
        "response_format": {"type": "json_object"},
    }
    with httpx.Client(timeout=240.0) as c:
        r = c.post(f"{base.rstrip('/')}/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
    txt = (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "{}"
    return (parser or _parse_decision)(txt)


def _ask_opencode(order: dict, ctx: dict, model: str, url: str,
                  system: str = SYSTEM, parser=None) -> dict:
    """Big Pickle и другие модели opencode zen — через локальный `opencode serve`
    (бесплатный tier Zen работает только внутри opencode)."""
    url = url.rstrip("/")
    with httpx.Client(timeout=120.0) as c:
        sid = c.post(f"{url}/session", json={"title": "ai-gate"}).json().get("id")
        try:
            body = {
                "model": {"providerID": "opencode", "modelID": model},
                "system": system,
                "parts": [{"type": "text", "text": json.dumps(
                    {"order": order, "context": ctx}, ensure_ascii=False, default=str)[:12000]}],
            }
            r = c.post(f"{url}/session/{sid}/message", json=body)
            r.raise_for_status()
            d = r.json()
            txt = "".join(p.get("text", "") for p in (d.get("parts") or [])
                          if p.get("type") == "text")
            return (parser or _parse_decision)(txt)
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


def _apply_ai_levels(args, api: str, p: dict, new_sl: float | None, new_tp: float | None) -> dict:
    """Применить SL/TP от ИИ с жёсткими правилами.

    SL — только подтяжка (в сторону прибыли); TP — только подтяжка к цене (защита прибыли).
    Не ближе buffer ATR к цене; шаг не больше max_step ATR. Иначе — отказ с причиной.
    """
    side = str(p.get("side") or "").upper()
    last = p.get("last")
    sl0 = p.get("sl")
    tp0 = p.get("tp")
    atr = p.get("atr")
    if not (last and atr):
        return {"ok": False, "error": "нет данных (last/atr)"}
    try:
        last = float(last)
        atr = float(atr)
        sl0 = float(sl0) if sl0 else None
        tp0 = float(tp0) if tp0 else None
        new_sl = float(new_sl) if new_sl else None
        new_tp = float(new_tp) if new_tp else None
    except Exception:
        return {"ok": False, "error": "нечисловые данные"}
    buf = float(args.ai_sl_buffer_atr) * atr
    step = float(args.ai_sl_max_step_atr) * atr
    applied: dict = {}
    errs: list[str] = []

    if new_sl is not None:
        if sl0 is None:
            errs.append("нет текущего SL")
        elif side == "LONG":
            if new_sl <= sl0:
                errs.append(f"SL {new_sl:.4f} не выше текущего {sl0:.4f}")
            elif new_sl >= last - buf:
                errs.append(f"SL близко к цене (буфер {buf:.4f})")
            elif new_sl - sl0 > step:
                errs.append(f"шаг SL {new_sl - sl0:.4f} > лимита {step:.4f}")
            else:
                applied["sl"] = round(new_sl, 6)
        else:
            if new_sl >= sl0:
                errs.append(f"SL {new_sl:.4f} не ниже текущего {sl0:.4f}")
            elif new_sl <= last + buf:
                errs.append(f"SL близко к цене (буфер {buf:.4f})")
            elif sl0 - new_sl > step:
                errs.append(f"шаг SL {sl0 - new_sl:.4f} > лимита {step:.4f}")
            else:
                applied["sl"] = round(new_sl, 6)

    if new_tp is not None:
        if tp0 is None:
            errs.append("нет текущего TP (трейлинг?)")
        elif side == "LONG":
            if new_tp >= tp0:
                errs.append(f"TP {new_tp:.4f} не ниже текущего {tp0:.4f}")
            elif new_tp <= last + buf:
                errs.append(f"TP близко к цене (буфер {buf:.4f})")
            elif tp0 - new_tp > step:
                errs.append(f"шаг TP {tp0 - new_tp:.4f} > лимита {step:.4f}")
            else:
                applied["tp"] = round(new_tp, 6)
        else:
            if new_tp <= tp0:
                errs.append(f"TP {new_tp:.4f} не выше текущего {tp0:.4f}")
            elif new_tp >= last - buf:
                errs.append(f"TP близко к цене (буфер {buf:.4f})")
            elif new_tp - tp0 > step:
                errs.append(f"шаг TP {new_tp - tp0:.4f} > лимита {step:.4f}")
            else:
                applied["tp"] = round(new_tp, 6)

    if not applied:
        return {"ok": False, "error": "; ".join(errs) or "нечего применять"}
    try:
        res = _http("POST", f"{api}/api/v1/bot/positions/levels",
                    {"ticker": p.get("ticker"), **applied})
        return {"ok": True, "applied": applied, "errors": errs, "result": res}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:100]}"}


def run_watch(args, provs: list[str], models: dict) -> None:
    """Вахтёр позиций: раз в N сек смотрит позиции у SL/TP или в минусе,
    спрашивает модели (hold/tighten/close) и пишет заметки — словами, без управления."""
    _seen: dict[str, tuple[str, float]] = {}
    MSK = timezone(timedelta(hours=3))
    print(f"[ai-watch] providers={','.join(provs)} interval={args.watch_interval}s "
          f"min_atr={args.watch_min_atr}", flush=True)
    while True:
        try:
            st = _http("GET", f"{args.api}/api/v1/bot/state")
            stt = _http("GET", f"{args.api}/api/v1/bot/status")
            poss = (st.get("positions") or []) if isinstance(st, dict) else []
            guard = (stt or {}).get("imoex_guard") if isinstance(stt, dict) else None
            risk = (stt or {}).get("risk") if isinstance(stt, dict) else None
            for p in poss:
                ds = p.get("dist_sl_atr")
                dt = p.get("dist_tp_atr")
                pnl = p.get("pnl")
                near = ((ds is not None and ds <= float(args.watch_min_atr))
                        or (dt is not None and dt <= float(args.watch_min_atr)))
                losing = (pnl is not None and pnl < 0)
                if not (near or losing):
                    continue
                tk = str(p.get("ticker") or "")
                # Готовые уровни-подсказки (безопасные), чтобы модель не считала, а выбирала.
                _levels: dict = {}
                try:
                    _last = float(p.get("last") or 0)
                    _atr = float(p.get("atr") or 0)
                    _sl0 = float(p.get("sl")) if p.get("sl") else None
                    _tp0 = float(p.get("tp")) if p.get("tp") else None
                    _side = str(p.get("side") or "").upper()
                    if _last and _atr:
                        if _side == "LONG":
                            _cand = round(_last - 0.5 * _atr, 6)
                            if _sl0 is None or _cand > _sl0:
                                _levels["sl_suggest"] = _cand
                            _cand_tp = round(_last + 0.5 * _atr, 6)
                            if _tp0 is not None and _cand_tp < _tp0:
                                _levels["tp_suggest"] = _cand_tp
                        else:
                            _cand = round(_last + 0.5 * _atr, 6)
                            if _sl0 is None or _cand < _sl0:
                                _levels["sl_suggest"] = _cand
                            _cand_tp = round(_last - 0.5 * _atr, 6)
                            if _tp0 is not None and _cand_tp > _tp0:
                                _levels["tp_suggest"] = _cand_tp
                except Exception:
                    pass
                ctx = {
                    "now_msk": datetime.now(timezone.utc).astimezone(MSK).strftime("%Y-%m-%d %H:%M"),
                    "position": {k: p.get(k) for k in
                                 ("ticker", "side", "qty", "entry", "last", "pnl", "sl", "tp",
                                  "atr", "dist_sl_pct", "dist_tp_pct", "dist_sl_atr", "dist_tp_atr",
                                  "regime", "trail_active")},
                    "levels": _levels,
                    "guard": guard, "risk": risk,
                }

                def _one(prov: str):
                    _t = time.monotonic()
                    try:
                        if prov == "opencode":
                            d = _ask_opencode(p, ctx, models.get("opencode", "big-pickle"),
                                              args.opencode_url, system=SYSTEM_WATCH, parser=_parse_watch)
                        elif prov == "ollama":
                            d = _ask_ollama(p, ctx, models.get("ollama", "llama3.2:3b"),
                                            args.ollama_url, system=SYSTEM_WATCH, parser=_parse_watch)
                        else:
                            d = _ask_deepseek(p, ctx, models.get("deepseek", "deepseek-chat"),
                                              os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                                              os.environ.get("DEEPSEEK_API_KEY", ""), system=SYSTEM_WATCH, parser=_parse_watch)
                    except Exception as e:
                        d = {"action": "watch", "reason": f"error: {type(e).__name__}: {str(e)[:80]}",
                             "advice": ""}
                    return d, int((time.monotonic() - _t) * 1000)

                with ThreadPoolExecutor(max_workers=max(1, len(provs))) as ex:
                    futs = {pr: ex.submit(_one, pr) for pr in provs}
                    out = {pr: f.result() for pr, f in futs.items()}
                for prov, (d, lat) in out.items():
                    act = d.get("action", "watch")
                    prev = _seen.get(tk)
                    if prev and prev[0] == act and (time.monotonic() - prev[1]) < 600:
                        continue  # не спамим одинаковым советом
                    _seen[tk] = (act, time.monotonic())
                    rec = {"ticker": tk, "side": p.get("side"), "action": act,
                           "note": d.get("reason", ""), "advice": d.get("advice", ""),
                           "model": models.get(prov, ""), "provider": prov, "latency_ms": lat,
                           "dist_sl_atr": ds, "dist_tp_atr": dt, "pnl": pnl}
                    # --- Применение SL/TP (если разрешено): только подтяжка, с лимитами ---
                    if act == "tighten" and not (d.get("sl") or d.get("tp")) and _levels:
                        if _levels.get("sl_suggest"):
                            d["sl"] = _levels["sl_suggest"]
                        if _levels.get("tp_suggest"):
                            d["tp"] = _levels["tp_suggest"]
                    _want = bool(d.get("sl") or d.get("tp"))
                    if args.ai_sl_manage and act == "tighten" and _want and not p.get("trail_active"):
                        _ap = _apply_ai_levels(args, args.api, p, d.get("sl"), d.get("tp"))
                        rec["applied_levels"] = _ap
                        if _ap.get("ok"):
                            rec["advice"] = f"{rec['advice']} → применено: {_ap.get('applied')}"
                        else:
                            rec["note"] = f"{rec['note']} | уровни отклонены: {_ap.get('error')}"
                    elif args.ai_sl_manage and act == "tighten" and _want and p.get("trail_active"):
                        rec["note"] = f"{rec['note']} | трейлинг активен — уровни не трогаем"
                    try:
                        _http("POST", f"{args.api}/api/v1/bot/ai_notes", rec)
                    except Exception:
                        pass
                    _log({"kind": "position_watch", **rec})
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"[ai-watch] cycle error: {type(e).__name__}: {str(e)[:120]}", flush=True)
        time.sleep(max(15.0, float(args.watch_interval)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("BOT_API_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--dry-run", action="store_true",
                    help="shadow: решения НЕ применяются, только логируются и уходят в UI")
    ap.add_argument("--provider", default="opencode", choices=("opencode", "deepseek", "ollama"))
    ap.add_argument("--providers", default="", help="список через запятую: opencode,ollama (параллельно)")
    ap.add_argument("--apply", default="", help="чей вердикт применять (по умолчанию первый из providers)")
    ap.add_argument("--opencode-url", default=os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096"))
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://192.168.1.2:11434/v1"))
    ap.add_argument("--ollama-model", default="", help="модель Ollama (по умолчанию llama3.2:3b)")
    ap.add_argument("--model", default="")
    ap.add_argument("--selftest", action="store_true", help="проверить провайдера синтетической заявкой и выйти")
    ap.add_argument("--full-context", action="store_true",
                    help="слать полный контекст (state+portfolio+8 сделок); по умолчанию — компактный")
    ap.add_argument("--watch-positions", action="store_true",
                    help="режим вахтёра позиций: hold/tighten/close словами (без управления)")
    ap.add_argument("--watch-interval", type=float, default=60.0)
    ap.add_argument("--watch-min-atr", type=float, default=1.5,
                    help="наблюдать позиции ближе N ATR к SL/TP (и все убыточные)")
    ap.add_argument("--ai-sl-manage", action="store_true",
                    help="разрешить llama ПОДТЯГИВАТЬ SL (только в сторону прибыли, с лимитами)")
    ap.add_argument("--ai-sl-max-step-atr", type=float, default=2.0)
    ap.add_argument("--ai-sl-buffer-atr", type=float, default=0.3)
    args = ap.parse_args()

    _load_env()
    _provs = [p.strip() for p in (args.providers or args.provider).split(",") if p.strip()]
    if not _provs:
        _provs = ["opencode"]
    _apply = (args.apply or _provs[0]).strip()
    if _apply not in _provs:
        _apply = _provs[0]
    _models = {
        "opencode": args.model or "big-pickle",
        "ollama": args.ollama_model or "llama3.2:3b",
        "deepseek": args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
    }
    if "deepseek" in _provs and not os.environ.get("DEEPSEEK_API_KEY"):
        print("НЕТ DEEPSEEK_API_KEY (env или backend/.env) — выход", file=sys.stderr)
        raise SystemExit(2)
    print(f"[ai-gate] providers={','.join(_provs)} apply={_apply} "
          f"models={ {p: _models.get(p) for p in _provs} } api={args.api} dry_run={args.dry_run}",
          flush=True)

    def decide(provider: str, order: dict, ctx: dict) -> dict:
        if provider == "opencode":
            return _ask_opencode(order, ctx, _models["opencode"], args.opencode_url)
        if provider == "ollama":
            return _ask_ollama(order, ctx, _models["ollama"], args.ollama_url)
        return _ask_deepseek(order, ctx, _models["deepseek"],
                             os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                             os.environ.get("DEEPSEEK_API_KEY", ""))

    # Отдаём текущий промпт/конфиг в бота — виден в UI (вкладка AI-гейт).
    def post_prompt() -> bool:
        try:
            _http("POST", f"{args.api}/api/v1/bot/ai_prompt", {
                "provider": ",".join(_provs), "model": ",".join(_models.get(p, "") for p in _provs),
                "shadow": args.dry_run, "system": SYSTEM,
                "context_schema": {
                    "order": "ticker, side, qty, price, reason",
                    "context": "now_msk, positions, equity, guard, risk, session, "
                               "recent_trades_ticker, candles_1m (5×1м, МСК), volume{last,mean50,ratio}",
                    "apply": _apply,
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
        with ThreadPoolExecutor(max_workers=max(1, len(_provs))) as ex:
            futs = {p: ex.submit(decide, p, sample, {"note": "synthetic"}) for p in _provs}
            for p, f in futs.items():
                try:
                    print(f"SELFTEST[{p}]:", json.dumps(f.result(), ensure_ascii=False), flush=True)
                except Exception as e:
                    print(f"SELFTEST[{p}]: ERROR {type(e).__name__}: {e}", flush=True)
        return

    if args.watch_positions:
        run_watch(args, _provs, _models)
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

                def _timed(p: str):
                    _t = time.monotonic()
                    try:
                        _d = decide(p, order, ctx)
                    except Exception as e:
                        _d = {"decision": "skip",
                              "reason": f"{p}_error: {type(e).__name__}: {str(e)[:120]}",
                              "advice": "", "confidence": 0.0}
                    return _d, int((time.monotonic() - _t) * 1000)

                with ThreadPoolExecutor(max_workers=max(1, len(_provs))) as ex:
                    futs = {p: ex.submit(_timed, p) for p in _provs}
                    out = {p: f.result() for p, f in futs.items()}
                decs = {p: out[p][0] for p in _provs}
                agree = len({str(decs[p].get("decision")) for p in _provs}) == 1
                applied_dec = decs.get(_apply, {})
                applied_res = None
                if not args.dry_run and applied_dec.get("decision") in ("approve", "reject"):
                    try:
                        applied_res = _http(
                            "POST", f"{args.api}/api/v1/bot/approvals/{oid}/{applied_dec['decision']}",
                            {"reason": f"AI[{_apply}]: {applied_dec.get('reason', '')[:200]}"})
                    except Exception as e:
                        applied_res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
                for p in _provs:
                    d = decs[p]
                    rec = {"ts": datetime.now(timezone.utc).isoformat(), "order_id": oid,
                           "ticker": order.get("ticker"), "side": order.get("side"),
                           "qty": order.get("qty"), "figi": order.get("figi"),
                           "provider": p, "model": _models.get(p, ""),
                           "decision": d.get("decision"), "reason": d.get("reason", ""),
                           "advice": d.get("advice", ""), "confidence": d.get("confidence"),
                           "latency_ms": out[p][1], "agreement": agree,
                           "applied": bool(p == _apply and applied_res is not None),
                           "dry_run": args.dry_run, "shadow": args.dry_run}
                    if p == _apply and applied_res is not None:
                        rec["apply_result"] = applied_res
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
