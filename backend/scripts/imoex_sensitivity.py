"""IMOEX sensitivity — исследование связи акций с индексом MOEX.

Отвечает на вопросы:
1. При каком ходе IMOEX акции начинают идти за индексом (contemporaneous) и
   продолжают ли движение в следующие 1-5 минут (догон/lead-lag).
2. Какие акции наиболее подвержены влиянию IMOEX: beta, corr, R²,
   доля совпадения направления, follow после больших ходов.
3. Есть ли разворот после экстремальных ходов (fade) и переживёт ли он издержки.

Запуск (на .3, из backend):
    .venv/bin/python3 scripts/imoex_sensitivity.py --days 90
    .venv/bin/python3 scripts/imoex_sensitivity.py --days 90 --json /tmp/imoex_sens.json

Данные: `candles` (interval=1) — IMOEX (ISS) + ликвидные акции (T-Invest/ISS).
Метки баров IMOEX (begin) и акций совпадают — выравнивание подбирается
автоматически (shift по максимуму corr).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import asyncpg

IMOEX_FIGI = "BBG00KDWPPW2"
DSN = "postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading"

# Ликвидные бумаги с плотными 1м данными (проверено: >20k баров за 90 дней).
TICKERS = [
    ("BBG004730N88", "SBER"), ("TCS80A107UL4", "T"), ("BBG004731354", "ROSN"),
    ("BBG004730RP0", "GAZP"), ("BBG00475KKY8", "NVTK"), ("BBG00475KHX6", "TRNFP"),
    ("BBG004S683W7", "AFLT"), ("BBG004731032", "LKOH"), ("BBG00F6NKQX3", "SMLT"),
    ("BBG004S68507", "MAGN"), ("BBG004S681M2", "SNGSP"), ("BBG004731489", "GMKN"),
    ("BBG004S681W1", "MTSS"), ("BBG008F2T3T2", "RUAL"), ("BBG004S681B4", "NLMK"),
    ("BBG004S68473", "IRAO"), ("TCS00A107T19", "YDEX"), ("TCS00A106YF0", "VKCO"),
    ("BBG00475K6C3", "CHMF"), ("RU000A106T36", "ASTR"), ("BBG003LYCMB1", "SFIN"),
    ("BBG004RVFFC0", "TATN"), ("BBG004S68CP5", "MVID"), ("BBG004S68B31", "ALRS"),
    ("BBG0063FKTD9", "LENT"), ("BBG000R607Y3", "PLZL"),
]


async def _load(conn, figi: str, d1: datetime, d2: datetime) -> pd.DataFrame:
    rows = await conn.fetch(
        "SELECT ts, close FROM candles WHERE figi=$1 AND interval=1 AND ts>=$2 AND ts<$3 ORDER BY ts",
        figi, d1, d2)
    return pd.DataFrame({"ts": [r["ts"] for r in rows], "close": [float(r["close"]) for r in rows]})


def _tstat(x: pd.Series) -> float:
    x = x.dropna()
    return float(x.mean() / (x.std() / np.sqrt(len(x)))) if len(x) > 2 else float("nan")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--json", type=str, default="")
    ap.add_argument("--save-db", action="store_true",
                    help="записать beta/corr/R² в instruments (imoex_beta, imoex_corr, imoex_r2)")
    args = ap.parse_args()

    d2 = datetime.now(timezone.utc)
    d1 = d2 - timedelta(days=args.days)
    conn = await asyncpg.connect(DSN)
    ix = await _load(conn, IMOEX_FIGI, d1, d2)
    stocks = {t: await _load(conn, f, d1, d2) for f, t in TICKERS}
    await conn.close()
    if ix.empty:
        print("no IMOEX data")
        return
    ix_s = ix.set_index("ts")["close"]
    print(f"IMOEX: {len(ix)} баров; акций: {len(stocks)}; период {d1.date()}..{d2.date()}")

    # --- 0. автоподбор выравнивания меток ---
    print("\n## 0. Выравнивание меток: corr(rs(t), ri(t+shift))")
    print("| shift, мин | pooled corr |")
    print("|---|---|")
    best = {}
    for shift in (-2, -1, 0, 1, 2, 3):
        cs = []
        for t, st in stocks.items():
            rs = st.set_index("ts")["close"].pct_change()
            ri = ix_s.pct_change().shift(-shift)
            df = pd.concat([rs.rename("s"), ri.rename("i")], axis=1).dropna()
            df = df[(df.s.abs() < 0.2) & (df.i.abs() < 0.05)]
            if len(df) > 5000:
                cs.append(float(np.corrcoef(df.s, df.i)[0, 1]))
        best[shift] = float(np.mean(cs))
        print(f"| {shift:+d} | {best[shift]:+.3f} |")
    best_shift = max(best, key=best.get)
    print(f"-> выравнивание shift={best_shift:+d} мин")

    # --- данные по тикерам ---
    data = {}
    ri_full = ix_s.pct_change().shift(-best_shift)
    for t, st in stocks.items():
        st = st.set_index("ts")
        df = pd.concat([st["close"].rename("close"), ri_full.rename("ri")], axis=1).dropna()
        df["rs"] = df["close"].pct_change()
        df["move5"] = ix_s.pct_change(5).shift(-best_shift).reindex(df.index) * 100
        df["move20"] = ix_s.pct_change(20).shift(-best_shift).reindex(df.index) * 100
        for h in (1, 2, 3, 5):
            df[f"f{h}"] = (df["close"].shift(-h) / df["close"] - 1) * 100
        data[t] = df[(df["rs"].abs() < 0.2) & (df["ri"].abs() < 0.05)]

    pool = pd.concat(data.values(), ignore_index=True)

    # --- 1. co-movement и догон ---
    print("\n## 1. Ход IMOEX за 5 мин: согласие (в ту же минуту) и догон вперёд")
    print("| |move5|, % | N | P(акция в стороне IMOEX), % | Follow 1м, бп | Follow 3м, бп | Follow 5м, бп | t(3м) |")
    print("|---|---|---|---|---|---|---|---|")
    pool["absm5"] = pool["move5"].abs()
    pool["agree"] = np.sign(pool["rs"]) == np.sign(pool["ri"])
    for lo, hi in [(0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.5), (0.5, 1.0), (1.0, 99)]:
        g = pool[(pool["absm5"] >= lo) & (pool["absm5"] < hi)].dropna(subset=["f3", "ri"])
        if len(g) < 100:
            continue
        d3 = np.sign(g["ri"]) * g["f3"]
        print(f"| {lo:.1f}–{hi:.1f} | {len(g)} | {g['agree'].mean()*100:.1f} | "
              f"{(np.sign(g['ri'])*g['f1']).mean()*100:+.1f} | {d3.mean()*100:+.1f} | "
              f"{(np.sign(g['ri'])*g['f5']).mean()*100:+.1f} | {_tstat(d3):+.1f} |")

    print("\n## 1b. После хода IMOEX за 20 мин")
    print("| |move20|, % | N | Follow 3м, бп | Follow 5м, бп | t(3м) |")
    print("|---|---|---|---|---|---|")
    pool["absm20"] = pool["move20"].abs()
    for lo, hi in [(0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 99)]:
        g = pool[(pool["absm20"] >= lo) & (pool["absm20"] < hi)].dropna(subset=["f3"])
        if len(g) < 100:
            continue
        d3 = np.sign(g["move20"]) * g["f3"]
        print(f"| {lo:.1f}–{hi:.1f} | {len(g)} | {d3.mean()*100:+.1f} | "
              f"{(np.sign(g['move20'])*g['f5']).mean()*100:+.1f} | {_tstat(d3):+.1f} |")

    print("\n## 1c. Разворот ПОСЛЕ экстремального хода (fade): -sign(move20) * f_h")
    print("| порог | N | Fade 3м, бп | t(3м) | Fade 5м, бп | t(5м) |")
    print("|---|---|---|---|---|---|")
    for thr in (0.8, 1.0, 1.5, 2.0):
        g = pool[pool["absm20"] >= thr].dropna(subset=["f3", "f5"])
        if len(g) < 100:
            continue
        d3 = -np.sign(g["move20"]) * g["f3"]
        d5 = -np.sign(g["move20"]) * g["f5"]
        print(f"| ≥{thr:.1f}% | {len(g)} | {d3.mean()*100:+.1f} | {_tstat(d3):+.1f} | "
              f"{d5.mean()*100:+.1f} | {_tstat(d5):+.1f} |")

    # --- 2. per-ticker ---
    print("\n## 2. Чувствительность акций к IMOEX")
    print("| Ticker | beta | corr | R² | Agree@|m5|≥0.3%, % | Follow@|m20|≥0.8%, бп (f3) | t(f3) | N big |")
    print("|---|---|---|---|---|---|---|---|")
    rows = []
    for t, df in data.items():
        d = df.dropna(subset=["rs", "ri"])
        beta = float(np.polyfit(d["ri"], d["rs"], 1)[0])
        corr = float(np.corrcoef(d["ri"], d["rs"])[0, 1])
        m = df[df["move5"].abs() >= 0.3]
        agree = ((np.sign(m["rs"]) == np.sign(m["move5"])).mean() * 100) if len(m) > 50 else float("nan")
        big = df[df["move20"].abs() >= 0.8].dropna(subset=["f3"])
        if len(big) >= 30:
            d3 = np.sign(big["move20"]) * big["f3"]
            f3, t3 = d3.mean() * 100, _tstat(d3)
        else:
            f3, t3 = float("nan"), float("nan")
        rows.append({"ticker": t, "beta": beta, "corr": corr, "r2": corr * corr,
                     "agree": agree, "follow_bp": f3, "t": t3, "n_big": len(big)})
    rows.sort(key=lambda r: -r["beta"])
    for r in rows:
        print(f"| {r['ticker']} | {r['beta']:.2f} | {r['corr']:.2f} | {r['r2']:.2f} | "
              f"{r['agree']:.1f} | {r['follow_bp']:+.1f} | {r['t']:+.1f} | {r['n_big']} |")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved:", args.json)

    if args.save_db:
        conn = await asyncpg.connect(DSN)
        await conn.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS imoex_beta DOUBLE PRECISION")
        await conn.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS imoex_corr DOUBLE PRECISION")
        await conn.execute("ALTER TABLE instruments ADD COLUMN IF NOT EXISTS imoex_r2 DOUBLE PRECISION")
        tick2figi = {t: f for f, t in TICKERS}
        n_saved = 0
        for r in rows:
            figi = tick2figi.get(r["ticker"])
            if not figi:
                continue
            await conn.execute(
                "UPDATE instruments SET imoex_beta=$1, imoex_corr=$2, imoex_r2=$3 WHERE figi=$4",
                r["beta"], r["corr"], r["r2"], figi)
            n_saved += 1
        await conn.close()
        print(f"saved to DB: {n_saved} инструментов (instruments.imoex_beta/corr/r2)")


if __name__ == "__main__":
    asyncio.run(main())
