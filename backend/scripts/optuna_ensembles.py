#!/usr/bin/env python3
"""Optuna по порогам ансамблей (ENSEMBLE_CFG) — IS/OOS.

Оптимизирует кворумы, пороги score, ADX/объём/ATR-расширение/перерастяжение + sl/rr
по net на IS, проверяет на OOS. Роутер: long→TREND_UP, short→TREND_DOWN, range→RANGE,
hv→HV, neutral→NEUTRAL. Вход/сторона — из ансамблей (entry_from_setups).

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/optuna_ensembles.py --trials 40
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import optuna  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.services.signals import _load_candles as _lc  # noqa: E402
from app.services.ensemble import compute_ensemble  # noqa: E402
from app.engine.regime_ensembles import ENSEMBLE_CFG  # noqa: E402

optuna.logging.set_verbosity(optuna.logging.WARNING)

SIDS = ["long_ensemble", "short_ensemble", "range_ensemble", "hv_ensemble", "neutral_ensemble"]
ROUTER = {
    "long_ensemble": ["TREND_UP"], "short_ensemble": ["TREND_DOWN"],
    "range_ensemble": ["RANGE"], "hv_ensemble": ["HIGH_VOLATILITY"], "neutral_ensemble": ["NEUTRAL"],
}
IS_FROM = datetime(2026, 7, 14, tzinfo=timezone.utc)
IS_TO = datetime(2026, 8, 15, 23, 59, tzinfo=timezone.utc)
OOS_FROM = datetime(2026, 9, 8, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc)


def build_req(figi, lot, sl, rr, f, t):
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "1min", "entry": {"tf": "1min", "lookback": 1}, "entry_session": "all",
        "quorum": 1, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": [{"strategy_id": s, "tf": "5min", "params": {}} for s in SIDS],
        "use_all_setups": False, "drop_useless": False, "neutral_mode": None,
        "regime_setups_filter": ROUTER, "entry_from_setups": True, "entry_macd_1m": True,
        "regime": {"tf": "hour"},
        "from_ts": f.isoformat(), "to_ts": t.isoformat(),
    }


async def load_set(tickers, f, t):
    out = {}
    async with SessionLocal() as db:
        for tkr in tickers:
            row = (await db.execute(text("SELECT figi, lot FROM instruments WHERE ticker=:t"), {"t": tkr})).first()
            if not row:
                continue
            c = await _lc(db, row[0], 1, date_from=f, date_to=t)
            if c and len(c) >= 200:
                out[tkr] = (row[0], int(row[1]) if row[1] else 1, c)
    return out


def net_of(cmap, f, t, sl, rr):
    tot = 0.0
    tr = 0
    for _t, (figi, lot, c) in cmap.items():
        try:
            res = compute_ensemble(c, build_req(figi, lot, sl, rr, f, t))
        except Exception:
            continue
        if "error" in res:
            continue
        ec = res.get("static", {}).get("economic", {})
        tot += float(ec.get("net", 0.0) or 0.0)
        tr += int(ec.get("trades", 0) or 0)
    return tot, tr


BASE_CFG = dict(ENSEMBLE_CFG)


def apply_cfg(trial):
    ENSEMBLE_CFG.update({
        "k_trend": trial.suggest_int("k_trend", 1, 3),
        "k_neutral": trial.suggest_int("k_neutral", 2, 4),
        "trend_score_min": trial.suggest_int("trend_score_min", 2, 5),
        "vol_struct_min": trial.suggest_int("vol_struct_min", 2, 4),
        "adx_min": trial.suggest_float("adx_min", 15.0, 35.0, step=1.0),
        "vol_mult": trial.suggest_float("vol_mult", 1.0, 3.0, step=0.25),
        "atr_exp_mult": trial.suggest_float("atr_exp_mult", 1.0, 2.5, step=0.25),
        "hv_score_min": trial.suggest_int("hv_score_min", 3, 6),
        "overext_mult": trial.suggest_float("overext_mult", 2.0, 6.0, step=0.5),
    })
    return trial.suggest_float("sl_mult", 3.0, 6.0, step=0.5), trial.suggest_float("rr", 3.0, 8.0, step=0.5)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=40)
    ap.add_argument("--tickers", default="SMLT,GAZP,ROSN,SBER,NVTK,CHMF")
    args = ap.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print("Загрузка IS/OOS...")
    is_cmap = await load_set(tickers, IS_FROM, IS_TO)
    oos_cmap = await load_set(tickers, OOS_FROM, OOS_TO)
    b_is = net_of(is_cmap, IS_FROM, IS_TO, 4.0, 6.0)
    b_oos = net_of(oos_cmap, OOS_FROM, OOS_TO, 4.0, 6.0)
    print("BASE: IS %+.0f (%d) | OOS %+.0f (%d)" % (b_is[0], b_is[1], b_oos[0], b_oos[1]))

    def objective(trial):
        sl, rr = apply_cfg(trial)
        return net_of(is_cmap, IS_FROM, IS_TO, sl, rr)[0]

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=args.trials)
    print("\nBEST IS: %+.0f" % study.best_value)
    print("params:", study.best_params)
    # применить лучшие к OOS
    ENSEMBLE_CFG.clear(); ENSEMBLE_CFG.update(BASE_CFG)
    bp = study.best_params
    ENSEMBLE_CFG.update({k: bp[k] for k in ("k_trend", "k_neutral", "trend_score_min", "vol_struct_min",
                                            "adx_min", "vol_mult", "atr_exp_mult", "hv_score_min", "overext_mult")})
    o = net_of(oos_cmap, OOS_FROM, OOS_TO, bp["sl_mult"], bp["rr"])
    print("BEST OOS: %+.0f (%d)" % (o[0], o[1]))
    print("Δ: IS %+.0f | OOS %+.0f" % (study.best_value - b_is[0], o[0] - b_oos[0]))


if __name__ == "__main__":
    asyncio.run(main())
