#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trend_day_sim.py — стратегия «тренд дня»: утром вход по тренду, вечером выход.

Идея (пользователь):
  * с утра видно тренд каждой акции (старший ТФ, минутки не смотрим);
  * тренд есть -> вход по тренду; тренда нет -> вообще не входим;
  * тренд перевернулся внутри дня -> закрываем позицию сразу;
  * вечером закрываем всё, через ночь не переносим.

Варианты определения тренда (только ЗАКРЫТЫЕ бары до момента t):
  h1_ema     : EMA8 vs EMA24 на H1-закрытиях, нейтральная зона 0.15%
  h1_macd    : MACD(12,26,9) на H1, macd vs signal, зона 0.1% цены
  daily_ema  : вчерашнее закрытие vs EMA5 дневных закрытий, зона 0.1%
  daily_x_h1 : совпадение daily_ema и h1_ema (вход при обоих; flip по H1)
  oracle     : (lookahead) знак хода дня 10:05 -> 18:35 — верхняя граница
  raw_long   : (контроль) всегда long — ценность фильтра тренда

Модель: вход по close 1м-бара (10:05 МСК); выход — flip (первый 1м-бар после
закрытия H1-бара, где тренд перевернулся) или EOD 18:35 МСК. Комиссия
0.045%/сторона (как в live-сделках бота: CHMF 10@649.20 -> 647.27,
costs 5.84 = 0.045% x 2). Позиция: фикс. нотион 40000 RUB (лот-округление
вниз), шорты разрешены (маржа как у бота).

Данные: live-БД .2 (read-only SELECT), 1м-свечи с 2026-05-01 (warmup),
окно теста 01.07 - 21.09.2026 МСК.
Запуск: ssh Denis@192.168.1.2 'cd ~/Dev/Deeptrading/backend &&
        .venv/bin/python3 scripts/trend_day_sim.py'
