#!/usr/bin/env python3
"""Volume Exhaustion Шаг 1: статистика «сигнал на входе → exit_reason».

Читает закрытые сделки из compute_ensemble (с volume_features в meta) и считает
delta WR/delta Net по каждому сигналу на входе (V1–V5). Торговлю не трогает.

Usage: python scripts/volume_exhaustion_stats.py [--tickers SMLT,GAZP] [--month 7]
"""
import argparse, asyncio, os, sys, json
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]
V2P = {
    "rsi_reversal": {"period": 16, "oversold": 30, "overbought": 80},
    "bollinger_reclaim": {"period": 15, "k": 1.0},
    "pullback_ema": {"trend_ema": 20, "pull_ema": 10},
    "range_compression_breakout": {"lookback": 15, "atr_period": 16, "pct": 40.0},
    "donchian_breakout": {"period": 45},
    "vwap_reclaim": {"k": 2.0},
    "macd_cross": {"fast": 12, "slow": 26, "signal_period": 9},
}
SIGNALS = ["dryup", "divergence_bear", "divergence_bull", "climax_long", "climax_short",
           "volume_on_drop"]


def optuna_params_dict(p):
    if not p:
        return {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0}
    return {
        "sl_mult": float(p.get("sl_mult", 4.0)),
        "rr": float(p.get("rr", 4.0)),
        "quorum": int(p.get("quorum", 2)),
        "vol_thr": float(p.get("vol_thr", 0.0) or 0.0),
        "active_sids": list(p.get("active_sids", ALL_SIDS)),
        "strategy_params": {k: dict(v) for k, v in (p.get("strategy_params") or {}).items()},
    }


def per_ticker_stats(trades):
    """Совокупная статистика по всем тикерам: on (сигнал есть) vs off."""
    base = {"trades": 0, "wins": 0, "gross_win": 0.0, "gross_loss": 0.0, "net": 0.0}
    by: dict[str, dict] = {}
    by_exit: dict[str, dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0})
    per_sig = {s: {"on": dict(base), "off": dict(base)} for s in SIGNALS}
    per_sig_side = {s: {"LONG": {"on": dict(base), "off": dict(base)},
                        "SHORT": {"on": dict(base), "off": dict(base)}} for s in SIGNALS}
    sig_exit = {s: defaultdict(lambda: {"trades": 0, "wins": 0, "net": 0.0}) for s in SIGNALS}
    for t in trades:
        base["trades"] += 1
        base["net"] += t["net"]
        if t["net"] > 0:
            base["wins"] += 1
            base["gross_win"] += t["net"]
        else:
            base["gross_loss"] += -t["net"]
        by_exit[t["exit_reason"]]["trades"] += 1
        by_exit[t["exit_reason"]]["net"] += t["net"]
        if t["net"] > 0:
            by_exit[t["exit_reason"]]["wins"] += 1
        vf = t.get("volume_features")
        if not vf:
            continue
        side = t.get("side", "LONG")
        for s in SIGNALS:
            bucket = per_sig[s]["on"] if vf.get(s) else per_sig[s]["off"]
            bucket["trades"] += 1
            bucket["net"] += t["net"]
            if t["net"] > 0:
                bucket["wins"] += 1
                bucket["gross_win"] += t["net"]
            else:
                bucket["gross_loss"] += -t["net"]
            sb = per_sig_side[s][side]["on"] if vf.get(s) else per_sig_side[s][side]["off"]
            sb["trades"] += 1
            sb["net"] += t["net"]
            if t["net"] > 0:
                sb["wins"] += 1
                sb["gross_win"] += t["net"]
            else:
                sb["gross_loss"] += -t["net"]
            if vf.get(s):
                eb = sig_exit[s][t["exit_reason"]]
                eb["trades"] += 1
                eb["net"] += t["net"]
                if t["net"] > 0:
                    eb["wins"] += 1
    return base, by_exit, per_sig, per_sig_side, sig_exit


def fmt(b):
    if b["trades"] == 0:
        return f'{"":5} | {"":5} | {b["net"]:+9.2f}₽'
    if "gross_win" in b and "gross_loss" in b:
        pf = b["gross_win"] / b["gross_loss"] if b["gross_loss"] > 1e-9 else float("inf")
        return f'{b["trades"]:5d} | {b["wins"] / b["trades"] * 100:5.1f}% | {b["net"]:+9.2f}₽ | PF {pf:6.2f}'
    wr = b["wins"] / b["trades"] * 100 if b["trades"] else 0.0
    return f'{b["trades"]:5d} | {wr:5.1f}% | {b["net"]:+9.2f}₽'


