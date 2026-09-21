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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SYSTEM = """Ты — автономный трейдер на MOEX (счёт бота; активный контур указан в контексте:
sandbox = тестовый счёт, live = реальные деньги). Ты САМ решаешь, что делать:
открывать/закрывать позиции, какие ставить стоп/тейк. Никаких «рекомендаций» — только действия.

Вход:
- portfolio: equity/cash/маржа; long_short: баланс L/S;
- positions[]: открытые позиции (entry/last/pnl/sl/tp/dist_*_atr/regime) + orderbook каждой;
- orderbooks{ticker}: ЖИВОЙ СТАКАН — last, best_bid/best_ask, spread_bps, bid_qty/ask_qty,
  imbalance (-1..+1, >0 = перевес покупок), depth_rub (глубина в рублях);
- positions[].m5/h1: ЖИВЫЕ СВЕЧИ позиции (o/h/l/c/v, время МСК) — 5м и час;
- movers: движения по горизонтам 1д/1н/1м/3м (up/down + streak; all — все тикеры);
- universe[]: весь универс. У ЛЮБОГО тикера есть price/turnover/rng_pct и
  chg_1d/chg_1w/chg_1m/chg_3m. Полные данные (orderbook, m5/h1, d20/dv20, atr5/atr_d)
  добавлены только по ФОКУСУ (focus[]: позиции + очередь движка + лидеры движений + новости).
  Если нужен тикер без полных данных — можешь торговать по цене/движению, но оценивай риск выше.
  in_universe=false значит тикер НЕ входит в торговый универс движка (нет стрима/оптуны);
  заявка по нему может не исполниться — предпочитай in_universe=true.
- news[]: свежие новости (source/age_min/title/tickers, топ-6 за ≤6ч) — контекст «в теме»;
- queue[]: очередь движка — сильнейшие кандидаты бота (ticker/side/score/why);
- imoex: направление индекса (dir/pct_20m/pct_60m/pct_day, breadth_up_pct);
- risk: дневной P&L/лимит; recent_trades: последние сделки; now_msk.

Правила:
- маржа: бот не даст превысить 80% equity; стресс ±5% IMOEX ≤ 10% equity; слот = slot_pct
  от equity (bot.slot_pct), следи за margin_use_pct — не выходи за лимит маржи;
- максимум 6 позиций одновременно; не открывай больше 3 новых позиций за цикл;
- КЛАСТЕРЫ: не набирай >2 позиции в одном секторе/кластере (нефтегаз, металлы и т.п.) —
  коррелированные лонги дают общий стресс, а не диверсификацию;
- CHASING GATE: НЕ покупай после роста >2.5-3% за день без отката и НЕ шорти после
  падения >2.5-3% без отскока — это погоня за ценой с плохим R/R. Вход в лонг — после
  отката (цена ≤ high дня − 0.5×atr_d) или у VWAP; зеркально для шорта;
- СТАКАН: не входи против потока — BUY при imbalance < −0.3 или SELL при imbalance > +0.3
  пропускай; spread > 25 б.п. — не входить (плохая точка входа);
- СТОП/ТЕЙК от ATR: SL ≈ 1.5-3× ATR (для интрадея обычно 2-3× atr5/atr_d), риск на
  сделку ≤0.8% equity; TP 3-5× ATR (12+ ATR для интрадея недостижим). Если позиция дала
  +1.5-2× ATR — подтяни TP к цене (зафиксируй часть), а SL — не хуже безубытка;
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
               как это связано с текущими позициями, общий план на цикл (3-5 предложений; короче — быстрее цикл)",
  "actions": [
    {"action":"open","ticker":"SBER","side":"SELL","notional_pct":1.0,"sl_pct":0.03,"tp_pct":0.06,
     "hold":"intraday|swing",
     "reason":"почему именно эта сделка: что в данных говорит за вход, почему эта сторона,
               где стоп/тейк и что подтверждает/опровергает тезис (1-2 предложения)"},
    {"action":"close","ticker":"GAZP",
     "reason":"почему закрываю: что изменилось в данных/тезисе (1-2 предложения)"}
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


def _parse_reply(txt: str) -> tuple:
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
    if m2:
        try:
            obj = json.loads(m2.group(0))
            if isinstance(obj, list):
                return ["", obj, []]
        except Exception:
            pass
    return ["", [], []]


def _ask_deepseek(system: str, user: dict, model: str = "deepseek-chat") -> tuple:
    """DeepSeek через API (ключ из env/.env). Замена opencode, когда Zen недоступен."""
    import os
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    if not key:
        try:
            for line in open(".env", encoding="utf-8"):
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if line.startswith("DEEPSEEK_BASE_URL="):
                    base = line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    if not key:
        raise RuntimeError("нет DEEPSEEK_API_KEY")
    body = {"model": model, "stream": False, "temperature": 0.3, "max_tokens": 3000,
            "response_format": {"type": "json_object"},
            "reasoning": os.environ.get("AI_THINKING", "0").lower() in ("1", "true", "yes", "on"), "messages": [
        {"role": "system", "content": system},
        {"role": "user",
         "content": json.dumps(user, ensure_ascii=False, default=str)[:100000]},
    ]}

    _usage: dict = {}

    def _call(msgs: list) -> str:
        body["messages"] = msgs
        with httpx.Client(timeout=300.0) as c:
            r = c.post(f"{base}/chat/completions",
                       headers={"Authorization": f"Bearer {key}"}, json=body)
            r.raise_for_status()
            data = r.json()
            try:
                _usage.update(data.get("usage") or {})
            except Exception:
                pass
            return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    msgs = list(body["messages"])
    txt = _call(msgs)
    out = _parse_reply(txt)
    if not out[1]:
        if not out[0]:
            # Модель ответила прозой без JSON — сохраняем текст как analysis.
            # Жёсткий ретрай убран: он провоцировал ответы про формат вместо анализа.
            return [str(txt).strip()[:3000], [], [], _usage]
    return [out[0], out[1], out[2], _usage]


def _ask(system: str, user: dict, model: str, url: str) -> tuple:
    url = url.rstrip("/")
    with httpx.Client(timeout=300.0) as c:
        sid = c.post(f"{url}/session", json={"title": "ai-trader"}).json().get("id")
        try:
            body = {
                "model": (lambda m: {"providerID": m.split("/", 1)[0], "modelID": m.split("/", 1)[1]}
                              if "/" in m else {"providerID": "opencode", "modelID": m})(str(model)),
                "system": system,
                "parts": [{"type": "text", "text": json.dumps(user, ensure_ascii=False, default=str)[:100000]}],
            }
            r = c.post(f"{url}/session/{sid}/message", json=body)
            r.raise_for_status()
            txt = "".join(p.get("text", "") for p in (r.json().get("parts") or [])
                          if p.get("type") == "text")
            _r = _parse_reply(txt)
            return [_r[0], _r[1], _r[2], {}]
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


def _calc_atr(highs: list, lows: list, closes: list, period: int = 14) -> float | None:
    """ATR (среднее True Range за period) по OHLC-сериям — для точных SL/TP уровней."""
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


_LEVEL_DISCIPLINE = """

