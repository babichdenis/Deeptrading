#!/usr/bin/env python3
"""Optuna по функциям, распределённым по режимам (после калибровки детектора).

Роутер: trend_up→TREND_UP, trend_down→TREND_DOWN, range_reversion→RANGE/NEUTRAL,
vwap_reclaim выключен в NEUTRAL, остальные — во всех режимах.
Оптимизируем quorum/sl/rr + параметры новых стратегий по net на IS, проверяем на OOS.

Запуск: PYTHONPATH=. .venv/bin/python3 scripts/optuna_regime_functions.py --trials 30 --tickers SMLT,GAZP,ROSN,SBER,NVTK,CHMF
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

ALL_REG = ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "NEUTRAL", "RANGE"]
V2P = {s["strategy_id"]: dict(s["params"]) for s in V2_SETUPS}
V2P["volume_drop"] = {"ma_len": 20, "drop_ratio": 1.5}
ROUTER = {
    "trend_up": ["TREND_UP"],
    "trend_down": ["TREND_DOWN"],
    "range_reversion": ["RANGE", "NEUTRAL"],
    "vwap_reclaim": ["HIGH_VOLATILITY", "TREND_UP", "TREND_DOWN", "RANGE"],
}
SIDS = list(V2P.keys()) + ["trend_up", "trend_down", "range_reversion"]

IS_FROM = datetime(2026, 7, 14, tzinfo=timezone.utc)
IS_TO = datetime(2026, 8, 15, 23, 59, tzinfo=timezone.utc)
OOS_FROM = datetime(2026, 9, 8, tzinfo=timezone.utc)
OOS_TO = datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc)


def build_req(figi, lot, p, f, t):
    setups = []
    for sid in SIDS:
        params = dict(V2P.get(sid, {}))
        params.update(p.get("params", {}).get(sid, {}))
        setups.append({"strategy_id": sid, "tf": "5min", "params": params})
    return {
        "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
        "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "all",
        "quorum": int(p["quorum"]), "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
        "opposite_hold": False, "confirm_flip": 2,
        "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": p["sl_mult"], "risk_reward": p["rr"]}},
        "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10000, "lot": lot,
        "setups": setups, "use_all_setups": False, "drop_useless": True,
        "neutral_mode": "semi_flip", "regime_setups_filter": ROUTER,
        "from_ts": f.isoformat(), "to_ts": t.isoformat(),
    }


async def load_set(tickers, f, t):
    out = {}
    async with SessionLocal() as db:
        for tkr in tickers:
            row = (await db.execute(text(
                "SELECT figi, lot FROM instruments WHERE ticker=:t"), {"t": tkr})).first()
            if not row:
                continue
            c = await _lc(db, row[0], 1, date_from=f, date_to=t)
            if c and len(c) >= 200:
                out[tkr] = (row[0], int(row[1]) if row[1] else 1, c)
    return out


def net_of(cmap, f, t, p):
    tot = 0.0
    tr = 0
    for _t, (figi, lot, c) in cmap.items():
        try:
            res = compute_ensemble(c, build_req(figi, lot, p, f, t))
        except Exception:
            continue
        if "error" in res:
            continue
        ec = res.get("static", {}).get("economic", {})
        tot += float(ec.get("net", 0.0) or 0.0)
        tr += int(ec.get("trades", 0) or 0)
    return tot, tr


BASE = {"quorum": 2, "sl_mult": 4.0, "rr": 4.0, "params": {}}


def suggest(trial):
    p = {
        "quorum": trial.suggest_int("quorum", 2, 3),
        "sl_mult": trial.suggest_float("sl_mult", 3.0, 6.0, step=0.5),
        "rr": trial.suggest_float("rr", 3.0, 6.0, step=0.5),
        "params": {
            "donchian_breakout": {"period": trial.suggest_int("donchian.period", 20, 80, step=5)},
            "macd_cross": {"fast": 12, "slow": trial.suggest_int("macd.slow", 20, 40, step=2),
                           "signal_period": trial.suggest_int("macd.signal", 5, 12)},
            "volume_drop": {"ma_len": 20, "drop_ratio": trial.suggest_float("vd.drop", 1.0, 2.5, step=0.25)},
            "trend_up": {"sma_long": trial.suggest_int("tu.sma", 100, 250, step=25),
                         "donchian": trial.suggest_int("tu.donchian", 10, 40, step=5),
                         "vol_mult": trial.suggest_float("tu.vol", 1.0, 2.5, step=0.25)},
            "trend_down": {"sma_long": trial.suggest_int("td.sma", 100, 250, step=25),
                           "donchian": trial.suggest_int("td.donchian", 10, 40, step=5),
                           "vol_mult": trial.suggest_float("td.vol", 1.0, 2.5, step=0.25)},
            "range_reversion": {"range_pct": trial.suggest_float("rr.range_pct", 3.0, 12.0, step=1.0),
                                "rsi_oversold": trial.suggest_float("rr.os", 20, 40, step=5),
                                "rsi_overbought": trial.suggest_float("rr.ob", 60, 80, step=5)},
        },
    }
    return p


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--tickers", default="SMLT,GAZP,ROSN,SBER,NVTK,CHMF")
    args = ap.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print("Загрузка IS/OOS...")
    is_cmap = await load_set(tickers, IS_FROM, IS_TO)
    oos_cmap = await load_set(tickers, OOS_FROM, OOS_TO)
    b_is = net_of(is_cmap, IS_FROM, IS_TO, BASE)
    b_oos = net_of(oos_cmap, OOS_FROM, OOS_TO, BASE)
    print("BASE: IS %+.0f (%d) | OOS %+.0f (%d)" % (b_is[0], b_is[1], b_oos[0], b_oos[1]))

    def objective(trial):
        return net_of(is_cmap, IS_FROM, IS_TO, suggest(trial))[0]

    study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=args.trials)
    print("\nBEST IS: %+.0f" % study.best_value)
    print("params:", study.best_params)
    o = net_of(oos_cmap, OOS_FROM, OOS_TO, suggest(study.best_trial))
    print("BEST OOS: %+.0f (%d)" % (o[0], o[1]))
    print("Δ: IS %+.0f | OOS %+.0f" % (study.best_value - b_is[0], o[0] - b_oos[0]))


if __name__ == "__main__":
    asyncio.run(main())