def print_report(title, base, by_exit, per_sig, per_sig_side, sig_exit):
    print(f"\n=== {title} ===")
    print(f"{'Signal':<16} | {'ON trades':>26} | {'OFF trades':>26} | Delta WR | Delta Net")
    print("-" * 110)
    for s in SIGNALS:
        on, off = per_sig[s]["on"], per_sig[s]["off"]
        if on["trades"] == 0 and off["trades"] == 0:
            continue
        wr_on = on["wins"] / on["trades"] * 100 if on["trades"] else 0.0
        wr_off = off["wins"] / off["trades"] * 100 if off["trades"] else 0.0
        d_wr = wr_on - wr_off
        d_net = on["net"] - off["net"]
        print(f"{s:<16} | {fmt(on):>26} | {fmt(off):>26} | {d_wr:+6.1f}pp | {d_net:+10.2f}₽")

    print("\n  ON-sig side split (LONG / SHORT / vs OFF):")
    for s in SIGNALS:
        row = []
        for side in ("LONG", "SHORT"):
            on = per_sig_side[s][side]["on"]
            off = per_sig_side[s][side]["off"]
            if on["trades"] == 0:
                row.append(f"{side}: -")
                continue
            wr_on = on["wins"] / on["trades"] * 100
            wr_off = off["wins"] / off["trades"] * 100 if off["trades"] else 0.0
            row.append(f"{side}: {on['trades']}ts {wr_on:.0f}% {on['net']:+.0f}₽ (off {wr_off:.0f}%)")
        print(f"  {s:<15} | {' | '.join(row)}")

    print("\n  ON-sig exit_reason:")
    for s in SIGNALS:
        if not any(v["trades"] for v in sig_exit[s].values()):
            continue
        parts = [f"{r}: {b['trades']}t {b['net']:+.0f}₽" for r, b in sig_exit[s].items()]
        print(f"  {s:<15} | {' | '.join(parts)}")

    print(f"\nBaseline total: {fmt(base)}")
    print("\nExit-reason breakdown (all trades):")
    for r, b in sorted(by_exit.items(), key=lambda kv: -kv[1]["net"]):
        print(f"  {r:<16} | {fmt(b)}")
    print()


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="SMLT,GAZP,CHMF,NLMK,MAGN,LENT,NVTK,SNGSP,GMKN,RUAL")
    ap.add_argument("--month", type=int, choices=[7, 8], default=7,
                    help="месяц теста: 7 = JUL (20.07–27.07), 8 = AUG")
    args = ap.parse_args()
    tks = [t.strip() for t in args.tickers.split(",") if t.strip()]

    if args.month == 7:
        from_ts, to_ts = datetime(2026, 7, 20, tzinfo=timezone.utc), datetime(2026, 7, 27, tzinfo=timezone.utc)
    else:
        from_ts, to_ts = datetime(2026, 8, 20, tzinfo=timezone.utc), datetime(2026, 8, 27, tzinfo=timezone.utc)
    warmup = datetime(2026, 5, 15, tzinfo=timezone.utc)

    async def load(figi):
        async with SessionLocal() as db:
            return await _lc(db, figi, 1, date_from=warmup, date_to=to_ts)

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tks})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    all_trades = []
    for tkr in tks:
        if tkr not in meta:
            print(f"{tkr}: NOT FOUND"); continue
        figi, lot, opt = meta[tkr]
        p = optuna_params_dict(opt)
        candles = await load(figi)
        if not candles:
            print(f"{tkr}: no candles"); continue
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
                  for s in ALL_SIDS]
        req = {
            "figi": figi, "bias_mode": "info",
            "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
            "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
            "quorum": p["quorum"], "same_side_reentry_cooldown_bars": 15,
            "carry_overnight": True, "opposite_hold": False, "confirm_flip": 2,
            "neutral_mode": "semi_flip",
            "exit_policy": {"id": "atr_stop",
                            "params": {"period": 14, "multiplier": p["sl_mult"],
                                       "risk_reward": p["rr"]}},
            "commission_rate": 0.0005, "slippage_bps": 2.0,
            "capital": 10000, "lot": lot, "setups": setups,
            "use_all_setups": False, "drop_useless": True,
            "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
            "volume_features": True,
        }
        if p["vol_thr"] and p["vol_thr"] > 0:
            req["volume_filter_threshold"] = p["vol_thr"]
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"{tkr}: ERROR {res['error']}"); continue
        trades = res.get("static", {}).get("trades", [])
        all_trades.extend(trades)
        print(f"{tkr}: {len(trades)} trades")
        with open(f"/tmp/vol_exh_{tkr}_{args.month}.jsonl", "w") as f:
            for tr in trades:
                f.write(json.dumps(tr) + "\n")

    base, by_exit, per_sig, per_sig_side, sig_exit = per_ticker_stats(all_trades)
    print_report(f"Volume Exhaustion deltas, {len(tks)} tickers, month {args.month}",
                 base, by_exit, per_sig, per_sig_side, sig_exit)


if __name__ == "__main__":
    asyncio.run(main())