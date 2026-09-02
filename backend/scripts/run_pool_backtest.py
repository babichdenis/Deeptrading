#!/usr/bin/env python3
"""Портфельная симуляция V4 с ОБЩИМ капиталом 10000₽ на 5 акций (август 2026).

Модель: все сделки всех акций сортируются по времени входа. Сделка занимает
капитал (entry_notional) и возвращает его с прибылью/убытком (exit_notional) при
закрытии. Если свободного кэша не хватает на вход — сделка пропускается
(не "влезла"). Так воспроизводится конкуренция за единый капитал.

Варианты: канон (дефолты) и V2 (RSI16/30/80 Boll15/1.0 Pull20/10 Range15/16/40 Donch45).
"""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal

FIGIS = {
    "BBG008F2T3T2": "SBER",
    "BBG004S681M2": "GAZP",
    "BBG004S683W7": "LKOH",
    "BBG004S68CP5": "ROSN",
    "BBG004S681B4": "RUAL",
}

DATE_FROM = datetime(2026, 8, 1, tzinfo=timezone.utc)
DATE_TO = datetime(2026, 9, 1, tzinfo=timezone.utc)

TOTAL_CAPITAL = 10000.0
LOT = 10

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]


def build_params(variant: str) -> dict:
    if variant == "v2":
        return {
            "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
            "bollinger_reclaim": {"period": 15, "k": 1.0},
            "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
            "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
            "donchian_breakout": {"period": 45},
            "vwap_reclaim": {"k": 2.0},
            "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
        }
    return {}


def base_req(figi: str, params_overrides: dict) -> dict:
    setups = [
        {"strategy_id": sid, "tf": "5min", "params": dict(params_overrides.get(sid, {}))}
        for sid in ALL_SIDS
    ]
    return {
        "figi": figi,
        "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "main",
        "quorum": 2,
        "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True,
        "opposite_hold": False,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
        "commission_rate": 0.0005,
        "slippage_bps": 2.0,
        "capital": TOTAL_CAPITAL,
        "lot": LOT,
        "setups": setups,
        "use_all_setups": False,
        "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(),
        "to_ts": DATE_TO.isoformat(),
    }


async def load_august(figi: str):
    from app.services.signals import _load_candles as _lc
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


def simulate_pool(trades_by_figi: dict[str, list[dict]], capital: float) -> dict:
    """Общий пул капитала: сделки по времени, резервирование на входе, возврат на выходе."""
    events = []  # (ts, kind, figi, notional, trade)
    for figi, trades in trades_by_figi.items():
        for t in trades:
            entry_ts = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
            exit_ts = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
            events.append(("open", entry_ts, figi, t["entry_notional"], t))
            events.append(("close", exit_ts, figi, t["exit_notional"], t))
    events.sort(key=lambda e: (e[1], 0 if e[0] == "close" else 1))  # close раньше при равенстве

    cash = capital
    open_trades: dict[tuple, dict] = {}
    executed = 0
    skipped_no_capital = 0
    max_used = 0.0

    for kind, ts, figi, notional, t in events:
        key = id(t)
        if kind == "close":
            if key in open_trades:
                cash += notional  # возвращаем капитал + прибыль/убыток
                del open_trades[key]
        else:  # open
            if cash >= notional:
                cash -= notional
                open_trades[key] = t
                executed += 1
            else:
                skipped_no_capital += 1
        used = capital - cash
        if used > max_used:
            max_used = used

    return {
        "executed": executed,
        "skipped_no_capital": skipped_no_capital,
        "final_cash": round(cash, 2),
        "net": round(cash - capital, 2),
        "max_used": round(max_used, 2),
        "open_at_end": len(open_trades),
    }


async def run_variant(name: str, params_overrides: dict) -> dict:
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    from app.services.ensemble import compute_ensemble

    trades_by_figi = {}
    per_figi_free = {}
    for figi, ticker in FIGIS.items():
        t0 = time.time()
        candles = await load_august(figi)
        load_t = time.time() - t0
        t0 = time.time()
        res = compute_ensemble(candles, base_req(figi, params_overrides))
        ens_t = time.time() - t0
        if "error" in res:
            print(f"  {ticker}: ERROR {res['error']}")
            continue
        trades = res["static"].get("trades", [])
        trades_by_figi[ticker] = trades
        free_net = sum(t["net"] for t in trades)
        per_figi_free[ticker] = free_net
        print(f"  {ticker}: {len(trades)} trades free-cap net={free_net:+.2f}₽ "
              f"(load {load_t:.1f}s ens {ens_t:.1f}s)")

    # 1) независимый прогон (10к на каждую акцию)
    independent_net = sum(per_figi_free.values())
    # 2) общий пул
    pool = simulate_pool(trades_by_figi, TOTAL_CAPITAL)

    print(f"\n  [Независимо, 10к/акцию] net={independent_net:+.2f}₽")
    print(f"  [Общий пул 10к]          net={pool['net']:+.2f}₽ final={pool['final_cash']:.2f}₽")
    print(f"    сделок исполнено: {pool['executed']}  пропущено (нет капитала): {pool['skipped_no_capital']}")
    print(f"    max занято капитала: {pool['max_used']:.2f}₽  открыто на конец: {pool['open_at_end']}")

    return {
        "name": name,
        "independent_net": round(independent_net, 2),
        "pool_net": pool["net"],
        "pool_final": pool["final_cash"],
        "executed": pool["executed"],
        "skipped": pool["skipped_no_capital"],
        "max_used": pool["max_used"],
        "per_figi": {k: round(v, 2) for k, v in per_figi_free.items()},
    }


async def main():
    results = []
    results.append(await run_variant("CANONICAL V4 (defaults)", build_params("canon")))
    results.append(await run_variant("V4 v2 (RSI16/30/80 Boll15/1.0 Pull20/10 Range15/16/40 Donch45)", build_params("v2")))
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for r in results:
        print(f"  {r['name']}")
        print(f"    независимо (10к/акцию): net={r['independent_net']:+.2f}₽")
        print(f"    общий пул (10к на всех): net={r['pool_net']:+.2f}₽ final={r['pool_final']:.2f}₽ "
              f"({r['executed']} exec / {r['skipped']} skipped)")


if __name__ == "__main__":
    asyncio.run(main())
