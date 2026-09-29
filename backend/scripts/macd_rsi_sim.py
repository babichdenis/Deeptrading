#!/usr/bin/env python3
"""macd_rsi_sim.py — офлайн-сим правила «MACD + RSI» (до запуска в боте).

Запрос: «входить в лонг по macd когда rsi ниже 30, и выходить по macd и rsi
выше 70, и наоборот с шортами».

Вход LONG : гистограмма MACD(12/26/9) 1м пересекает 0 снизу вверх
            И RSI(14) 1м < oversold — на самом баре (win=0) или в последние
            win баров (кросс MACD случается чуть позже экстремума RSI).
Вход SHORT: пересечение сверху вниз И RSI > overbought.
Выход     : flip — гистограмма MACD против позиции;
            rsi  — RSI в противоположном экстремуме (LONG: >ob, SHORT: <os);
            or   — первое из двух («по macd ИЛИ rsi»);
            and  — оба на одном баре («полный цикл» os→ob, иначе закрытие
                   в конце дня).
Прочее как в прошлых симах: сессия 9:50–19:00 МСК, оборот бара >=50к₽,
кулдаун 15 баров, комиссия 0.045%/стор, ~6400₽/позиция, закрытие в конце дня.

Запуск: cd ~/Dev/Deeptrading/backend && PYTHONPATH=. .venv/bin/python3 scripts/macd_rsi_sim.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import numpy as np
from sqlalchemy import text

from app.database import SessionLocal

MSK = timezone(timedelta(hours=3))
COST = 0.00045
NOTIONAL = 6400.0
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60


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


def sim(ts, cl, vl, h1, r, os_thr=30.0, ob_thr=70.0, win=0, exit_mode="flip",
        cooldown=15, long_only=False, use_rsi_entry=True):
    """Один прогон одной бумаги. Возвращает сделки (вход, выход, сторона,
    причина, pnl)."""
    n = len(ts)
    trades = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    prev_date = ts[0].astimezone(MSK).date()
    for i in range(1, n):
        tm = ts[i].astimezone(MSK)
        if tm.date() != prev_date and pos != 0:
            px = float(cl[i - 1])
            trades.append((ts[entry_i], ts[i - 1], sgn, "day_end",
                           sgn * (px - ep) * eq - COST * (ep + px) * eq))
            pos = 0
            last_exit = i - 1
        prev_date = tm.date()
        if i < 60:
            continue
        hm = tm.hour * 60 + tm.minute
        in_day = DAY_START <= hm < DAY_END
        turn_ok = float(cl[i]) * float(vl[i]) >= 50000.0
        up = h1[i - 1] <= 0 < h1[i]
        dn = h1[i - 1] >= 0 > h1[i]
        if pos == 0:
            if in_day and turn_ok and i - last_exit > cooldown and (up or dn):
                w0 = max(0, i - win)
                if use_rsi_entry:
                    lo_ok = float(r[w0:i + 1].min()) < os_thr
                    hi_ok = float(r[w0:i + 1].max()) > ob_thr
                else:
                    lo_ok = True
                    hi_ok = True
                if up and lo_ok:
                    sgn = 1
                elif (not long_only) and dn and hi_ok:
                    sgn = -1
                else:
                    sgn = 0
                if sgn:
                    ep = float(cl[i])
                    eq = max(1, round(NOTIONAL / ep))
                    pos = 1
                    entry_i = i
        else:
            flip = h1[i] * sgn < 0
            rsi_ex = (r[i] > ob_thr) if sgn == 1 else (r[i] < os_thr)
            if exit_mode == "flip":
                ex = flip
            elif exit_mode == "rsi":
                ex = bool(rsi_ex)
            elif exit_mode == "or":
                ex = flip or bool(rsi_ex)
            elif exit_mode == "and":
                ex = flip and bool(rsi_ex)
            else:
                ex = False
            if ex:
                px = float(cl[i])
                trades.append((ts[entry_i], ts[i], sgn, exit_mode,
                               sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
    return trades


def agg(trades):
    n = len(trades)
    wins = sum(1 for t in trades if t[4] > 0)
    net = sum(t[4] for t in trades)
    by_day = defaultdict(float)
    for t in trades:
        by_day[t[0].astimezone(MSK).strftime("%d.%m")] += t[4]
    by_side = defaultdict(lambda: [0, 0.0])
    for t in trades:
        k = "L" if t[2] == 1 else "S"
        by_side[k][0] += 1
        by_side[k][1] += t[4]
    by_reason = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_reason[t[3]][0] += 1
        by_reason[t[3]][1] += t[4]
    by_tk = defaultdict(lambda: [0, 0.0])
    for t in trades:
        by_tk[t[5]][0] += 1
        by_tk[t[5]][1] += t[4]
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
    t_from = datetime(2026, 9, 12, tzinfo=timezone.utc)
    t_to = datetime(2026, 9, 20, tzinfo=timezone.utc)
    per = {}
    async with SessionLocal() as db:
        figis = [x[0] for x in (await db.execute(text(
            "SELECT DISTINCT figi FROM sandbox_trades WHERE mode='paper'"))).all()]
        for f in figis:
            rr = (await db.execute(
                text("SELECT ticker FROM instruments WHERE figi=:f"), {"f": f})).first()
            rows = (await db.execute(text(
                "SELECT ts, close, volume FROM candles "
                "WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": f, "a": t_from, "b": t_to})).all()
            if len(rows) < 300:
                continue
            ts = [x[0] for x in rows]
            cl = [float(x[1]) for x in rows]
            r = rsi_wilder(cl, 14)
            per[f] = dict(ticker=str(rr[0]) if rr else f, ts=ts, cl=cl,
                          vl=[float(x[2] or 0) for x in rows],
                          h1=macd_hist(cl), r=r, nbars=len(ts),
                          n_os=int((r < 30).sum()), n_ob=int((r > 70).sum()))
    if not per:
        print("нет данных")
        return
    tot_bars = sum(d["nbars"] for d in per.values())
    tot_os = sum(d["n_os"] for d in per.values())
    tot_ob = sum(d["n_ob"] for d in per.values())
    print("=== ДАННЫЕ ===")
    print("бумаг: %d | 1м баров: %d | RSI(14)<30: %.2f%% баров | RSI(14)>70: %.2f%% баров" % (
        len(per), tot_bars, 100.0 * tot_os / tot_bars, 100.0 * tot_ob / tot_bars))

    variants = [
        ("база MACD-кросс без RSI (вых flip)",
         dict(os_thr=0.0, ob_thr=100.0, win=0, exit_mode="flip", use_rsi_entry=False)),
    ]
    for os_thr, ob_thr in ((30.0, 70.0), (35.0, 65.0), (25.0, 75.0)):
        for win in (0, 5, 15):
            for ex in ("flip", "rsi", "or", "and"):
                variants.append(("os=%d ob=%d win=%-2d вых=%s" % (os_thr, ob_thr, win, ex),
                                 dict(os_thr=os_thr, ob_thr=ob_thr, win=win, exit_mode=ex)))

    print()
    print("=== ВАРИАНТЫ (cost %.3f%%/стор, ~%.0f₽/поз, сессия 9:50–19:00) ===" % (100 * COST, NOTIONAL))
    results = []
    for name, kw in variants:
        trades = []
        for f, d in per.items():
            for tr in sim(d["ts"], d["cl"], d["vl"], d["h1"], d["r"], **kw):
                trades.append(tr + (d["ticker"],))
        a = agg(trades)
        results.append((name, kw, a))
        print("%-38s n=%4d wr=%5.1f%% net=%+9.2f avg=%+7.2f | L %3d/%+8.1f S %3d/%+8.1f | макс.одн %2d" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
            a["net"] / max(a["n"], 1),
            a["by_side"]["L"][0], a["by_side"]["L"][1],
            a["by_side"]["S"][0], a["by_side"]["S"][1], a["mx"]))

    results.sort(key=lambda x: -x[2]["net"])
    print()
    print("=== ТОП-3 ПО NET: ДЕТАЛИ ===")
    for name, kw, a in results[:3]:
        print("--- %s: n=%d wr=%.1f%% net=%+.2f ---" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"]))
        print("  по дням:", "  ".join("%s:%+7.0f" % (d, v) for d, v in sorted(a["by_day"].items())))
        print("  причины выхода:", {k: (v[0], round(v[1], 1))
                                    for k, v in sorted(a["by_reason"].items(), key=lambda kv: -kv[1][0])})
        if a["hold"]:
            print("  удержание, мин: med=%.0f p90=%.0f max=%.0f" % (
                a["hold"][len(a["hold"]) // 2],
                a["hold"][int(len(a["hold"]) * 0.9)], a["hold"][-1]))
        print("  худшие:", [(t, v[0], round(v[1], 1)) for t, v in
                            sorted(a["by_tk"].items(), key=lambda kv: kv[1][1])[:5]])
        print("  лучшие:", [(t, v[0], round(v[1], 1)) for t, v in
                             sorted(a["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]])
        trades_lo = []
        for f, d in per.items():
            for tr in sim(d["ts"], d["cl"], d["vl"], d["h1"], d["r"],
                          long_only=True, **kw):
                trades_lo.append(tr + (d["ticker"],))
        alo = agg(trades_lo)
        print("  LONG-only: n=%d wr=%.1f%% net=%+.2f" % (
            alo["n"], 100.0 * alo["wins"] / max(alo["n"], 1), alo["net"]))


if __name__ == "__main__":
    asyncio.run(main())
