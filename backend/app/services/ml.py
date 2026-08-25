from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.indicators import atr as atr_series
from app.services.indicators import ema as ema_series, rsi as rsi_series, sma as sma_series
from app.services.tinvest import INTERVAL_NAMES
from app.engine.models import Candle as EngineCandle
from app.models.candle import Candle as CandleModel
from app.models.ml import MlModel, MlPrediction
from app.models.signals import StrategyRun, StrategySignal

_MSK = ZoneInfo("Europe/Moscow")

FEATURE_NAMES = [
    "rsi",
    "atr_pct",
    "ema50_slope_pct",
    "vol_ratio",
    "dist_sma20_pct",
    "hour_sin",
    "hour_cos",
    "side_long",
]

HORIZON_BARS = 20
R_MULTIPLE = 1.0


async def _load_candles(db: AsyncSession, figi: str, interval_value: int) -> list[EngineCandle]:
    rows = await db.execute(
        select(CandleModel)
        .where(CandleModel.figi == figi, CandleModel.interval == interval_value)
        .order_by(CandleModel.ts)
    )
    return [
        EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high), low=float(r.low),
                     close=float(r.close), volume=float(r.volume))
        for r in rows.scalars()
    ]


def _indicator_context(candles: list[EngineCandle]) -> dict:
    closes = [c.close for c in candles]
    vols = [c.volume for c in candles]
    return {
        "rsi": rsi_series(closes, 14),
        "atr": atr_series(candles, 14),
        "ema50": ema_series(closes, 50),
        "sma20": sma_series(closes, 20),
        "vol_sma20": sma_series(vols, 20),
    }


def _features_at(candles: list[EngineCandle], i: int, ctx: dict, side: str) -> list[float] | None:
    if i < 55:
        return None
    c = candles[i]
    rsi = ctx["rsi"][i]
    atr = ctx["atr"][i]
    ema = ctx["ema50"][i]
    sma = ctx["sma20"][i]
    vsma = ctx["vol_sma20"][i]
    if None in (rsi, atr, ema, sma) or not vsma or c.close == 0:
        return None
    local = c.ts.astimezone(_MSK)
    hour_angle = (local.hour + local.minute / 60) / 24 * 2 * math.pi
    return [
        float(rsi) / 100.0,
        float(atr) / c.close * 10.0,
        (float(ema) - float(ctx["ema50"][max(0, i - 5)])) / c.close * 100.0,
        min(c.volume / float(vsma), 5.0),
        (c.close - float(sma)) / c.close * 10.0,
        math.sin(hour_angle),
        math.cos(hour_angle),
        1.0 if side == "BUY" else -1.0,
    ]


def _label_at(candles: list[EngineCandle], i: int, horizon: int, r_mult: float, ctx: dict) -> int | None:
    if i + horizon >= len(candles):
        return None
    entry = candles[i].close
    risk = ctx["atr"][i]
    if not risk:
        return None
    upper = entry + r_mult * float(risk)
    lower = entry - r_mult * float(risk)
    for j in range(i + 1, i + horizon + 1):
        hit_up = candles[j].high >= upper
        hit_dn = candles[j].low <= lower
        if hit_up and hit_dn:
            return 0
        if hit_up:
            return 1
        if hit_dn:
            return 0
    return None


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _train_lr(X: list[list[float]], y: list[int], epochs: int = 400, lr: float = 0.3):
    n = len(X)
    d = len(X[0])
    means = [sum(row[k] for row in X) / n for k in range(d)]
    stds = []
    for k in range(d):
        var = sum((row[k] - means[k]) ** 2 for row in X) / n
        stds.append(math.sqrt(var) or 1.0)
    Z = [[(row[k] - means[k]) / stds[k] for k in range(d)] for row in X]
    w = [0.0] * d
    b = 0.0
    m = len(Z)
    for _ in range(epochs):
        gw = [0.0] * d
        gb = 0.0
        for xi, yi in zip(Z, y):
            p = _sigmoid(sum(wj * xj for wj, xj in zip(w, xi)) + b)
            err = p - yi
            for k in range(d):
                gw[k] += err * xi[k]
            gb += err
        for k in range(d):
            w[k] -= lr * gw[k] / m
        b -= lr * gb / m
    preds = [(_sigmoid(sum(wk * xk for wk, xk in zip(w, xi)) + b) >= 0.5) == bool(yi)
             for xi, yi in zip(Z, y)]
    accuracy = sum(preds) / m
    pos_rate = sum(y) / m
    base_acc = max(pos_rate, 1 - pos_rate)
    auc_proxy = min(1.0, max(0.5, accuracy))
    return w, b, means, stds, round(accuracy, 4), round(base_acc, 4), round(auc_proxy, 4)


