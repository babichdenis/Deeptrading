#!/usr/bin/env python3
"""q2_tf_sim.py — голоса ансамбля на 5-минутных барах вместо 10-минутных.

Запрос: «может быть попробовать голоса ансамбля 5-минутные — 10 минут
как-то сильно редко: 6 раз в час, 48 раз за день, и то не факт».

Текущий лидер (голоса 10м, окно накопления 15м):
  вход = чистый кворум-2 (BUY>=2 и SELL==0, либо SELL>=2 и BUY==0) +
  выход RSI(14) 1м 70/30, мгновенно:
  n=457 wr=71.8% net=+4046₽ (12–19.09, 26 бумаг, cost 0.045%/стор,
  ~6400₽/позиция, сессия 9:50–19:00 МСК, кулдаун 15 баров).

Что проверяем (офлайн; бот не трогаем — там доигрывает Q2rsi):
  1. ЧАСТОТА: сколько РЕАЛЬНО событий голосов и «кворум-эпизодов» дают
     10м-бары против 5м — «48/день» это лишь потолок, голоса бывают
     не на каждом баре.
  2. ТРИ НАБОРА ГОЛОСОВ (сетапы — из q2-конфига лидера):
       10м (эталон)        — как у лидера;
       5м те же параметры  — голоса чаще, но горизонты индикаторов вдвое
                             короче (donchian 45×5м = 3.75ч вместо 7.5ч);
       5м периоды/2        — те же ВРЕМЕННЫЕ горизонты (rsi 16→8,
                             boll 15→8, donchian 45→22, vol-MA 20→10;
                             MACD 12/26/9 — канонический, не трогаем),
                             просто бар мельче.
  3. ОКНО НАКОПЛЕНИЯ голосов: 10/15/20/30 мин — 5м-голос «живёт» меньше,
     для кворума окно может понадобиться шире.
  4. Вход q2c (чистый) | q2 (допускает встречные); выход RSI 30/70 |
     confirm-3 MACD (бывший лидер выходов).

Прочее неизменно: сессия 9:50–19:00 МСК, оборот бара ≥50к₽, кулдаун 15
баров, закрытие в конце дня, комиссия 0.045%/сторона.

Запуск: cd ~/Dev/Deeptrading/backend && PYTHONPATH=. .venv/bin/python3 scripts/q2_tf_sim.py
"""
from __future__ import annotations

import asyncio
import json
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.database import SessionLocal
from app.engine.models import Candle
from app.services.ensemble import generate_signals, resample

MSK = timezone(timedelta(hours=3))
TF_SECONDS = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "hour": 3600}
COST = 0.00045
NOTIONAL = 6400.0
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
COOLDOWN = 15
LOOKBACKS = (10, 15, 20, 30)
BASE_NAME = "10м (эталон) | lb=15м | q2c + rsi30/70"


def ema(vals, p):
    k = 2.0 / (p + 1)
    out = []
    e = vals[0] if vals else 0.0
    for v in vals:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def macd_hist(closes):
    f = ema(closes, 12)
    s = ema(closes, 26)
    m = [a - b for a, b in zip(f, s)]
    g = ema(m, 9)
    return [a - b for a, b in zip(m, g)]


def rsi_wilder(closes, period=14):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    out = np.full(n, 50.0)
    if n < period + 1:
        return out
    diffs = np.diff(closes)
    gains = np.maximum(diffs, 0.0)
    losses = np.maximum(-diffs, 0.0)
    ag = float(gains[:period].mean())
    al = float(losses[:period].mean())

    def _val(a, l):
        if l <= 0:
            return 100.0 if a > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + a / l)

    out[period] = _val(ag, al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + float(gains[i - 1])) / period
        al = (al * (period - 1) + float(losses[i - 1])) / period
        out[i] = _val(ag, al)
    return out


