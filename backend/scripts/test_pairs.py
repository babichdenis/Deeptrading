#!/usr/bin/env python3
"""
test_pairs.py — арбитражные гипотезы на свежих данных из БД.
Результаты в % от notional (сравнимо между парами).

Usage:
    cd backend && .venv/bin/python3 scripts/test_pairs.py
"""
import asyncio
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.chdir(Path(__file__).resolve().parents[1])
from dotenv import load_dotenv
load_dotenv(Path(".env"))

from app.database import SessionLocal
from sqlalchemy import text


def pairs_bt_pct(series_a: list[float], series_b: list[float],
                  window: int = 20, entry_thr: float = 2.0,
                  commission_bps: float = 0, slippage_bps: float = 0) -> dict:
    """
    Pairs spread в % от notional.
    Returns: n_trades, win_rate, net% (after costs), avg_net%, costs%
    """
    if len(series_a) != len(series_b) or len(series_a) < window + 2:
        return {"n_trades": 0}
    a = np.array(series_a, dtype=float)
    b = np.array(series_b, dtype=float)
    spread = a - b
    ma = np.array([np.mean(spread[i-window:i]) for i in range(window, len(spread))])
    s = spread[window:] - ma
    std = np.std(s)
    if std < 1e-10:
        return {"n_trades": 0}
    z = s / std
    # notional = avg price of both legs at entry
    notional = (np.mean(a[window:]) + np.mean(b[window:])) / 2
    comm_pct = commission_bps / 10_000
    slip_pct = slippage_bps / 10_000
    trades_pct = []
    position, entry_px = 0, 0.0
    for i, zi in enumerate(z):
        px_level = (a[i + window] + b[i + window]) / 2
        if position == 0:
            if abs(zi) > entry_thr:
                direction = 1 if zi < 0 else -1
                entry_px = px_level * (1 + slip_pct) if direction == 1 else px_level * (1 - slip_pct)
                position = direction
        else:
            exit_px = px_level * (1 - slip_pct) if position == 1 else px_level * (1 + slip_pct)
            pnl_pct = (exit_px - entry_px) * position / notional
            total_cost_pct = comm_pct * 2
            trades_pct.append(pnl_pct - total_cost_pct)
            position = 0
    n = len(trades_pct)
    if n == 0:
        return {"n_trades": 0}
    net = sum(trades_pct) * 100
    gross = sum(abs(t) for t in trades_pct) * 100
    costs = n * commission_bps * 2 / 10_000 * 100
    return {
        "n_trades": n, "n_win": sum(1 for t in trades_pct if t > 0),
        "net_pct": net, "avg_net_pct": net/n,
        "gross_pct": gross, "costs_pct": costs,
        "max_dd_pct": min(trades_pct) * 100 if trades_pct else 0,
    }


async def fetch_day_prices(figi: str, days: int = 400) -> list[tuple]:
    async with SessionLocal() as session:
        r = await session.execute(text("""
            SELECT ts, close FROM candles
            WHERE figi=:figi AND interval=5
            ORDER BY ts
            LIMIT :days
        """), {"figi": figi, "days": days})
        return [(row[0], float(row[1])) for row in r.fetchall()]


def _align(a: list, b: list):
    ts_a = {t: p for t, p in a}
    ts_b = {t: p for t, p in b}
    common = sorted(set(ts_a) & set(ts_b))
    return [ts_a[t] for t in common], [ts_b[t] for t in common], common


