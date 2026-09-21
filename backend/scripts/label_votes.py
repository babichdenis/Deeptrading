#!/usr/bin/env python3
"""label_votes.py — «правильные» входы (разметка для ML) × все голоса ансамбля.

Идея (запрос): софт размечает правильные точки входа; прогоняем туда весь
ансамбль, записываем все 7 голосов и смотрим, какие комбинации повторяются —
если есть устойчивые, следуем за ними. ML подключаем как проверку (исторически
процент узнаваемости минимальный — меряем честно против базовой частоты).

Всё офлайн и быстро (минуты вместо часов реплея); бот — только финальное
подтверждение лучшего правила.

Что делает:
  1. Бумаги = figi из paper-тестов; 1m свечи 12–20.09.
  2. Разметка «правильных входов» двумя способами:
     • triple-barrier ATR (как scripts/h_label_dataset.py): +1/-1/0 на 1m бар;
     • zigzag-свинг (как вкладка «Тест», app/services/ceiling.py): идеальные
       подтверждённые минимумы/максимумы.
  3. Голоса всех сетапов data/ensemble_config.test.json; на каждом баре —
     «подпись» из 7 голосов (BUY/SELL/·) за lookback минут.
  4. Повторяемость: P(метка | подпись), lift, топы для +1 и -1.
  5. Сверка: видит ли ансамбль идеальные zigzag-точки; quorum-2 и MACD-кросс
     1m против метки.
  6. ML (lightgbm/sklearn, split по времени): голоса+фичи → метка против базы.
  7. Мини-сим: лучшие подписи как ПРАВИЛО входа (выход confirm-3 как в v2),
     против MACD-only и quorum-2.
  8. Датасет со всеми голосами: reports/label_votes.parquet.

Запуск (на .4):  cd ~/Dev/Deeptrading/backend && .venv/bin/python3 scripts/label_votes.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from bisect import bisect_left
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
from sqlalchemy import text

from app.database import SessionLocal
from app.engine.models import Candle
from app.services.ceiling import zigzag_swings
from app.services.ensemble import generate_signals, resample

MSK = timezone(timedelta(hours=3))
TF = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "hour": 3600}
COST = 0.00045          # комиссия+слиппедж на сторону (как в симах)
NOTIONAL = 6400.0       # ~размер позиции скальпера из тестов
SHORT = {
    "rsi_reversal": "rsi",
    "bollinger_reclaim": "boll",
    "vwap_reclaim": "vwap",
    "vwap_reversion": "vwapr",
    "donchian_breakout": "donch",
    "macd_cross": "macd",
    "volume_drop": "vold",
    "kama_trend": "kama",
}


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


def atr_series(high, low, close, period=14):
    n = len(close)
    tr = np.empty(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.full(n, np.nan)
    if n >= period:
        atr[period - 1] = tr[:period].mean()
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def triple_barrier(close, high, low, atr, tp, sl, max_hold):
    """+1 (upper first) / -1 (lower first) / 0 (timeout). ok=True — метка определена."""
    n = len(close)
    lab = np.zeros(n, np.int8)
    ok = np.zeros(n, bool)
    for i in range(n):
        a = atr[i]
        if not np.isfinite(a) or a <= 0:
            continue
        if i + max_hold >= n:  # цензурированный хвост — не размечаем
            continue
        ok[i] = True
        up = close[i] + tp * a
        dn = close[i] - sl * a
        for j in range(i + 1, i + max_hold + 1):
            if high[j] >= up:
                lab[i] = 1
                break
            if low[j] <= dn:
                lab[i] = -1
                break
    return lab, ok


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


def sig_str(sig, names):
    return " ".join("%s%s" % (n, "+" if v == 1 else ("-" if v == -1 else "·"))
                    for n, v in zip(names, sig))


def sim_rule(d, rule, cooldown=15):
    """Мини-сим: вход по правилу, выход confirm-3 (гист. MACD 1m против 3 бара
    подряд) или конец дня; день-сессия 9:50–19:00, оборот бара ≥50к, cost/стор."""
    ts, cl, vl, h1, states = d["ts"], d["cl"], d["vl"], d["h1"], d["states"]
    n = len(ts)
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    out = []
    prev_date = ts[0].astimezone(MSK).date()
    for i in range(1, n):
        tm = ts[i].astimezone(MSK)
        if tm.date() != prev_date and pos != 0:
            px = float(cl[i - 1])
            out.append(sgn * (px - ep) * eq - COST * (ep + px) * eq)
            pos = 0
            last_exit = i - 1
        prev_date = tm.date()
        if i < 60:
            continue
        if pos == 0:
            if i - last_exit <= cooldown:
                continue
            if float(cl[i]) * float(vl[i]) < 50000.0:
                continue
            hm = tm.hour * 60 + tm.minute
            if not (9 * 60 + 50 <= hm < 19 * 60):
                continue
            want = 0
            kind = rule[0]
            if kind == "macd":
                if h1[i - 1] <= 0 < h1[i]:
                    want = 1
                elif h1[i - 1] >= 0 > h1[i]:
                    want = -1
            elif kind == "q2":
                st = states[i]
                b = sum(1 for x in st if x == 1)
                s2 = sum(1 for x in st if x == -1)
                if b >= 2 and b > s2:
                    want = 1
                elif s2 >= 2 and s2 > b:
                    want = -1
            elif kind == "sig":
                if states[i] == rule[1]:
                    want = rule[2]
            if want:
                sgn = want
                ep = float(cl[i])
                eq = max(1, round(NOTIONAL / ep))
                pos = 1
                entry_i = i
        else:
            held = i - entry_i
            if (held >= 3 and h1[i] * sgn < 0
                    and h1[i - 1] * sgn < 0 and h1[i - 2] * sgn < 0):
                px = float(cl[i])
                out.append(sgn * (px - ep) * eq - COST * (ep + px) * eq)
                pos = 0
                last_exit = i
    return out


async def main():
    ap = argparse.ArgumentParser(description="голоса ансамбля в размеченных точках")
    ap.add_argument("--lookback", type=int, default=15, help="окно голосов, мин (default 15)")
    ap.add_argument("--minsup", type=int, default=25, help="мин. поддержка подписи (default 25)")
    ap.add_argument("--zig", type=float, default=0.0025, help="порог зигзага, доля (default 0.0025)")
    args = ap.parse_args()

    cfg = json.load(open("data/ensemble_config.test.json", encoding="utf-8"))
    setups = [(s["strategy_id"], s.get("tf", "10min"), s.get("params", {}))
              for s in cfg.get("setups", []) if s.get("enabled", True)]
    sids = [s[0] for s in setups]
    names = [SHORT.get(s, s[:6]) for s in sids]
    print("=== КОНФИГ ===")
    print("ансамбль (%d голосов): %s | quorum=%s" % (
        len(sids), ", ".join("%s(%s)" % (n, s[1]) for s, n in zip(setups, names)),
        cfg.get("quorum")))
    print("легенда подписи: %s" % ", ".join(names))

    t_from = datetime(2026, 9, 12, tzinfo=timezone.utc)
    t_to = datetime(2026, 9, 20, tzinfo=timezone.utc)
    per = {}
    async with SessionLocal() as db:
        figis = [r[0] for r in (await db.execute(text(
            "SELECT DISTINCT figi FROM sandbox_trades WHERE mode='paper'"))).all()]
        tickers = {}
        for f in figis:
            rr = (await db.execute(
                text("SELECT ticker FROM instruments WHERE figi=:f"), {"f": f})).first()
            tickers[f] = str(rr[0]) if rr else f
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
            hi = np.array([c.high for c in candles])
            lo = np.array([c.low for c in candles])
            vl = np.array([float(c.volume or 0) for c in candles])
            n = len(ts)

            ev = defaultdict(lambda: {"BUY": set(), "SELL": set()})
            n_ev = 0
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
                        n_ev += 1
            ev_list = [(t, ev[t]["BUY"], ev[t]["SELL"]) for t in sorted(ev)]

            atr = atr_series(hi, lo, cl)
            lab60, ok60 = triple_barrier(cl, hi, lo, atr, 2.0, 1.0, 60)
            lab30, ok30 = triple_barrier(cl, hi, lo, atr, 1.5, 1.0, 30)
            h1 = macd_hist([float(x) for x in cl])
            states = states_for(ts, ev_list, sids, args.lookback * 60)

            volr = np.zeros(n)
            for i in range(n):
                w0 = max(0, i - 20)
                base = vl[w0:i]
                m = float(base.mean()) if len(base) else 0.0
                volr[i] = float(vl[i]) / m if m > 0 else 0.0

            per[f] = dict(ticker=tickers[f], ts=ts, cl=cl, hi=hi, lo=lo, vl=vl, n=n,
                          atr=atr, lab60=lab60, ok60=ok60, lab30=lab30, ok30=ok30,
                          h1=h1, states=states, ev=ev_list, n_ev=n_ev, volr=volr,
                          candles=candles)

    if not per:
        print("нет данных — выходим")
        return
    all_ts = [d["ts"][0] for d in per.values()] + [d["ts"][-1] for d in per.values()]
    print("данные: %s → %s МСК | бумаг: %d | 1м баров: %d | событий голосов: %d" % (
        min(all_ts).astimezone(MSK).strftime("%d.%m %H:%M"),
        max(all_ts).astimezone(MSK).strftime("%d.%m %H:%M"),
        len(per), sum(d["n"] for d in per.values()),
        sum(d["n_ev"] for d in per.values())))
    ev_by_sid = Counter()
    for d in per.values():
        for _t, bs, ss in d["ev"]:
            ev_by_sid.update(bs)
            ev_by_sid.update(ss)
    print("события по сетапам:", {SHORT.get(k, k): v for k, v in ev_by_sid.most_common()})

    # ---------- zigzag: идеальные точки ----------
    zlow_lbl = Counter()
    zhigh_lbl = Counter()
    zlow_b = Counter()
    zlow_b_lbl = defaultdict(Counter)
    zhigh_s = Counter()
    zhigh_s_lbl = defaultdict(Counter)
    zlow_states = Counter()
    for d in per.values():
        dicts = [{"low": float(x), "high": float(y)} for x, y in zip(d["lo"], d["hi"])]
        try:
            swings = zigzag_swings(dicts, args.zig)
        except Exception:
            swings = []
        d["swings"] = swings
        for sw in swings:
            i = sw["idx"]
            if i >= d["n"] or not d["ok60"][i]:
                continue
            st = d["states"][i]
            b = sum(1 for x in st if x == 1)
            s2 = sum(1 for x in st if x == -1)
            lab = int(d["lab60"][i])
            if sw["kind"] == "low":
                zlow_lbl[lab] += 1
                zlow_b[min(b, 3)] += 1
                zlow_b_lbl[min(b, 3)][lab] += 1
                zlow_states[st] += 1
            else:
                zhigh_lbl[lab] += 1
                zhigh_s[min(s2, 3)] += 1
                zhigh_s_lbl[min(s2, 3)][lab] += 1

    # ---------- глобальная статистика баров ----------
    base = Counter()
    sig_stats = defaultdict(Counter)
    bc_lbl = defaultdict(Counter)
    sc_lbl = defaultdict(Counter)
    q2b = Counter()
    q2s = Counter()
    upx = Counter()
    dnx = Counter()
    for d in per.values():
        ok = d["ok60"]
        lab = d["lab60"]
        states = d["states"]
        h1 = d["h1"]
        for i in range(d["n"]):
            if not ok[i]:
                continue
            L = int(lab[i])
            base[L] += 1
            st = states[i]
            sig_stats[st][L] += 1
            b = sum(1 for x in st if x == 1)
            s2 = sum(1 for x in st if x == -1)
            bc_lbl[min(b, 3)][L] += 1
            sc_lbl[min(s2, 3)][L] += 1
            if b >= 2 and b > s2:
                q2b[L] += 1
            if s2 >= 2 and s2 > b:
                q2s[L] += 1
            if i > 0:
                if h1[i - 1] <= 0 < h1[i]:
                    upx[L] += 1
                elif h1[i - 1] >= 0 > h1[i]:
                    dnx[L] += 1

    tot = sum(base.values())
    p_base = {k: (base[k] / tot if tot else 0.0) for k in (1, 0, -1)}
    print()
    print("=== БАЗА (метка60: TP=2×ATR, SL=1×ATR, hold≤60м) ===")
    print("баров с меткой: %d | +1(LONG): %.1f%% | -1(SHORT): %.1f%% | 0(timeout): %.1f%%" % (
        tot, 100 * p_base[1], 100 * p_base[-1], 100 * p_base[0]))

    print()
    print("=== ГОЛОСА: P(метка | сколько голосов за %dм) ===" % args.lookback)
    for k in (0, 1, 2, 3):
        c = bc_lbl.get(k)
        if not c:
            continue
        s = sum(c.values())
        lift = (c[1] / s) / p_base[1] if p_base[1] else 0.0
        print("  BUY=%s:  баров %7d | P(+1)=%5.1f%% (lift %.2f) | P(-1)=%5.1f%%" % (
            k if k < 3 else "3+", s, 100 * c[1] / s, lift, 100 * c[-1] / s))
    for k in (0, 1, 2, 3):
        c = sc_lbl.get(k)
        if not c:
            continue
        s = sum(c.values())
        lift = (c[-1] / s) / p_base[-1] if p_base[-1] else 0.0
        print("  SELL=%s: баров %7d | P(-1)=%5.1f%% (lift %.2f) | P(+1)=%5.1f%%" % (
            k if k < 3 else "3+", s, 100 * c[-1] / s, lift, 100 * c[1] / s))

    rows = []
    for st, c in sig_stats.items():
        nn = sum(c.values())
        if nn >= args.minsup:
            rows.append((st, nn, c[1], c[-1], c[0]))

    print()
    print("=== ПОДПИСИ (все %d голосов, lookback %dм, support≥%d; подписей всего %d) ===" % (
        len(sids), args.lookback, args.minsup, len(rows)))
    if p_base[1]:
        print("топ-12 по P(+1|sig) — «повторения» в правильных ЛОНГ-точках:")
        print("  %-58s %6s %6s %6s %6s %6s" % ("подпись", "n", "+1%", "-1%", "0%", "lift+"))
        for st, nn, np_, nm_, nz_ in sorted(rows, key=lambda r: -(r[2] / r[1]) / p_base[1])[:12]:
            print("  %-58s %6d %6.1f %6.1f %6.1f %6.2f" % (
                sig_str(st, names), nn, 100 * np_ / nn, 100 * nm_ / nn, 100 * nz_ / nn,
                (np_ / nn) / p_base[1]))
    if p_base[-1]:
        print("топ-12 по P(-1|sig) — правильные ШОРТ-точки:")
        print("  %-58s %6s %6s %6s %6s %6s" % ("подпись", "n", "+1%", "-1%", "0%", "lift-"))
        for st, nn, np_, nm_, nz_ in sorted(rows, key=lambda r: -(r[3] / r[1]) / p_base[-1])[:12]:
            print("  %-58s %6d %6.1f %6.1f %6.1f %6.2f" % (
                sig_str(st, names), nn, 100 * np_ / nn, 100 * nm_ / nn, 100 * nz_ / nn,
                (nm_ / nn) / p_base[-1]))
    print("топ-6 по числу баров с меткой +1 (какие голоса стоят в правильных точках):")
    for st, nn, np_, nm_, nz_ in sorted(rows, key=lambda r: -r[2])[:6]:
        print("    %-58s n(+1)=%5d | P(+1|sig)=%.0f%%" % (
            sig_str(st, names), np_, 100 * np_ / nn))

    print()
    print("=== СВЕРКА ТРИГГЕРОВ С МЕТКОЙ ===")
    for name, c, want in (("quorum-2 BUY", q2b, 1), ("quorum-2 SELL", q2s, -1),
                          ("MACD 1m up-cross", upx, 1), ("MACD 1m down-cross", dnx, -1)):
        s = sum(c.values())
        if not s:
            continue
        p = c[want] / s
        pb = p_base[want]
        print("  %-20s баров %6d | P(%+d)=%5.1f%% (lift %.2f) | P(0)=%5.1f%%" % (
            name, s, want, 100 * p, (p / pb if pb else 0.0), 100 * c[0] / s))

    print()
    print("=== ZIGZAG-ТОЧКИ (идеальные входы, порог %.2f%%) ===" % (100 * args.zig))
    nl = sum(zlow_lbl.values())
    nh = sum(zhigh_lbl.values())
    if nl:
        print("минимумов: %d | метка на самом low-баре: +1 %.0f%% | 0 %.0f%% | -1 %.0f%%" % (
            nl, 100 * zlow_lbl[1] / nl, 100 * zlow_lbl[0] / nl, 100 * zlow_lbl[-1] / nl))
        print("  голоса BUY за %dм НА low-баре: %s" % (
            args.lookback,
            "  ".join("%s: %d%%" % (k if k < 3 else "3+", 100 * zlow_b[k] / nl)
                      for k in sorted(zlow_b))))
        for k in (1, 2, 3):
            c = zlow_b_lbl.get(k)
            if c and sum(c.values()) >= 10:
                s = sum(c.values())
                print("  P(+1 | low & BUY≥%d) = %.0f%% (n=%d) — ансамбр «видит» идеальный вход?" % (
                    k, 100 * c[1] / s, s))
        print("  топ-3 подписи на low-барах:")
        for st, c in zlow_states.most_common(3):
            print("    %s  ×%d" % (sig_str(st, names), c))
    if nh:
        print("максимумов: %d | метка на high-баре: -1 %.0f%% | 0 %.0f%% | +1 %.0f%%" % (
            nh, 100 * zhigh_lbl[-1] / nh, 100 * zhigh_lbl[0] / nh, 100 * zhigh_lbl[1] / nh))
        print("  голоса SELL за %dм НА high-баре: %s" % (
            args.lookback,
            "  ".join("%s: %d%%" % (k if k < 3 else "3+", 100 * zhigh_s[k] / nh)
                      for k in sorted(zhigh_s))))
        for k in (1, 2, 3):
            c = zhigh_s_lbl.get(k)
            if c and sum(c.values()) >= 10:
                s = sum(c.values())
                print("  P(-1 | high & SELL≥%d) = %.0f%% (n=%d)" % (k, 100 * c[-1] / s, s))

    # ---------- ML-проверка ----------
    print()
    print("=== ML: голоса+фичи → метка60 (split 70/30 по времени) ===")
    X, y, tnum = [], [], []
    for d in per.values():
        ok = d["ok60"]
        lab = d["lab60"]
        states = d["states"]
        atr = d["atr"]
        cl = d["cl"]
        for i in range(d["n"]):
            if not ok[i]:
                continue
            a = atr[i]
            fin = np.isfinite(a) and a > 0
            h1n = (d["h1"][i] / a) if fin else 0.0
            natr = (a / cl[i]) if (fin and cl[i]) else 0.0
            r5 = (cl[i] / cl[i - 5] - 1.0) if (i >= 5 and cl[i - 5]) else 0.0
            X.append(list(states[i]) + [d["ts"][i].astimezone(MSK).hour,
                                        float(d["volr"][i]), float(natr), float(r5), float(h1n)])
            y.append(int(lab[i]) + 1)  # 0=-1, 1=0, 2=+1
            tnum.append(d["ts"][i].timestamp())
    X = np.array(X, dtype=float)
    y = np.array(y)
    tnum = np.array(tnum)
    thr = float(np.quantile(tnum, 0.7))
    tr = tnum <= thr
    te = ~tr

    model = None
    mname = ""
    try:
        import lightgbm as lgb
        model = lgb.LGBMClassifier(n_estimators=250, learning_rate=0.06, num_leaves=31,
                                   min_child_samples=50, verbose=-1, random_state=7)
        mname = "lightgbm"
    except Exception:
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
            model = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.08,
                                                   random_state=7)
            mname = "sklearn HGB"
        except Exception:
            model = None
    if model is None or int(tr.sum()) < 1000 or int(te.sum()) < 300:
        print("  lightgbm/sklearn недоступны или мало данных — ML-проверка пропущена")
    else:
        model.fit(X[tr], y[tr])
        pred = model.predict(X[te])
        yt = y[te]
        acc = float((pred == yt).mean())
        maj = int(np.bincount(yt).argmax())
        base_acc = float((yt == maj).mean())
        recalls = []
        for c in (0, 1, 2):
            m = yt == c
            if m.sum():
                recalls.append(float((pred[m] == c).mean()))
        bal = sum(recalls) / len(recalls) if recalls else 0.0
        print("  модель=%s | train n=%d / test n=%d" % (mname, int(tr.sum()), int(te.sum())))
        print("  accuracy=%.1f%% | база (всегда самый частый класс)=%.1f%% | balanced=%.1f%%" % (
            100 * acc, 100 * base_acc, 100 * bal))
        for c, nm in ((2, "+1 LONG"), (0, "-1 SHORT"), (1, "0 timeout")):
            tp = int(((pred == c) & (yt == c)).sum())
            fp = int(((pred == c) & (yt != c)).sum())
            fn_ = int(((pred != c) & (yt == c)).sum())
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn_) if tp + fn_ else 0.0
            share = float((yt == c).mean())
            print("    %-9s precision=%5.1f%% recall=%5.1f%% | доля класса в тесте %5.1f%%" % (
                nm, 100 * prec, 100 * rec, 100 * share))

    # ---------- мини-сим правил ----------
    print()
    print("=== СИМ: правило входа → выход confirm-3, день 9:50–19:00, оборот≥50к, cost %.3f%%/стор ===" % (100 * COST))
    cand_l = [r for r in rows if r[1] >= 30 and any(r[0]) and p_base[1]
              and (r[2] / r[1]) >= 1.25 * p_base[1]]
    cand_l.sort(key=lambda r: -r[2])
    cand_s = [r for r in rows if r[1] >= 30 and any(r[0]) and p_base[-1]
              and (r[3] / r[1]) >= 1.25 * p_base[-1]]
    cand_s.sort(key=lambda r: -r[3])
    rules = [("MACD-кросс 1m (база)", ("macd",)), ("quorum-2 ансамбля", ("q2",))]
    for r in cand_l[:3]:
        rules.append(("sig+ " + sig_str(r[0], names), ("sig", r[0], 1)))
    for r in cand_s[:2]:
        rules.append(("sig- " + sig_str(r[0], names), ("sig", r[0], -1)))
    results = []
    for name, rule in rules:
        allp = []
        for d in per.values():
            allp += sim_rule(d, rule)
        n_ = len(allp)
        w_ = sum(1 for p in allp if p > 0)
        results.append((name, n_, w_, sum(allp)))
    for name, n_, w_, net_ in sorted(results, key=lambda x: -x[3]):
        print("  %-58s n=%4d wr=%4.1f%% net=%+9.2f" % (name, n_, 100 * w_ / max(n_, 1), net_))

    # ---------- датасет ----------
    try:
        import polars as pl
        os.makedirs("reports", exist_ok=True)
        cols = {"figi": [], "ticker": [], "ts": [], "lab60": [], "lab30": [],
                "zig": []}
        for nm in names:
            cols["v_" + nm] = []
        cols.update({"volr": [], "natr": [], "hour": [], "h1n": []})
        for f, d in per.items():
            zig = np.zeros(d["n"], np.int8)
            for sw in d.get("swings", []):
                i = sw["idx"]
                if 0 <= i < d["n"]:
                    zig[i] = 1 if sw["kind"] == "low" else 2
            for i in range(d["n"]):
                if not d["ok60"][i]:
                    continue
                a = d["atr"][i]
                fin = np.isfinite(a) and a > 0
                cols["figi"].append(f)
                cols["ticker"].append(d["ticker"])
                cols["ts"].append(d["ts"][i])
                cols["lab60"].append(int(d["lab60"][i]))
                cols["lab30"].append(int(d["lab30"][i]) if d["ok30"][i] else 99)
                cols["zig"].append(int(zig[i]))
                for nm, v in zip(names, d["states"][i]):
                    cols["v_" + nm].append(v)
                cols["volr"].append(float(d["volr"][i]))
                cols["natr"].append(float(a / d["cl"][i]) if (fin and d["cl"][i]) else 0.0)
                cols["hour"].append(d["ts"][i].astimezone(MSK).hour)
                cols["h1n"].append(float(d["h1"][i] / a) if fin else 0.0)
        pl.DataFrame(cols).write_parquet("reports/label_votes.parquet")
        print()
        print("датасет со всеми голосами: reports/label_votes.parquet (строк %d)" % len(cols["figi"]))
    except Exception as e:
        print("parquet не записан:", type(e).__name__, str(e)[:100])


if __name__ == "__main__":
    asyncio.run(main())
