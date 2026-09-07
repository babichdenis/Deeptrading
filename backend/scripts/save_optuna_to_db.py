#!/usr/bin/env python3
"""Сохранить результаты Optuna (optuna_params_results.json) в instruments.optuna_params.

Формат optuna_params в БД:
{
    "version": 2,                       # версия схемы параметров
    "trained_on": "w1_2026-08-10_17",   # что было неделей обучения
    "source": "optuna_sweep_params_v2",
    "sl_mult": 4.5,
    "rr": 5.5,
    "quorum": 2,
    "vol_thr": 0.4,
    "neutral_mode": "semi_flip",
    "active_sids": ["bollinger_reclaim", "vwap_reclaim"],
    "strategy_params": {                # только активные стратегии
        "bollinger_reclaim": {"period": 15, "k": 0.5},
        ...
    },
    "oos_w2_net": ..., "oos_w3_net": ...,   # для сравнения при переобучении
    "baseline_w2_net": ..., "baseline_w3_net": ...,
}
"""
import asyncio, os, sys, json
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import text
from app.database import SessionLocal

ALL_SIDS = ["rsi_reversal", "bollinger_reclaim", "pullback_ema", "vwap_reclaim",
            "range_compression_breakout", "macd_cross", "donchian_breakout"]


def convert_ticker_result(ticker, r):
    bp = r["best_params"]
    active = [s for s in ALL_SIDS if bp.get(f"inc_{s}", False)]
    if not active:
        active = ["pullback_ema"]

    strategy_params = {}
    for sid in active:
        sp = {}
        for k, v in bp.items():
            if k.startswith(f"{sid}."):
                sp[k.split(".", 1)[1]] = v
        strategy_params[sid] = sp

    return {
        "version": 2,
        "trained_on": "w1_2026-08-10..17",
        "source": "optuna_sweep_params_v2",
        "sl_mult": bp["sl_mult"],
        "rr": bp["rr"],
        "quorum": bp["quorum"],
        "vol_thr": bp["vol_thr"],
        "neutral_mode": "semi_flip",
        "active_sids": active,
        "strategy_params": strategy_params,
        "oos_w2_net": r["opt"]["w2"][0],
        "oos_w3_net": r["opt"]["w3"][0],
        "baseline_w2_net": r["baseline"]["w2"][0],
        "baseline_w3_net": r["baseline"]["w3"][0],
    }


async def main():
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "optuna_params_results.json")
    with open(json_path) as f:
        results = json.load(f)

    now = datetime.now(timezone.utc)
    saved = 0
    missing = []
    async with SessionLocal() as db:
        for ticker, r in results.items():
            # find instrument by figi (results keyed by ticker)
            row = (await db.execute(text(
                "SELECT id, figi, lot FROM instruments WHERE ticker = :tk"),
                {"tk": ticker})).first()
            if row is None:
                # try by figi match
                row = (await db.execute(text(
                    "SELECT id, figi, lot FROM instruments WHERE figi = :f"),
                    {"f": r.get("figi")})).first()
            if row is None:
                missing.append(ticker)
                continue
            payload = convert_ticker_result(ticker, r)
            payload_json = json.dumps(payload, ensure_ascii=False, default=str)
            await db.execute(text(
                "UPDATE instruments SET optuna_params = CAST(:p AS JSONB), "
                "optuna_trained_at = :t, optuna_source = :src WHERE id = :id"),
                {"p": payload_json, "t": now, "src": payload["source"], "id": row[0]})
            saved += 1
            print(f"  {ticker:6s} saved: sl={payload['sl_mult']} rr={payload['rr']} "
                  f"q={payload['quorum']} vol={payload['vol_thr']} "
                  f"active={payload['active_sids']}")
        await db.commit()
    print(f"\nSaved {saved}/{len(results)}")
    if missing:
        print("NOT FOUND:", missing)


if __name__ == "__main__":
    asyncio.run(main())
