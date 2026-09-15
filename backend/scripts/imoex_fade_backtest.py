"""IMOEX fade backtest — откат акций после экстремального хода индекса.

Правило (causal, без look-ahead):
- move20(t) = IMOEX close(t)/close(t-20)-1.
- Сигнал: первый пробой |move20| >= T.
  * variant=immediate  — вход на следующем баре против хода индекса;
  * variant=retrace25/50 — ждём откат |move20| до peak*(1-decay), затем вход.
- Вход: open(t+1) акции; выход: open(t+1+H). Одна позиция на бумагу за раз.
- Издержки: комиссия 0.05%×2 + проскальзывание 2бп×2 = 0.14% на круг (тариф
  бота). Дополнительно показывается тариф 0.3%×2 = 0.60%.
- Бумаги: top по beta к IMOEX (instruments.imoex_beta), направление — против индекса
  (fade) и, для контроля, ПО индексу (chase).

Запуск (на .3):
    .venv/bin/python3 scripts/imoex_fade_backtest.py --days 90
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import asyncpg

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.imoex_sensitivity import TICKERS, IMOEX_FIGI  # noqa: E402

DSN = "postgresql://deeptrading:deeptrading@127.0.0.1:5432/deeptrading"
COST_BOT = 0.0005 * 2 + 0.0002 * 2   # 0.14%
COST_TARIFF = 0.003 * 2              # 0.60%


async def _load(conn, figi: str, d1, d2, with_open: bool = False):
    cols = "ts, close, open" if with_open else "ts, close"
    rows = await conn.fetch(
        f"SELECT {cols} FROM candles WHERE figi=$1 AND interval=1 AND ts>=$2 AND ts<$3 ORDER BY ts",
        figi, d1, d2)
    if len(rows) < 5000:
        return None
    df = pd.DataFrame({"ts": [r["ts"] for r in rows], "close": [float(r["close"]) for r in rows]})
    if with_open:
        df["open"] = [float(r["open"]) for r in rows]
    return df.set_index("ts")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--top-beta", type=int, default=10)
    ap.add_argument("--min-beta", type=float, default=1.0)
    args = ap.parse_args()

    d2 = datetime.now(timezone.utc)
    d1 = d2 - timedelta(days=args.days)
    conn = await asyncpg.connect(DSN)
    ix = await _load(conn, IMOEX_FIGI, d1, d2)
    beta_rows = await conn.fetch(
        "SELECT figi, ticker, imoex_beta FROM instruments "
        "WHERE imoex_beta >= $1 ORDER BY imoex_beta DESC LIMIT $2",
        args.min_beta, args.top_beta)
    picks = [(r["figi"], r["ticker"]) for r in beta_rows]
    stocks = {}
    for figi, tick in picks:
        df = await _load(conn, figi, d1, d2, with_open=True)
        if df is not None:
            stocks[tick] = df
    await conn.close()
    print(f"IMOEX: {len(ix)} баров; бумаги (beta≥{args.min_beta}): "
          f"{', '.join(t for _, t in picks if t in stocks)}")

    m20 = (ix["close"] / ix["close"].shift(20) - 1) * 100

    def make_signals(T: float, variant: str, direction: str) -> pd.DatetimeIndex:
        """Моменты t (минуты индекса), когда надо входить на t+1."""
        sig = []
        active = False
        peak = 0.0
        spike_dir = 0
        entered = False
        for t, m in m20.items():
            if not np.isfinite(m):
                continue
            am = abs(m)
            if not active:
                if am >= T:
                    active = True
                    peak = am
                    spike_dir = 1 if m > 0 else -1
                    entered = False
                    if variant == "immediate":
                        sig.append((t, -spike_dir if direction == "fade" else spike_dir))
                        entered = True
                continue
            peak = max(peak, am)
            if not entered and variant.startswith("retrace"):
                decay = 0.25 if variant == "retrace25" else 0.5
                if am <= peak * (1 - decay):
                    sig.append((t, -spike_dir if direction == "fade" else spike_dir))
                    entered = True
            if am < T * 0.25:
                active = False
        return pd.DataFrame(sig, columns=["ts", "dir"]).set_index("ts")

    print("\nФормат: Trades/Wins/Losses/GrossWin/GrossLoss/Net/PF/WR%/AvgWin/AvgLoss, %-доходности сумм.")
    for direction in ("fade", "chase"):
        print(f"\n### Направление: {direction} ({'против' if direction=='fade' else 'по'} индексу)")
        for T in (1.0, 1.5, 2.0):
            for variant in ("immediate", "retrace25", "retrace50"):
                sigs = make_signals(T, variant, direction)
                if len(sigs) == 0:
                    continue
                for H in (1, 3, 5, 10):
                    rows = []
                    for tick, df in stocks.items():
                        fo = (df["open"].shift(-(H + 1)) / df["open"].shift(-1) - 1) * 100
                        s = sigs.reindex(fo.index)
                        m = s["dir"].notna() & fo.notna()
                        if not m.any():
                            continue
                        # дедуп: не входить, если позиция ещё открыта
                        idx = np.where(m.values)[0]
                        last_exit = -10**9
                        for i in idx:
                            if i <= last_exit:
                                continue
                            rows.append((s["dir"].iloc[i], fo.iloc[i]))
                            last_exit = i + H
                    if not rows:
                        continue
                    arr = pd.DataFrame(rows, columns=["dir", "ret"])
                    pnl = arr["dir"] * arr["ret"] / 100.0
                    gross = float(pnl.sum() * 100)
                    wins = pnl[pnl > 0]
                    losses = pnl[pnl <= 0]
                    net = float((pnl - COST_BOT).sum() * 100)
                    net_tariff = float((pnl - COST_TARIFF).sum() * 100)
                    gw = float(wins.sum() * 100)
                    gl = float(-losses.sum() * 100)
                    pf = (gw / gl) if gl > 0 else float("inf")
                    print(f"| T={T} {variant} H={H} | n={len(pnl)} | W/L={len(wins)}/{len(losses)} | "
                          f"GW={gw:+.0f} GL={-gl:.0f} | gross={gross:+.1f}% | net={net:+.1f}% | "
                          f"net@0.3%={net_tariff:+.1f}% | PF={pf:.2f} | WR={len(wins)/len(pnl)*100:.1f}% | "
                          f"avgW={wins.mean()*100 if len(wins) else 0:+.2f} avgL={losses.mean()*100 if len(losses) else 0:+.2f} |")


if __name__ == "__main__":
    asyncio.run(main())
