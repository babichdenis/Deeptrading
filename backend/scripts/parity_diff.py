#!/usr/bin/env python3
"""Паритет live vs compute_ensemble по одному тикеру/тесту.

Печатает сделки из sandbox_trades (runtime) и сделки compute_ensemble за тот же период,
чтобы глазами найти расхождения (вход/выход/причина/режим).

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/parity_diff.py <test_name> <ticker> [YYYY-MM-DD] [YYYY-MM-DD]
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text

from app.database import SessionLocal
from app.services.signals import _load_candles as _lc
from app.services.ensemble import compute_ensemble, resample
from app.services.regime import RegimeDetector, regime_at
from app.bot.ensemble_strategy import V2_SETUPS

V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
V2P["volume_drop"] = {"ma_len": 20, "drop_ratio": 1.5}
LIVE_SIDS = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross",
             "donchian_breakout", "volume_drop"]
BOL_FILTER = {"bollinger_reclaim": ["NEUTRAL", "TREND_UP", "TREND_DOWN"]}


def build_req(figi, lot, sl, rr, f, t):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in LIVE_SIDS]
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "all",
        "quorum": 2, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": setups, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip", "regime_setups_filter": BOL_FILTER,
        "from_ts": f.isoformat(), "to_ts": t.isoformat(),
    }


async def main():
    test_name = sys.argv[1]
    ticker = sys.argv[2]
    async with SessionLocal() as db:
        row = (await db.execute(text(
            "SELECT figi, lot, optuna_params FROM instruments WHERE ticker=:t"), {"t": ticker})).first()
        figi, lot, opt = row
        lot = int(lot) if lot else 1
        opt = opt or {}
        rows = (await db.execute(text(
            "SELECT side, entry_time, exit_time, entry_price, exit_price, exit_reason, net_pnl, meta "
            "FROM sandbox_trades WHERE test_name=:n AND ticker=:t ORDER BY entry_time"),
            {"n": test_name, "t": ticker})).fetchall()
        if len(sys.argv) >= 5:
            f = datetime.fromisoformat(sys.argv[3] + "T00:00:00+00:00")
            t = datetime.fromisoformat(sys.argv[4] + "T23:59:00+00:00")
        elif rows:
            f = min(r[1] for r in rows if r[1]) - timedelta(minutes=5)
            t = max((r[2] or r[1]) for r in rows if r[1]) + timedelta(minutes=5)
        else:
            print("нет сделок в БД и не задан период"); return
        candles = await _lc(db, figi, 1, date_from=f, date_to=t)

    print(f"### {ticker} {test_name}  период {f.isoformat()[:16]} .. {t.isoformat()[:16]}  свечей={len(candles)}")
    print(f"### optuna sl={opt.get('sl_mult')} rr={opt.get('rr')} q={opt.get('quorum')} vol={opt.get('vol_thr')}")

    print("\n--- LIVE (runtime, sandbox_trades) ---")
    for r in rows:
        meta = json.loads(r[7]) if r[7] else {}
        print("%-4s in=%s ex=%s  %8.3f -> %8.3f  %-12s net=%+8.2f  regime=%s" % (
            r[0], str(r[1])[11:16], (str(r[2])[11:16] if r[2] else "--"), r[3] or 0, r[4] or 0,
            r[5] or "?", r[6] or 0, meta.get("regime")))

    res = compute_ensemble(candles, build_req(figi, lot, float(opt.get("sl_mult", 4.0)),
                                              float(opt.get("rr", 4.0)), f, t))
    if "error" in res:
        print("\ncompute_ensemble error:", res); return
    st = res.get("static", {})
    trades = st.get("trades", [])
    tl = RegimeDetector().compute(resample(candles, 300) or []) or []
    print("\n--- BACKTEST (compute_ensemble)  trades=%d ---" % len(trades))
    for tr in trades:
        et = tr.get("entry_ts", "")
        r = regime_at(tl, datetime.fromisoformat(et)) if (et and tl) else None
        print("%-4s in=%s ex=%s  %8.3f -> %8.3f  %-12s net=%+8.2f  regime=%s" % (
            tr.get("side"), et[11:16], str(tr.get("exit_ts", ""))[11:16],
            tr.get("entry_px", 0), tr.get("exit_px", 0), tr.get("exit_reason", "?"),
            tr.get("net", 0), r["state"] if r else "NO_REGIME"))

    ec = st.get("economic", {})
    print("\nLIVE net=%+.2f  BACKTEST net=%+.2f" % (sum(r[6] or 0 for r in rows), ec.get("net", 0)))


if __name__ == "__main__":
    asyncio.run(main())
