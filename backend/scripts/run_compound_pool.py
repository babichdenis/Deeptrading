#!/usr/bin/env python3
"""Портфельная симуляция V4 с КОМПАУНДИНГОМ: 20% от текущего equity на позицию.

Ключевое отличие: 20% считается от equity (кэш + стоимость открытых позиций),
а не от фиксированной суммы. Прибыльные сделки растят equity → следующие позиции
крупнее → компаунд-эффект.

Qty пересчитывается: actual_qty = max(int(max_notional / (entry_px × lot)) × lot, lot).
"""
import asyncio
import os
import sys
from datetime import datetime, timezone
from collections import Counter

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
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
        "entry_session": "main", "quorum": 2,
        "same_side_reentry_cooldown_bars": 15, "carry_overnight": True, "opposite_hold": False,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": TOTAL_CAPITAL, "lot": LOT,
        "setups": setups, "use_all_setups": False, "drop_useless": True,
        "from_ts": DATE_FROM.isoformat(), "to_ts": DATE_TO.isoformat(),
    }


async def load_august(figi: str):
    from app.services.signals import _load_candles as _lc
    async with SessionLocal() as db:
        return await _lc(db, figi, 1, date_from=DATE_FROM, date_to=DATE_TO)


def simulate_compound(
    trades_by_figi: dict[str, list[dict]],
    capital: float,
    max_position_pct: float,
    max_open: int,
) -> dict:
    events = []
    for figi, trades in trades_by_figi.items():
        for t in trades:
            entry_ts = datetime.fromisoformat(t["entry_ts"].replace("Z", "+00:00"))
            exit_ts = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
            events.append(("open", entry_ts, figi, t))
            events.append(("close", exit_ts, figi, t))
    events.sort(key=lambda e: (e[1], 0 if e[0] == "close" else 1))

    cash = capital
    open_pos = []
    closed_trades = []
    skipped_cap = 0
    skipped_max = 0
    max_equity = capital
    min_equity = capital
    equity_curve = []

    def get_equity():
        pos_value = sum(p["entry_px"] * p["actual_qty"] for p in open_pos)
        return cash + pos_value

    for kind, ts, figi, t in events:
        if kind == "close":
            pos = None
            for p in open_pos:
                if p["figi"] == figi and p["trade"] is t:
                    pos = p
                    break
            if pos is None:
                continue
            proceeds = t["exit_px"] * pos["actual_qty"]
            cash += proceeds
            orig_qty = t["entry_notional"] / t["entry_px"] if t["entry_px"] > 0 else 1
            scale = pos["actual_qty"] / orig_qty
            actual_net = t["net"] * scale
            closed_trades.append({
                "figi": figi, "net": actual_net, "qty": pos["actual_qty"],
                "entry_px": t["entry_px"], "exit_px": t["exit_px"],
                "bars_held": t["bars_held"], "exit_reason": t["exit_reason"],
                "equity_after": get_equity(),
            })
            open_pos.remove(pos)
        else:
            if len(open_pos) >= max_open:
                skipped_max += 1
                continue
            equity = get_equity()
            max_notional = equity * max_position_pct
            orig_qty = t["entry_notional"] / t["entry_px"] if t["entry_px"] > 0 else 0
            if orig_qty <= 0:
                continue
            actual_qty = max(int(max_notional / (t["entry_px"] * LOT)) * LOT, LOT)
            actual_cost = t["entry_px"] * actual_qty
            if actual_cost > cash:
                actual_qty = max(int(cash / (t["entry_px"] * LOT)) * LOT, LOT)
                if actual_qty < LOT:
                    skipped_cap += 1
                    continue
                actual_cost = t["entry_px"] * actual_qty
            cash -= actual_cost
            open_pos.append({"figi": figi, "trade": t, "actual_qty": actual_qty,
                             "entry_px": t["entry_px"]})

        eq = get_equity()
        equity_curve.append(eq)
        if eq > max_equity:
            max_equity = eq
        if eq < min_equity:
            min_equity = eq

    for p in open_pos:
        t = p["trade"]
        proceeds = t["exit_px"] * p["actual_qty"]
        cash += proceeds
        orig_qty = t["entry_notional"] / t["entry_px"] if t["entry_px"] > 0 else 1
        scale = p["actual_qty"] / orig_qty
        actual_net = t["net"] * scale
        closed_trades.append({
            "figi": p["figi"], "net": actual_net, "qty": p["actual_qty"],
            "entry_px": t["entry_px"], "exit_px": t["exit_px"],
            "bars_held": t["bars_held"], "exit_reason": t["exit_reason"],
            "equity_after": cash,
        })

    total_net = sum(c["net"] for c in closed_trades)
    wins = sum(1 for c in closed_trades if c["net"] > 0)
    gross_pos = sum(c["net"] for c in closed_trades if c["net"] > 0)
    gross_neg = abs(sum(c["net"] for c in closed_trades if c["net"] < 0))
    pf = gross_pos / gross_neg if gross_neg else float("inf")
    drawdown = (max_equity - min_equity) / max_equity * 100 if max_equity > 0 else 0

    return {
        "final_cash": round(cash, 2),
        "net": round(total_net, 2),
        "trades": len(closed_trades),
        "wins": wins,
        "losses": len(closed_trades) - wins,
        "wr": round(wins / max(1, len(closed_trades)) * 100, 1),
        "pf": round(pf, 2),
        "max_equity": round(max_equity, 2),
        "max_dd": round(drawdown, 1),
        "skipped_cap": skipped_cap,
        "skipped_max": skipped_max,
    }


