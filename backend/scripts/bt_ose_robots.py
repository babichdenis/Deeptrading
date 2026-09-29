#!/usr/bin/env python3
"""Замер 5 готовых OsEngine-портов Wave A через app/engine/ose/metrics.py.

Все роботы гоняются по одной детерминированной серии (ose.metrics.synthetic_series,
seed=42) — числа воспроизводимы на любой машине. Приёмка: таблица на Mac и на .8
должна совпадать построчно (пункты, 1 контракт, без комиссий).

Запуск: python scripts/bt_ose_robots.py   (из backend/)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.engine.ose.metrics import run_robot_with_metrics, synthetic_series
from app.engine.ose.robots import (
    EnvelopTrend,
    PriceChannelTrade,
    RsiContrtrend,
    SmaStochastic,
    StrategyBollinger,
    TesterTab,
)

ROBOTS = {
    "price_channel": PriceChannelTrade,
    "sma_stoch": SmaStochastic,
    "envelop_trend": EnvelopTrend,
    "rsi_contrtrend": RsiContrtrend,
    "bollinger": StrategyBollinger,
}


def main() -> int:
    candles = synthetic_series(400, seed=42)
    bh = candles[-1].close - candles[0].close
    print(f"bars={len(candles)} first={candles[0].close:.2f} "
          f"last={candles[-1].close:.2f} buy_hold={bh:+.2f}")
    print(f"{'robot':<16}{'trades':>7}{'win%':>7}{'PF':>8}{'pnl':>9}{'real':>9}"
          f"{'unreal':>8}{'maxDD':>8}{'inMkt%':>8}")
    for name, cls in ROBOTS.items():
        r = run_robot_with_metrics(cls(TesterTab()), candles)
        pf = r["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(f"{name:<16}{r['trades']:>7}{r['win_pct']:>7.1f}{pf_s:>8}"
              f"{r['pnl']:>9.2f}{r['realized']:>9.2f}{r['unrealized']:>8.2f}"
              f"{r['max_dd']:>8.2f}{r['in_market_pct']:>8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
