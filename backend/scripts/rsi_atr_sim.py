#!/usr/bin/env python3
"""rsi_atr_sim.py — MACD+RSI × ATR: адаптивные пороги RSI, ATR-стопы, фильтры входа.

Продолжение scripts/macd_rsi_sim.py. Прошлый победитель (фиксированные пороги):
  вход = кросс гист. MACD(12/26/9) 1м при RSI(14) < 30 / > 70 в последние 15 баров,
  выход = RSI в противоположном экстремуме:
  n=403 wr=64.5% net=+1187₽ (12–19.09, 26 бумаг, cost 0.045%/стор, ~6400₽/поз).

Что тестируем (по запросу):
  1. АДАПТИВНЫЕ ПОРОГИ RSI ОТ ATR (StockSharp-style):
       band = 20 × clamp(ATR% / MA96(ATR%), lo/20, hi/20)
       os = 50 − band,  ob = 50 + band
     Спокойный рынок (ATR% ниже своей МА) → пороги УЖЕ (например 35/65),
     волатильный → ШИРЕ (25/75 и шире). Варианты клампов:
       [15..30] — умеренно, [20..40] — агрессивно.
     Порог применяется и на ВХОДЕ (экстремум в окне WIN), и на ВЫХОДЕ (RSI).
  2. SL/TP ОТ ATR: SL ∈ {1.5, 2.0, 2.5} × ATR(14) 1м, TP ∈ {3.0, 3.5} × ATR —
     внутрибарово (low/high), стоп проверяется первым.
  3. ФИЛЬТРЫ ВХОДА:
       объём — объём бара входа ≥ 1.2 / 1.5 × MA20(объёма): за движением стоят деньги;
       ATR  — ATR% бара ≥ 0.9 / 1.0 × своей MA96: не торгуем «в боковике».

Прочее как раньше: сессия 9:50–19:00 МСК, оборот бара ≥50к₽, кулдаун 15 баров,
закрытие в конце дня, комиссия 0.045%/сторона.

Запуск: cd ~/Dev/Deeptrading/backend && PYTHONPATH=. .venv/bin/python3 scripts/rsi_atr_sim.py
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
WIN = 15          # окно поиска RSI-экстремума на входе (победитель macd_rsi_sim)
COOLDOWN = 15     # кулдаун после выхода (баров)
BAND_REF = 96     # окно нормировки ATR% (баров, ≈1.6 часа)


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


def atr_wilder(hi, lo, cl, period=14):
    n = len(cl)
    tr = np.empty(n)
    tr[0] = float(hi[0]) - float(lo[0])
    for i in range(1, n):
        tr[i] = max(float(hi[i]) - float(lo[i]),
                    abs(float(hi[i]) - float(cl[i - 1])),
                    abs(float(lo[i]) - float(cl[i - 1])))
    atr = np.full(n, np.nan)
    if n >= period:
        atr[period - 1] = float(tr[:period].mean())
        for i in range(period, n):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr


def roll_mean(a, w):
    """Скользящее среднее последних w элементов (включая текущий); nan игнорируются."""
    v = np.nan_to_num(a, nan=0.0)
    f = np.isfinite(a).astype(float)
    cs = np.concatenate(([0.0], np.cumsum(v)))
    cf = np.concatenate(([0.0], np.cumsum(f)))
    idx = np.arange(1, len(a) + 1)
    w0 = np.maximum(0, idx - w)
    cnt = cf[idx] - cf[w0]
    s = cs[idx] - cs[w0]
    return np.where(cnt > 0, s / np.maximum(cnt, 1.0), np.nan)


def sim(d, os_arr, ob_arr, exitk, vol_f=0.0, use_rsi_entry=True, atr_gate=0.0):
    """Один прогон одной бумаги.

    os_arr/ob_arr — пороги RSI по бару (вход И signal-выход);
    exitk: ("flip",) | ("rsi",) | ("sltp", sl, tp) | ("rsi+sltp", sl, tp);
    vol_f: 0 = без фильтра, иначе объём бара >= vol_f × MA20(объёма);
    atr_gate: 0 = без фильтра, иначе ATR%/MA96(ATR%) >= atr_gate на входе.
    Возвращает [(ts входа, ts выхода, сторона, причина, pnl)].
    """
    ts, hi, lo, cl, vl = d["ts"], d["hi"], d["lo"], d["cl"], d["vl"]
    r, h1, atr, vol_ma, ratio = d["r"], d["h1"], d["atr"], d["vol_ma"], d["ratio"]
    n = len(ts)
    trades = []
    pos = 0
    sgn = 0
    ep = 0.0
    eq = 1
    entry_i = -1
    last_exit = -10 ** 9
    sl_px = None
    tp_px = None
    prev_date = ts[0].astimezone(MSK).date()
    for i in range(1, n):
        tm = ts[i].astimezone(MSK)
        if tm.date() != prev_date and pos != 0:
            px = float(cl[i - 1])
            trades.append((ts[entry_i], ts[i - 1], sgn, "day_end",
                           sgn * (px - ep) * eq - COST * (ep + px) * eq))
            pos = 0
            last_exit = i - 1
            sl_px = None
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
            if atr_gate > 0:
                rt = float(ratio[i])
                if not (np.isfinite(rt) and rt >= atr_gate):
                    continue
            up = h1[i - 1] <= 0 < h1[i]
            dn = h1[i - 1] >= 0 > h1[i]
            if not (up or dn):
                continue
            want = 1 if up else -1
            if use_rsi_entry:
                w0 = max(0, i - WIN)
                if want == 1:
                    if not (float(r[w0:i + 1].min()) < float(os_arr[i])):
                        continue
                else:
                    if not (float(r[w0:i + 1].max()) > float(ob_arr[i])):
                        continue
            if vol_f > 0:
                ma = float(vol_ma[i])
                if not (np.isfinite(ma) and ma > 0 and float(vl[i]) >= vol_f * ma):
                    continue
            if exitk[0] in ("sltp", "rsi+sltp"):
                a = float(atr[i])
                if not (np.isfinite(a) and a > 0):
                    continue  # без ATR стопы не выставить — не входим
            sgn = want
            ep = float(cl[i])
            eq = max(1, round(NOTIONAL / ep))
            pos = 1
            entry_i = i
            if exitk[0] in ("sltp", "rsi+sltp"):
                a = float(atr[i])
                if sgn == 1:
                    sl_px = ep - exitk[1] * a
                    tp_px = ep + exitk[2] * a
                else:
                    sl_px = ep + exitk[1] * a
                    tp_px = ep - exitk[2] * a
            else:
                sl_px = None
                tp_px = None
        else:
            px = None
            why = ""
            if sl_px is not None:
                if sgn == 1:
                    if float(lo[i]) <= sl_px:
                        px, why = float(sl_px), "sl"
                    elif float(hi[i]) >= tp_px:
                        px, why = float(tp_px), "tp"
                else:
                    if float(hi[i]) >= sl_px:
                        px, why = float(sl_px), "sl"
                    elif float(lo[i]) <= tp_px:
                        px, why = float(tp_px), "tp"
            if px is None and exitk[0] in ("rsi", "rsi+sltp"):
                if sgn == 1 and float(r[i]) > float(ob_arr[i]):
                    px, why = float(cl[i]), "rsi"
                elif sgn == -1 and float(r[i]) < float(os_arr[i]):
                    px, why = float(cl[i]), "rsi"
            if px is None and exitk[0] == "flip":
                if h1[i] * sgn < 0:
                    px, why = float(cl[i]), "flip"
            if px is not None:
                trades.append((ts[entry_i], ts[i], sgn, why,
                               sgn * (px - ep) * eq - COST * (ep + px) * eq))
                pos = 0
                last_exit = i
                sl_px = None
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
        e, x, sgn, why, pnl = t[0], t[1], t[2], t[3], t[4]
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


def run_variant(per, os_key, ob_key, exitk, vol_f=0.0, use_rsi_entry=True, atr_gate=0.0):
    trades = []
    for f, d in per.items():
        for tr in sim(d, d[os_key], d[ob_key], exitk, vol_f, use_rsi_entry, atr_gate):
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
                "SELECT ts, high, low, close, volume FROM candles "
                "WHERE figi=:f AND interval=1 AND ts>=:a AND ts<:b ORDER BY ts"),
                {"f": f, "a": t_from, "b": t_to})).all()
            if len(rows) < 300:
                continue
            ts = [x[0] for x in rows]
            hi = np.array([float(x[1]) for x in rows])
            lo = np.array([float(x[2]) for x in rows])
            cl_list = [float(x[3]) for x in rows]
            cl = np.array(cl_list)
            vl = np.array([float(x[4] or 0) for x in rows])
            n = len(ts)
            atr = atr_wilder(hi, lo, cl, 14)
            with np.errstate(invalid="ignore", divide="ignore"):
                ap = np.where(np.isfinite(atr) & (cl > 0), atr / cl, np.nan)
                ref = roll_mean(ap, BAND_REF)
                ratio = np.where(np.isfinite(ref) & (ref > 0) & np.isfinite(ap),
                                 ap / ref, 1.0)
            band_a = np.clip(20.0 * ratio, 15.0, 30.0)
            band_b = np.clip(20.0 * ratio, 20.0, 40.0)
            rmv = roll_mean(vl, 20)
            vol_ma = np.concatenate(([np.nan], rmv[:-1])) if n > 1 else rmv
            per[f] = dict(
                ticker=str(rr[0]) if rr else f,
                ts=ts, hi=hi, lo=lo, cl=cl, vl=vl,
                r=rsi_wilder(cl_list, 14),
                h1=macd_hist(cl_list),
                atr=atr, vol_ma=vol_ma, ratio=ratio, band_a=band_a,
                os_f=np.full(n, 30.0), ob_f=np.full(n, 70.0),
                os_a=50.0 - band_a, ob_a=50.0 + band_a,
                os_b=50.0 - band_b, ob_b=50.0 + band_b,
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

    bands = np.concatenate([d["band_a"][60:] for d in per.values()])
    print("адаптивная полоса ATR[15..30]: медиана %.1f | p10 %.1f | p90 %.1f | "
          "у ниж.клампа %.0f%% баров | у верх.клампа %.0f%%" % (
              float(np.median(bands)), float(np.percentile(bands, 10)),
              float(np.percentile(bands, 90)),
              100.0 * float((bands <= 15.001).mean()),
              100.0 * float((bands >= 29.999).mean())))
    print("  (os = 50 − band, ob = 50 + band; медианные пороги: %.0f/%.0f)" % (
        50 - float(np.median(bands)), 50 + float(np.median(bands))))

    entries = [
        ("фикс 30/70", "os_f", "ob_f"),
        ("ATR[15..30]", "os_a", "ob_a"),
        ("ATR[20..40]", "os_b", "ob_b"),
    ]
    exits = [("RSI", ("rsi",))]
    for sl in (1.5, 2.0, 2.5):
        for tp in (3.0, 3.5):
            exits.append(("SL%.1f/TP%.1f" % (sl, tp), ("sltp", sl, tp)))
            exits.append(("RSI+SL%.1f/TP%.1f" % (sl, tp), ("rsi+sltp", sl, tp)))

    print()
    print("=== СЕТКА (cost %.3f%%/стор, ~%.0f₽/поз, сессия 9:50–19:00, win=%dм) ===" % (
        100 * COST, NOTIONAL, WIN))
    grid = []
    base_flip = agg(run_variant(per, "os_f", "ob_f", ("flip",), 0.0, False))
    grid.append(("сырой MACD, вых=flip (якорь)", "сырой", "os_f", "ob_f",
                 "flip", ("flip",), base_flip))
    for ename, osk, obk in entries:
        for xname, exitk in exits:
            a = agg(run_variant(per, osk, obk, exitk))
            grid.append(("%s | %s" % (ename, xname), ename, osk, obk, xname, exitk, a))
    grid.sort(key=lambda g: -g[6]["net"])
    for label, _en, _os, _ob, _x, _ex, a in grid:
        print("  %-34s n=%4d wr=%5.1f%% net=%+9.2f avg=%+7.2f | L %3d/%+8.1f S %3d/%+8.1f | макс.одн %2d" % (
            label, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
            a["net"] / max(a["n"], 1),
            a["by_side"]["L"][0], a["by_side"]["L"][1],
            a["by_side"]["S"][0], a["by_side"]["S"][1], a["mx"]))
    print("  справка: прошлый победитель macd_rsi_sim «фикс 30/70 | RSI» = n=403 wr=64.5% net=+1187.35 — сверить со строкой выше")

    print()
    print("=== ФИЛЬТРЫ ВХОДА (vol ≥ k×MA20; ATR% ≥ g×MA96) на топ-5 + прошлый победитель ===")
    cand = [g for g in grid if g[1] != "сырой"][:5]
    prev = next((g for g in grid if g[0] == "фикс 30/70 | RSI"), None)
    if prev is not None and all(g[0] != prev[0] for g in cand):
        cand.append(prev)
    for label, ename, osk, obk, xname, exitk, a0 in cand:
        for vf in (1.2, 1.5):
            a = agg(run_variant(per, osk, obk, exitk, vol_f=vf))
            print("  %-38s vol>=%.1f×:  n=%4d wr=%5.1f%% net=%+9.2f   (без фильтра n=%d net=%+.2f)" % (
                label, vf, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
                a0["n"], a0["net"]))
        for agt in (0.9, 1.0):
            a = agg(run_variant(per, osk, obk, exitk, atr_gate=agt))
            print("  %-38s ATR>=%.1f×MA: n=%4d wr=%5.1f%% net=%+9.2f   (без фильтра n=%d net=%+.2f)" % (
                label, agt, a["n"], 100.0 * a["wins"] / max(a["n"], 1), a["net"],
                a0["n"], a0["net"]))

    print()
    print("=== ТОП-3 ПО NET: ДЕТАЛИ ===")
    for label, ename, osk, obk, xname, exitk, a in grid[:3]:
        wr = 100.0 * a["wins"] / max(a["n"], 1)
        print("--- %s: n=%d wr=%.1f%% net=%+.2f ---" % (label, a["n"], wr, a["net"]))
        print("  по дням: " + "  ".join("%s:%+7.0f" % (d, v)
                                       for d, v in sorted(a["by_day"].items())))
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
    print("маппинг в бота (variant-конфиг): sl_mult = SL×ATR, rr = TP/SL "
          "(atr_stop в app/engine/exits.py — формулу TP сверить); адаптивные пороги "
          "RSI и volume-фильтр входа в боте пока нет — добавим в ensemble_strategy.py, "
          "если выиграют в симе.")


if __name__ == "__main__":
    asyncio.run(main())
