"""Тест моментум-портфеля: лонг лидеров / шорт аутсайдеров, выход вечером.

Логика (без SL/TP — вечер = выход):
  1. На день D считаем моментум по дневным барам до D-1 (без look-ahead):
     chg = close(D-1)/close(D-1-N) - 1.
  2. Топ-K → LONG на открытии дня D, низ-K → SHORT.
  3. Выход — закрытие дня D (вечер).
  4. Размер: equity × pos_pct × lev / K на позицию; плечо = 1/риск-ставка (dlong/dshort),
     ограничено --max-lev. Издержки 0.05%/сторону + 2 б.п.

Запуск: python scripts/test_momentum_ls.py [--n 21] [--k 5] [--side ls|long|short]
        [--pos-pct 0.5] [--max-lev 3] [--days 300]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COMMISSION = 0.0005
SLIP = 0.0002


async def load(days: int):
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    rows = await c.fetch("""
        SELECT c.figi, i.ticker, c.ts, c.open, c.high, c.low, c.close,
               coalesce(i.dlong, 0) AS dlong, coalesce(i.dshort, 0) AS dshort
        FROM candles c JOIN instruments i ON i.figi = c.figi
        WHERE c.interval = 24 AND i.class_code = 'TQBR' ORDER BY c.figi, c.ts""")
    await c.close()
    msk = ZoneInfo("Europe/Moscow")
    today = datetime.now(timezone.utc).astimezone(msk).date()
    by_day: dict = defaultdict(dict)   # date -> ticker -> bar
    meta: dict = {}
    for r in rows:
        d = r["ts"].astimezone(msk).date()
        if d == today:
            continue
        t = str(r["ticker"])
        by_day[d][t] = {"o": float(r["open"] or 0), "h": float(r["high"] or 0),
                        "l": float(r["low"] or 0), "c": float(r["close"] or 0)}
        meta[t] = {"dlong": float(r["dlong"] or 0), "dshort": float(r["dshort"] or 0)}
    days_sorted = sorted(by_day)[-days:]
    return days_sorted, by_day, meta


def lev_for(meta: dict, ticker: str, side: str, max_lev: float) -> float:
    m = meta.get(ticker) or {}
    risk = float(m.get("dshort") if side == "SHORT" else m.get("dlong") or 0.0)
    lev = (1.0 / risk) if 0 < risk < 1 else 1.0
    return max(1.0, min(lev, max_lev))


def run(days_sorted, by_day, meta, *, n=21, k=5, side="ls", pos_pct=0.5,
        max_lev=3.0, equity0=50000.0, stop_pct=0.0, hold="oc") -> dict:
    """hold: "oc" = вход на открытии, выход на закрытии дня;
             "cc" = вход на закрытии дня D, выход на закрытии D+1."""
    equity = equity0
    curve = [equity]
    trades = []
    for i in range(n, len(days_sorted)):
        d = days_sorted[i]
        prev_days = days_sorted[max(0, i - n):i]
        if len(prev_days) < n:
            continue
        today = by_day[d]
        # моментум по close(D-1)/close(D-1-N)
        mom = {}
        for t, bar in today.items():
            try:
                c1 = by_day[prev_days[-1]].get(t, {}).get("c")
                c0 = by_day[prev_days[0]].get(t, {}).get("c")
                if c1 and c0 and c0 > 0 and bar["o"] > 0 and bar["c"] > 0:
                    mom[t] = c1 / c0 - 1.0
            except Exception:
                continue
        if len(mom) < 2 * k:
            continue
        ranked = sorted(mom.items(), key=lambda x: -x[1])
        picks = []
        if side in ("ls", "long"):
            picks += [(t, "LONG") for t, _ in ranked[:k]]
        if side in ("ls", "short"):
            picks += [(t, "SHORT") for t, _ in ranked[-k:]]
        if not picks:
            continue
        per = equity * pos_pct / max(1, len(picks))   # свои средства на позицию
        for t, sd in picks:
            bar = today.get(t)
            if not bar:
                continue
            lev = lev_for(meta, t, sd, max_lev)
            if hold == "cc":
                # вход на закрытии дня D, выход на закрытии D+1
                if i + 1 >= len(days_sorted):
                    continue
                nxt = by_day[days_sorted[i + 1]].get(t)
                if not nxt:
                    continue
                entry, exit_ = bar["c"], nxt["c"]
                hi, lo = nxt["h"], nxt["l"]
            else:
                entry, exit_ = bar["o"], bar["c"]
                hi, lo = bar["h"], bar["l"]
            if entry <= 0:
                continue
            notional = per * lev
            qty = int(notional / entry)
            if qty < 1:
                continue
            # стоп внутри дня (по high/low): шорт — вверх, лонг — вниз
            if stop_pct > 0:
                if sd == "SHORT" and hi >= entry * (1 + stop_pct):
                    exit_ = entry * (1 + stop_pct)
                elif sd == "LONG" and lo <= entry * (1 - stop_pct):
                    exit_ = entry * (1 - stop_pct)
            pnl = (exit_ - entry) * qty if sd == "LONG" else (entry - exit_) * qty
            cost = (entry + exit_) * qty * (COMMISSION + SLIP)
            net = pnl - cost
            equity += net
            trades.append({"date": str(d), "ticker": t, "side": sd, "qty": qty,
                           "entry": entry, "exit": exit_, "net": net})
        curve.append(equity)
    n_tr = len(trades)
    gw = sum(x["net"] for x in trades if x["net"] > 0)
    gl = sum(x["net"] for x in trades if x["net"] < 0)
    wins = sum(1 for x in trades if x["net"] > 0)
    peak = curve[0]
    mdd = 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, (e - peak) / peak)
    by_t: dict[str, float] = defaultdict(float)
    for x in trades:
        by_t[x["ticker"]] += x["net"]
    return {"n": n_tr, "net": round(equity - equity0, 1), "wr": round(wins / n_tr * 100, 1) if n_tr else 0,
            "pf": round(gw / abs(gl), 2) if gl else 0.0, "gw": round(gw, 1), "gl": round(gl, 1),
            "maxdd": round(mdd * 100, 1), "days": len(days_sorted),
            "by_ticker": dict(by_t), "ret_pct": round((equity / equity0 - 1) * 100, 1)}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=21, help="окно моментума (торговых дней)")
    ap.add_argument("--k", type=int, default=5, help="сколько имён на сторону")
    ap.add_argument("--side", default="ls", choices=("ls", "long", "short"))
    ap.add_argument("--pos-pct", type=float, default=0.5)
    ap.add_argument("--max-lev", type=float, default=3.0)
    ap.add_argument("--days", type=int, default=300)
    ap.add_argument("--equity", type=float, default=50000.0)
    ap.add_argument("--stop", type=float, default=0.0, help="внутридневной стоп, доля (0.03)")
    ap.add_argument("--hold", default="oc", choices=("oc", "cc"))
    ap.add_argument("--grid", action="store_true", help="сетка: шорт × плечо × стоп")
    args = ap.parse_args()

    days_sorted, by_day, meta = await load(args.days)
    print(f"дней: {len(days_sorted)} | тикеров: {len(meta)}\n")
    if args.grid:
        print(f"{'Вариант':<34}{'N':>6}{'Net':>10}{'Ret%':>8}{'WR%':>7}{'PF':>6}{'MaxDD%':>8}")
        for lev in (1.0, 1.5, 2.0, 3.0):
            for stop in (0.0, 0.03, 0.05):
                for hold in ("oc", "cc"):
                    r = run(days_sorted, by_day, meta, n=63, k=5, side="short",
                            pos_pct=args.pos_pct, max_lev=lev, equity0=args.equity,
                            stop_pct=stop, hold=hold)
                    print(f"{f'short n63 k5 lev{lev:g} stop{stop:.0%} {hold}':<34}"
                          f"{r['n']:>6}{r['net']:>10.1f}{r['ret_pct']:>8.1f}"
                          f"{r['wr']:>7.1f}{r['pf']:>6.2f}{r['maxdd']:>8.1f}")
        return
    print(f"{'Вариант':<34}{'N':>6}{'Net':>10}{'Ret%':>8}{'WR%':>7}{'PF':>6}{'MaxDD%':>8}")
    for n in (args.n, 63):
        for k in (3, 5, 10):
            for side in ("long", "short", "ls"):
                r = run(days_sorted, by_day, meta, n=n, k=k, side=side,
                        pos_pct=args.pos_pct, max_lev=args.max_lev, equity0=args.equity)
                name = f"n{n} k{k} {side}"
                print(f"{name:<34}{r['n']:>6}{r['net']:>10.1f}{r['ret_pct']:>8.1f}"
                      f"{r['wr']:>7.1f}{r['pf']:>6.2f}{r['maxdd']:>8.1f}")
    # лучший вариант — по тикерам
    best = max(
        ((n, k, s, run(days_sorted, by_day, meta, n=n, k=k, side=s,
                       pos_pct=args.pos_pct, max_lev=args.max_lev, equity0=args.equity))
         for n in (args.n, 63) for k in (3, 5, 10) for s in ("long", "short", "ls")),
        key=lambda x: x[3]["net"])
    print(f"\nлучший: n{best[0]} k{best[1]} {best[2]} → {best[3]['net']:+.1f}₽ "
          f"({best[3]['ret_pct']:+.1f}%)")
    print("по тикерам:", ", ".join(f"{t} {v:+.0f}" for t, v in
                                   sorted(best[3]["by_ticker"].items(), key=lambda x: -x[1])[:10]))


if __name__ == "__main__":
    asyncio.run(main())
