#!/usr/bin/env python3
"""q2_rsi_sim.py — RSI-гейт для лидера (quorum-2 + confirm-3): вход по стороне RSI.

Триггер (запрос, сделка TATN 14.09 10:46→10:48, LONG по donchian_breakout,
−16.54₽): «в точке входа RSI явно выше 60 — значит выше 50 — значит это только
шорт; надо искать максимальный RSI и ждать подтверждения MACD, а выходить
по минимальному RSI».

Что проверяем офлайн на входах лидера (quorum-2 ансамбля, 12–19.09):
  0. Разбор сделки-триггера: RSI/MACD на входе TATN (живой Q2s).
  1. Диагностика: RSI@вход всех сделок лидера — что приносят LONG-ы, вошедшие
     в «верхней половине» RSI (гипотеза: их блокировка = профит).
  2. RSI-side гейт входа: LONG требует RSI@вх < X, SHORT — RSI@вх > 100−X
     (X = 50..65); асимметричные версии — гейт только LONG / только SHORT.
  3. RSI-ext гейт (как у чемпиона MACDrsi): LONG — min RSI(15м) < os,
     SHORT — max RSI(15м) > ob.
  4. Выходы: confirm-3 (лидер) | фикс-экстремум RSI | confirm-3 ИЛИ RSI |
     трейлинг-RSI (выход, когда RSI откатил от экстремума на N пунктов —
     «выход по минимальному RSI» для шорта).

Сверка баз:
  лидер q2c + confirm-3 ............ n=587 wr=45.7% net=+2317₽ (q2_sim)
  чемпион macd+ext30/70 + rsi30/70 ... n=403 wr=64.5% net=+1187₽ (macd_rsi_sim)

Условия: сессия 9:50–19:00 МСК, оборот бара ≥50к₽, кулдаун 15 баров,
закрытие в конце дня, комиссия 0.045%/стор, ~6400₽/позиция.

Запуск: cd ~/Dev/Deeptrading/backend && PYTHONPATH=. .venv/bin/python3 scripts/q2_rsi_sim.py
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
TF = {"1min": 60, "5min": 300, "10min": 600, "15min": 900, "hour": 3600}
COST = 0.00045
NOTIONAL = 6400.0
LOOKBACK_MIN = 15
DAY_START = 9 * 60 + 50
DAY_END = 19 * 60
WIN = 15


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


def sim(d, entry_rule, exit_mode, gate=None, cooldown=15):
    """Сделки одной бумаги; кортеж (вх, вых, sgn, причина, pnl, RSI@вх).

    entry_rule: "q2c" | "q2" | "macd"
    gate: None | ("side", X) | ("sideL", X) | ("sideS", X) | ("ext", os, ob, win)
    exit_mode: ("c3",) | ("rsi", os, ob) | ("c3rsi", os, ob) | ("rsitrail", buf)
    """
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
    r_ext = 50.0
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
            if entry_rule in ("q2c", "q2"):
                st = states[i]
                b = sum(1 for x in st if x == 1)
                s2 = sum(1 for x in st if x == -1)
                if entry_rule == "q2c":
                    if b >= 2 and s2 == 0:
                        want = 1
                    elif s2 >= 2 and b == 0:
                        want = -1
                else:
                    if b >= 2 and b > s2:
                        want = 1
                    elif s2 >= 2 and s2 > b:
                        want = -1
            elif entry_rule == "macd":
                if h1[i - 1] <= 0 < h1[i]:
                    want = 1
                elif h1[i - 1] >= 0 > h1[i]:
                    want = -1
            if want:
                if gate is not None:
                    g0 = gate[0]
                    if g0 == "side":
                        thr = gate[1]
                        if want == 1 and not (float(r[i]) < thr):
                            want = 0
                        elif want == -1 and not (float(r[i]) > 100.0 - thr):
                            want = 0
                    elif g0 == "sideL":
                        if want == 1 and not (float(r[i]) < gate[1]):
                            want = 0
                    elif g0 == "sideS":
                        if want == -1 and not (float(r[i]) > gate[1]):
                            want = 0
                    elif g0 == "ext":
                        os_t, ob_t, win = gate[1], gate[2], gate[3]
                        w0 = max(0, i - win)
                        if want == 1 and not (float(r[w0:i + 1].min()) < os_t):
                            want = 0
                        elif want == -1 and not (float(r[w0:i + 1].max()) > ob_t):
                            want = 0
            if want:
                sgn = want
                ep = float(cl[i])
                eq = max(1, round(NOTIONAL / ep))
                pos = 1
                entry_i = i
                r_ent = float(r[i])
                r_ext = float(r[i])
        else:
            px = None
            why = ""
            if exit_mode[0] == "c3":
                against = h1[i] * sgn < 0
                held = i - entry_i
                if (held >= 3 and against
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
            elif exit_mode[0] == "c3rsi":
                os_t, ob_t = exit_mode[1], exit_mode[2]
                if sgn == 1 and float(r[i]) > ob_t:
                    px = float(cl[i])
                    why = "rsi"
                elif sgn == -1 and float(r[i]) < os_t:
                    px = float(cl[i])
                    why = "rsi"
                if px is None:
                    against = h1[i] * sgn < 0
                    held = i - entry_i
                    if (held >= 3 and against
                            and h1[i - 1] * sgn < 0 and h1[i - 2] * sgn < 0):
                        px = float(cl[i])
                        why = "c3"
            elif exit_mode[0] == "rsitrail":
                buf = exit_mode[1]
                ri = float(r[i])
                if sgn == 1:
                    if ri > r_ext:
                        r_ext = ri
                    if ri < r_ext - buf:
                        px = float(cl[i])
                        why = "rsitrail"
                else:
                    if ri < r_ext:
                        r_ext = ri
                    if ri > r_ext + buf:
                        px = float(cl[i])
                        why = "rsitrail"
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


def run(per, entry_rule, exit_mode, gate=None):
    trades = []
    for f, d in per.items():
        for tr in sim(d, entry_rule, exit_mode, gate):
            trades.append(tr + (d["ticker"],))
    return trades


async def main():
    _cfg_file = "data/ensemble_config.test.q2.json"
    try:
        cfg = json.load(open(_cfg_file, encoding="utf-8"))
    except Exception:
        _cfg_file = "data/ensemble_config.test.json"
        cfg = json.load(open(_cfg_file, encoding="utf-8"))
    print(f"конфиг сетапов: {_cfg_file} (как у живого лидера Q2s)")
    setups = [(s["strategy_id"], s.get("tf", "10min"), s.get("params", {}))
              for s in cfg.get("setups", []) if s.get("enabled", True)]
    sids = [s[0] for s in setups]

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
                states=states_for(ts, ev_list, sids, LOOKBACK_MIN * 60),
                day=day_ids, hm=hm_arr,
            )

    if not per:
        print("нет данных")
        return
    print("=== ДАННЫЕ ===")
    print(f"ансамбль ({len(sids)} голосов: {', '.join(sids)}) | quorum=2 | lookback {LOOKBACK_MIN}м")
    print(f"бумаг: {len(per)} | 1м баров: {sum(len(d['ts']) for d in per.values())} | окно "
          f"{min(d['ts'][0] for d in per.values()).astimezone(MSK):%d.%m %H:%M} → "
          f"{max(d['ts'][-1] for d in per.values()).astimezone(MSK):%d.%m %H:%M} МСК")

    # --- 0) сделка-триггер TATN (живой Q2s) ---
    print()
    print("=== СДЕЛКА-ТРИГГЕР: TATN 14.09 10:46 (LONG по donchian_breakout, −16.54₽) ===")
    async with SessionLocal() as db:
        trows = (await db.execute(text(
            "SELECT figi, entry_time, exit_time, side, qty, entry_price, net_pnl, exit_reason "
            "FROM sandbox_trades WHERE mode='paper' AND test_name IN ('Q2','Q2s') AND ticker='TATN' "
            "ORDER BY entry_time"))).all()
    shown = 0
    for figi, E, X, side, qty, ep, net, xr in trows:
        d = per.get(figi)
        if d is None:
            continue
        ts, cl, r, h1 = d["ts"], d["cl"], d["r"], d["h1"]
        i0 = min(bisect_left(ts, E), len(ts) - 1)
        i1 = min(bisect_left(ts, X), len(ts) - 1) if X is not None else i0
        em_ = E.astimezone(MSK)
        xm_ = X.astimezone(MSK) if X is not None else None
        xs = xm_.strftime("%H:%M") if xm_ is not None else "open"
        ns = f"{float(net):.2f}" if net is not None else "open"
        print(f"  {em_.strftime('%d.%m %H:%M')} → {xs} {side} q={qty} @{float(ep):.2f} net={ns} exit={xr}")
        print(f"  RSI@вх={float(r[i0]):.1f} | MACD hist@вх={h1[i0]:+.3f} | "
              f"RSI(15м до вх): min={float(r[max(0, i0 - 15):i0 + 1].min()):.1f} "
              f"max={float(r[max(0, i0 - 15):i0 + 1].max()):.1f}")
        if shown == 0:
            print("  бары вокруг входа (close, RSI14, MACD hist):")
            for i in range(max(0, i0 - 8), min(len(ts), i0 + 7)):
                mk = "   <<< ВХОД" if i == i0 else ("   <<< ВЫХОД" if i == i1 else "")
                print(f"    {ts[i].astimezone(MSK).strftime('%H:%M')} c={float(cl[i]):8.2f} "
                      f"RSI={float(r[i]):5.1f} h={h1[i]:+7.3f}{mk}")
        shown += 1
    if not shown:
        print("  (сделок TATN в Q2/Q2s нет — реплей ещё не дошёл)")

    # --- 0b) RSI@вход всех ЖИВЫХ сделок Q2s ---
    print()
    print("=== RSI(14)@вход: живые сделки Q2s ===")
    async with SessionLocal() as db:
        lrows = (await db.execute(text(
            "SELECT figi, ticker, entry_time, side, net_pnl, exit_reason "
            "FROM sandbox_trades WHERE mode='paper' AND test_name='Q2s' ORDER BY entry_time"))).all()
    for f, tk, E, side, net, xr in lrows:
        d = per.get(f)
        if d is None:
            continue
        ts_, r_ = d["ts"], d["r"]
        i0 = min(bisect_left(ts_, E), len(ts_) - 1)
        w0 = max(0, i0 - 15)
        lng = str(side).upper() in ("BUY", "LONG")
        ns = f"{float(net):+.2f}" if net is not None else "open"
        print(f"  {E.astimezone(MSK):%d.%m %H:%M} {str(tk):6s} {'L' if lng else 'S'} "
              f"RSI@вх={float(r_[i0]):5.1f} (15м: {float(r_[w0:i0 + 1].min()):.0f}..{float(r_[w0:i0 + 1].max()):.0f}) "
              f"net={ns} {xr or 'open'}")

    # --- 1) диагностика RSI@вход лидера ---
    base = run(per, "q2c", ("c3",))
    ab = agg(base)
    print()
    print(f"=== БАЗА: q2c + confirm-3 = n={ab['n']} wr={100.0 * ab['wins'] / max(ab['n'], 1):.1f}% "
          f"net={ab['net']:+.2f} (справка q2_sim: n=587 wr=45.7% net=+2317) ===")
    print("RSI(14)@вход по сторонам:")
    for lab, want in (("LONG", 1), ("SHORT", -1)):
        sel = [t for t in base if t[2] == want]
        print(f"  {lab}: n={len(sel):3d} net={sum(t[4] for t in sel):+9.2f}")
        for lo_b in (0, 30, 40, 50, 60, 70):
            b = [t for t in sel if lo_b <= t[5] < lo_b + 10]
            if b:
                w = sum(1 for t in b if t[4] > 0)
                print(f"    RSI@вх [{lo_b:2d}-{lo_b + 10:2d}): n={len(b):3d} "
                      f"wr={100.0 * w / len(b):4.1f}% net={sum(t[4] for t in b):+9.2f}")
    print()
    print("контрфакт «RSI-сторона» (убрать входы ПРОТИВ RSI):")
    bnet = sum(t[4] for t in base)
    for thr in (50.0, 55.0, 60.0):
        rem = [t for t in base if (t[2] == 1 and t[5] >= thr) or (t[2] == -1 and t[5] <= 100.0 - thr)]
        kept = [t for t in base if t not in rem]
        print(f"  X={thr:.0f}: убрано {len(rem):3d} (их net {sum(t[4] for t in rem):+8.2f}) "
              f"→ net {sum(t[4] for t in kept):+9.2f} (факт {bnet:+.2f})")
    for thr in (50.0, 55.0, 60.0):
        rem = [t for t in base if t[2] == 1 and t[5] >= thr]
        print(f"  только LONG RSI>={thr:.0f}: убрано {len(rem):3d} "
              f"(их net {sum(t[4] for t in rem):+8.2f}) → net {bnet - sum(t[4] for t in rem):+9.2f}")
    for thr in (50.0, 55.0, 60.0):
        rem = [t for t in base if t[2] == -1 and t[5] <= thr]
        print(f"  только SHORT RSI<={thr:.0f}: убрано {len(rem):3d} "
              f"(их net {sum(t[4] for t in rem):+8.2f}) → net {bnet - sum(t[4] for t in rem):+9.2f}")

    # --- 2-4) сетка вариантов ---
    variants = [
        ("q2c + c3 [ЛИДЕР]", "q2c", ("c3",), None),
        ("q2 + c3", "q2", ("c3",), None),
        ("macd + ext30/70 + rsi30/70 [ЧЕМПИОН]", "macd", ("rsi", 30.0, 70.0), ("ext", 30.0, 70.0, WIN)),
        ("macd + ext30/60 + rsi30/70 (шире шорт)", "macd", ("rsi", 30.0, 70.0), ("ext", 30.0, 60.0, WIN)),
    ]
    for thr in (50.0, 55.0, 60.0, 65.0):
        variants.append((f"q2c side{thr:.0f} + c3", "q2c", ("c3",), ("side", thr)))
    for thr in (50.0, 55.0, 60.0):
        variants.append((f"q2c sideL{thr:.0f} (гейт только LONG) + c3", "q2c", ("c3",), ("sideL", thr)))
        variants.append((f"q2c sideS{thr:.0f} (гейт только SHORT) + c3", "q2c", ("c3",), ("sideS", thr)))
    for os_t, ob_t in ((30.0, 70.0), (35.0, 65.0), (40.0, 60.0), (45.0, 55.0)):
        variants.append((f"q2c ext{os_t:.0f}/{ob_t:.0f} + c3", "q2c", ("c3",), ("ext", os_t, ob_t, WIN)))
    for os_t, ob_t in ((30.0, 70.0), (35.0, 65.0), (40.0, 60.0), (45.0, 55.0)):
        variants.append((f"q2c + rsi{os_t:.0f}/{ob_t:.0f} (вых)", "q2c", ("rsi", os_t, ob_t), None))
        variants.append((f"q2c + c3|rsi{os_t:.0f}/{ob_t:.0f}", "q2c", ("c3rsi", os_t, ob_t), None))
        variants.append((f"q2c side50 + c3|rsi{os_t:.0f}/{ob_t:.0f}", "q2c", ("c3rsi", os_t, ob_t), ("side", 50.0)))
        variants.append((f"q2c ext35/65 + c3|rsi{os_t:.0f}/{ob_t:.0f}", "q2c", ("c3rsi", os_t, ob_t), ("ext", 35.0, 65.0, WIN)))
    for buf in (5.0, 10.0, 15.0):
        variants.append((f"q2c + rsitrail{buf:.0f}", "q2c", ("rsitrail", buf), None))
        variants.append((f"q2c side50 + rsitrail{buf:.0f}", "q2c", ("rsitrail", buf), ("side", 50.0)))

    print()
    print(f"=== СЕТКА (cost {100 * COST:.3f}%/стор, ~{NOTIONAL:.0f}₽/поз, кулдаун 15) ===")
    results = []
    for name, er, ex, gt in variants:
        trades = run(per, er, ex, gt)
        a2 = agg(trades)
        results.append((name, a2, trades))
    for name, a2, _tr in sorted(results, key=lambda x: -x[1]["net"]):
        print(f"  {name:44s} n={a2['n']:4d} wr={100.0 * a2['wins'] / max(a2['n'], 1):5.1f}% "
              f"net={a2['net']:+9.2f} avg={a2['net'] / max(a2['n'], 1):+6.2f} | "
              f"L {a2['by_side']['L'][0]:3d}/{a2['by_side']['L'][1]:+8.1f} "
              f"S {a2['by_side']['S'][0]:3d}/{a2['by_side']['S'][1]:+8.1f} | макс.одн {a2['mx']:2d}")

    print()
    print("=== ТОП-3 ПО NET: ДЕТАЛИ ===")
    for name, a2, trades in sorted(results, key=lambda x: -x[1]["net"])[:3]:
        print(f"--- {name}: n={a2['n']} wr={100.0 * a2['wins'] / max(a2['n'], 1):.1f}% net={a2['net']:+.2f} ---")
        print("  по дням: " + "  ".join(f"{dd}:{vv:+7.0f}" for dd, vv in sorted(a2["by_day"].items())))
        print("  причины выхода: " + str({k: (v[0], round(v[1], 1)) for k, v in
                                           sorted(a2["by_reason"].items(), key=lambda kv: -kv[1][0])}))
        if a2["hold"]:
            print(f"  удержание, мин: med={a2['hold'][len(a2['hold']) // 2]:.0f} "
                  f"p90={a2['hold'][min(len(a2['hold']) - 1, int(len(a2['hold']) * 0.9))]:.0f} "
                  f"max={a2['hold'][-1]:.0f}")
        print("  худшие: " + str([(t, v[0], round(v[1], 1)) for t, v in
                                  sorted(a2["by_tk"].items(), key=lambda kv: kv[1][1])[:5]]))
        print("  лучшие: " + str([(t, v[0], round(v[1], 1)) for t, v in
                                  sorted(a2["by_tk"].items(), key=lambda kv: -kv[1][1])[:5]]))

    print()
    print("маппинг в бота: side-гейт = новые поля entry_rsi_max(LONG)/entry_rsi_min(SHORT) "
          "в ensemble_strategy.py (текущие entry_rsi_os/ob — экстремум за окно, не то же "
          "самое); rsi-выход уже есть (exit_rsi_ob/os); rsitrail — нового кода.")


if __name__ == "__main__":
    asyncio.run(main())
