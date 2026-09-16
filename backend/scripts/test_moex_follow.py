"""Быстрый тест стратегии «следуем за IMOEX» (index-following с разворотом).

Идея:
  - сигнал направления = IMOEX 5м (EMA-кросс или MACD-гистограмма);
  - при смене знака сигнала: закрыть всё и открыть K акций в новую сторону;
  - размер: pos_pct × equity × lev на все позиции поровну (маржа);
  - издержки: 0.05%/сторону + 2 б.п. слиппедж.

Запуск: python scripts/test_moex_follow.py [--signal ema|macd] [--pos-pct 0.5] [--lev 2]
        [--k 3] [--hold 0] [--days 45] [--tickers 10]
"""
from __future__ import annotations

import argparse
import asyncio
import bisect
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

COMMISSION = 0.0005
SLIP = 0.0002
IMOEX_FIGI = "BBG00KDWPPW2"


async def load(days: int, n_tickers: int):
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    # 10 тикеров с наибольшей бетой к IMOEX (и с 5м свечами)
    rows = await c.fetch("""
        SELECT i.figi, i.ticker, coalesce(i.imoex_beta, 0) AS beta,
               coalesce(i.lot, 1) AS lot,
               (SELECT count(*) FROM candles c WHERE c.figi = i.figi AND c.interval = 5
                 AND c.ts >= $1) AS n5
        FROM instruments i
        WHERE i.class_code = 'TQBR' AND i.imoex_beta IS NOT NULL
        ORDER BY coalesce(i.imoex_beta, 0) DESC LIMIT 40""", since)
    picked = [(str(r["figi"]), str(r["ticker"]), float(r["beta"]), int(r["lot"] or 1))
              for r in rows if int(r["n5"]) > 200]
    picked = picked[:n_tickers]
    figis = [p[0] for p in picked]
    m5_rows = await c.fetch("""
        SELECT figi, ts, open, high, low, close FROM candles
        WHERE interval = 5 AND figi = ANY($1) AND ts >= $2 ORDER BY figi, ts""", figis, since)
    # IMOEX хранится 1м — ресемплим в 5м (последнее закрытие бакета)
    idx_rows = await c.fetch("""
        SELECT ts, close FROM candles
        WHERE interval = 1 AND figi = $1 AND ts >= $2 ORDER BY ts""", IMOEX_FIGI, since)
    await c.close()
    data: dict[str, list[dict]] = defaultdict(list)
    for r in m5_rows:
        data[str(r["figi"])].append({"ts": r["ts"], "o": float(r["open"]), "h": float(r["high"]),
                                     "l": float(r["low"]), "c": float(r["close"])})
    bucket = None
    last = None
    for r in idx_rows:
        ts = r["ts"]
        b = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        if bucket is not None and b != bucket and last is not None:
            data[IMOEX_FIGI].append({"ts": bucket, "o": last, "h": last, "l": last, "c": last})
        bucket, last = b, float(r["close"])
    if last is not None and bucket is not None:
        data[IMOEX_FIGI].append({"ts": bucket, "o": last, "h": last, "l": last, "c": last})
    return picked, data


