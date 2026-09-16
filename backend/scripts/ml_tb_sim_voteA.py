#!/usr/bin/env python3
"""Вариант C: ML как ГОЛОС в ансамбле (8-й участник кворума).
Прогон compute_ensemble на OOS с req.ml_vote = {threshold, tcode}.
Сравнение в конце: baseline (без голоса) vs c ML-голосом — на тех же тикерах.
"""
import asyncio, os, sys, json
from datetime import datetime, timezone
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

TICKERS_ORDER = ["SBER","GAZP","LKOH","ROSN","RUAL","SNGSP","AFLT","MVID","NLMK","SMLT",
                 "NVTK","MAGN","TRNFP","T","ASTR","MTSS","CHMF","LENT","SFIN","GMKN",
                 "VKCO","YDEX","RNFT","IRKT"]


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


def make_req(figi, lot, p, from_ts, to_ts, ml_vote=None):
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
    if ml_vote is not None:
        req["ml_vote"] = ml_vote
    return req


async def run_one(tkr, figi, lot, pmean, ml_vote):
    async with SessionLocal() as db:
        candles = await _lc(db, figi, 1, date_from=WARMUP, date_to=OOS_TO)
    if not candles:
        return None
    req = make_req(figi, lot, pmean, OOS_FROM, OOS_TO, ml_vote)
    res = compute_ensemble(candles, req)
    if "error" in res:
        return {"error": res["error"]}
    st = res.get("static", {})
    tr = st.get("trades", [])
    return {"trades": len(tr), "net": sum(float(t["net"]) for t in tr),
            "gw": sum(max(0.0, float(t["net"])) for t in tr),
            "gl": -sum(min(0.0, float(t["net"])) for t in tr),
            "wins": sum(1 for t in tr if float(t["net"]) > 0)}


async def main():
    import joblib
    m = joblib.load("reports/ml_tb_1m_v3.joblib")
    clf = m["clf"]
    mtb = __import__("ml_train_tb")
    tcode_map = {t: i for i, t in enumerate(mtb.TICKERS)}

    args = sys.argv[1:] if len(sys.argv) > 1 else ["0.34", "0.36", "0.38", "0.40"]
    thresholds = [float(x) for x in args]
    out = {"period": [OOS_FROM.isoformat(), OOS_TO.isoformat()],
           "thresholds": thresholds, "rows": {}}

    async with SessionLocal() as db:
        rows = (await db.execute(text("""
            SELECT ticker, figi, lot, optuna_params FROM instruments
            WHERE ticker = ANY(:tk) AND NOT (ticker='T' AND figi='BBG000BSJK37')
            ORDER BY ticker"""), {"tk": TICKERS_ORDER})).fetchall()
    meta = {r.ticker: (r.figi, r.lot, r.optuna_params) for r in rows}

    for tkr in TICKERS_ORDER:
        if tkr not in meta:
            continue
        figi, lot, opt = meta[tkr]
        pmean = optuna_params_dict(opt)
        base = await run_one(tkr, figi, lot, pmean, None)
        if base is None or "error" in base:
            print(f"  {tkr}: skip"); continue
        row = {"baseline": base, "ml_vote": {}}
        tc = tcode_map.get(tkr)
        for th in thresholds:
            mv = {"threshold": th, "tcode": tc,
                  "model_path": "reports/ml_tb_1m_v3.joblib"}
            r = await run_one(tkr, figi, lot, pmean, mv)
            row["ml_vote"][str(th)] = r if r and "error" not in r else {"error": ".."}
        out["rows"][tkr] = row
        m0 = row["baseline"]; print(f"  {tkr}: base n={m0['trades']} net={m0['net']:.0f}", flush=True)
        for th in thresholds:
            r = row["ml_vote"][str(th)]
            print(f"      vote@{th}: n={r.get('trades')} net={r.get('net', 0):.0f}", flush=True)

    # Aggregate
    agg_base = {"trades": 0, "net": 0.0, "gw": 0.0, "gl": 0.0, "wins": 0}
    agg_vote = {str(th): dict(agg_base) for th in thresholds}
    for tkr, row in out["rows"].items():
        b = row["baseline"]
        for k in agg_base:
            agg_base[k] += b[k]
        for th in thresholds:
            v = row["ml_vote"][str(th)]
            if "error" in v:
                continue
            for k in agg_base:
                agg_vote[str(th)][k] += v[k]
    print("\n=== Вариант C: ML как голос в кворуме (OOS) ===")
    print(f"{'Config':<14} {'Trades':>7} {'Wins':>5} {'GW':>10} {'GL':>8} {'Net':>10} {'PF':>5} {'WR':>6}")
    def _line(name, a):
        pf = a["gw"]/a["gl"] if a["gl"] else None
        wr = a["wins"]/max(1, a["trades"])
        print(f"{name:<14} {a['trades']:>7} {a['wins']:>5} {a['gw']:>10.0f} {a['gl']:>8.0f} "
              f"{a['net']:>10.0f} {pf if pf else 0:>5.2f} {wr:>6.3f}")
    _line("baseline", agg_base)
    for th in thresholds:
        _line(f"ml_vote@{th}", agg_vote[str(th)])
    out["aggregate"] = {"baseline": agg_base,
                        "ml_vote": {str(th): agg_vote[str(th)] for th in thresholds}}
    with open("reports/ml_tb_sim_C.json", "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
