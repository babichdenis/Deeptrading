#!/usr/bin/env python3
"""Прогон канонического V4 ЧЕРЕЗ ДВИЖОК (compute_ensemble → static.trades).

Движок бота (EnsembleV4Strategy) в live вызывает compute_ensemble и исполняет
входы. Бэктест-эквивалент: compute_ensemble(candles, req) возвращает static.trades —
ровно те сделки, которые движок воспроизвёл бы на этой истории (тот же qty,
SL/TP, комиссия, slippage). Здесь мы берём эти trades напрямую.

Канон: дефолтные параметры всех стратегий.
V2: RSI16/30/80, Boll15/1.0, Pull20/10, Range15/16/40, Donch45.
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

CAPITAL = 10000.0
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
        "capital": CAPITAL,
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


async def run_variant(name: str, params_overrides: dict) -> dict:
    print(f"\n{'='*70}\n{name}\n{'='*70}")
    from app.services.ensemble import compute_ensemble

    per_figi = {}
    all_trades = []
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
        econ = res["static"].get("economic", {})
        qty_shares = (res.get("meta") or {}).get("qty_shares")
        net = sum(t["net"] for t in trades)
        per_figi[ticker] = {"trades": len(trades), "net": net}
        all_trades.extend(trades)
        print(f"  {ticker}: {len(trades)} trades net={net:+.2f}₽ "
              f"(load {load_t:.1f}s ens {ens_t:.1f}s qty_shares={qty_shares})")

    wins = sum(1 for t in all_trades if t["net"] > 0)
    losses = sum(1 for t in all_trades if t["net"] <= 0)
    total = sum(t["net"] for t in all_trades)
    gross_pos = sum(t["net"] for t in all_trades if t["net"] > 0)
    gross_neg = abs(sum(t["net"] for t in all_trades if t["net"] < 0))
    pf = gross_pos / gross_neg if gross_neg else float("inf")

    print(f"\n  TOTAL: {len(all_trades)} trades (wins={wins} losses={losses})")
    print(f"  NET: {total:+.2f}₽  |  Final equity: {CAPITAL + total:.2f}₽")
    print(f"  Win rate: {wins/max(1,len(all_trades))*100:.1f}%  |  PF: {pf:.2f}")
    for ticker, d in per_figi.items():
        print(f"    {ticker}: {d['trades']} trades {d['net']:+.2f}₽")

    return {
        "name": name,
        "trades": len(all_trades),
        "wins": wins,
        "losses": losses,
        "net": round(total, 2),
        "final_equity": round(CAPITAL + total, 2),
        "win_rate": round(wins / max(1, len(all_trades)) * 100, 1),
        "pf": round(pf, 2),
        "per_figi": {k: round(v["net"], 2) for k, v in per_figi.items()},
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
        print(f"    net={r['net']:+.2f}₽ trades={r['trades']} wr={r['win_rate']}% pf={r['pf']}")
        print(f"    per_figi: {r['per_figi']}")


if __name__ == "__main__":
    asyncio.run(main())
