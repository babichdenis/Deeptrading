#!/usr/bin/env python3
"""Optuna-калибровка порогов режим-детектора (walk-forward: IS июль–авг, OOS сентябрь).

Оптимизирует slope_threshold / adx_threshold / atr_percentile_threshold / range_mult
по net на IS (изолированные 10K/тикер, live-подобная конфигурация + regime_setups_filter),
затем проверяет лучшие параметры на OOS. Пороги прокидываются через req["regime"].

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/optuna_regime_calibration.py --trials 12 --tickers SMLT,GAZP,ROSN,SBER,NVTK,CHMF
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
from app.bot.ensemble_strategy import V2_SETUPS  # noqa: E402

optuna.logging.set_verbosity(optuna.logging.WARNING)

V2P = {s["strategy_id"]: s["params"] for s in V2_SETUPS}
V2P["volume_drop"] = {"ma_len": 20, "drop_ratio": 1.5}
LIVE_SIDS = ["rsi_reversal", "bollinger_reclaim", "vwap_reclaim", "macd_cross",
             "donchian_breakout", "volume_drop"]
# роутер из матрицы: vwap отключаем в NEUTRAL (единственный минус)
ROUTER = {"vwap_reclaim": ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"]}

IS_FROM = datetime(2026, 7, 14, tzinfo=timezone.utc)
IS_TO = datetime(2026, 8, 15, 23, 59, tzinfo=timezone.utc)
OOS_FROM = datetime(2026, 9, 8, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc)

DEFAULT_REG = {"slope_threshold": 0.002, "adx_threshold": 18.0,
               "atr_percentile_threshold": 90.0, "range_mult": 2.5}


def build_req(figi, lot, sl, rr, f, t, reg):
    setups = [{"strategy_id": s, "tf": "5min", "params": dict(V2P.get(s, {}))} for s in LIVE_SIDS]
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "all",
        "quorum": 2, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": sl, "risk_reward": rr}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": setups, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip", "regime_setups_filter": ROUTER,
        "regime": {"tf": "5min", **reg},
        "from_ts": f.isoformat(), "to_ts": t.isoformat(),
    }


async def load_set(tickers, f, t):
    out = {}
    async with SessionLocal() as db:
        for tkr in tickers:
            row = (await db.execute(text(
                "SELECT figi, lot, optuna_params FROM instruments WHERE ticker=:t"), {"t": tkr})).first()
            if not row:
                continue
            c = await _lc(db, row[0], 1, date_from=f, date_to=t)
            if c and len(c) >= 200:
                out[tkr] = (row[0], int(row[1]) if row[1] else 1, row[2] or {}, c)
    return out


def net_of(cmap, f, t, reg):
    tot = 0.0
    tr = 0
    for _tkr, (figi, lot, opt, c) in cmap.items():
        try:
            res = compute_ensemble(c, build_req(figi, lot, float(opt.get("sl_mult", 4.0)),
                                                float(opt.get("rr", 4.0)), f, t, reg))
        except Exception:
            continue
        if "error" in res:
            continue
        ec = res.get("static", {}).get("economic", {})
        tot += float(ec.get("net", 0.0) or 0.0)
        tr += int(ec.get("trades", 0) or 0)
    return tot, tr


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=12)
    ap.add_argument("--tickers", default="SMLT,GAZP,ROSN,SBER,NVTK,CHMF")
    args = ap.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print("Загрузка IS/OOS...")
    is_cmap = await load_set(tickers, IS_FROM, IS_TO)
    oos_cmap = await load_set(tickers, OOS_FROM, OOS_TO)
    print("IS тикеров:", len(is_cmap), "| OOS тикеров:", len(oos_cmap))

    base_is = net_of(is_cmap, IS_FROM, IS_TO, DEFAULT_REG)
    base_oos = net_of(oos_cmap, OOS_FROM, OOS_TO, DEFAULT_REG)
    print("Базовые пороги: IS net=%+.0f (%d) | OOS net=%+.0f (%d)" % (base_is[0], base_is[1], base_oos[0], base_oos[1]))

    def objective(trial):
        reg = {
            "slope_threshold": trial.suggest_float("slope_threshold", 0.0005, 0.004, step=0.0005),
            "adx_threshold": trial.suggest_float("adx_threshold", 12.0, 28.0, step=1.0),
            "atr_percentile_threshold": trial.suggest_float("atr_percentile_threshold", 70.0, 98.0, step=2.0),
            "range_mult": trial.suggest_float("range_mult", 1.5, 4.0, step=0.25),
        }
        net, _ = net_of(is_cmap, IS_FROM, IS_TO, reg)
        return net

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=args.trials)
    best = study.best_params
    print("\nBEST IS:", best, "value=%+.0f" % study.best_value)
    best_oos = net_of(oos_cmap, OOS_FROM, OOS_TO, best)
    print("BEST OOS: net=%+.0f (%d)" % (best_oos[0], best_oos[1]))
    print("Δ vs base: IS %+.0f | OOS %+.0f" % (study.best_value - base_is[0], best_oos[0] - base_oos[0]))


if __name__ == "__main__":
    asyncio.run(main())
