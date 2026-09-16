"""Тест MTF-фильтров (дневной bias, H1-подтверждение, M5-триггер) на истории сделок.

Для каждой сделки пересчитываем MACD-состояния НА МОМЕНТ ВХОДА:
  - дневной bias (interval=24)
  - H1 MACD (из 5м свечей, последнее закрытие часа)
  - M5 MACD (5м свечи)
и применяем фильтры. Сравниваем Net/PF/WR/сделки.

Запуск: python scripts/test_mtf_filters.py [--days 120]
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import sys
from collections import defaultdict

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from app.bot.daily_bias import bias_from_closes, macd_state


async def load(days: int):
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    trades = await c.fetch("""
        SELECT ticker, figi, side, entry_time, net_pnl
        FROM sandbox_trades
        WHERE net_pnl IS NOT NULL AND exit_time IS NOT NULL AND entry_time IS NOT NULL
          AND side IN ('BUY', 'SELL')
        ORDER BY entry_time""")
    figis = sorted({str(t["figi"]) for t in trades})
    m5_rows = await c.fetch("""
        SELECT figi, ts, close FROM candles
        WHERE interval = 5 AND figi = ANY($1) ORDER BY figi, ts""", figis)
    d_rows = await c.fetch("""
        SELECT figi, ts, close FROM candles
        WHERE interval = 24 AND figi = ANY($1) ORDER BY figi, ts""", figis)
    await c.close()
    m5: dict[str, tuple[list, list]] = {}
    tmp = defaultdict(list)
    for r in m5_rows:
        tmp[str(r["figi"])].append((r["ts"], float(r["close"] or 0.0)))
    for f, seq in tmp.items():
        m5[f] = ([x[0] for x in seq], [x[1] for x in seq])
    daily: dict[str, tuple[list, list]] = {}
    tmpd = defaultdict(list)
    for r in d_rows:
        tmpd[str(r["figi"])].append((r["ts"], float(r["close"] or 0.0)))
    for f, seq in tmpd.items():
        daily[f] = ([x[0] for x in seq], [x[1] for x in seq])
    return trades, m5, daily


def h1_closes(ts_list: list, closes: list) -> list[float]:
    out: list[float] = []
    bucket = None
    last = None
    for ts, c in zip(ts_list, closes):
        b = ts.replace(minute=0, second=0, microsecond=0)
        if bucket is not None and b != bucket and last is not None:
            out.append(last)
        bucket, last = b, c
    if last is not None:
        out.append(last)
    return out


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "net": 0.0, "wr": 0.0, "pf": 0.0, "gw": 0.0, "gl": 0.0}
    gw = sum(r["net"] for r in rows if r["net"] > 0)
    gl = sum(r["net"] for r in rows if r["net"] < 0)
    wins = sum(1 for r in rows if r["net"] > 0)
    return {"n": n, "net": round(sum(r["net"] for r in rows), 2),
            "wr": round(wins / n * 100, 1), "pf": round(gw / abs(gl), 2) if gl else 0.0,
            "gw": round(gw, 1), "gl": round(gl, 1)}


def line(name: str, s: dict) -> str:
    return f"{name:<26}{s['n']:>5}{s['net']:>10.1f}{s['wr']:>7.1f}{s['pf']:>6.2f}{s['gw']:>10.1f}{s['gl']:>10.1f}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120, help="глубина 5м истории для расчёта")
    args = ap.parse_args()

    trades, m5, daily = await load(args.days)
    print(f"сделок: {len(trades)} | фиг с 5м: {len(m5)} | с дневными: {len(daily)}\n", flush=True)

    enriched: list[dict] = []
    no_data = 0
    for t in trades:
        figi = str(t["figi"])
        ts = t["entry_time"]
        if figi not in m5 or figi not in daily:
            no_data += 1
            continue
        ts_list, closes = m5[figi]
        i = bisect.bisect_right(ts_list, ts) - 1
        if i < 60:
            no_data += 1
            continue
        m5_st = macd_state(closes[max(0, i - 399):i + 1])
        h1_st = macd_state(h1_closes(ts_list[max(0, i - 399):i + 1], closes[max(0, i - 399):i + 1]))
        dts, dcl = daily[figi]
        di = bisect.bisect_right(dts, ts) - 1
        d_st = bias_from_closes(dcl[:di + 1]) if di >= 35 else {"bias": "unknown"}
        enriched.append({"ticker": t["ticker"], "side": t["side"], "net": float(t["net_pnl"]),
                         "bias": str(d_st.get("bias") or "unknown"),
                         "h1": str(h1_st.get("side") or ""), "h1_ok": bool(h1_st.get("ok")),
                         "m5": str(m5_st.get("side") or ""), "m5_ok": bool(m5_st.get("ok")),
                         "m5_trend": str(m5_st.get("trend") or "")})
    print(f"с MACD-состояниями: {len(enriched)} | без данных: {no_data}\n")

    def daily_ok(r: dict) -> bool:
        b = r["bias"]
        if b == "up":
            return r["side"] != "SELL"
        if b == "down":
            return r["side"] != "BUY"
        return True

    def h1_ok(r: dict) -> bool:
        if not r["h1_ok"] or not r["h1"]:
            return True
        if r["h1"] != r["side"]:
            return False
        if r["bias"] in ("up", "down"):
            return r["h1"] == ("BUY" if r["bias"] == "up" else "SELL")
        return True

    def m5_ok(r: dict) -> bool:
        if not r["m5_ok"] or not r["m5_trend"]:
            return True
        return (r["side"] == "BUY" and r["m5_trend"] == "rising") or \
               (r["side"] == "SELL" and r["m5_trend"] == "falling")

    variants = [
        ("baseline (все)", lambda r: True),
        ("+дневной bias", daily_ok),
        ("+H1 подтверждение", h1_ok),
        ("+M5 триггер", m5_ok),
        ("+bias +H1", lambda r: daily_ok(r) and h1_ok(r)),
        ("+bias +M5", lambda r: daily_ok(r) and m5_ok(r)),
        ("+bias +H1 +M5", lambda r: daily_ok(r) and h1_ok(r) and m5_ok(r)),
        ("H1 +M5 (без bias)", lambda r: h1_ok(r) and m5_ok(r)),
    ]
    print(f"{'Вариант':<26}{'N':>5}{'Net':>10}{'WR%':>7}{'PF':>6}{'GrossWin':>10}{'GrossLoss':>10}")
    for name, fn in variants:
        print(line(name, summarize([r for r in enriched if fn(r)])))

    print()
    for name, fn in (("отброшено H1", lambda r: not h1_ok(r)),
                     ("отброшено M5", lambda r: not m5_ok(r)),
                     ("отброшено bias", lambda r: not daily_ok(r))):
        print(line(name, summarize([r for r in enriched if fn(r)])))


if __name__ == "__main__":
    asyncio.run(main())
