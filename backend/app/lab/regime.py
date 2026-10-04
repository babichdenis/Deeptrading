"""M5: персист Regime V2 в lab_regime_observations (descriptive, без runtime).

Воркер: один тикер → 1m бары (с warmup) → TF-режим тем же Resampler, что и
сигналы → RegimeProvider(provider="v2") по каждому ТФ → DELETE+COPY.
label/raw кладём в measurements JSON (в модели отдельных колонок нет).
"""
from __future__ import annotations

import hashlib
import io
import json
from datetime import datetime, timezone

from sqlalchemy import text

from app.lab.data import build_tf, engine_sync, iso, load_1m, to_engine_candles

_COLS = ("figi", "ticker", "tf_seconds", "obs_ts", "version", "params_hash",
         "direction", "direction_strength", "trend_strength", "volatility",
         "volatility_percentile", "structure", "confidence", "session",
         "reason_codes", "measurements", "created_at")
_VERSION = "v2.0"
_V2_WINDOW = 12


def _params_hash() -> str:
    blob = json.dumps({"provider": "v2", "window": _V2_WINDOW}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def regime_task(args: dict) -> dict:
    figi = args["figi"]
    res: dict = {"figi": figi, "ticker": args.get("ticker", figi),
                 "by_tf": {}, "error": None}
    try:
        from app.services.regime_v2.provider import RegimeProvider
        eng = engine_sync(args["db_url"])
        bars_1m = load_1m(eng, figi, args["warmup_from"], args["period_to"])
        if not bars_1m:
            return res
        ph = _params_hash()
        now = datetime.now(timezone.utc).isoformat()
        provider = RegimeProvider(provider="v2", v2_window=_V2_WINDOW)
        for tf in args["tfs"]:
            tf_sec = int(args["tf_seconds"][tf])
            bars = bars_1m if tf == "1min" else build_tf(bars_1m, tf)
            # прогреваем ровно так же, как сигналы: полный ряд, затем фильтр периода
            obs_rows = provider.run(to_engine_candles(bars), tf_sec)
            keep = [o for o in obs_rows
                    if args["period_from"] <= str(o["ts"])[:19] <= args["period_to"]]
            rows = []
            for o in keep:
                od = o.get("observation") or {}
                meas = dict(od.get("measurements") or {})
                meas["label"] = o.get("label")
                meas["raw"] = o.get("raw")
                rows.append((
                    figi, res["ticker"], tf_sec, iso(o["ts"]), _VERSION, ph,
                    od.get("direction"), od.get("direction_strength"),
                    od.get("trend_strength"), od.get("volatility"),
                    od.get("volatility_percentile"), od.get("structure"),
                    od.get("confidence"), od.get("session"),
                    json.dumps(od.get("reason_codes") or [], ensure_ascii=False, default=str),
                    json.dumps(meas, ensure_ascii=False, default=str), now))
            if not rows:
                continue
            with eng.begin() as c:
                # чистим только своё окно периода (иначе второй прогон
                # другого месяца затирает наблюдения первого)
                c.execute(text(
                    "DELETE FROM lab_regime_observations WHERE figi = :f AND "
                    "tf_seconds = :s AND version = :v AND params_hash = :p AND "
                    "obs_ts >= :a AND obs_ts <= :b"),
                    {"f": figi, "s": tf_sec, "v": _VERSION, "p": ph,
                     "a": args["period_from"], "b": args["period_to"]})
                drv = c.connection.driver_connection
                dbc = drv.cursor()
                try:
                    buf = io.StringIO()
                    for row in rows:
                        buf.write("\t".join(
                            "\\N" if v is None else str(v).replace("\t", " ") for v in row) + "\n")
                    buf.seek(0)
                    dbc.copy_expert(
                        f"COPY lab_regime_observations ({', '.join(_COLS)}) FROM STDIN", buf)
                finally:
                    dbc.close()
            res["by_tf"][tf] = len(rows)
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    return res
