#!/usr/bin/env python3
"""A2: ML-гейт c адаптивным порогом по входам ансамбля.
Прогон балансов ансамбля (как A), сохраняет только сделки и p на баре входа,
далее отдельный анализ: кривая порог(p-квантиль входов) -> net/PF/WR.
"""
import asyncio, os, sys, json
from datetime import datetime, timezone
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

OOS_FROM = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 9, 13, 0, 0, tzinfo=timezone.utc)
WARMUP = datetime(2026, 5, 15, tzinfo=timezone.utc)

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
FLIP = 2
NEUTRAL = "semi_flip"
ENTRY_SESSION = "main"


def optuna_params_dict(p):
    if not p:
        return {"sl_mult": 4.0, "rr": 4.0, "quorum": 2, "vol_thr": 0.0,
                "active_sids": ALL_SIDS, "strategy_params": dict(V2P)}
    return {
        "sl_mult": float(p.get("sl_mult", 4.0)),
        "rr": float(p.get("rr", 4.0)),
        "quorum": int(p.get("quorum", 2)),
        "vol_thr": float(p.get("vol_thr", 0.0) or 0.0),
        "active_sids": list(p.get("active_sids", ALL_SIDS)),
        "strategy_params": {k: dict(v) for k, v in (p.get("strategy_params") or {}).items()},
    }


def make_req(figi, lot, p, from_ts, to_ts):
    setups = [{"strategy_id": s, "tf": "5min",
               "params": dict(p["strategy_params"].get(s, V2P.get(s, {})))}
              for s in ALL_SIDS]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": ENTRY_SESSION,
        "quorum": p["quorum"], "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": FLIP,
        "exit_policy": {"id": "atr_stop",
                        "params": {"period": 14, "multiplier": p["sl_mult"], "risk_reward": p["rr"]}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": NEUTRAL,
        "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
    }
    if p["vol_thr"] and p["vol_thr"] > 0:
        req["volume_filter_threshold"] = p["vol_thr"]
    return req


async def main():
    import joblib
    m = joblib.load("reports/ml_tb_1m_v3.joblib")
    a = m["audit"]
    mtb = __import__("ml_train_tb")
    tk_full = mtb.TICKERS
    tcode_col = m["features"].index("tcode")
    tcode = np.round(a["X_oos"][:, tcode_col]).astype(int)
    tik = np.array([tk_full[t] for t in tcode])
    ts = a["ts_oos"]; side = a["side_oos"]; p = a["p_o"]
    plookup = {}
    for t in set(tik):
        mm = tik == t
        for s in ("LONG", "SHORT"):
            sm = mm & (side == s)
            plookup.update(zip(zip(np.full(sm.sum(), t), ts[sm], np.full(sm.sum(), s)), p[sm]))
    print(f"ML lookup rows: {len(plookup)}", flush=True)

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": tk_full})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    all_rows = []
    for tkr in tk_full:
        if tkr not in meta:
            continue
        figi, lot, opt = meta[tkr]
        pmean = optuna_params_dict(opt)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=WARMUP, date_to=OOS_TO)
        if not candles:
            continue
        req = make_req(figi, lot, pmean, OOS_FROM, OOS_TO)
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"  {tkr}: ERROR", flush=True); continue
        trades = res.get("static", {}).get("trades", [])
        for t in trades:
            ets = datetime.fromisoformat(t["entry_ts"])
            eps = int(ets.replace(tzinfo=timezone.utc).timestamp())
            pk = plookup.get((tkr, eps, t["side"]), -1.0)
            all_rows.append({"ticker": tkr, "side": t["side"], "net": float(t["net"]),
                             "p": pk})
        print(f"  {tkr}: {len(trades)} trades", flush=True)

    with open("/tmp/ml_tr_a2_rows.jsonl", "w") as f:
        for r in all_rows:
            f.write(json.dumps(r) + "\n")
    ps = [r["p"] for r in all_rows]
    ps_sorted = sorted(ps)
    n0 = len(all_rows); net0 = sum(r["net"] for r in all_rows)
    print(f"\ntotal trades={n0} net(total)={net0:.0f} | p: min/max={min(ps):.3f}/{max(ps):.3f} missing={sum(1 for x in ps if x<0)}")
    for q in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7):
        thq = np.quantile(ps_sorted, q)
        sub = [r for r in all_rows if r["p"] >= thq]
        netsub = sum(r["net"] for r in sub)
        gw = sum(r["net"] for r in sub if r["net"] > 0)
        gl = -sum(r["net"] for r in sub if r["net"] < 0)
        wr = sum(1 for r in sub if r["net"] > 0) / max(1, len(sub))
        print(f"  keep top{(1-q)*100:4.0f}% (p>={thq:.3f}): n={len(sub):4d} net={netsub:8.0f} "
              f"PF={gw/gl:5.2f} WR={wr:.3f}")
    # лучший квартil по итерации
    best = max(((q, np.quantile(ps_sorted, q)) for q in (0.3, 0.4, 0.5, 0.6, 0.7)),
               key=lambda qth: sum(r["net"] for r in all_rows if r["p"] >= qth[1]))
    print(f"\nЛучший порог по p-квантили: keep top{(1-best[0])*100:.0f}% (p>={best[1]:.3f})")


if __name__ == "__main__":
    asyncio.run(main())