def states_for(ts, ev_list, sids, lookback_sec):
    """Состояние голосов на каждом 1м баре: +1 BUY / -1 SELL / 0 — за окно lookback."""
    ev_t = [e[0] for e in ev_list]
    out = []
    j = 0
    lb = timedelta(seconds=lookback_sec)
    for t in ts:
        while j < len(ev_list) and ev_list[j][0] <= t:
            j += 1
        lo_i = bisect_left(ev_t, t - lb)
        buy = set()
        sell = set()
        for k in range(lo_i, j):
            buy |= ev_list[k][1]
            sell |= ev_list[k][2]
        out.append(tuple(1 if s in buy else (-1 if s in sell else 0) for s in sids))
    return out


def sim(d, entry_rule, exit_mode, cooldown=COOLDOWN):
    """Сделки одной бумаги. Возвращает (вх, вых, sgn, причина, pnl, RSI@вх)."""
    ts, cl, vl, h1, states = d["ts"], d["cl"], d["vl"], d["h1"], d["states"]
    r, day, hm_arr = d["r"], d["day"], d["hm"]
    n = len(ts)
    out = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    r_ent = 50.0
    last_exit = -10 ** 9
    for i in range(1, n):
        if day[i] != day[i - 1] and pos != 0:
            px = float(cl[i - 1])
            out.append((ts[entry_i], ts[i - 1], sgn, "day_end",
                        sgn * (px - ep) * eq - COST * (ep + px) * eq, r_ent))
            pos = 0
            last_exit = i - 1
        if i < 60:
            continue
        if pos == 0:
            if i - last_exit <= cooldown:
                continue
            if float(cl[i]) * float(vl[i]) < 50000.0:
                continue
            hm = int(hm_arr[i])
            if not (DAY_START <= hm < DAY_END):
                continue
            want = 0
            st = states[i]
            b = sum(1 for x in st if x == 1)
            s2 = sum(1 for x in st if x == -1)
            if entry_rule == "q2c":
                if b >= 2 and s2 == 0:
                    want = 1
                elif s2 >= 2 and b == 0:
                    want = -1
            else:  # q2: кворум при перевесе (встречные голоса допускаются)
                if b >= 2 and b > s2:
                    want = 1
                elif s2 >= 2 and s2 > b:
                    want = -1
            if want:
                sgn = want
                ep = float(cl[i])
                eq = max(1, round(NOTIONAL / ep))
                pos = 1
                entry_i = i
                r_ent = float(r[i])
        else:
            px = None
            why = ""
            if exit_mode[0] == "c3":
                held = i - entry_i
                if (held >= 3 and h1[i] * sgn < 0
                        and h1[i - 1] * sgn < 0 and h1[i - 2] * sgn < 0):
                    px = float(cl[i])
                    why = "c3"
            elif exit_mode[0] == "rsi":
                os_t, ob_t = exit_mode[1], exit_mode[2]
                if sgn == 1 and float(r[i]) > ob_t:
                    px = float(cl[i])
                    why = "rsi"
                elif sgn == -1 and float(r[i]) < os_t:
                    px = float(cl[i])
                    why = "rsi"
            if px is not None:
                out.append((ts[entry_i], ts[i], sgn, why,
                            sgn * (px - ep) * eq - COST * (ep + px) * eq, r_ent))
                pos = 0
                last_exit = i
    return out