async def train_ml_model(
    db: AsyncSession,
    figi: str,
    interval_name: str,
    strategy_id: str | None,
    params: dict | None,
    days: int = 120,
    horizon_bars: int = HORIZON_BARS,
    r_multiple: float = R_MULTIPLE,
) -> dict:
    from app.services.signals import compute_signals, _load_candles
    from app.services.tinvest import INTERVAL_NAMES
    from app.engine.version import ENGINE_ID

    interval = INTERVAL_NAMES.get(interval_name)
    if interval is None:
        raise ValueError(f"unknown interval {interval_name}")
    interval_value = int(getattr(interval, "value", interval))

    result = await compute_signals(db, figi, interval_name, strategy_id or "", params or {}, days)
    run_id = uuid.UUID(result["run_id"])
    run = await db.scalar(select(StrategyRun).where(StrategyRun.id == run_id))

    await ensure_day_candles_if_needed(db, figi, interval_value)

    candles = await _load_candles(db, figi, interval_value)
    ts_index = {c.ts: i for i, c in enumerate(candles)}

    sigs = (
        await db.execute(select(StrategySignal).where(StrategySignal.run_id == run_id).order_by(StrategySignal.ts))
    ).scalars().all()

    ctx = _indicator_context(candles)
    X: list[list[float]] = []
    y: list[int] = []
    used = 0
    for s in sigs:
        idx = ts_index.get(s.ts.replace(tzinfo=s.ts.tzinfo) if s.ts.tzinfo else s.ts)
        if idx is None:
            continue
        feats = _features_at(candles, idx, ctx, s.side)
        label = _label_at(candles, idx, horizon_bars, r_multiple, ctx)
        if feats is None or label is None:
            continue
        X.append(feats)
        y.append(label)
        used += 1

    good = sum(y)
    bad = len(y) - good
    if len(X) < 20 or good < 5 or bad < 5:
        raise ValueError(f"недостаточно размеченных сигналов для обучения: {len(X)} (good={good}, bad={bad})")

    split = int(len(X) * 0.75)
    w, b, means, stds, acc_train, _, _ = _train_lr(X[:split], y[:split])

    hold_X, hold_y = X[split:], y[split:]

    def predict_row(x: list[float]) -> bool:
        z = b + sum(wk * ((x[k] - means[k]) / stds[k]) for k, wk in enumerate(w))
        return _sigmoid(z) >= 0.5

    correct = sum(1 for x, hy in zip(hold_X, hold_y) if predict_row(x) == bool(hy)) if hold_X else 0
    acc_hold = correct / len(hold_X) if hold_X else None

    model = MlModel(
        name=f"{run.strategy_id}_{interval_name}_h{horizon_bars}x{r_multiple}",
        strategy_id=run.strategy_id,
        strategy_run_id=run_id,
        figi=figi,
        interval_name=interval_name,
        features_version="v1",
        target_spec={
            "type": "hit_+R_before_-R",
            "horizon_bars": horizon_bars,
            "r_multiple": r_multiple,
            "r_source": "ATR14_at_signal_close",
        },
        feature_names=FEATURE_NAMES,
        weights={"w": w, "b": b, "means": means, "stds": stds},
        metrics={
            "samples": len(X),
            "good": good,
            "bad": bad,
            "accuracy_train": acc_train,
            "accuracy_holdout": round(acc_hold, 4) if acc_hold is not None else None,
            "base_rate": round(max(good, bad) / len(y), 4),
            "engine_version": ENGINE_ID,
        },
        status="TRAINED",
    )
    db.add(model)
    await db.commit()
    await db.refresh(model)

    return {
        "model_id": str(model.id),
        "name": model.name,
        "strategy_run_id": str(run_id),
        "signals_used": used,
        "metrics": model.metrics,
    }


async def ensure_day_candles_if_needed(db: AsyncSession, figi: str, interval_value: int) -> None:
    del db, figi, interval_value


async def predict_for_run(db: AsyncSession, model_id: uuid.UUID, run_id: uuid.UUID, threshold: float = 0.5) -> dict:
    model = await db.scalar(select(MlModel).where(MlModel.id == model_id))
    if model is None:
        raise ValueError("model not found")
    run = await db.scalar(select(StrategyRun).where(StrategyRun.id == run_id))
    if run is None:
        raise ValueError("run not found")
    interval = INTERVAL_NAMES.get(run.interval_name)
    interval_value = int(getattr(interval, "value", interval)) if interval else 3600

    candles = await _load_candles(db, run.figi, interval_value)
    ts_index = {c.ts: i for i, c in enumerate(candles)}
    ctx = _indicator_context(candles)

    sigs = (
        await db.execute(select(StrategySignal).where(StrategySignal.run_id == run_id).order_by(StrategySignal.ts))
    ).scalars().all()

    w = model.weights["w"]
    b = model.weights["b"]
    means = model.weights["means"]
    stds = model.weights["stds"]

    await db.execute(delete(MlPrediction).where(MlPrediction.model_id == model_id, MlPrediction.signal_id.in_(select(StrategySignal.id).where(StrategySignal.run_id == run_id))))

    out = []
    accepted = rejected = 0
    now = datetime.now(timezone.utc)
    for s in sigs:
        idx = ts_index.get(s.ts)
        if idx is None:
            continue
        feats = _features_at(candles, idx, ctx, s.side)
        if feats is None:
            continue
        z = b + sum(wk * ((feats[k] - means[k]) / stds[k]) for k, wk in enumerate(w))
        p = _sigmoid(z)
        decision = "ACCEPT" if p >= threshold else "REJECT"
        if decision == "ACCEPT":
            accepted += 1
        else:
            rejected += 1
        db.add(
            MlPrediction(
                model_id=model_id,
                signal_id=s.id,
                figi=run.figi,
                ts=s.ts,
                side=s.side,
                probability=p,
                decision=decision,
            )
        )
        out.append({"ts": s.ts.isoformat(), "side": s.side, "probability": round(p, 4), "decision": decision})

    await db.commit()
    return {
        "model_id": str(model_id),
        "model_name": model.name,
        "run_id": str(run_id),
        "threshold": threshold,
        "scored": len(out),
        "accepted": accepted,
        "rejected": rejected,
        "predictions": sorted(out, key=lambda x: x["ts"]),
        "trained_at": (model.created_at or now).isoformat() if hasattr(model.created_at, "isoformat") else None,
        "_horizon_note": f"target={model.target_spec}",
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def horizon_note(horizon_bars: int) -> timedelta:
    return timedelta(minutes=horizon_bars)
