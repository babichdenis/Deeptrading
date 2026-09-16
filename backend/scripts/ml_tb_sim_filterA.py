#!/usr/bin/env python3
"""Вариант A: ансамбль (V4+V2, optuna-параметры из БД) + ml_tb ML-гейт (top-N% по p).
Сначала прогон compute_ensemble на OOS per ticker (semi_flip, main), затем для каждого
входа берём p модели на 1m-баре ts/сторона и фильтруем сделки p >= th.
Отчёт: без фильтра vs с ML-гейтом (net, PF, WR, keep).
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
FLIP = 2          # confirm_flip
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
    import importlib
    mtb = __import__("ml_train_tb")
    tk_full = mtb.TICKERS
    tcode_col = m["features"].index("tcode")
    tcode = np.round(a["X_oos"][:, tcode_col]).astype(int)
    tik = np.array([tk_full[t] for t in tcode])
    ts = a["ts_oos"]; side = a["side_oos"]; p = a["p_o"]; th = a["th_o"]
    # lookup: (ticker, ts, side) -> p
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

    all_rows = []   # сделки ансамбля: + p на баре входа
    for tkr in tk_full:
        if tkr not in meta:
            continue
        figi, lot, opt = meta[tkr]
        pmean = optuna_params_dict(opt)
        async with SessionLocal() as db:
            candles = await _lc(db, figi, 1, date_from=WARMUP, date_to=OOS_TO)
        if not candles:
            print(f"  {tkr}: no candles", flush=True); continue
        req = make_req(figi, lot, pmean, OOS_FROM, OOS_TO)
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"  {tkr}: ERROR {res['error']}", flush=True); continue
        trades = res.get("static", {}).get("trades", [])
        for t in trades:
            ets = datetime.fromisoformat(t["entry_ts"])
            eps = int(ets.replace(tzinfo=timezone.utc).timestamp())
            pk = plookup.get((tkr, eps, t["side"]))
            all_rows.append({"ticker": tkr, "side": t["side"], "entry_ts": t["entry_ts"],
                             "exit_ts": t["exit_ts"], "net": float(t["net"]),
                             "exit_reason": t["exit_reason"], "p": pk if pk is not None else -1.0})
        print(f"  {tkr}: {len(trades)} trades", flush=True)

    n0 = len(all_rows)
    net0 = sum(r["net"] for r in all_rows)
    g0 = sum(r["net"] for r in all_rows if r["net"] > 0)
    l0 = -sum(r["net"] for r in all_rows if r["net"] < 0)
    rows_gate = [r for r in all_rows if r["p"] >= th]
    n1 = len(rows_gate)
    net1 = sum(r["net"] for r in rows_gate)
    g1 = sum(r["net"] for r in rows_gate if r["net"] > 0)
    l1 = -sum(r["net"] for r in rows_gate if r["net"] < 0)
    out = {
        "period": [OOS_FROM.isoformat(), OOS_TO.isoformat()],
        "ml_threshold": th,
        "baseline_ensemble": {
            "trades": n0, "net": round(net0, 2), "gross_win": round(g0, 2),
            "gross_loss": round(l0, 2),
            "pf": round(g0 / l0, 2) if l0 else None,
            "wr": round(sum(1 for r in all_rows if r["net"] > 0) / max(1, n0), 4),
        },
        "ml_gate": {
            "trades": n1, "net": round(net1, 2), "gross_win": round(g1, 2),
            "gross_loss": round(l1, 2),
            "pf": round(g1 / l1, 2) if l1 else None,
            "wr": round(sum(1 for r in rows_gate if r["net"] > 0) / max(1, n1), 4),
            "keep_pct": round(n1 / max(1, n0), 4),
            "missing_p": round(sum(1 for r in all_rows if r["p"] < 0) / max(1, n0), 4),
        },
    }
    print("\n=== Вариант A: ансамбль + ML-гейт (OOS) ===")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    with open("reports/ml_tb_sim_A.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
