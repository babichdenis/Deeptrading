#!/usr/bin/env python3
"""q2_sim.py — подбор ВЫХОДА для входов quorum-2 (продолжение scripts/label_votes.py).

Факт из label_votes.py (12–19.09, 26 бумаг, cost 0.045%/стор, ~6400₽/позиция):
  вход quorum-2 ансамбля + выход confirm-3 ....... n=603 wr=45.3% net=+2219
  вход по кроссу 1m MACD (все MACD-тесты) ........ n=1627 wr=25.8% net=-10546
Вывод: у MACD-вариантов плохим был ВХОД, а не выход. Ансамбль (quorum-2) —
единственное устойчивое правило входа; осталось подобрать ему выход.

Здесь на тех же входах quorum-2 перебираем выходы и фильтры входа:
  inst    — мгновенный выход при флипе гист. MACD 1m против позиции;
  c3      — confirm-3 (эталон label_votes: 3 бара подряд против, held>=3);
  hyb     — в минусе мгновенно, в плюсе 2 бара подтверждения;
  *beNN   — плюс BE-стоп: пик>=NN₽ -> закрыть при возврате к нулю;
  q2_5m   — фильтр входа: 5m MACD согласен с направлением входа;
  q2c     — quorum-2 без встречных голосов (BUY>=2 И SELL==0);
  donch   — подпись «голосует только donchian_breakout» (топ label_votes).

Смотрим: n/wr/net/avg, устойчивость по дням, макс. одновременных позиций
(портфельная нагрузка), лучшие/худшие бумаги, комиссия.
Победитель -> конфиг data/ensemble_config.test.q2*.json и TEST_VARIANT в бот.

Запуск (на .4):
  cd ~/Dev/Deeptrading/backend && PYTHONPATH=. .venv/bin/python3 scripts/q2_sim.py
"""
from __future__ import annotations

import asyncio
import json
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.database import SessionLocal
from app.engine.models import Candle
from app.services.ensemble import generate_signals, resample

MSK = timezone(timedelta(hours=3))
TF = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "hour": 3600}
COST = 0.00045          # комиссия+слиппедж на сторону
NOTIONAL = 6400.0       # размер позиции (как в тестах бота)
LOOKBACK_MIN = 15       # окно голосов ансамбля, мин
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


def sim(d, entry_rule, exit_mode, be_thr=0.0, cooldown=15):
    """Мини-сим одной бумаги: вход quorum-2 (или подпись), день 9:50–19:00 МСК,
    оборот бара >=50к, кулдаун 15 баров; выход exit_mode (+BE-стоп) или конец дня.
    Возвращает список (ts входа, ts выхода, pnl, комиссия)."""
    ts, cl, vl, h1, states = d["ts"], d["cl"], d["vl"], d["h1"], d["states"]
    lab5, h5 = d.get("lab5"), d.get("h5")
    donch_sig = d.get("donch_sig")
    n = len(ts)
    out = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    peak = 0.0
    prev_date = ts[0].astimezone(MSK).date()
    for i in range(1, n):
        tm = ts[i].astimezone(MSK)
        if tm.date() != prev_date and pos != 0:
            px = float(cl[i - 1])
            out.append((ts[entry_i], ts[i - 1],
                        sgn * (px - ep) * eq - COST * (ep + px) * eq,
                        COST * (ep + px) * eq))
            pos = 0
            last_exit = i - 1
            peak = 0.0
        prev_date = tm.date()
        if i < 60:
            continue
        if pos == 0:
            if i - last_exit <= cooldown:
                continue
            if float(cl[i]) * float(vl[i]) < 50000.0:
                continue
            hm = tm.hour * 60 + tm.minute
            if not (DAY_START <= hm < DAY_END):
                continue
            want = 0
            if entry_rule in ("q2", "q2_5m", "q2c"):
                st = states[i]
                b = sum(1 for x in st if x == 1)
                s2 = sum(1 for x in st if x == -1)
                if entry_rule == "q2c":
                    # чистый кворум: голосов «за» >=2 и НИ ОДНОГО «против»
                    if b >= 2 and s2 == 0:
                        want = 1
                    elif s2 >= 2 and b == 0:
                        want = -1
                else:
                    if b >= 2 and b > s2:
                        want = 1
                    elif s2 >= 2 and s2 > b:
                        want = -1
            elif entry_rule == "donch" and donch_sig is not None:
                # подпись-победитель label_votes: голосует только donchian_breakout
                if states[i] == donch_sig:
                    want = 1
            if want and entry_rule == "q2_5m" and lab5:
                # фильтр: 5m MACD согласен с направлением входа
                j5 = bisect_right(lab5, ts[i]) - 1
                hv = h5[j5] if j5 >= 0 else 0.0
                if (want == 1 and hv <= 0) or (want == -1 and hv >= 0):
                    want = 0
            if want:
                sgn = want
                ep = float(cl[i])
                eq = max(1, round(NOTIONAL / ep))
                pos = 1
                entry_i = i
                peak = 0.0
        else:
            px_i = float(cl[i])
            cur = sgn * (px_i - ep) * eq - COST * (ep + px_i) * eq
            if cur > peak:
                peak = cur
            against = h1[i] * sgn < 0
            held = i - entry_i
            exit_now = False
            if exit_mode == "inst":
                exit_now = against
            elif exit_mode == "c3":
                exit_now = (held >= 3 and against
                            and h1[i - 1] * sgn < 0 and h1[i - 2] * sgn < 0)
            elif exit_mode == "hyb":
                # в минусе — сразу; в плюсе — 2 бара подтверждения
                exit_now = against and (cur < 0 or h1[i - 1] * sgn < 0)
            if (not exit_now) and be_thr and peak >= be_thr and cur <= 0.0:
                exit_now = True
            if exit_now:
                out.append((ts[entry_i], ts[i],
                            sgn * (px_i - ep) * eq - COST * (ep + px_i) * eq,
                            COST * (ep + px_i) * eq))
                pos = 0
                last_exit = i
                peak = 0.0
    return out


