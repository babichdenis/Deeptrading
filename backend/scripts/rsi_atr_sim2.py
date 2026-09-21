#!/usr/bin/env python3
"""rsi_atr_sim2.py — VW-RSI (объёмно-взвешенный RSI) + направленный объём.

Продолжение scripts/rsi_atr_sim.py. Факт (12-19.09, 26 бумаг, cost 0.045%/стор, ~6400₽/поз):
  чемпион «фикс 30/70 | RSI-выход» .... n=403 wr=64.5% net=+1187₽ | L 225/-887 S 178/+2074
  адаптивные пороги RSI от ATR ........ хуже: ATR[15..30] +401, ATR[20..40] -431
  SL/TP от ATR ....................... катастрофа: лучший -1330
  фильтр объёма vol>=1.2×MA20 ........ убивает входы: +1187 -> +83
  фильтр ATR% >= 0.9×MA96 ............ нейтрально: +1171

Осталось из запроса (п.4 «Volume-weighted RSI» и «Buy/Sell Volume Split»):
  1. VW-RSI (MFI-стиль): gain/loss взвешены объёмом бара — «экстремум с деньгами».
     Вход: кросс MACD 1м + VW-RSI < os / > ob (окно 15м); выход: противоположный
     экстремум. Пороги 30/70, 35/65, 40/60, 45/55 (VW-RSI экстремальнее обычного).
     Кросс-варианты: VW на входе + обычный RSI на выходе, и наоборот.
  2. Направленный объём (up-vol vs down-vol за 15 баров) как фильтр входа
     (прокси Buy/Sell Volume Split: тиковых данных нет, объём бара относим к
     растущим/падающим барам):
       confirm — BUY требует перевес покупательского объёма (за разворотом деньги),
                 SELL — перевес продавца;
       contra  — наоборот (капитуляция перед разворотом); ratio 1.0× / 1.2×.

Прочее как у чемпиона: сессия 9:50-19:00 МСК, оборот бара >=50к₽, кулдаун 15 баров,
закрытие в конце дня, комиссия 0.045%/сторона.

Запуск: cd ~/Dev/Deeptrading/backend && PYTHONPATH=. .venv/bin/python3 scripts/rsi_atr_sim2.py
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.database import SessionLocal

MSK = timezone(timedelta(hours=3))
COST = 0.00045
NOTIONAL = 6400.0
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
WIN = 15        # окно поиска экстремума на входе
COOLDOWN = 15
DVMIN = 15      # окно направленного объёма (баров)


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


def vw_rsi_wilder(closes, vols, period=14):
    """RSI Уайлдера, где gain/loss взвешены объёмом бара (≈ Money Flow Index)."""
    closes = np.asarray(closes, dtype=float)
    vols = np.asarray(vols, dtype=float)
    n = len(closes)
    out = np.full(n, 50.0)
    if n < period + 1:
        return out
    gv = np.zeros(n)
    lv = np.zeros(n)
    d = np.diff(closes)
    gv[1:] = np.maximum(d, 0.0) * vols[1:]
    lv[1:] = np.maximum(-d, 0.0) * vols[1:]
    ag = float(gv[1:period + 1].mean())
    al = float(lv[1:period + 1].mean())

    def _val(a, l):
        if l <= 0:
            return 100.0 if a > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + a / l)

    out[period] = _val(ag, al)
    for i in range(period + 1, n):
        ag = (ag * (period - 1) + float(gv[i])) / period
        al = (al * (period - 1) + float(lv[i])) / period
        out[i] = _val(ag, al)
    return out


def roll_updown(closes, vols, w=DVMIN):
    """Скользящие суммы объёма растущих/падающих баров (w баров, включая текущий)."""
    closes = np.asarray(closes, dtype=float)
    vols = np.asarray(vols, dtype=float)
    d = np.diff(closes, prepend=closes[0])
    upv = np.where(d > 0, vols, 0.0)
    dnv = np.where(d < 0, vols, 0.0)
    cup = np.concatenate(([0.0], np.cumsum(upv)))
    cdn = np.concatenate(([0.0], np.cumsum(dnv)))
    idx = np.arange(1, len(closes) + 1)
    w0 = np.maximum(0, idx - w)
    return cup[idx] - cup[w0], cdn[idx] - cdn[w0]


def sim(d, os_e, ob_e, r_e, ex_os, ex_ob, r_x, dirvol=None, dv_ratio=1.0):
    """Один прогон одной бумаги.

    Вход: кросс гист. MACD 1м + экстремум r_e в окне WIN (os_e/ob_e);
    выход: r_x в противоположном экстремуме (LONG: >ex_ob, SHORT: <ex_os);
    dirvol: None | 'confirm' | 'contra' — направленный объём за DVMIN баров.
    """
    ts, cl, vl, h1 = d["ts"], d["cl"], d["vl"], d["h1"]
    up15, dn15 = d["up15"], d["dn15"]
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
        if pos == 0:
            if not (DAY_START <= hm < DAY_END):
                continue
            if float(cl[i]) * float(vl[i]) < 50000.0:
                continue
            if i - last_exit <= COOLDOWN:
                continue
            up = h1[i - 1] <= 0 < h1[i]
            dn = h1[i - 1] >= 0 > h1[i]
            if not (up or dn):
                continue
            want = 1 if up else -1
            w0 = max(0, i - WIN)
            if want == 1:
                if not (float(r_e[w0:i + 1].min()) < os_e):
                    continue
            else:
                if not (float(r_e[w0:i + 1].max()) > ob_e):
                    continue
            if dirvol is not None:
                uv = float(up15[i])
                dv = float(dn15[i])
                if want == 1:
                    ok = (uv > dv_ratio * dv) if dirvol == "confirm" else (dv > dv_ratio * uv)
                else:
                    ok = (dv > dv_ratio * uv) if dirvol == "confirm" else (uv > dv_ratio * dv)
                if not ok:
                    continue
            sgn = want
            ep = float(cl[i])
            eq = max(1, round(NOTIONAL / ep))
            pos = 1
            entry_i = i
        else:
            ex = (float(r_x[i]) > ex_ob) if sgn == 1 else (float(r_x[i]) < ex_os)
            if ex:
                px = float(cl[i])
                trades.append((ts[entry_i], ts[i], sgn, "rsi",
                               sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
    return trades


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
        tk = t[5]
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


def run(per, v):
    trades = []
    for f, d in per.items():
        for tr in sim(d, v["os_e"], v["ob_e"], d[v["r_e"]],
                      v["ex_os"], v["ex_ob"], d[v["r_x"]],
                      v.get("dirvol"), v.get("dv_ratio", 1.0)):
            trades.append(tr + (d["ticker"],))
    return trades


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
            cl = np.array([float(x[1]) for x in rows])
            vl = np.array([float(x[2] or 0) for x in rows])
            up15, dn15 = roll_updown(cl, vl, DVMIN)
            per[f] = dict(
                ticker=str(rr[0]) if rr else f,
                ts=ts, cl=cl, vl=vl,
                h1=macd_hist([float(x) for x in cl]),
                r=rsi_wilder(cl, 14),
                vw=vw_rsi_wilder(cl, vl, 14),
                up15=up15, dn15=dn15,
            )
    if not per:
        print("нет данных")
        return

    tot_bars = sum(len(d["ts"]) for d in per.values())
    print("=== ДАННЫЕ ===")
    print("бумаг: %d | 1м баров: %d | окно: %s → %s МСК" % (
        len(per), tot_bars,
        min(d["ts"][0] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(d["ts"][-1] for d in per.values()).astimezone(MSK).strftime("%d.%m %H:%M")))
    r_all = np.concatenate([d["r"][60:] for d in per.values()])
    vw_all = np.concatenate([d["vw"][60:] for d in per.values()])
    print("RSI(14):  <30 на %5.2f%% баров | >70 на %5.2f%%" % (
        100.0 * float((r_all < 30).mean()), 100.0 * float((r_all > 70).mean())))
    print("VW-RSI:   <30 на %5.2f%% баров | >70 на %5.2f%%  (экстремальнее = сигналов больше)" % (
        100.0 * float((vw_all < 30).mean()), 100.0 * float((vw_all > 70).mean())))

    variants = [
        ("якорь: RSI вх 30/70 → RSI вых 30/70",
         dict(os_e=30.0, ob_e=70.0, r_e="r", ex_os=30.0, ex_ob=70.0, r_x="r")),
    ]
    for os_t, ob_t in ((30.0, 70.0), (35.0, 65.0), (40.0, 60.0), (45.0, 55.0)):
        variants.append(("VW вх %d/%d → VW вых %d/%d" % (os_t, ob_t, os_t, ob_t),
                         dict(os_e=os_t, ob_e=ob_t, r_e="vw", ex_os=os_t, ex_ob=ob_t, r_x="vw")))
    variants += [
        ("VW вх 30/70 → RSI вых 30/70", dict(os_e=30.0, ob_e=70.0, r_e="vw", ex_os=30.0, ex_ob=70.0, r_x="r")),
        ("VW вх 40/60 → RSI вых 30/70", dict(os_e=40.0, ob_e=60.0, r_e="vw", ex_os=30.0, ex_ob=70.0, r_x="r")),
        ("RSI вх 30/70 → VW вых 30/70", dict(os_e=30.0, ob_e=70.0, r_e="r", ex_os=30.0, ex_ob=70.0, r_x="vw")),
        ("RSI вх 30/70 → VW вых 40/60", dict(os_e=30.0, ob_e=70.0, r_e="r", ex_os=40.0, ex_ob=60.0, r_x="vw")),
    ]
    for mode in ("confirm", "contra"):
        for ratio in (1.0, 1.2):
            variants.append(("RSI 30/70 + dirvol %s %.1f×" % (mode, ratio),
                             dict(os_e=30.0, ob_e=70.0, r_e="r", ex_os=30.0, ex_ob=70.0, r_x="r",
                                  dirvol=mode, dv_ratio=ratio)))
    variants.append(("VW 40/60 + dirvol confirm 1.0×",
                     dict(os_e=40.0, ob_e=60.0, r_e="vw", ex_os=40.0, ex_ob=60.0, r_x="vw",
                          dirvol="confirm", dv_ratio=1.0)))

    print()
    print("=== СЕТКА (cost %.3f%%/стор, ~%.0f₽/поз, сессия 9:50–19:00, win=%dм) ===" % (
        100 * COST, NOTIONAL, WIN))
    results = []
    for name, v in variants:
        a = agg(run(per, v))
        results.append((name, v, a))
    for name, _v, a in sorted(results, key=lambda x: -x[2]["net"]):
        print("  %-36s n=%4d wr=%5.1f%% net=%+9.2f avg=%+7.2f | L %3d/%+8.1f S %3d/%+8.1f | макс.одн %2d" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
            a["net"] / max(a["n"], 1),
            a["by_side"]["L"][0], a["by_side"]["L"][1],
            a["by_side"]["S"][0], a["by_side"]["S"][1], a["mx"]))
    print("  справка: якорь должен совпасть с rsi_atr_sim «фикс 30/70 | RSI» = n=403 wr=64.5% net=+1186.90")

    print()
    print("=== ТОП-3 ПО NET: ДЕТАЛИ ===")
    for name, _v, a in sorted(results, key=lambda x: -x[2]["net"])[:3]:
        print("--- %s: n=%d wr=%.1f%% net=%+.2f ---" % (
            name, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"]))
        print("  по дням: " + "  ".join("%s:%+7.0f" % (dd, vv)
                                       for dd, vv in sorted(a["by_day"].items())))
        print("  причины выхода: %s" % {k: (vv[0], round(vv[1], 1)) for k, vv in
                                        sorted(a["by_reason"].items(), key=lambda kv: -kv[1][0])})
        if a["hold"]:
            print("  удержание, мин: med=%.0f p90=%.0f max=%.0f" % (
                a["hold"][len(a["hold"]) // 2],
                a["hold"][min(len(a["hold"]) - 1, int(len(a["hold"]) * 0.9))],
                a["hold"][-1]))
        print("  худшие: %s" % [(t, vv[0], round(vv[1], 1)) for t, vv in
                                sorted(a["by_tk"].items(), key=lambda kv: kv[1][1])[:5]])
        print("  лучшие: %s" % [(t, vv[0], round(vv[1], 1)) for t, vv in
                                sorted(a["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]])


if __name__ == "__main__":
    asyncio.run(main())