async def get_equity_figis() -> dict[str, str]:
    async with SessionLocal() as session:
        r1 = await session.execute(text("""
            SELECT DISTINCT c.figi FROM candles c WHERE c.interval=5 AND c.figi LIKE 'BBG%'
        """))
        bbg_figis = {row[0] for row in r1.fetchall()}
        r2 = await session.execute(text(
            "SELECT ticker, figi FROM instrument_info WHERE sectype IN ('1','2')"
        ))
        figi_to_ticker = {row[1]: row[0] for row in r2.fetchall()}
    result = {figi_to_ticker[bbg]: bbg for bbg in bbg_figis if bbg in figi_to_ticker}
    big_tcs = {
        'SBER': 'BBG004730N88', 'SBERP': 'BBG0047315Y7',
        'GAZP': 'BBG004730RP0', 'ROSN': 'BBG004731354',
        'LKOH': 'BBG004731032', 'NVTK': 'BBG00475KKY8',
        'VTBR': 'BBG004730ZJ9', 'GMKN': 'BBG004731489',
        'CHMF': 'BBG00475K6C3', 'NLMK': 'BBG004S681B4',
        'RUAL': 'BBG008F2T3T2', 'AFLT': 'BBG004S683W7',
    }
    for ticker, bbg in big_tcs.items():
        if bbg in bbg_figis:
            result[ticker] = bbg
    return result


def print_results(label: str, a_p: list, b_p: list, dates: list,
                  configs: list[tuple]):
    if not a_p:
        return
    avg_price = (np.mean(a_p) + np.mean(b_p)) / 2
    print(f"\n{label} — {len(dates)} aligned days, avg_notional=₽{avg_price:,.0f}")
    print(f"  {'window':>6} {'thr':>4} | {'n':>3} | {'net%_0':>8} {'net%_5':>8} {'costs%':>7} | {'wr%':>6} | {'+ ev%':>8}")
    print("  " + "-"*70)
    for window, thr in configs:
        r0 = pairs_bt_pct(a_p, b_p, window=window, entry_thr=thr, commission_bps=0, slippage_bps=0)
        r5 = pairs_bt_pct(a_p, b_p, window=window, entry_thr=thr, commission_bps=5, slippage_bps=2)
        if r0["n_trades"] > 0:
            wr = r0["n_win"]/r0["n_trades"]*100
            ev = r0["avg_net_pct"] if r0["n_trades"] > 0 else 0
            print(f"  w={window:4d} t={thr:.1f} | {r0['n_trades']:3d} | "
                  f"{r0['net_pct']:+7.3f}% {r5['net_pct']:+7.3f}% {r5['costs_pct']:6.3f}% | "
                  f"{wr:5.1f}% | {ev:+7.3f}%")


async def main():
    print("=== Arbitrage Hypothesis Tests (%, notional-adj) ===")
    figis = await get_equity_figis()
    pairs_to_test = [
        ("SBER", "SBERP", [(10,2.0),(20,2.0),(50,2.0)]),
        ("LKOH", "NVTK",  [(10,2.0),(20,2.0),(50,2.0),(10,1.5),(20,1.5),(50,1.5)]),
        ("CHMF", "NLMK",  [(10,2.0),(20,1.5),(20,2.0),(50,2.0),(10,1.5)]),
        ("GMKN", "RUAL",  [(10,2.0),(20,2.0),(50,2.0)]),
        ("ROSN", "LKOH",  [(20,2.0),(50,2.0)]),
        ("VTBR", "SBER",  [(20,2.0),(50,2.0)]),
        ("AFLT", "ALRS",  [(20,2.0),(50,2.0)]),
    ]

    for ticker_a, ticker_b, configs in pairs_to_test:
        if ticker_a not in figis or ticker_b not in figis:
            print(f"\n{ticker_a}/{ticker_b}: missing figi")
            continue
        a = await fetch_day_prices(figis[ticker_a])
        b = await fetch_day_prices(figis[ticker_b])
        a_p, b_p, dates = _align(a, b)
        if not a_p:
            continue
        print_results(f"{ticker_a}/{ticker_b}", a_p, b_p, dates, configs)

    print("\n=== End ===")
    print("net% = spread P&L / notional, after commission+slippage")
    print("costs% = 2×commission_bps × 100 (round-trip)")
    print("+ ev% = avg_net_pct per trade (edge per signal)")


if __name__ == "__main__":
    asyncio.run(main())