async def main():
    cfg = json.load(open("data/ensemble_config.test.json", encoding="utf-8"))
    setups = [(s["strategy_id"], s.get("tf", "10min"), s.get("params", {}))
              for s in cfg.get("setups", []) if s.get("enabled", True)]
    sids = [s[0] for s in setups]
    donch_sig = tuple(1 if s == "donchian_breakout" else 0 for s in sids)
    print("=== КОНФИГ ===")
    print("ансамбль (%d голосов): %s | quorum=%s | окно голосов %dм | сессия %02d:%02d–19:00 МСК" % (
        len(sids), ", ".join(sids), cfg.get("quorum"), LOOKBACK_MIN,
        DAY_START // 60, DAY_START % 60))

    t_from = datetime(2026, 9, 12, tzinfo=timezone.utc)
    t_to = datetime(2026, 9, 20, tzinfo=timezone.utc)
    per = {}
    async with SessionLocal() as db:
        figis = [r[0] for r in (await db.execute(text(
            "SELECT DISTINCT figi FROM sandbox_trades WHERE mode='paper'"))).all()]
        for f in figis:
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

            ev = defaultdict(lambda: {"BUY": set(), "SELL": set()})
            for sid, tf_name, params in setups:
                bars = resample(candles, TF.get(tf_name, 600))
                try:
                    sigs = generate_signals(sid, params, bars)
                except Exception:
                    sigs = []
                for sg in sigs:
                    sds = str(sg.get("side", "")).upper()
                    side = "BUY" if "BUY" in sds else ("SELL" if "SELL" in sds else None)
                    if side:
                        ev[sg["ts"]][side].add(sid)
            ev_list = [(t, ev[t]["BUY"], ev[t]["SELL"]) for t in sorted(ev)]

            # 5m MACD по завершённым корзинам (без заглядывания вперёд)
            buckets = {}
            for i, t in enumerate(ts):
                b = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0) + timedelta(minutes=5)
                buckets[b] = float(cl[i])
            lab5 = sorted(buckets)

            rr = (await db.execute(text("SELECT ticker FROM instruments WHERE figi=:f"),
                                   {"f": f})).first()
            per[f] = dict(ticker=str(rr[0]) if rr else f, ts=ts, cl=cl, vl=vl,
                          h1=macd_hist([float(x) for x in cl]),
                          states=states_for(ts, ev_list, sids, LOOKBACK_MIN * 60),
                          lab5=lab5, h5=macd_hist([buckets[b] for b in lab5]),
                          donch_sig=donch_sig)

    if not per:
        print("нет данных — выходим")
        return
    all_ts = [d["ts"][0] for d in per.values()] + [d["ts"][-1] for d in per.values()]
    print("данные: %s → %s МСК | бумаг: %d | 1м баров: %d" % (
        min(all_ts).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(all_ts).astimezone(MSK).strftime("%d.%m %H:%M"),
        len(per), sum(len(d["ts"]) for d in per.values())))

    variants = [
        ("q2 + confirm-3 (эталон)", "q2", "c3", 0.0),
        ("q2 + instant", "q2", "inst", 0.0),
        ("q2 + hybrid", "q2", "hyb", 0.0),
        ("q2 + c3 + BE25", "q2", "c3", 25.0),
        ("q2 + c3 + BE40", "q2", "c3", 40.0),
        ("q2 + inst + BE40", "q2", "inst", 40.0),
        ("q2 + 5m-фильтр + c3", "q2_5m", "c3", 0.0),
        ("q2c (чистый) + c3", "q2c", "c3", 0.0),
        ("q2c + instant", "q2c", "inst", 0.0),
        ("donch+ + c3", "donch", "c3", 0.0),
    ]
    results = {}
    print()
    print("=== ВАРИАНТЫ (cost %.3f%%/стор, ~%.0f₽/позиция) ===" % (100 * COST, NOTIONAL))
    for name, er, exm, be in variants:
        trades = []
        for f, d in per.items():
            for e_t, x_t, pnl, cst in sim(d, er, exm, be):
                trades.append((e_t, x_t, pnl, cst, d["ticker"]))
        n = len(trades)
        wins = [t for t in trades if t[2] > 0]
        net = sum(t[2] for t in trades)
        cost = sum(t[3] for t in trades)
        days = defaultdict(float)
        for e_t, _x, pnl, _c, _tk in trades:
            days[e_t.astimezone(MSK).strftime("%d.%m")] += pnl
        pts = []
        for e_t, x_t, _p, _c, _tk in trades:
            pts.append((e_t, 1))
            pts.append((x_t, -1))
        pts.sort()
        cur = mx = 0
        for _t, dd in pts:
            cur += dd
            if cur > mx:
                mx = cur
        avg_w = sum(t[2] for t in wins) / len(wins) if wins else 0.0
        avg_l = sum(t[2] for t in trades if t[2] <= 0) / max(n - len(wins), 1)
        pos_days = sum(1 for v in days.values() if v > 0)
        results[name] = dict(n=n, wr=100.0 * len(wins) / max(n, 1), net=net, cost=cost,
                             days=days, mx=mx, trades=trades,
                             avg_w=avg_w, avg_l=avg_l, pos_days=pos_days, ndays=len(days))
        print("  %-24s n=%4d wr=%4.1f%% net=%+9.2f avg=%+6.2f | ср.выигр %+7.1f ср.проигр %+7.1f"
              " | комиссия %6.0f | макс.одновр %2d | дни+ %d/%d" % (
                  name, n, results[name]["wr"], net, net / max(n, 1), avg_w, avg_l,
                  cost, mx, pos_days, len(days)))

    days_all = sorted({d for r in results.values() for d in r["days"]})
    print()
    print("=== ПО ДНЯМ (net, ₽) ===")
    for name in results:
        r = results[name]
        print("  %-24s %s" % (name, "  ".join("%s:%+7.0f" % (d, r["days"].get(d, 0.0))
                                              for d in days_all)))

    best = max(results.items(), key=lambda kv: kv[1]["net"])
    name, r = best
    print()
    print("=== ЛУЧШИЙ: %s (n=%d wr=%.1f%% net=%+.2f) ===" % (name, r["n"], r["wr"], r["net"]))
    per_t = defaultdict(lambda: [0, 0.0])
    for _e, _x, pnl, _c, tk in r["trades"]:
        per_t[tk][0] += 1
        per_t[tk][1] += pnl
    print("  худшие бумаги:", [(t, c, round(v, 1)) for t, (c, v)
                               in sorted(per_t.items(), key=lambda kv: kv[1][1])[:6]])
    print("  лучшие бумаги:", [(t, c, round(v, 1)) for t, (c, v)
                              in sorted(per_t.items(), key=lambda kv: -kv[1][1])[:6]])
    print("  справка: эталон label_votes «quorum-2 + confirm-3» = n=603 wr=45.3%% net=+2219.35"
          " — сверка строки выше")


if __name__ == "__main__":
    asyncio.run(main())
