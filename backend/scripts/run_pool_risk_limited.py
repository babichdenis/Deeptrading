#!/usr/bin/env python3
"""Портфельная симуляция V4 с РИСК-ЛИМИТОМ: доля капитала на позицию.

Общий пул 10 000₽ на 5 акций. Каждая позиция занимает не более
max_position_pct от текущего equity. Максимум открытых позиций = max_open.
При закрытии позиции капитал возвращается с прибылью/убытком (пропорционально).

Qty пересчитывается: actual_qty = max(int(max_notional / (entry_px × lot)) × lot, lot).
Если 실제 cost > доступного кэша — уменьшаем до минимума, если не хватает — пропуск.
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


def simulate_pool_risk_limited(
    trades_by_figi: dict[str, list[dict]],
    capital: float,
    max_position_pct: float = 0.20,
    max_open: int = 5,
) -> dict:
    """Пул с риск-лимитом: каждая позиция ≤ max_position_pct от equity."""
    events = []
    for figi, trades in trades_by_figi.items():
        for t in trades:
            entry_ts = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
            exit_ts = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
            events.append(("open", entry_ts, figi, t))
            events.append(("close", exit_ts, figi, t))
    events.sort(key=lambda e: (e[1], 0 if e[0] == "close" else 1))

    cash = capital
    open_positions: list[dict] = []
    closed_trades: list[dict] = []
    skipped_no_capital = 0
    skipped_max_open = 0
    max_equity = capital
    min_equity = capital

    for kind, ts, figi, t in events:
        if kind == "close":
            pos = None
            for p in open_positions:
                if p["figi"] == figi and p["trade"] is t:
                    pos = p
                    break
            if pos is None:
                continue
            actual_exit_notional = t["exit_px"] * pos["actual_qty"]
            cash += actual_exit_notional
            scale = pos["actual_qty"] / (t["entry_notional"] / t["entry_px"]) if t["entry_px"] > 0 else 1.0
            actual_net = t["net"] * scale
            closed_trades.append({
                "figi": figi, "net": actual_net, "qty": pos["actual_qty"],
                "entry_px": t["entry_px"], "exit_px": t["exit_px"],
                "bars_held": t["bars_held"], "exit_reason": t["exit_reason"],
                "original_net": t["net"],
            })
            open_positions.remove(pos)
        else:
            if len(open_positions) >= max_open:
                skipped_max_open += 1
                continue
            equity = cash + sum(
                p["trade"]["exit_px"] * p["actual_qty"]  # mark-to-market на exit_px (approx)
                for p in open_positions
            )
            max_notional = equity * max_position_pct
            original_qty = t["entry_notional"] / t["entry_px"] if t["entry_px"] > 0 else 0
            if original_qty <= 0:
                continue
            actual_qty = max(int(max_notional / (t["entry_px"] * LOT)) * LOT, LOT)
            actual_cost = t["entry_px"] * actual_qty
            if actual_cost > cash:
                actual_qty = max(int(cash / (t["entry_px"] * LOT)) * LOT, LOT)
                if actual_qty < LOT:
                    skipped_no_capital += 1
                    continue
                actual_cost = t["entry_px"] * actual_qty
            cash -= actual_cost
            open_positions.append({
                "figi": figi, "trade": t, "actual_qty": actual_qty,
                "actual_cost": actual_cost,
            })

        equity_now = cash + sum(
            p["trade"]["exit_px"] * p["actual_qty"]
            for p in open_positions
        )
        if equity_now > max_equity:
            max_equity = equity_now
        if equity_now < min_equity:
            min_equity = equity_now

    # Закрываем оставшиеся позиции по последней цене
    for p in open_positions:
        t = p["trade"]
        actual_exit_notional = t["exit_px"] * p["actual_qty"]
        cash += actual_exit_notional
        scale = p["actual_qty"] / (t["entry_notional"] / t["entry_px"]) if t["entry_px"] > 0 else 1.0
        actual_net = t["net"] * scale
        closed_trades.append({
            "figi": p["figi"], "net": actual_net, "qty": p["actual_qty"],
            "entry_px": t["entry_px"], "exit_px": t["exit_px"],
            "bars_held": t["bars_held"], "exit_reason": t["exit_reason"],
            "original_net": t["net"],
        })

    total_net = sum(c["net"] for c in closed_trades)
    wins = sum(1 for c in closed_trades if c["net"] > 0)
    drawdown = (max_equity - min_equity) / max_equity * 100 if max_equity > 0 else 0

    return {
        "final_cash": round(cash, 2),
        "net": round(total_net, 2),
        "trades_executed": len(closed_trades),
        "skipped_no_capital": skipped_no_capital,
        "skipped_max_open": skipped_max_open,
        "wins": wins,
        "losses": len(closed_trades) - wins,
        "win_rate": round(wins / max(1, len(closed_trades)) * 100, 1),
        "max_equity": round(max_equity, 2),
        "min_equity": round(min_equity, 2),
        "max_drawdown_pct": round(drawdown, 1),
    }


async def run_variant(name: str, params_overrides: dict, max_pos_pct: float, max_open: int) -> dict:
    print(f"\n{'='*70}\n{name}  (max_pos={max_pos_pct*100:.0f}%  max_open={max_open})\n{'='*70}")
    from app.services.ensemble import compute_ensemble

    trades_by_figi = {}
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
        print(f"  {ticker}: {len(trades)} trades free-net={free_net:+.2f}₽ ({load_t:.1f}s + {ens_t:.1f}s)")

    independent_net = sum(sum(t["net"] for t in trades) for trades in trades_by_figi.values())
    pool = simulate_pool_risk_limited(trades_by_figi, TOTAL_CAPITAL, max_pos_pct, max_open)

    print(f"\n  [Независимо 10к/акцию]  net={independent_net:+.2f}₽")
    print(f"  [Пул 10к + риск-лимит]  net={pool['net']:+.2f}₽  final={pool['final_cash']:.2f}₽")
    print(f"    сделок: {pool['trades_executed']}  (wins={pool['wins']} losses={pool['losses']})")
    print(f"    win_rate: {pool['win_rate']}%  max_dd: {pool['max_drawdown_pct']}%")
    print(f"    пропущено: no_capital={pool['skipped_no_capital']}  max_open={pool['skipped_max_open']}")

    return {
        "name": name,
        "max_pos_pct": max_pos_pct,
        "max_open": max_open,
        "independent_net": round(independent_net, 2),
        "pool_net": pool["net"],
        "pool_final": pool["final_cash"],
        "pool_trades": pool["trades_executed"],
        "pool_wr": pool["win_rate"],
        "pool_pf": round(
            sum(c["net"] for c in [] if c["net"] > 0) / max(0.01, abs(sum(c["net"] for c in [] if c["net"] < 0)))
            , 2
        ),
        "pool_max_dd": pool["max_drawdown_pct"],
        "pool_skipped": pool["skipped_no_capital"] + pool["skipped_max_open"],
    }


async def main():
    scenarios = [
        ("Канон 20%/5", "canon", 0.20, 5),
        ("Канон 20%/3", "canon", 0.20, 3),
        ("Канон 10%/5", "canon", 0.10, 5),
        ("V2 20%/5", "v2", 0.20, 5),
        ("V2 20%/3", "v2", 0.20, 3),
        ("V2 10%/5", "v2", 0.10, 5),
    ]
    results = []
    for name, variant, pct, max_open in scenarios:
        results.append(await run_variant(name, build_params(variant), pct, max_open))

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"  {'Сценарий':<20} {'Независимо':>12} {'Пул net':>12} {'Финал':>10} {'Trades':>7} {'WR':>6} {'MaxDD':>7}")
    print(f"  {'-'*74}")
    for r in results:
        print(f"  {r['name']:<20} {r['independent_net']:>+11.2f}₽ {r['pool_net']:>+11.2f}₽ "
              f"{r['pool_final']:>9.2f}₽ {r['pool_trades']:>6} {r['pool_wr']:>5.1f}% {r['pool_max_dd']:>6.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