async def run_variant(name, params_overrides, pct, max_open):
    print(f"\n{'='*70}\n{name}  ({pct*100:.0f}%/{max_open})\n{'='*70}")
    from app.services.ensemble import compute_ensemble

    trades_by_figi = {}
    for figi, ticker in FIGIS.items():
        candles = await load_august(figi)
        res = compute_ensemble(candles, base_req(figi, params_overrides))
        if "error" in res:
            continue
        trades = res["static"].get("trades", [])
        trades_by_figi[ticker] = trades
        net_free = sum(t["net"] for t in trades)
        print(f"  {ticker}: {len(trades)} trades free-net={net_free:+.2f}₽")

    independent_net = sum(sum(t["net"] for t in tt) for tt in trades_by_figi.values())
    pool = simulate_compound(trades_by_figi, TOTAL_CAPITAL, pct, max_open)

    print(f"\n  Независимо (10к/акцию): net={independent_net:+.2f}₽")
    print(f"  Пул компаунд ({pct*100:.0f}%/{max_open}): net={pool['net']:+.2f}₽  final={pool['final_cash']:.2f}₽")
    print(f"    сделок: {pool['trades']}  wins={pool['wins']}  losses={pool['losses']}")
    print(f"    WR: {pool['wr']}%  PF: {pool['pf']}  MaxDD: {pool['max_dd']}%")
    print(f"    max_equity: {pool['max_equity']:.2f}₽")
    print(f"    пропущено: no_cap={pool['skipped_cap']}  max_open={pool['skipped_max']}")

    return {"name": name, "independent": independent_net, **pool}


async def main():
    results = []
    results.append(await run_variant("Канон 20%/5", build_params("canon"), 0.20, 5))
    results.append(await run_variant("Канон 20%/3", build_params("canon"), 0.20, 3))
    results.append(await run_variant("V2 20%/5", build_params("v2"), 0.20, 5))
    results.append(await run_variant("V2 20%/3", build_params("v2"), 0.20, 3))
    results.append(await run_variant("V2 10%/5", build_params("v2"), 0.10, 5))
    results.append(await run_variant("V2 30%/5", build_params("v2"), 0.30, 5))

    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    for r in results:
        print(f"  {r['name']:<20} net={r['net']:>+8.2f}₽  trades={r['trades']:>4}  "
              f"WR={r['wr']:>5.1f}%  PF={r['pf']:>5.2f}  MaxDD={r['max_dd']:>5.1f}%  "
              f"skip(cap/max)={r['skipped_cap']}/{r['skipped_max']}")


if __name__ == "__main__":
    asyncio.run(main())
