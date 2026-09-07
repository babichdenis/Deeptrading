import asyncio, sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from datetime import datetime, timezone
from sqlalchemy import text
from app.database import SessionLocal
from app.services.ensemble import compute_ensemble
from app.services.signals import _load_candles as _lc

ALL_SIDS = ["rsi_reversal","bollinger_reclaim","pullback_ema","vwap_reclaim","range_compression_breakout","macd_cross","donchian_breakout"]
V2_PARAMS = {
    "rsi_reversal": {"period":16,"oversold":30,"overbought":80},"bollinger_reclaim":{"period":15,"k":1.0},
    "pullback_ema":{"trend_ema":20,"pull_ema":10},"range_compression_breakout":{"lookback":15,"atr_period":16,"pct":40.0},
    "donchian_breakout":{"period":45},"vwap_reclaim":{"k":2.0},"macd_cross":{"fast":12,"slow":26,"signal_period":9}}

# точная копия из optuna_sweep_params.make_req
def make_req(figi, lot, from_ts, to_ts, sl_mult, rr, quorum, vol_thr, strat_params, confirm_flip=2):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(strat_params.get(s, V2_PARAMS.get(s, {})))} for s in ALL_SIDS]
    req = {
        "figi": figi, "bias_mode": "info",
        "bias": {"tf": "hour", "period": 50}, "entry_tf": "5min",
        "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
        "quorum": quorum, "same_side_reentry_cooldown_bars": 15,
        "carry_overnight": True, "opposite_hold": False, "confirm_flip": confirm_flip,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl_mult, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0,
        "capital": 10000, "lot": lot, "setups": setups,
        "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip",
        "from_ts": from_ts.isoformat(), "to_ts": to_ts.isoformat(),
    }
    if vol_thr and vol_thr > 0:
        req["volume_filter_threshold"] = vol_thr
    return req

async def main():
    async with SessionLocal() as db:
        row = (await db.execute(text("SELECT figi, lot, optuna_params FROM instruments WHERE ticker='SMLT'"))).first()
        figi, lot, opt = row
        c = await _lc(db, figi, 1, date_from=datetime(2026,8,10,tzinfo=timezone.utc), date_to=datetime(2026,8,17,tzinfo=timezone.utc))
    # как в optuna: strat_params для ВСЕХ sids (не только активных!), неактивные = V2
    bp = json.load(open(os.path.join(os.path.dirname(__file__),"..","scripts","optuna_params_results.json")))["SMLT"]["best_params"]
    strat_params = {}
    for sid in ALL_SIDS:
        sp = {}
        for k,v in bp.items():
            if k.startswith(f"{sid}."):
                sp[k.split('.',1)[1]] = v
        strat_params[sid] = sp if sp else dict(V2_PARAMS[sid])
    w1f, w1t = datetime(2026,8,10,tzinfo=timezone.utc), datetime(2026,8,17,tzinfo=timezone.utc)
    req = make_req(figi, lot, w1f, w1t, bp["sl_mult"], bp["rr"], bp["quorum"], bp["vol_thr"], strat_params)
    res = compute_ensemble(c, req)
    st = res.get("static",{})
    print("trades:", len(st.get("trades",[])), "quorum_list:", len(st.get("quorum_list",[])))

asyncio.run(main())
