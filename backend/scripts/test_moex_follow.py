"""Быстрый тест стратегии «следуем за IMOEX» (v2: пороги входа/выхода раздельно).

Сигнал: spread = (EMA_fast - EMA_slow)/EMA_slow * 100 (%) на 5м IMOEX.
  - вход:   spread >= +entry_thr (лонг) / <= -entry_thr (шорт)
  - выход:  лонг закрывается при spread <= exit_thr; шорт — при spread >= -exit_thr
  - гистерезис: exit_thr < entry_thr → меньше ложных разворотов
  - подтверждение: сигнал должен держаться hold баров

Позиции: K акций с макс. бетой, размер pos_pct × equity × lev на всех поровну,
реальные лоты, издержки 0.05%/сторону + 2 б.п., исполнение со сдвигом lag мин.

Запуск: python scripts/test_moex_follow.py [--entry-thr 0.05] [--exit-thr 0.0]
        [--hold 6] [--k 10] [--fast 20] [--slow 50] [--sweep-k]
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


async def load(days: int, n_tickers: int = 40):
    import asyncpg
    from app.config import get_settings
    s = get_settings()
    c = await asyncpg.connect(host=s.postgres_host, port=s.postgres_port,
                              user=s.postgres_user, password=s.postgres_password,
                              database=s.postgres_db)
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = await c.fetch("""
        SELECT i.figi, i.ticker, coalesce(i.imoex_beta, 0) AS beta, coalesce(i.lot, 1) AS lot,
               (SELECT count(*) FROM candles c WHERE c.figi = i.figi AND c.interval = 5
                 AND c.ts >= $1) AS n5
        FROM instruments i
        WHERE i.class_code = 'TQBR' AND i.imoex_beta IS NOT NULL
        ORDER BY coalesce(i.imoex_beta, 0) DESC LIMIT 60""", since)
    picked = [(str(r["figi"]), str(r["ticker"]), float(r["beta"]), int(r["lot"] or 1))
              for r in rows if int(r["n5"]) > 200][:n_tickers]
    figis = [p[0] for p in picked]
    m5_rows = await c.fetch("""
        SELECT figi, ts, open, high, low, close FROM candles
        WHERE interval = 5 AND figi = ANY($1) AND ts >= $2 ORDER BY figi, ts""", figis, since)
    idx_rows = await c.fetch("""
        SELECT ts, close FROM candles WHERE interval = 1 AND figi = $1 AND ts >= $2
        ORDER BY ts""", IMOEX_FIGI, since)
    await c.close()
    data: dict[str, list[dict]] = defaultdict(list)
    for r in m5_rows:
        data[str(r["figi"])].append({"ts": r["ts"], "c": float(r["close"])})
    bucket = None
    last = None
    for r in idx_rows:
        ts = r["ts"]
        b = ts.replace(minute=(ts.minute // 5) * 5, second=0, microsecond=0)
        if bucket is not None and b != bucket and last is not None:
            data[IMOEX_FIGI].append({"ts": bucket, "c": last})
        bucket, last = b, float(r["close"])
    if last is not None and bucket is not None:
        data[IMOEX_FIGI].append({"ts": bucket, "c": last})
    return picked, data


def ema(vals: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def spread_series(closes: list[float], fast: int, slow: int) -> list[float]:
    ef, es = ema(closes, fast), ema(closes, slow)
    return [((a - b) / b * 100.0) if b else 0.0 for a, b in zip(ef, es)]


def simulate(picked, data, *, entry_thr=0.05, exit_thr=0.0, hold=6, k=10,
             fast=20, slow=50, pos_pct=0.5, lev=2.0, lag=5, equity0=50000.0,
             start_i=0, end_i=None) -> dict:
    """Прогон стратегии. Возвращает метрики."""
    idx = data.get(IMOEX_FIGI) or []
    if len(idx) < slow + hold + 5:
        return {"n": 0, "net": 0.0}
    ts_list = [b["ts"] for b in idx]
    closes = [b["c"] for b in idx]
    spread = spread_series(closes, fast, slow)
    if end_i is None:
        end_i = len(ts_list)

    def _bar_at(figi: str, ts):
        lst = tick_ts[figi]
        j = bisect.bisect_right(lst, ts) - 1
        return j if j >= 0 else None

    tick_ts = {f: [b["ts"] for b in (data.get(f) or [])] for f, _t, _b, _l in picked}
    equity = equity0
    curve = [equity]
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    flips = 0
    cur = 0
    hold_cnt = 0
    # подтверждение: сигнал держится hold баров подряд
    for i in range(start_i, end_i):
        ts = ts_list[i]
        sp = spread[i]
        want = 1 if sp >= entry_thr else (-1 if sp <= -entry_thr else 0)
        # состояние с гистерезисом
        if cur == 1 and sp <= exit_thr:
            want = -1 if sp <= -entry_thr else 0
        elif cur == -1 and sp >= -exit_thr:
            want = 1 if sp >= entry_thr else 0
        if want != cur:
            hold_cnt += 1          # подряд идущие бары в новую сторону
        else:
            hold_cnt = 0
            continue
        if hold_cnt < max(1, hold):
            continue
        # --- смена состояния: закрыть всё и (если want != 0) открыть заново ---
        flips += 1
        ts_ex = ts + timedelta(minutes=lag)
        for figi, p in list(positions.items()):
            j = _bar_at(figi, ts_ex)
            if j is None:
                continue
            bars = data[figi]
            px = bars[j]["c"]
            pnl = (px - p["entry"]) * p["qty"] if p["side"] == "BUY" else (p["entry"] - px) * p["qty"]
            cost = (px + p["entry"]) * abs(p["qty"]) * (COMMISSION + SLIP)
            equity += pnl - cost
            trades.append({"ticker": p["ticker"], "side": p["side"], "pnl": pnl - cost})
            del positions[figi]
        cur = want
        hold_cnt = 0
        if want == 0:
            curve.append(equity)
            continue
        side = "BUY" if want > 0 else "SELL"
        per = equity * pos_pct * lev / max(1, k)
        for figi, ticker, beta, lot in picked[:k]:
            bars = data.get(figi) or []
            j = _bar_at(figi, ts_ex)
            if j is None:
                continue
            px = bars[j]["c"]
            lots = int(per / (px * lot))
            if lots < 1:
                continue
            positions[figi] = {"ticker": ticker, "side": side, "qty": lots * lot, "entry": px}
        curve.append(equity)
    # закрыть остаток
    for figi, p in list(positions.items()):
        bars = data.get(figi) or []
        if not bars:
            continue
        px = bars[-1]["c"]
        pnl = (px - p["entry"]) * p["qty"] if p["side"] == "BUY" else (p["entry"] - px) * p["qty"]
        cost = (px + p["entry"]) * abs(p["qty"]) * (COMMISSION + SLIP)
        equity += pnl - cost
        trades.append({"ticker": p["ticker"], "side": p["side"], "pnl": pnl - cost})
    n = len(trades)
    gw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gl = sum(t["pnl"] for t in trades if t["pnl"] < 0)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    peak = curve[0]
    mdd = 0.0
    for e in curve:
        peak = max(peak, e)
        mdd = min(mdd, (e - peak) / peak)
    by_t: dict[str, float] = defaultdict(float)
    for t in trades:
        by_t[t["ticker"]] += t["pnl"]
    return {"n": n, "net": round(equity - equity0, 1), "wr": round(wins / n * 100, 1) if n else 0.0,
            "pf": round(gw / abs(gl), 2) if gl else 0.0, "gross_win": round(gw, 1),
            "gross_loss": round(gl, 1), "maxdd": round(mdd * 100, 1), "flips": flips,
            "by_ticker": dict(by_t), "curve": curve}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--entry-thr", type=float, default=0.05)
    ap.add_argument("--exit-thr", type=float, default=0.0)
    ap.add_argument("--hold", type=int, default=6)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--fast", type=int, default=20)
    ap.add_argument("--slow", type=int, default=50)
    ap.add_argument("--pos-pct", type=float, default=0.5)
    ap.add_argument("--lev", type=float, default=2.0)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--equity", type=float, default=50000.0)
    ap.add_argument("--sweep-k", action="store_true", help="прогон по K для диверсификации")
    args = ap.parse_args()

    picked, data = await load(args.days)
    base = dict(entry_thr=args.entry_thr, exit_thr=args.exit_thr, hold=args.hold,
                k=args.k, fast=args.fast, slow=args.slow, pos_pct=args.pos_pct,
                lev=args.lev, equity0=args.equity)
    if args.sweep_k:
        print(f"{'K':>4}{'N':>6}{'Net':>10}{'WR%':>7}{'PF':>6}{'MaxDD%':>8}{'Флипов':>7}")
        for k in (1, 3, 5, 8, 10, 15, 20):
            r = simulate(picked, data, **{**base, "k": k})
            print(f"{k:>4}{r['n']:>6}{r['net']:>10.1f}{r['wr']:>7.1f}{r['pf']:>6.2f}{r['maxdd']:>8.1f}{r['flips']:>7}")
        return
    r = simulate(picked, data, **base)
    print(f"IMOEX-follow thr={args.entry_thr}/{args.exit_thr} hold={args.hold} K={args.k} "
          f"fast/slow={args.fast}/{args.slow} pos={args.pos_pct} lev={args.lev} | флипов {r['flips']}")
    print(f"сделок {r['n']} | net {r['net']:+.1f}₽ | WR {r['wr']}% | PF {r['pf']} | "
          f"gross {r['gross_win']:+.1f}/{r['gross_loss']:+.1f} | MaxDD {r['maxdd']}%")
    print("по тикерам:", ", ".join(f"{k} {v:+.0f}" for k, v in
                                   sorted(r["by_ticker"].items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    asyncio.run(main())