— ДИСЦИПЛИНА УРОВНЕЙ (обязательно, приоритет выше твоих правил) —
1. НЕ подтягивай SL ближе 1 ATR к текущей цене. Стоп — защита от разворота, а не ловушка.
2. Переводи SL в зону прибыли (безубыток и выше) ТОЛЬКО когда прибыль ≥ 1 ATR. Иначе не трогай.
3. TP не ближе 0.5 ATR к цене. TP можно только ПОДТЯГИВАТЬ к цене, отодвигать запрещено.
4. Не трогай уровни чаще 1 раза в 15 минут и без веской причины (новый экстремум/разворот тренда).
5. Шум 1-2 свечей — НЕ причина двигать SL. Дай позиции дышать: цель — дать прибыли вырасти.
6. Новую сделку открывай ТОЛЬКО при уверенности ≥ 0.75, что будет плюс (тезис + данные).
   Сомневаешься — не открывай. Лучше пропустить, чем кормить комиссии.
7. Бот жёстко отклоняет уровни, нарушающие пп.1-3 (это не ошибка — это защита от переторговки).
"""

_AI_CTL_CACHE: dict = {"ts": 0.0, "data": {}}


def _ai_control(api: str, ttl: float = 30.0) -> dict:
    """Промпты/режим из UI (кэш ttl сек)."""
    if _AI_CTL_CACHE["data"] and (time.monotonic() - float(_AI_CTL_CACHE["ts"])) < ttl:
        return _AI_CTL_CACHE["data"]
    try:
        d = _http("GET", f"{api}/api/v1/bot/ai_control", timeout=10)
        _AI_CTL_CACHE["data"] = d if isinstance(d, dict) else {}
        _AI_CTL_CACHE["ts"] = time.monotonic()
    except Exception:
        pass
    return _AI_CTL_CACHE["data"] or {}


def _context(api: str) -> dict:
    out: dict = {}
    st: dict = {}
    try:
        st = _http("GET", f"{api}/api/v1/bot/status")
        out["portfolio"] = st.get("portfolio")
        out["long_short"] = st.get("long_short")
        out["imoex"] = st.get("imoex_guard")
        out["risk"] = st.get("risk")
        try:
            _sum = _http("GET", f"{api}/api/v1/bot/portfolio_summary", timeout=20)
            _pf = out.get("portfolio") or {}
            if isinstance(_pf, dict) and isinstance(_sum, dict):
                _pf.setdefault("margin_pct", _sum.get("margin_use_pct"))
                _pf.setdefault("margin_used", _sum.get("margin_used"))
                _pf.setdefault("long_share", _sum.get("long_exposure_pct"))
                _pf.setdefault("short_share", _sum.get("short_exposure_pct"))
                _pf.setdefault("net_exposure", _sum.get("net_exposure_pct"))
                _st = _sum.get("stress_pct") or {}
                _pf.setdefault("stress_5pct_up", _st.get("imoex_+5%"))
                _pf.setdefault("stress_5pct_down", _st.get("imoex_-5%"))
                _sw = _sum.get("stress_worst")
                _pf.setdefault("stress_worst",
                               _sw[1] if isinstance(_sw, list) and len(_sw) > 1 else _sw)
        except Exception:
            pass
        _cfg = st.get("config") or {}
        # Полный конфиг бота (лимиты слота/маржи/сектора) — status.config урезан.
        try:
            _full = _http("GET", f"{api}/api/v1/bot/config", timeout=10) or {}
            if isinstance(_full, dict):
                _cfg = {**_cfg, **_full}
        except Exception:
            pass
        _contour = st.get("broker_mode") or st.get("contour") or _cfg.get("mode")
        out["contour"] = _contour
        out["bot"] = {"session_now": st.get("session"), "sessions": _cfg.get("sessions"),
                      "entries_paused": _cfg.get("entries_paused"),
                      "overnight": _cfg.get("overnight"),
                      "max_positions": _cfg.get("max_positions"),
                      "slot_pct": _cfg.get("pos_pct"),
                      "max_margin_use_pct": _cfg.get("max_margin_use_pct"),
                      "max_sector_pct": _cfg.get("max_sector_pct"),
                      "margin_sizing": _cfg.get("margin_sizing"),
                      "ensemble_quorum": _cfg.get("ensemble_quorum"),
                      "contour": _contour}
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
            # Поля, которые ждёт промпт трейдера: pnl_pct/notional_pct/dist_high_atr/dist_low_atr
            try:
                _q = abs(float(p.get("qty") or 0))
                _ep = float(p.get("entry") or 0)
                _lp = float(p.get("last") or 0)
                _notional = abs(float(p.get("notional") or (_q * _lp)))
                _eqp = float((out.get("portfolio") or {}).get("equity") or 0)
                d["pnl_pct"] = (round(float(p.get("pnl") or 0) / (_q * _ep), 4)
                                if (_q and _ep) else None)
                d["notional_pct"] = round(_notional / _eqp, 3) if _eqp else None
                _atr = float(p.get("atr") or 0)
                _cs = d.get("m5") or []
                if _cs and _atr > 0 and _lp > 0:
                    _hi = max(float(x.get("h") or 0) for x in _cs)
                    _lo = min(float(x.get("l") or 1e18) for x in _cs)
                    d["dist_high_atr"] = round((_hi - _lp) / _atr, 2)
                    d["dist_low_atr"] = round((_lp - _lo) / _atr, 2)
            except Exception:
                pass
            out["positions"].append(d)
    except Exception:
        out["positions"] = []
    # Движения по горизонтам (+ стаж в группе)
    try:
        mv = _http("GET", f"{api}/api/v1/screener/movers?top=6")
        out["movers"] = mv.get("horizons")
    except Exception:
        out["movers"] = {}
    # Вселенная: ВСЕ eligible-тикеры + лидеры движений. Полные данные (стакан, m5/h1,
    # d20/dv20, ATR) — только по ФОКУСУ (позиции + очередь + лидеры движений + новости),
    # остальные — лёгкие строки. Так контекст в разы меньше, а «в теме» AI остаётся.
    try:
        scr = _http("GET", f"{api}/api/v1/screener")
        _items = scr.get("items") or []
        rows = [r for r in _items if r.get("in_universe")][:40]
        _uni_tk = {r["ticker"] for r in rows}
        _by_all = {r["ticker"]: r for r in _items}
        # Очередь движка — до обогащения (влияет на фокус-набор)
        try:
            _pf = _http("GET", f"{api}/api/v1/bot/portfolio_summary", timeout=15)
            out["queue"] = [{"ticker": q.get("ticker"), "side": q.get("side"),
                             "score": q.get("score"), "why": q.get("why")}
                            for q in (_pf.get("queue") or [])[:5]]
        except Exception:
            out["queue"] = []
        # Новости (компактно, топ-6 за ≤6ч) — контекст «в теме» + тикеры в фокус
        _news_focus: set[str] = set()
        try:
            _nr = _http("GET", f"{api}/api/v1/news?limit=60", timeout=20)
            _news_rows = [x for x in (_nr.get("news") or [])
                          if x.get("age_min") is not None and int(x["age_min"]) <= 360]
            _news_rows.sort(key=lambda x: int(x.get("age_min") or 999))
            out["news"] = [{"source": x.get("source"), "age_min": x.get("age_min"),
                            "title": str(x.get("title") or "")[:140],
                            "tickers": x.get("tickers") or []}
                           for x in _news_rows[:6]]
            for x in out["news"]:
                _news_focus.update(str(t).upper() for t in (x.get("tickers") or []))
        except Exception:
            out["news"] = []
        # Фокус: позиции → очередь → лидеры движений (1д/1н/1м) → новости (кап 14)
        _focus: list[str] = []

        def _add_focus(tk):
            tk = str(tk or "").upper()
            if tk and tk not in _focus:
                _focus.append(tk)

        for p_ in (out.get("positions") or []):
            _add_focus(p_.get("ticker"))
        for q_ in (out.get("queue") or []):
            _add_focus(q_.get("ticker"))
        for lbl, k in (("1д", 3), ("1н", 2), ("1м", 2)):
            h = (out.get("movers") or {}).get(lbl) or {}
            for x in (h.get("up") or [])[:k] + (h.get("down") or [])[:k]:
                _add_focus(x.get("ticker"))
        for tk in _news_focus:
            _add_focus(tk)
        _focus_set = set(_focus[:14])
        out["focus"] = sorted(_focus_set)
        # Лидеры движений вне eligible-универса (LKOH/OZON/YDEX/MVID…)
        _extra: list[dict] = []
        for lbl in ("1д", "1н", "1м"):
            h = (out.get("movers") or {}).get(lbl) or {}
            for x in (h.get("up") or []) + (h.get("down") or []):
                tk = str(x.get("ticker") or "")
                if tk and tk not in _uni_tk and tk in _by_all:
                    _uni_tk.add(tk)
                    _extra.append(_by_all[tk])
        rows = rows + _extra[:6]
        # Движение по горизонтам для каждого тикера (полные списки movers)
        _chg: dict[str, dict] = {}
        for lbl, key in (("1д", "chg_1d"), ("1н", "chg_1w"), ("1м", "chg_1m"), ("3м", "chg_3m")):
            for x in (((out.get("movers") or {}).get(lbl) or {}).get("all") or []):
                tk = str(x.get("ticker") or "")
                if tk:
                    _chg.setdefault(tk, {})[key] = x.get("chg")

        def _enrich(r):
            tk = r["ticker"]
            fg = r["figi"]
            row = {"ticker": tk, "price": r.get("price"), "turnover": r.get("turnover"),
                   "rng_pct": r.get("rng_pct"),
                   "in_universe": bool(r.get("in_universe"))}
            row.update(_chg.get(tk) or {})
            if tk not in _focus_set:
                return row  # лёгкая строка: только цена/оборот/движение
            row["orderbook"] = _orderbook(api, fg)
            row["m5"] = _bars(api, fg, "5min", 6)
            row["h1"] = _bars(api, fg, "hour", 4)
            # ATR на 5м (по 20 барам) — точная база для стопов/тейков
            try:
                _m5all = _bars(api, fg, "5min", 20)
                _a5 = _calc_atr([b["h"] for b in _m5all], [b["l"] for b in _m5all],
                                [b["c"] for b in _m5all], 14)
                if _a5:
                    row["atr5"] = round(_a5, 4)
                    row["atr5_pct"] = round(_a5 / (row.get("price") or 1) * 100, 2)
            except Exception:
                pass
            # История из БД: 20 дневных OHLCV + дневной ATR
            try:
                _d = _http("GET", f"{api}/api/candles/{fg}?interval_name=day&limit=20",
                           timeout=20)
                _cs = _d.get("candles") or []
                _cl = [float(c.get("close") or 0) for c in _cs]
                row["d20"] = [round(v, 4) for v in _cl]
                row["dv20"] = [int(c.get("volume") or 0) for c in _cs]
                _ad = _calc_atr([float(c.get("high") or 0) for c in _cs],
                                [float(c.get("low") or 0) for c in _cs], _cl, 14)
                if _ad:
                    row["atr_d"] = round(_ad, 4)
                    row["atr_d_pct"] = round(_ad / (_cl[-1] or 1) * 100, 2)
            except Exception:
                row["d20"] = []
                row["dv20"] = []
            return row

        # Параллельно: полные данные только по фокусу, остальные — мгновенно.
        with ThreadPoolExecutor(max_workers=8) as ex:
            out["universe"] = list(ex.map(_enrich, rows))
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
    try:
        ctl = _http("GET", f"{api}/api/v1/bot/ai_control", timeout=10)
        _p = str((ctl.get("prompts") or {}).get("trader") or "")
        if _p.strip():
            out["prompt_override"] = _p
        _n = str(ctl.get("note") or "")
        if _n.strip():
            out["human_note"] = _n
    except Exception:
        pass
    out["now_msk"] = (datetime.now(timezone.utc)
                      .astimezone(timezone(timedelta(hours=3)))
                      .strftime("%Y-%m-%d %H:%M") + " МСК")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--opencode-url", default="http://192.168.1.3:4096")
    ap.add_argument("--model", default="")
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--heartbeat", type=float, default=900.0,
                    help="период вызова LLM при пустом портфеле и очереди, сек (экономия токенов)")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report-only", action="store_true",
                    help="только отчёт/предложения и закрытия, без открытия позиций")
    ap.add_argument("--provider", default="opencode", choices=("opencode", "deepseek"),
                    help="opencode (Zen/big-pickle) или deepseek (API)")
    ap.add_argument("--no-buy", action="store_true",
                    help="запретить покупки (BUY), шорты разрешены")
    args = ap.parse_args()

    def post_prompt() -> None:
        try:
            _http("POST", f"{args.api}/api/v1/bot/ai_prompt", {
                "provider": "opencode", "model": args.model, "shadow": False,
                "kind": "trader",
                "system": SYSTEM,
                "context_schema": {
                    "portfolio": "equity/cash/маржа, long_short",
                    "positions[]": "entry/last/pnl/sl/tp/dist_*_atr/regime + orderbook + m5/h1 свечи",
                    "orderbooks{}": "стакан кандидатов (imbalance, depth_rub)",
                    "movers": "1д/1н/1м/3м + streak",
                    "universe[]": "price/turnover/rng_pct + chg_1d/1w/1m/3m + orderbook + m5/h1 + d20/dv20 (20 дней из БД)",
                    "imoex": "dir/pct_20m/pct_60m/pct_day, breadth_up_pct",
                    "recent_trades": "последние сделки",
                },
            }, timeout=20)
        except Exception:
            pass

    print(f"[ai-trader] api={args.api} model={args.model} interval={args.interval}s", flush=True)
    post_prompt()
    _cycle = 0
    _last_llm_ts = 0.0
    while True:
        _cycle += 1
        if _cycle % 100 == 0:
            post_prompt()
        t0 = time.monotonic()
        try:
            _spec = (_ai_control(args.api).get("spec") or {})
            if _spec and not _spec.get("trader"):
                print("[ai-trader] режим AI не разрешает трейдера — пропуск цикла", flush=True)
                if args.once:
                    return
                time.sleep(max(15.0, float(args.interval)))
                continue
            # Экономия токенов: если позиций нет и очередь пуста — модель не зовём,
            # только лёгкий peek (state+portfolio) и ждём до heartbeat.
            _now_m = time.monotonic()
            if _last_llm_ts and (_now_m - _last_llm_ts) < float(args.heartbeat):
                _has_pos = _has_q = False
                try:
                    _peek = _http("GET", f"{args.api}/api/v1/bot/state", timeout=15)
                    _has_pos = bool(_peek.get("positions"))
                except Exception:
                    pass
                if not _has_pos:
                    try:
                        _pfq = _http("GET", f"{args.api}/api/v1/bot/portfolio_summary", timeout=15)
                        _has_q = bool(_pfq.get("queue"))
                    except Exception:
                        pass
                if not (_has_pos or _has_q):
                    print(f"[ai-trader] нет позиций/очереди — пропуск LLM "
                          f"(до heartbeat {max(0, int(float(args.heartbeat) - (_now_m - _last_llm_ts)))}с)",
                          flush=True)
                    if args.once:
                        return
                    time.sleep(max(15.0, float(args.interval)))
                    continue
            ctx = _context(args.api)
            n_pos = len(ctx.get("positions") or [])
            eq = (ctx.get("portfolio") or {}).get("equity")
            _contour = str(ctx.get("contour") or "")
            _contour_ok = (not _contour) or _contour in ("sandbox", "live")
            print(f"[ai-trader] контекст: позиций {n_pos}, equity {eq}, "
                  f"контур {_contour or '?'}, тикеров {len(ctx.get('universe') or [])}", flush=True)
            if not _contour_ok:
                print(f"[ai-trader] контур {_contour}: торговля недоступна — только анализ "
                      f"(переключи бота на sandbox/live)", flush=True)
            _ov = str(ctx.pop("prompt_override", "") or "")
            for _mk in ("— SYSTEM —", "- SYSTEM -"):
                _i = _ov.find(_mk)
                if _i >= 0:
                    _ov = _ov[_i + len(_mk):]
                    break
            for _cut in ("— Контекст заявки (JSON) —", "— Контекст заявки"):
                _j = _ov.find(_cut)
                if _j > 0:
                    _ov = _ov[:_j]
            _sys = ((_ov.strip() or SYSTEM)
                    + f"\n\n— АКТИВНЫЙ КОНТУР СЧЁТА: {_contour or 'sandbox'} —\n"
                      f"Позиции, сделки, портфель и заявки в контексте — ТОЛЬКО этого счёта. "
                      f"Если контур sandbox — это тестовые деньги; если live — реальные."
                    + _LEVEL_DISCIPLINE + """