def agg(trades):
    n = len(trades)
    wins = sum(1 for t in trades if t[4] > 0)
    net = sum(t[4] for t in trades)
    by_day = defaultdict(float)
    by_side = defaultdict(lambda: [0, 0.0])
    by_reason = defaultdict(lambda: [0, 0.0])
    by_tk = defaultdict(lambda: [0, 0.0])
    for t in trades:
        e, _x, sgn, why, pnl = t[0], t[1], t[2], t[3], t[4]
        tk = t[6]
        by_day[e.astimezone(MSK).strftime("%d.%m")] += pnl
        k = "L" if sgn == 1 else "S"
        by_side[k][0] += 1
        by_side[k][1] += pnl
        by_reason[why][0] += 1
        by_reason[why][1] += pnl
        by_tk[tk][0] += 1
        by_tk[tk][1] += pnl
    pts = []
    for t in trades:
        pts.append((t[0], 1))
        pts.append((t[1], -1))
    pts.sort()
    cur = mx = 0
    for _t, dd in pts:
        cur += dd
        if cur > mx:
            mx = cur
    hold = sorted((t[1] - t[0]).total_seconds() / 60.0 for t in trades)
    return dict(n=n, wins=wins, net=net, by_day=by_day, by_side=by_side,
                by_reason=by_reason, by_tk=by_tk, mx=mx, hold=hold)


