"""Скрининг тренда: кто вырос за месяц/квартал и какова их внутридневная волатильность.

Считает по дневным барам (interval=24) и 5м свечам:
  - chg_21d / chg_63d: изменение за 21/63 торговых дня, %
  - dd_from_high: просадка от максимума за 63 дня, %
  - atr_pct: дневной ATR% (волатильность дня)
  - rng_5d: средний внутридневной размах 5м баров за 5 дней, % (сколько «ходит» за день)

Запуск: python scripts/screen_trend.py [--top 15] [--min-chg 50]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


async def load():
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    rows = await c.fetch("""
        SELECT c.figi, i.ticker, c.ts, c.open, c.high, c.low, c.close
        FROM candles c JOIN instruments i ON i.figi = c.figi
        WHERE c.interval = 24 AND i.class_code = 'TQBR'
        ORDER BY c.figi, c.ts""")
    m5 = await c.fetch("""
        SELECT figi, ts, high, low, close FROM candles
        WHERE interval = 5 AND ts > now() - interval '7 days'""")
    await c.close()
    daily: dict[str, list] = defaultdict(list)
    names: dict[str, str] = {}
    for r in rows:
        f = str(r["figi"])
        names[f] = str(r["ticker"])
        daily[f].append((r["ts"], float(r["open"] or 0), float(r["high"] or 0),
                         float(r["low"] or 0), float(r["close"] or 0)))
    intraday: dict[str, list] = defaultdict(list)
    for r in m5:
        try:
            hi, lo, cl = float(r["high"] or 0), float(r["low"] or 0), float(r["close"] or 0)
            if cl > 0 and hi > 0 and lo > 0:
                intraday[str(r["figi"])].append((hi - lo) / cl * 100.0)
        except Exception:
            continue
    return daily, names, intraday


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-chg", type=float, default=50.0)
    args = ap.parse_args()

    daily, names, intraday = asyncio.run(load())
    out = []
    for figi, bars in daily.items():
        if len(bars) < 70:
            continue
        closes = [b[4] for b in bars]
        c_now = closes[-1]
        c21 = closes[-22] if len(closes) >= 22 else closes[0]
        c63 = closes[-64] if len(closes) >= 64 else closes[0]
        hi63 = max(b[2] for b in bars[-63:])
        trs = []
        for i in range(1, len(bars)):
            trs.append(max(bars[i][2] - bars[i][3],
                           abs(bars[i][2] - bars[i - 1][4]),
                           abs(bars[i][3] - bars[i - 1][4])))
        atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else 0.0
        rng = intraday.get(figi) or []
        out.append({
            "ticker": names.get(figi, figi[-6:]),
            "price": round(c_now, 2),
            "chg21": round((c_now / c21 - 1) * 100, 1) if c21 else 0.0,
            "chg63": round((c_now / c63 - 1) * 100, 1) if c63 else 0.0,
            "dd": round((c_now / hi63 - 1) * 100, 1) if hi63 else 0.0,
            "atr_pct": round(atr / c_now * 100, 2) if c_now else 0.0,
            "rng5": round(sum(rng) / len(rng), 2) if rng else 0.0,
        })
    out.sort(key=lambda x: -x["chg21"])
    print(f"всего тикеров с дневными барами: {len(out)}\n")
    print(f"{'Тикер':<7}{'Цена':>9}{'21д%':>8}{'63д%':>8}{'от макс%':>10}"
          f"{'ATRдн%':>8}{'размах5м%':>10}")
    for r in out[: args.top]:
        print(f"{r['ticker']:<7}{r['price']:>9.2f}{r['chg21']:>8.1f}{r['chg63']:>8.1f}"
              f"{r['dd']:>10.1f}{r['atr_pct']:>8.2f}{r['rng5']:>10.2f}")
    print(f"\n--- худшие {args.top}:")
    for r in out[-args.top:]:
        print(f"{r['ticker']:<7}{r['price']:>9.2f}{r['chg21']:>8.1f}{r['chg63']:>8.1f}"
              f"{r['dd']:>10.1f}{r['atr_pct']:>8.2f}{r['rng5']:>10.2f}")
    n_up = sum(1 for r in out if r["chg21"] >= args.min_chg)
    n_dbl = sum(1 for r in out if r["chg21"] >= 80)
    n_dn = sum(1 for r in out if r["chg21"] <= -args.min_chg)
    print(f"\nвыросших за месяц ≥{args.min_chg:.0f}%: {n_up} | из них ≥80%: {n_dbl} | "
          f"упавших ≥{args.min_chg:.0f}%: {n_dn}")
    if n_up:
        ups = [r for r in out if r["chg21"] >= args.min_chg]
        print("средний дневной ATR% у выросших: %.2f | средний размах 5м за день: %.2f%%" % (
            sum(r["atr_pct"] for r in ups) / len(ups),
            sum(r["rng5"] for r in ups) / len(ups)))


if __name__ == "__main__":
    main()