""")
            if str(args.provider) == "deepseek":
                analysis, acts, sugg, _usage = _ask_deepseek(_sys, ctx, args.model or "deepseek-chat")
            else:
                analysis, acts, sugg, _usage = _ask(_sys, ctx, args.model, args.opencode_url)
            _prompt_est = (len(_sys) + len(json.dumps(ctx, ensure_ascii=False, default=str))) // 4
            _usage_out = {
                "prompt": int(_usage.get("prompt_tokens") or 0) or _prompt_est,
                "completion": int(_usage.get("completion_tokens") or 0),
                "estimated": not bool(_usage.get("prompt_tokens")),
            }
            _usage_out["total"] = _usage_out["prompt"] + _usage_out["completion"]
            _last_llm_ts = time.monotonic()
            if analysis:
                print(f"[ai-trader] РАЗБОР: {analysis}", flush=True)
                try:
                    _http("POST", f"{args.api}/api/v1/bot/ai_notes", {
                        "ticker": "ПОРТФЕЛЬ", "side": "", "action": "analysis",
                        "note": str(analysis)[:3000], "advice": "", "model": args.model,
                        "provider": "opencode",
                    }, timeout=20)
                except Exception:
                    pass
                try:
                    _http("POST", f"{args.api}/api/v1/bot/ai_report", {
                        "model": args.model, "analysis": str(analysis)[:8000],
                        "suggestions": sugg[:8], "actions": acts[:10],
                        "now_msk": ctx.get("now_msk"), "usage": _usage_out,
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
                    if not _contour_ok:
                        print(f"[ai-trader] {act} {tk} пропущен (контур {_contour})", flush=True)
                        continue
                    if args.report_only and act == "open":
                        print(f"[ai-trader] open {tk} пропущен (--report-only)", flush=True)
                        continue
                    if (args.no_buy and act == "open"
                            and str(a.get("side") or "SELL").upper() in ("BUY", "LONG")):
                        print(f"[ai-trader] BUY {tk} пропущен (--no-buy)", flush=True)
                        continue
                    if act in ("update_sl", "update_tp", "update_levels"):
                        r = _http("POST", f"{args.api}/api/v1/bot/ai_trade", {
                            "ticker": tk, "action": act, "sl": a.get("sl"), "tp": a.get("tp"),
                            "reason": a.get("reason", ""),
                        })
                        print(f"[ai-trader] {act} {tk}: {json.dumps(r, ensure_ascii=False)[:100]}", flush=True)
                        try:
                            _http("POST", f"{args.api}/api/v1/bot/ai_notes", {
                                "ticker": tk, "side": "", "action": "tighten",
                                "note": str(a.get("reason") or "")[:1500], "advice": "", "model": args.model,
                                "provider": "opencode",
                            }, timeout=20)
                        except Exception:
                            pass
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
                    _why = str(a.get("reason") or "")[:1500]
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