async def main():
    _cfg_file = "data/ensemble_config.test.q2.json"
    try:
        cfg = json.load(open(_cfg_file, encoding="utf-8"))
    except Exception:
        _cfg_file = "data/ensemble_config.test.json"
        cfg = json.load(open(_cfg_file, encoding="utf-8"))
    setups10 = [(s["strategy_id"], s.get("tf", "10min"), s.get("params", {}))
                for s in cfg.get("setups", []) if s.get("enabled", True)]
    setups5_same = [(sid, "5min", dict(params)) for sid, _tf, params in setups10]
    setups5_half = []
    for sid, _tf, params in setups10:
        p2 = dict(params)
        if sid == "rsi_reversal" and "period" in p2:
            p2["period"] = max(2, int(round(p2["period"] / 2)))
        elif sid == "bollinger_reclaim" and "period" in p2:
            p2["period"] = max(2, int(round(p2["period"] / 2)))
        elif sid == "donchian_breakout" and "period" in p2:
            p2["period"] = max(2, int(round(p2["period"] / 2)))
        elif sid == "volume_drop" and "ma_len" in p2:
            p2["ma_len"] = max(2, int(round(p2["ma_len"] / 2)))
        setups5_half.append((sid, "5min", p2))
    sids = [s[0] for s in setups10]
    setdefs = [("10м (эталон)", setups10),
               ("5м те же параметры", setups5_same),
               ("5м периоды/2", setups5_half)]

    print("=== КОНФИГ ===")
    print("сетапы из %s | quorum=2 | сессия 9:50–19:00 МСК | кулдаун %d баров" % (
        _cfg_file, COOLDOWN))
    for setname, setups in setdefs:
        print("  %-20s: %s" % (setname,
              ", ".join("%s:%s:%s" % (s, tf, p) for s, tf, p in setups)))

    t_from = datetime(2026, 9, 12, tzinfo=timezone.utc)
    t_to = datetime(2026, 9, 20, tzinfo=timezone.utc)
    per = {}
    async with SessionLocal() as db:
        figis = [r[0] for r in (await db.execute(text(
            "SELECT DISTINCT figi FROM sandbox_trades WHERE mode='paper'"))).all()]
        for f in figis:
            rr = (await db.execute(
                text("SELECT ticker FROM instruments WHERE figi=:f"), {"f": f})).first()
            rows = (await db.execute(text(
                "SELECT ts, open, high, low, close, volume FROM candles "
                "WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": f, "a": t_from, "b": t_to})).all()
            if len(rows) < 300:
                continue
            candles = [Candle(ts=x[0], open=float(x[1]), high=float(x[2]), low=float(x[3]),
                              close=float(x[4]), volume=int(x[5] or 0)) for x in rows]
            ts = [c.ts for c in candles]
            cl = np.array([c.close for c in candles])
            vl = np.array([float(c.volume or 0) for c in candles])
            ev = {}
            for setname, setups in setdefs:
                e = defaultdict(lambda: {"BUY": set(), "SELL": set()})
                for sid, tf_name, params in setups:
                    bars = resample(candles, TF_SECONDS[tf_name])
                    try:
                        sigs = generate_signals(sid, params, bars)
                    except Exception:
                        sigs = []
                    for sg in sigs:
                        sds = str(sg.get("side", "")).upper()
                        side = "BUY" if "BUY" in sds else ("SELL" if "SELL" in sds else None)
                        if side:
                            e[sg["ts"]][side].add(sid)
                ev[setname] = [(t, e[t]["BUY"], e[t]["SELL"]) for t in sorted(e)]
            day_ids = []
            hm_arr = []
            for t in ts:
                tmsk = t.astimezone(MSK)
                day_ids.append(tmsk.toordinal())
                hm_arr.append(tmsk.hour * 60 + tmsk.minute)
            per[f] = dict(
                ticker=str(rr[0]) if rr else f,
                ts=ts, cl=cl, vl=vl,
                h1=macd_hist([float(x) for x in cl]),
                r=rsi_wilder([float(x) for x in cl], 14),
                ev=ev, day=day_ids, hm=hm_arr,
            )
    if not per:
        print("нет данных")
        return

    nf = len(per)
    n_days = len({dv for d in per.values() for dv in set(d["day"])})
    print()
    print("=== ДАННЫЕ ===")
    print("бумаг: %d | 1м баров: %d | торговых дней: %d | окно %s → %s МСК" % (
        nf, sum(len(d["ts"]) for d in per.values()), n_days,
        min(d["ts"][0] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(d["ts"][-1] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M")))

    states_cache = {}

    def get_states(f, setname, lb):
        key = (f, setname, lb)
        if key not in states_cache:
            states_cache[key] = states_for(per[f]["ts"], per[f]["ev"][setname], sids, lb * 60)
        return states_cache[key]

    # --- 1. частота голосов (запрос: «10м — 6/час, 48/день, и то не факт») ---
    print()
    print("=== ЧАСТОТА ГОЛОСОВ И КВОРУМОВ (окно накопления 15м) ===")
    for setname, _s in setdefs:
        n_ev = sum(len(d["ev"][setname]) for d in per.values())
        n_ep = 0
        n_active = 0
        n_daybars = 0
        for f, d in per.items():
            st = get_states(f, setname, 15)
            prev = False
            for i, s in enumerate(st):
                b = sum(1 for x in s if x == 1)
                s2 = sum(1 for x in s if x == -1)
                on = (b >= 2 and s2 == 0) or (s2 >= 2 and b == 0)
                if on and not prev:
                    n_ep += 1
                prev = on
                hm = int(d["hm"][i])
                if DAY_START <= hm < DAY_END:
                    n_daybars += 1
                    if on:
                        n_active += 1
        print("  %-20s: событий голосов %5d (~%.1f/день/бумага) | кворум-эпизодов %5d "
              "(~%.1f/день/бумага) | q2c активен на %.2f%% дневных 1м-баров" % (
                  setname, n_ev, n_ev / nf / n_days, n_ep, n_ep / nf / n_days,
                  100.0 * n_active / max(n_daybars, 1)))

    # --- сетка вариантов ---
    variants = []
    for setname, _s in setdefs:
        for lb in LOOKBACKS:
            for er in ("q2c", "q2"):
                for exname, ex in (("rsi30/70", ("rsi", 30.0, 70.0)), ("c3", ("c3",))):
                    variants.append(("%s | lb=%dм | %s + %s" % (setname, lb, er, exname),
                                     setname, lb, er, ex))
    results = []
    for name, setname, lb, er, ex in variants:
        trades = []
        for f, d in per.items():
            d2 = dict(d)
            d2["states"] = get_states(f, setname, lb)
            for tr in sim(d2, er, ex):
                trades.append(tr + (d["ticker"],))
        results.append((name, agg(trades)))

    print()
    print("=== СЕТКА (cost %.3f%%/стор, ~%.0f₽/поз) ===" % (100 * COST, NOTIONAL))
    for name, a in sorted(results, key=lambda x: -x[1]["net"]):
        print("  %-46s n=%4d wr=%5.1f%% net=%+9.2f avg=%+7.2f | L %3d/%+8.1f S %3d/%+8.1f | макс.одн %2d" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
            a["net"] / max(a["n"], 1),
            a["by_side"]["L"][0], a["by_side"]["L"][1],
            a["by_side"]["S"][0], a["by_side"]["S"][1], a["mx"]))
    ref = next((a for nm, a in results if nm == BASE_NAME), None)
    if ref:
        print("  справка: эталон должен совпасть с q2_rsi_sim «q2c + rsi30/70 (вых)»: n=457 wr=71.8% net=+4046.31")
    else:
        print("  (!) эталонная строка «%s» не найдена" % BASE_NAME)

    print()
    print("=== ТОП-3 ПО NET: ДЕТАЛИ ===")
    for name, a in sorted(results, key=lambda x: -x[1]["net"])[:3]:
        print("--- %s: n=%d wr=%.1f%% net=%+.2f ---" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"]))
        print("  по дням: " + "  ".join("%s:%+7.0f" % (dd, vv)
                                       for dd, vv in sorted(a["by_day"].items())))
        if ref and name != BASE_NAME:
            days_all = sorted(set(a["by_day"]) | set(ref["by_day"]))
            print("  дельта к эталону по дням: " + "  ".join(
                "%s:%+6.0f" % (dd, a["by_day"].get(dd, 0.0) - ref["by_day"].get(dd, 0.0))
                for dd in days_all))
        print("  причины выхода: %s" % {k: (v[0], round(v[1], 1)) for k, v in
                                        sorted(a["by_reason"].items(), key=lambda kv: -kv[1][0])})
        if a["hold"]:
            print("  удержание, мин: med=%.0f p90=%.0f max=%.0f" % (
                a["hold"][len(a["hold"]) // 2],
                a["hold"][min(len(a["hold"]) - 1, int(len(a["hold"]) * 0.9))],
                a["hold"][-1]))
        print("  худшие: %s" % [(t, v[0], round(v[1], 1)) for t, v in
                                sorted(a["by_tk"].items(), key=lambda kv: kv[1][1])[:5]])
        print("  лучшие: %s" % [(t, v[0], round(v[1], 1)) for t, v in
                                sorted(a["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]])

    print()
    print("=== РЕЗЮМЕ: 5м против 10м (вход q2c + выход rsi30/70, лучшее окно) ===")
    if ref:
        for setname in ("5м те же параметры", "5м периоды/2"):
            best = None
            for nm, a in results:
                if nm.startswith(setname) and "| q2c + rsi30/70" in nm:
                    if best is None or a["net"] > best[1]["net"]:
                        best = (nm, a)
            if best:
                print("  %-46s n=%4d wr=%5.1f%% net=%+9.2f  (эталон 10м: n=%d wr=%.1f%% net=%+.2f)" % (
                    best[0], best[1]["n"],
                    100.0 * best[1]["wins"] / max(best[1]["n"], 1), best[1]["net"],
                    ref["n"], 100.0 * ref["wins"] / max(ref["n"], 1), ref["net"]))
            else:
                print("  %s: вариантов не найдено" % setname)

    print()
    print("маппинг в бота (если 5м выиграет): вариант-файл")
    print("data/ensemble_config.test.q2rsi5.json — сетапы с \"tf\": \"5min\" (для")
    print("«периоды/2» — и с уменьшенными period/ma_len) + та же секция \"bot\";")
    print("TEST_VARIANTS += \"q2rsi5\": {\"confirm_flip\": 0}; .env: BOT_TEST_NAME=Q2rsi5,")
    print("TEST_VARIANT/BOT_TEST_VARIANT=q2rsi5. Запускать после завершения")
    print("текущего Q2rsi (или прервать — решать вам).")


if __name__ == "__main__":
    asyncio.run(main())
