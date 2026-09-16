"""Анализ устойчивости моментума: как долго тикеры держатся в лидерах/аутсайдерах.

Метрики (по дневным барам):
  1) Автокорреляция ранга: corr(rank(t), rank(t+N)) при N = 5/10/21/42/63 дней.
  2) Overlap топ-3/низ-3: сколько имён из группы осталось через N дней.
  3) Средняя длина «спэлла» (непрерывных дней в топ-3/низ-3).
  4) Форвардные доходности: средняя доходность низ-3 и топ-3 за 1/5/21 день вперёд
     (кого выгоднее держать и на каком горизонте).

Запуск: python scripts/analyze_momentum_persistence.py [--n 63] [--k 3] [--days 300]
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


async def load(days: int):
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    rows = await c.fetch("""
        SELECT i.ticker, c.ts, c.close FROM candles c
        JOIN instruments i ON i.figi = c.figi
        WHERE c.interval = 24 AND i.class_code = 'TQBR' ORDER BY c.ts, i.ticker""")
    await c.close()
    msk = ZoneInfo("Europe/Moscow")
    today = datetime.now(timezone.utc).astimezone(msk).date()
    by_day: dict = defaultdict(dict)
    for r in rows:
        d = r["ts"].astimezone(msk).date()
        if d == today:
            continue
        by_day[d][str(r["ticker"])] = float(r["close"] or 0)
    days_sorted = sorted(by_day)[-days:]
    return days_sorted, by_day


def spearman(a: dict, b: dict) -> float:
    common = sorted(set(a) & set(b))
    if len(common) < 5:
        return 0.0
    ra = {t: i for i, t in enumerate(sorted(common, key=lambda x: a[x]))}
    rb = {t: i for i, t in enumerate(sorted(common, key=lambda x: b[x]))}
    n = len(common)
    ma = sum(ra.values()) / n
    mb = sum(rb.values()) / n
    cov = sum((ra[t] - ma) * (rb[t] - mb) for t in common)
    va = sum((ra[t] - ma) ** 2 for t in common) ** 0.5
    vb = sum((rb[t] - mb) ** 2 for t in common) ** 0.5
    return cov / (va * vb) if va and vb else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=63)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--days", type=int, default=300)
    args = ap.parse_args()

    days_sorted, by_day = asyncio.run(load(args.days))
    n = args.n
    k = args.k
    # моментум по дням
    moms: dict = {}
    for i in range(n, len(days_sorted)):
        d = days_sorted[i]
        prev = days_sorted[i - n]
        m = {}
        for t, c1 in by_day[d].items():
            c0 = by_day[prev].get(t)
            if c0 and c0 > 0 and c1 > 0:
                m[t] = c1 / c0 - 1.0
        if len(m) >= 6:
            moms[d] = m
    dates = sorted(moms)
    print(f"дней с моментумом: {len(dates)} | тикеров в среднем: "
          f"{sum(len(m) for m in moms.values()) / max(1, len(moms)):.0f}\n")

    # 1) автокорреляция ранга
    print("Автокорреляция ранга (Spearman), N дней вперёд:")
    for lag in (5, 10, 21, 42, 63):
        vals = []
        for i in range(len(dates) - lag):
            vals.append(spearman(moms[dates[i]], moms[dates[i + lag]]))
        if vals:
            print(f"  +{lag:>3} дн.: {sum(vals)/len(vals):+.2f}")

    # 2) overlap топ/низ-k
    print(f"\nOverlap групп (топ-{k} и низ-{k}), доля сохранившихся:")
    for lag in (5, 10, 21, 42, 63):
        up_ov, dn_ov = [], []
        for i in range(len(dates) - lag):
            a, b = moms[dates[i]], moms[dates[i + lag]]
            ra = sorted(a, key=lambda x: -a[x])
            rb = sorted(b, key=lambda x: -b[x])
            up_ov.append(len(set(ra[:k]) & set(rb[:k])) / k)
            dn_ov.append(len(set(ra[-k:]) & set(rb[-k:])) / k)
        if up_ov:
            print(f"  +{lag:>3} дн.: лидеры {sum(up_ov)/len(up_ov)*100:.0f}% | "
                  f"аутсайдеры {sum(dn_ov)/len(dn_ov)*100:.0f}%")

    # 3) средние спэллы
    def spells(group: str) -> float:
        cur: dict[str, int] = defaultdict(int)
        lens: list[int] = []
        prev_set: set = set()
        for d in dates:
            m = moms[d]
            r = sorted(m, key=lambda x: -m[x])
            s = set(r[:k]) if group == "up" else set(r[-k:])
            for t in list(cur):
                if t not in s:
                    lens.append(cur.pop(t))
            for t in s:
                cur[t] = cur.get(t, 0) + 1
            prev_set = s
        lens.extend(cur.values())
        return sum(lens) / len(lens) if lens else 0.0

    print(f"\nСредний «спэлл» (дней подряд в группе): лидеры {spells('up'):.1f} | "
          f"аутсайдеры {spells('dn'):.1f}")

    # 4) форвардные доходности групп
    print("\nСредняя форвардная доходность группы (за день/5/21 вперёд), %:")
    for group in ("up", "dn"):
        row = []
        for fwd in (1, 5, 21):
            vals = []
            for i in range(len(dates) - fwd):
                m = moms[dates[i]]
                r = sorted(m, key=lambda x: -m[x])
                picks = r[:k] if group == "up" else r[-k:]
                rets = []
                for t in picks:
                    c0 = by_day[dates[i]].get(t)
                    c1 = by_day[dates[i + fwd]].get(t)
                    if c0 and c1 and c0 > 0:
                        rets.append(c1 / c0 - 1.0)
                if rets:
                    vals.append(sum(rets) / len(rets))
            row.append(f"+{fwd}д: {sum(vals)/len(vals)*100:+.2f}%" if vals else f"+{fwd}д: —")
        print(f"  {'лидеры' if group == 'up' else 'аутсайдеры':<12}" + " | ".join(row))


if __name__ == "__main__":
    main()
