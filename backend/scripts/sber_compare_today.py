#!/usr/bin/env python3
"""Сравнение эталонного движка compute_ensemble на SBER (сегодня) со сделками бота.
НЕ меняет движок — только вызывает compute_ensemble с теми параметрами,
которые бот берёт из instruments.optuna_params (см. runtime._build_ensemble_params +
ensemble_strategy.on_bar). Цель: воспроизвести сигналы/сделки эталона и показать
расхождения с тем, что реально наторговал бот в sandbox_trades.
"""
import asyncio, json
from datetime import datetime, timezone, timedelta
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
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


async def main():
    async with SessionLocal() as db:
        r = (await db.execute(text(
            "SELECT figi, ticker, lot, optuna_params FROM instruments WHERE ticker='SBER'"))).first()
        figi, lot, opt = r[0], r[2], r[3] or {}

        candles = await _lc(db, figi, 1,
                            date_from=datetime.now(timezone.utc) - timedelta(days=8),
                            date_to=datetime.now(timezone.utc))
        print(f"candles: {len(candles)}  first={candles[0].ts}  last={candles[-1].ts}", flush=True)

        # --- точная линза бота: _build_ensemble_params + on_bar ---
        active = list(opt.get("active_sids", ALL))
        sp = {k: dict(v) for k, v in (opt.get("strategy_params") or {}).items()}
        setups = [{"strategy_id": s, "tf": "5min",
                   "params": dict(sp.get(s, V2P.get(s, {})))} for s in active]
        sl_mult = float(opt.get("sl_mult", 4.0))
        rr = float(opt.get("rr", 4.0))
        quorum = int(opt.get("quorum", 2))
        vol_thr = float(opt.get("vol_thr", 0.0) or 0.0)

        req = {
            "figi": figi, "bias_mode": "info",
            "bias": {"tf": "hour", "period": 50},
            "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1},
            "entry_session": "all", "quorum": quorum,
            "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
            "opposite_hold": False, "confirm_flip": 2,
            "exit_policy": {"id": "atr_stop",
                            "params": {"period": 14, "multiplier": sl_mult, "risk_reward": rr}},
            "commission_rate": 0.0005, "slippage_bps": 2.0,
            "capital": 10000, "lot": lot, "setups": setups,
            "use_all_setups": False, "drop_useless": True,
            "neutral_mode": opt.get("neutral_mode", "semi_flip"),
            "from_ts": candles[0].ts.isoformat(), "to_ts": candles[-1].ts.isoformat(),
        }
        if vol_thr and vol_thr > 0:
            req["volume_filter_threshold"] = vol_thr
        print("=== запрос эталона (как бот из optuna) ===")
        print(json.dumps(req, ensure_ascii=False, indent=1), flush=True)

        res = compute_ensemble(candles, req)
        if "error" in res:
            print("ERROR:", res["error"], res.get("bars")); return
        st = res.get("static", {})

        print("\n=== ВСЕ сделки эталона (60d -> now) ===")
        for t in st.get("trades", []):
            print("%-4s %s -> %s e=%s x=%s %-12s net=%s" % (
                t["side"], t["entry_ts"][:16], t["exit_ts"][:16],
                t["entry_px"], t["exit_px"], t["exit_reason"], t["net"]))

        today = [t for t in st.get("trades", []) if t["entry_ts"][:10] == "2026-09-07"]
        print(f"\n=== СДЕЛКИ ЭТАЛОНА ЗА СЕГОДНЯ (9/7) — N={len(today)} ===")
        for t in today:
            print("%-4s %s -> %s e=%s x=%s %-12s net=%s" % (
                t["side"], t["entry_ts"][11:16], t["exit_ts"][11:16],
                t["entry_px"], t["exit_px"], t["exit_reason"], t["net"]))

        eco = st.get("economic", {})
        print("\n=== экономика эталона ===")
        print({k: eco.get(k) for k in ["trades", "net", "gross", "commission",
                                       "slippage", "win_rate_pct", "profit_factor"]})

        # --- сделки бота за сегодня для сравнения ---
        print("\n=== СДЕЛКИ БОТА за 9/7 (sandbox_trades) ===")
        bs = (await db.execute(text("""
            SELECT side, qty, entry_time, entry_price, exit_time, exit_price,
                   exit_reason, net_pnl, stop_loss, take_profit
            FROM sandbox_trades WHERE ticker='SBER' AND entry_time >= '2026-09-07'
            ORDER BY entry_time
        """))).fetchall()
        for x in bs:
            print("%-4s qty=%s %s -> %s e=%s x=%s %-12s net=%s sl=%s tp=%s" % (
                x[0], x[1],
                x[2].astimezone(timezone.utc).strftime("%H:%M"),
                x[4].astimezone(timezone.utc).strftime("%H:%M") if x[4] else "--",
                x[3], x[5] if x[5] else "--", x[6] or "OPEN", x[7],
                round(x[8], 3) if x[8] else None, round(x[9], 3) if x[9] else None))


asyncio.run(main())