"""

import asyncio
import bisect
import json
from collections import defaultdict
from datetime import datetime, time as dtime, timedelta, timezone

MSK = timezone(timedelta(hours=3))

FROM_UTC = datetime(2026, 5, 1, tzinfo=timezone.utc)   # тёплый буфер
W_START_MSK = datetime(2026, 7, 1, tzinfo=MSK)         # окно теста
W_END_MSK = datetime(2026, 9, 22, tzinfo=MSK)          # по 21.09 включительно

ENTRY_HM = dtime(10, 5)                                # вход утром
EOD_HM = dtime(18, 35)                                 # вечерний выход
DAY_CLOSE_HM = dtime(18, 40)                           # «закрытие дня» для daily-тренда

NEUTRAL_H1 = 0.0015      # нейтральная зона EMA8/EMA24 на H1 (0.15%)
NEUTRAL_D = 0.0010       # нейтральная зона цена vs EMA5 (день)
NEUTRAL_MACD = 0.0010    # нейтральная зона MACD-гистограммы (доля цены)
POSITION_NOTIONAL = 40000.0
COMMISSION = 0.00045     # 0.045% за сторону

FLIP_VARIANTS = ("h1_ema", "h1_macd", "daily_x_h1")
VARIANTS = ("h1_ema", "h1_macd", "daily_ema", "daily_x_h1", "oracle", "raw_long")


# ---------- индикаторы ----------

def ema_series(lst, n):
    k = 2.0 / (n + 1.0)
    e = lst[0]
    out = [e]
    for v in lst[1:]:
        e = v * k + e * (1.0 - k)
        out.append(e)
    return out


def trend_h1_ema(closes):
    """(+1/-1/0, готов?) по EMA8 vs EMA24 на H1-закрытиях."""
    if len(closes) < 24:
        return 0, False
    e8 = ema_series(closes, 8)[-1]
    e24 = ema_series(closes, 24)[-1]
    if e8 > e24 * (1.0 + NEUTRAL_H1):
        return 1, True
    if e8 < e24 * (1.0 - NEUTRAL_H1):
        return -1, True
    return 0, True


def trend_h1_macd(closes):
    """(+1/-1/0, готов?) по MACD(12,26,9) на H1."""
    if len(closes) < 35:
        return 0, False
    e12 = ema_series(closes, 12)
    e26 = ema_series(closes, 26)
    macd = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(macd, 9)
    d = macd[-1] - sig[-1]
    p = closes[-1]
    if d > NEUTRAL_MACD * p:
        return 1, True
    if d < -NEUTRAL_MACD * p:
        return -1, True
    return 0, True


def trend_daily(closes):
    """(+1/-1/0, готов?) по закрытию дня vs EMA5 дневных закрытий."""
    if len(closes) < 6:
        return 0, False
    e5 = ema_series(closes, 5)[-1]
    p = closes[-1]
    if p > e5 * (1.0 + NEUTRAL_D):
        return 1, True
    if p < e5 * (1.0 - NEUTRAL_D):
        return -1, True
    return 0, True


# ---------- ресемплинг ----------

def build_h1(mins):
    """[(msk_dt, close)] -> [(hour_end_msk, close)] — H1-закрытия."""
    out = []
    cur_key = None
    cur_close = None
    for ts, c in mins:
        key = (ts.date(), ts.hour)
        if key != cur_key:
            if cur_key is not None:
                d, h = cur_key
                out.append((datetime.combine(d, dtime(h), tzinfo=MSK) + timedelta(hours=1), cur_close))
            cur_key = key
            cur_close = c
        else:
            cur_close = c
    if cur_key is not None:
        d, h = cur_key
        out.append((datetime.combine(d, dtime(h), tzinfo=MSK) + timedelta(hours=1), cur_close))
    return out


def build_daily(mins):
    """[(msk_dt, close)] -> [(date, close)] — закрытие дня = последний 1м-бар <= 18:40 МСК."""
    last_any = {}
    to_close = {}
    for ts, c in mins:
        d = ts.date()
        last_any[d] = c
        if ts.time() <= DAY_CLOSE_HM:
            to_close[d] = c
    res = {}
    for d, c in last_any.items():
        res[d] = to_close.get(d, c)
    return sorted(res.items())


def qty_for(price, lot):
    if price <= 0 or lot <= 0:
        return 0
    n = int(POSITION_NOTIONAL // (price * lot))
    if n < 1:
        return 0
    return n * lot


# ---------- основной цикл ----------

async def main():
    from sqlalchemy import text
    from app.database import SessionLocal

    figis = []
    tickers = {}
    lots = {}
    async with SessionLocal() as db:
        rows = (await db.execute(text(
            "SELECT figi FROM candles WHERE interval=1 AND ts >= '2026-07-01' "
            "GROUP BY figi HAVING count(DISTINCT ts::date) >= 40 ORDER BY figi"
        ))).all()
        figis = [r[0] for r in rows]
        try:
            irows = (await db.execute(text("SELECT figi, ticker, lot FROM instruments"))).all()
            for f, t, l in irows:
                tickers[f] = t
                lots[f] = int(l or 1)
        except Exception:
            await db.rollback()
            irows = (await db.execute(text("SELECT figi, ticker FROM instruments"))).all()
            for f, t in irows:
                tickers[f] = t
                lots[f] = 1

    print(f"universe: {len(figis)} figis (>=40 дней 1м с 01.07)")

    trades = {v: [] for v in VARIANTS}
    stats = {v: {"no_trend": 0, "warmup": 0, "figi_days": 0} for v in VARIANTS}

    async with SessionLocal() as db:
        for fi, f in enumerate(figis):
            rows = (await db.execute(text(
                "SELECT ts, close::float8 FROM candles "
                "WHERE interval=1 AND figi = :f AND ts >= :from_ ORDER BY ts"
            ), {"f": f, "from_": FROM_UTC})).all()
            if len(rows) < 2000:
                continue
            mins = [(r[0].astimezone(MSK), float(r[1])) for r in rows]
            h1 = build_h1(mins)
            daily = build_daily(mins)
            h1_ends = [he for (he, _c) in h1]
            h1_cl = [c for (_he, c) in h1]

            by_day = defaultdict(list)
            for ts, c in mins:
                by_day[ts.date()].append((ts, c))

            lot = lots.get(f, 1)
            for day, bars in sorted(by_day.items()):
                d_start = datetime.combine(day, dtime(0, 0), tzinfo=MSK)
                if not (W_START_MSK <= d_start < W_END_MSK):
                    continue
                entry_cut = datetime.combine(day, ENTRY_HM, tzinfo=MSK)
                eod_cut = datetime.combine(day, EOD_HM, tzinfo=MSK)
                tb = [b for b in bars if entry_cut <= b[0] <= eod_cut]
                if len(tb) < 30:
                    continue
                entry_bar = tb[0]
                exit_eod = tb[-1]
                entry_price = entry_bar[1]

                i_entry = bisect.bisect_right(h1_ends, entry_cut)
                h1_closes_entry = h1_cl[:i_entry]
                daily_closes_entry = [c for (d, c) in daily if d < day]

                t_h1e, ok1 = trend_h1_ema(h1_closes_entry)
                t_h1m, ok2 = trend_h1_macd(h1_closes_entry)
                t_d, ok3 = trend_daily(daily_closes_entry)
                if not (ok1 and ok2 and ok3):
                    for v in VARIANTS:
                        stats[v]["warmup"] += 1
                    continue

                # H1-события внутри дня для flip-мониторинга
                h1_events = [he for he in h1_ends if entry_cut < he <= eod_cut]

                oracle_dir = 1 if exit_eod[1] > entry_price else (-1 if exit_eod[1] < entry_price else 0)
                dirs = {
                    "h1_ema": t_h1e,
                    "h1_macd": t_h1m,
                    "daily_ema": t_d,
                    "daily_x_h1": (t_d if (t_d != 0 and t_d == t_h1e) else 0),
                    "oracle": oracle_dir,
                    "raw_long": 1,
                }

                for v in VARIANTS:
                    stats[v]["figi_days"] += 1
                    d_ = dirs[v]
                    if d_ == 0:
                        stats[v]["no_trend"] += 1
                        continue
                    qty = qty_for(entry_price, lot)
                    if qty <= 0:
                        continue

                    exit_bar = exit_eod
                    reason = "eod"
                    if v in FLIP_VARIANTS:
                        for he in h1_events:
                            idx = bisect.bisect_right(h1_ends, he)
                            closes_now = h1_cl[:idx]
                            if v == "h1_ema":
                                tv, _ = trend_h1_ema(closes_now)
                                flip = (tv == -d_)
                            elif v == "h1_macd":
                                tv, _ = trend_h1_macd(closes_now)
                                flip = (tv == -d_)
                            else:  # daily_x_h1: H1-компонент перевернулся против дневного
                                tvh, _ = trend_h1_ema(closes_now)
                                flip = (tvh == -t_d)
                            if flip:
                                cand = [b for b in tb if b[0] >= he]
                                if cand:
                                    exit_bar = cand[0]
                                    reason = "flip"
                                break

                    exit_price = exit_bar[1]
                    gross = d_ * (exit_price - entry_price) * qty
                    cost = COMMISSION * qty * (entry_price + exit_price)
                    pnl = gross - cost
                    hold_min = (exit_bar[0] - entry_bar[0]).total_seconds() / 60.0
                    trades[v].append({
                        "figi": f, "ticker": tickers.get(f, f), "day": day.isoformat(),
                        "side": "L" if d_ > 0 else "S", "qty": qty,
                        "entry": round(entry_price, 4), "exit": round(exit_price, 4),
                        "pnl": round(pnl, 2), "reason": reason, "hold_min": int(hold_min),
                    })
            if (fi + 1) % 10 == 0:
                print(f"  ...{fi + 1}/{len(figis)} figis")

    # ---------- агрегаты ----------
    results = {}
    for v in VARIANTS:
        tl = trades[v]
        net = sum(t["pnl"] for t in tl)
        wins = [t for t in tl if t["pnl"] > 0]
        by_day = defaultdict(float)
        by_tkr = defaultdict(float)
        by_side = defaultdict(lambda: [0, 0.0])
        reasons = defaultdict(int)
        for t in tl:
            by_day[t["day"]] += t["pnl"]
            by_tkr[t["ticker"]] += t["pnl"]
            by_side[t["side"]][0] += 1
            by_side[t["side"]][1] += t["pnl"]
            reasons[t["reason"]] += 1
        days_sorted = sorted(by_day.items())
        cum = 0.0
        peak = 0.0
        maxdd = 0.0
        for _d, x in days_sorted:
            cum += x
            peak = max(peak, cum)
            maxdd = max(maxdd, peak - cum)
        results[v] = {
            "trades": len(tl),
            "net": round(net, 2),
            "winrate": round(len(wins) / len(tl), 3) if tl else 0.0,
            "avg": round(net / len(tl), 2) if tl else 0.0,
            "avg_hold_min": round(sum(t["hold_min"] for t in tl) / len(tl), 1) if tl else 0.0,
            "long": by_side.get("L", [0, 0.0]),
            "short": by_side.get("S", [0, 0.0]),
            "exit": dict(reasons),
            "no_trend_figi_days": stats[v]["no_trend"],
            "warmup_figi_days": stats[v]["warmup"],
            "figi_days": stats[v]["figi_days"],
            "days_traded": len(days_sorted),
            "best_day": max(days_sorted, key=lambda x: x[1]) if days_sorted else None,
            "worst_day": min(days_sorted, key=lambda x: x[1]) if days_sorted else None,
            "maxdd_day_curve": round(maxdd, 2),
            "top5_tickers": sorted(by_tkr.items(), key=lambda x: -x[1])[:5],
            "bottom5_tickers": sorted(by_tkr.items(), key=lambda x: x[1])[:5],
            "by_day": days_sorted,
        }

    # ---------- вывод ----------
    print()
    print(f"окно {W_START_MSK:%d.%m} - {W_END_MSK:%d.%m} МСК | figi-days: {stats['h1_ema']['figi_days']} | "
          f"вход {ENTRY_HM}, EOD {EOD_HM} | комиссия {COMMISSION * 200:.3f}% / сделка | нотион {POSITION_NOTIONAL:.0f}")
    hdr = (f"{'вариант':12s} {'сделок':>7s} {'нетто R':>10s} {'win%':>5s} {'avg R':>8s} "
           f"{'hold м':>6s} {'лонг':>13s} {'шорт':>13s} {'flip':>5s} {'без тренда':>10s} {'maxDD R':>9s}")
    print(hdr)
    for v in VARIANTS:
        r = results[v]
        L = r["long"]
        S = r["short"]
        print(f"{v:12s} {r['trades']:7d} {r['net']:10.0f} {r['winrate'] * 100:5.1f} {r['avg']:8.1f} "
              f"{r['avg_hold_min']:6.0f} {L[0]:5d}/{L[1]:7.0f} {S[0]:5d}/{S[1]:7.0f} "
              f"{r['exit'].get('flip', 0):5d} {r['no_trend_figi_days']:10d} {r['maxdd_day_curve']:9.0f}")

    print()
    for v in ("h1_ema", "daily_x_h1", "oracle"):
        r = results[v]
        print(f"{v}: лучший день {r['best_day']}, худший {r['worst_day']}")
        print(f"   топ-5 тикеров: {[(t, round(x)) for t, x in r['top5_tickers']]}")
        print(f"   дно-5 тикеров: {[(t, round(x)) for t, x in r['bottom5_tickers']]}")

    out = {
        "params": {
            "window_msk": [W_START_MSK.isoformat(), W_END_MSK.isoformat()],
            "entry": ENTRY_HM.isoformat(), "eod": EOD_HM.isoformat(),
            "neutral_h1": NEUTRAL_H1, "neutral_daily": NEUTRAL_D,
            "neutral_macd": NEUTRAL_MACD,
            "position_notional": POSITION_NOTIONAL, "commission_side": COMMISSION,
            "universe": len(figis),
        },
        "summary": {v: {k: val for k, val in results[v].items() if k != "by_day"} for v in VARIANTS},
        "by_day": {v: results[v]["by_day"] for v in VARIANTS},
        "trades": trades,
    }
    with open("scripts/results_trend_day.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\nsaved -> scripts/results_trend_day.json")


if __name__ == "__main__":
    asyncio.run(main())
