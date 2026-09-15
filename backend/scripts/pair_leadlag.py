"""Pair lead-lag — кто за кем ходит среди акций universe (+ IMOEX).

Для каждой пары (i, j): corr(ri(t), rj(t+k)) для k=0..+3.
- k=0 — синхронность;
- k>0 — i ЛИДИРУЕТ j (j двигается через k минут после i).

Дополнительно для топ-пар: «догон» после большого хода лидера — forward-доходность
ведомого в сторону хода лидера за 1/3 мин + t-статистика (без look-ahead:
условие на прошлый ход лидера, доход ведомого — вперёд).

Запуск (на .3, из backend):
    .venv/bin/python3 scripts/pair_leadlag.py --days 90
    .venv/bin/python3 scripts/pair_leadlag.py --days 90 --json /tmp/pairs.json
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

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.imoex_sensitivity import TICKERS, IMOEX_FIGI  # noqa: E402

DSN = "postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading"


async def _load(conn, figi: str, d1: datetime, d2: datetime):
    rows = await conn.fetch(
        "SELECT ts, close FROM candles WHERE figi=$1 AND interval=1 AND ts>=$2 AND ts<$3 ORDER BY ts",
        figi, d1, d2)
    if len(rows) < 5000:
        return None
    return pd.DataFrame({"ts": [r["ts"] for r in rows], "close": [float(r["close"]) for r in rows]})


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min-corr", type=float, default=0.04)
    ap.add_argument("--json", type=str, default="")
    args = ap.parse_args()

    d2 = datetime.now(timezone.utc)
    d1 = d2 - timedelta(days=args.days)
    conn = await asyncpg.connect(DSN)
    frames = {}
    for figi, tick in TICKERS + [(IMOEX_FIGI, "IMOEX")]:
        df = await _load(conn, figi, d1, d2)
        if df is not None:
            frames[tick] = df.set_index("ts")["close"].rename(tick)
    await conn.close()

    wide = pd.concat(frames.values(), axis=1).sort_index()
    # доходности считаем ПО КАЖДОМУ ряду отдельно (иначе гэпы сессий дают ложные скачки)
    rets = pd.DataFrame({t: frames[t].pct_change() for t in frames}).reindex(wide.index)
    rets = rets.where(rets.abs() < 0.2)
    cols = list(rets.columns)
    n_col = len(cols)
    print(f"тикеров: {n_col}; минут: {len(rets)}; период {d1.date()}..{d2.date()}")
    print("corr(ri(t), rj(t+k)): k=0 синхронно, k>0 — i лидирует j")

    M = rets.values
    valid = ~np.isnan(M)
    # nan-безопасная стандартизация (NaN -> 0), счётчик парами
    Z = np.zeros_like(M)
    for j in range(n_col):
        v = M[valid[:, j], j]
        if len(v) > 100:
            Z[valid[:, j], j] = (v - v.mean()) / (v.std() + 1e-12)

    C = {}
    for k in (0, 1, 2, 3):
        if k == 0:
            A, B, mA, mB = Z, Z, valid, valid
        else:
            A, B, mA, mB = Z[:-k], Z[k:], valid[:-k], valid[k:]
        n = mA.astype(np.float64).T @ mB.astype(np.float64)
        num = A.T @ B
        with np.errstate(divide="ignore", invalid="ignore"):
            c = np.where(n > 5000, num / np.maximum(n, 1), np.nan)
        C[k] = c
        np.fill_diagonal(c, np.nan)

    # directed pairs: i leads j если C[k][i,j] высокая при малой обратной
    print("\n## Топ направленных пар (лидер -> ведомый, k=1..3 мин)")
    print("| Лидер | Ведомый | corr k=1 | corr k=2 | corr k=3 | corr обратно (k=1) | N |")
    print("|---|---|---|---|---|---|---|")
    rows = []
    for i in range(n_col):
        for j in range(n_col):
            if i == j:
                continue
            c1, c2, c3 = C[1][i, j], C[2][i, j], C[3][i, j]
            rev = C[1][j, i]
            vals = [v for v in (c1, c2, c3) if np.isfinite(v)]
            if not vals:
                continue
            score = max(vals)
            if score >= args.min_corr:
                rows.append({"a": cols[i], "b": cols[j], "c1": c1, "c2": c2, "c3": c3,
                             "rev": rev, "score": score})
    # убираем зеркальные дубликаты (A->B и B->A): оставляем пару с большим score
    seen = set()
    best_pairs = []
    for r in sorted(rows, key=lambda x: -x["score"]):
        key = tuple(sorted([r["a"], r["b"]]))
        if key in seen:
            continue
        seen.add(key)
        best_pairs.append(r)
    best_pairs = best_pairs[:20]
    if not best_pairs:
        # диагностический топ по максимуму k=1..3 без порога
        print("(пар выше порога нет; топ-10 по max(k=1..3) без порога)")
        cand = []
        for i in range(n_col):
            for j in range(n_col):
                if i == j:
                    continue
                sc = max([v for v in (C[1][i, j], C[2][i, j], C[3][i, j]) if np.isfinite(v)] or [np.nan])
                if np.isfinite(sc):
                    cand.append((sc, cols[i], cols[j], C[0][i, j], C[1][i, j], C[1][j, i]))
        for sc, a, b, c0, c1, rev in sorted(cand, reverse=True)[:10]:
            print(f"  {a}->{b}: max={sc:+.3f} | corr0={c0:+.3f} | k1={c1:+.3f} | rev k1={rev:+.3f}")
        print(f"  (пар с corr>0.02: {sum(1 for x in cand if x[0] > 0.02)})")
    for r in best_pairs:
        n = int(max([np.sum(valid[:, cols.index(r["a"])]), 1]))
        print(f"| {r['a']} | {r['b']} | {r['c1']:+.3f} | {r['c2']:+.3f} | {r['c3']:+.3f} | "
              f"{r['rev']:+.3f} | {n} |")

    # --- догон для топ-пар после большого хода лидера ---
    print("\n## Догон: после хода лидера ≥0.3% за 5 мин — forward ведомого (1/3 мин)")
    print("| Пара | N сигн. | Follow 1м, бп | Follow 3м, бп | t(3м) |")
    print("|---|---|---|---|---|")
    detail = []
    for r in best_pairs[:12]:
        a, b = r["a"], r["b"]
        move5 = (frames[a] / frames[a].shift(5) - 1) * 100
        f1 = (frames[b].shift(-1) / frames[b] - 1) * 100
        f3 = (frames[b].shift(-3) / frames[b] - 1) * 100
        df = pd.concat([move5.rename("m"), f1.rename("f1"), f3.rename("f3")], axis=1, sort=True).dropna()
        g = df[df["m"].abs() >= 0.3]
        if len(g) < 100:
            continue
        d1v = np.sign(g["m"]) * g["f1"]
        d3 = np.sign(g["m"]) * g["f3"]
        t3 = d3.mean() / (d3.std() / np.sqrt(len(d3)))
        detail.append({"pair": f"{a}->{b}", "n": len(g), "f1": float(d1v.mean() * 100),
                       "f3": float(d3.mean() * 100), "t3": float(t3)})
        print(f"| {a}->{b} | {len(g)} | {d1v.mean()*100:+.1f} | {d3.mean()*100:+.1f} | {t3:+.1f} |")

    if args.json:
        Path(args.json).write_text(json.dumps(
            {"pairs": [{k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                        for k, v in r.items()} for r in best_pairs],
             "follow": detail}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("saved:", args.json)


if __name__ == "__main__":
    asyncio.run(main())