def ema(vals: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def signal_series(closes: list[float], kind: str) -> list[int]:
    """1 = up, -1 = down."""
    if kind == "macd":
        ef, es = ema(closes, 12), ema(closes, 26)
        line = [a - b for a, b in zip(ef, es)]
        sig = ema(line, 9)
        return [1 if line[i] > sig[i] else -1 for i in range(len(closes))]
    e20, e50 = ema(closes, 20), ema(closes, 50)
    return [1 if e20[i] > e50[i] else -1 for i in range(len(closes))]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="ema", choices=("ema", "macd"))
    ap.add_argument("--pos-pct", type=float, default=0.5)
    ap.add_argument("--lev", type=float, default=2.0)
    ap.add_argument("--k", type=int, default=3, help="сколько акций держим одновременно")
    ap.add_argument("--hold", type=int, default=0, help="подтверждение сигнала N баров")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--tickers", type=int, default=10)
    ap.add_argument("--equity", type=float, default=5000.0)
    ap.add_argument("--lag", type=int, default=5, help="задержка входа/выхода, минут (честное исполнение)")
    args = ap.parse_args()

    picked, data = await load(args.days, args.tickers)
    idx = data.get(IMOEX_FIGI) or []
    if len(idx) < 60:
        print("нет 5м свечей IMOEX за период")
        return
    ts_list = [b["ts"] for b in idx]
    closes = [b["c"] for b in idx]
    sig = signal_series(closes, args.signal)
    if args.hold > 0:
        # подтверждение: сигнал должен продержаться N баров
        confirmed = list(sig)
        for i in range(len(sig)):
            if i < args.hold or any(sig[j] != sig[i] for j in range(i - args.hold + 1, i + 1)):
                confirmed[i] = confirmed[i - 1] if i else 0
        sig = confirmed

    # для каждого тикера: список ts (для bisect) — метки баров могут не совпадать
    # с бакетами IMOEX (у тикеров ts = закрытие бара), берём последний бар <= сигнала.
    tick_ts: dict[str, list] = {}
    for figi, ticker, beta, lot in picked:
        tick_ts[figi] = [b["ts"] for b in (data.get(figi) or [])]

    def _bar_at(figi: str, ts) -> int | None:
        lst = tick_ts.get(figi) or []
        j = bisect.bisect_right(lst, ts) - 1
        return j if j >= 0 else None

    equity = args.equity
    equity_curve = [equity]
    positions: dict[str, dict] = {}  # figi -> {side, qty, entry}
    trades: list[dict] = []
    flips = 0
    cur = 0
    for i, ts in enumerate(ts_list):
        s_i = sig[i]
        if s_i == 0:
            continue
        if cur != 0 and s_i != cur:
            flips += 1
            # закрыть все
            for figi, p in list(positions.items()):
                bars = data.get(figi) or []
                j = _bar_at(figi, ts + timedelta(minutes=args.lag))
                if j is None:
                    continue
                px = bars[j]["c"]
                pnl = (px - p["entry"]) * p["qty"] if p["side"] == "BUY" else (p["entry"] - px) * p["qty"]
                cost = (px * abs(p["qty"]) + p["entry"] * abs(p["qty"])) * (COMMISSION + SLIP)
                equity += pnl - cost
                trades.append({"ticker": p["ticker"], "side": p["side"], "pnl": pnl - cost,
                               "bars": i - p["bar"]})
                del positions[figi]
            cur = 0
        if cur == 0:
            # открыть K акций в сторону сигнала
            cur = s_i
            side = "BUY" if s_i > 0 else "SELL"
            notional_total = equity * args.pos_pct * args.lev
            per = notional_total / max(1, args.k)
            for figi, ticker, beta, lot in picked[: args.k]:
                bars = data.get(figi) or []
                j = _bar_at(figi, ts + timedelta(minutes=args.lag))
                if j is None:
                    continue
                px = bars[j]["c"]
                lots = int(per / (px * lot))  # целые лоты, без «минимума в 1 шт»
                if lots < 1:
                    continue  # лот не влезает в бюджет позиции — пропускаем
                qty = lots * lot
                positions[figi] = {"ticker": ticker, "side": side, "qty": qty, "entry": px, "bar": i}
        equity_curve.append(equity)

    # закрыть остаток в конце
    for figi, p in list(positions.items()):
        bars = data.get(figi) or []
        if not bars:
            continue
        px = bars[-1]["c"]
        pnl = (px - p["entry"]) * p["qty"] if p["side"] == "BUY" else (p["entry"] - px) * p["qty"]
        cost = (px * abs(p["qty"]) + p["entry"] * abs(p["qty"])) * (COMMISSION + SLIP)
        equity += pnl - cost
        trades.append({"ticker": p["ticker"], "side": p["side"], "pnl": pnl - cost, "bars": 0})

    n = len(trades)
    gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = sum(t["pnl"] for t in trades if t["pnl"] < 0)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    peak = equity_curve[0]
    max_dd = 0.0
    for e in equity_curve:
        peak = max(peak, e)
        max_dd = min(max_dd, (e - peak) / peak)
    print(f"IMOEX-follow: сигнал={args.signal} K={args.k} pos={args.pos_pct} lev={args.lev} "
          f"hold={args.hold} | баров {len(ts_list)} | разворотов {flips}")
    print(f"сделок {n} | net {equity - args.equity:+.1f}₽ | WR {wins/n*100 if n else 0:.1f}% | "
          f"PF {gw/abs(gl) if gl else 0:.2f} | gross {gw:+.1f}/{gl:+.1f} | MaxDD {max_dd*100:.1f}%")
    by_t: dict[str, float] = defaultdict(float)
    for t in trades:
        by_t[t["ticker"]] += t["pnl"]
    print("по тикерам:", ", ".join(f"{k} {v:+.0f}" for k, v in sorted(by_t.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    asyncio.run(main())
