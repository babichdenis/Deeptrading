from __future__ import annotations

import hashlib
import uuid
import json
import uuid
from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.instrument import Instrument

from app.engine.exits import (
    AtrStopPolicy,
    AtrTrailingPolicy,
    ExitPolicy,
    FixedSlTpPolicy,
)
from app.engine.metrics import full_report
from app.engine.models import Candle as EngineCandle
from app.engine.runner import EngineConfig, EngineRunner
from app.engine.sessions import SessionPolicyConfig
from app.engine.strategies import ParamValidationError
from app.engine.policies import SignalPolicyConfig
from app.engine.version import COST_MODEL_ID, ENGINE_ID
from app.engine.wave1 import ReplayStrategy
from app.models.candle import Candle
from app.models.experiments import Experiment, ExperimentTrade
from app.models.signals import StrategyRun, StrategySignal
from app.services.candle_cache import DEFAULT_DAYS, candle_coverage, ensure_candles
from app.services.signals import compute_signals
from app.services.tinvest import INTERVAL_NAMES

EXIT_POLICY_SPECS: dict[str, dict] = {
    "fixed_sl_tp": {
        "cls": FixedSlTpPolicy,
        "label": "Fixed SL/TP",
        "schema": {
            "stop_pct": {"type": "float", "default": 1.0, "min": 0.1, "max": 50.0},
            "target_pct": {"type": "float", "default": 2.0, "min": 0.2, "max": 100.0},
        },
    },
    "atr_stop": {
        "cls": AtrStopPolicy,
        "label": "ATR Stop",
        "schema": {
            "period": {"type": "int", "default": 14, "min": 3, "max": 100},
            "multiplier": {"type": "float", "default": 2.0, "min": 0.5, "max": 6},
            "risk_reward": {"type": "float", "default": 2.0, "min": 0.5, "max": 6},
        },
    },
    "atr_trailing": {
        "cls": AtrTrailingPolicy,
        "label": "ATR Trailing",
        "schema": {
            "period": {"type": "int", "default": 14, "min": 3, "max": 100},
            "initial_stop_atr": {"type": "float", "default": 2.0, "min": 0.5, "max": 6},
            "activation_atr": {"type": "float", "default": 1.0, "min": 0.2, "max": 5},
            "trail_distance_atr": {"type": "float", "default": 2.0, "min": 0.5, "max": 6},
        },
    },
}


class ExperimentError(ValueError):
    pass


def validate_exit_params(policy_id: str, params: dict | None) -> dict:
    spec = EXIT_POLICY_SPECS.get(policy_id)
    if spec is None:
        raise ExperimentError(f"unknown exit policy: {policy_id}")
    incoming = dict(params or {})
    validated: dict = {}
    for key, pspec in spec["schema"].items():
        value = incoming.pop(key, pspec["default"])
        try:
            value = int(value) if pspec["type"] == "int" else float(value)
        except (TypeError, ValueError):
            raise ExperimentError(f"exit param {key}: wrong type")
        if value < pspec["min"] or value > pspec["max"]:
            raise ExperimentError(f"exit param {key}={value} out of bounds")
        validated[key] = value
    if incoming:
        raise ExperimentError(f"unknown exit params: {sorted(incoming)}")
    return validated


def build_exit_policy(policy_id: str, params: dict | None) -> ExitPolicy:
    spec = EXIT_POLICY_SPECS[policy_id]
    validated = validate_exit_params(policy_id, params)
    if policy_id == "fixed_sl_tp":
        validated = {k: (v / 100.0 if k in ("stop_pct", "target_pct") else v) for k, v in validated.items()}
    return spec["cls"](**validated)


def _config_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def _load_candles(
    db: AsyncSession, figi: str, interval_value: int,
    date_from=None, date_to=None,
) -> list[EngineCandle]:
    stmt = select(Candle).where(
        Candle.figi == figi, Candle.interval == interval_value
    )
    if date_from is not None:
        stmt = stmt.where(Candle.ts >= date_from)
    if date_to is not None:
        stmt = stmt.where(Candle.ts <= date_to)
    rows = await db.execute(stmt.order_by(Candle.ts))
    return [
        EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high), low=float(r.low),
                     close=float(r.close), volume=float(r.volume))
        for r in rows.scalars()
    ]


async def prepare_run(
    db: AsyncSession,
    figi: str,
    interval_name: str,
    strategy_id: str | None,
    params: dict | None,
    strategy_run_id: uuid.UUID | None,
    days: int,
) -> tuple[uuid.UUID, StrategyRun, dict]:
    if strategy_run_id is not None:
        run_row = await db.scalar(select(StrategyRun).where(StrategyRun.id == strategy_run_id))
        if run_row is None:
            raise ExperimentError("strategy_run_id not found")
        result = await compute_signals(
            db, figi, interval_name, run_row.strategy_id, run_row.params, days
        )
    elif strategy_id is not None:
        result = await compute_signals(db, figi, interval_name, strategy_id, params, days)
    else:
        raise ExperimentError("need strategy_id or strategy_run_id")

    run = await db.scalar(select(StrategyRun).where(StrategyRun.id == uuid.UUID(result["run_id"])))
    if run is None:
        raise ExperimentError("run vanished after compute")
    return run.id, run, result


def _verdict(report: dict, min_trades: int = 15) -> dict:
    s = report["summary"]
    net = s["net"]
    pf = s["profit_factor"]
    trades = s["trades"]

    if trades < min_trades:
        trading = "INSUFFICIENT_DATA"
    elif net > 0 and pf >= 1.15:
        trading = "POSITIVE"
    elif net > 0:
        trading = "WEAK_POSITIVE"
    else:
        trading = "NEGATIVE"

    dd = report.get("max_drawdown_pct", 0.0)
    if trading == "POSITIVE" and dd < 25:
        research_status = "DESIGN_CANDIDATE"
    elif trading == "NEGATIVE":
        research_status = "REJECT"
    else:
        research_status = "INCONCLUSIVE"

    h1_net = report["halves"]["h1"]["net"]
    h2_net = report["halves"]["h2"]["net"]
    stable = h1_net > 0 and h2_net > 0

    return {
        "technical_ok": True,
        "trading_verdict": trading,
        "research_status": research_status,
        "stable_halves": stable,
        "conflict_rule": "STOP_LOSS_FIRST",
        "execution_rule": "next_open",
    }


async def create_and_run_experiment(db: AsyncSession, req: dict) -> dict:
    figi = req.get("figi")
    interval_name = req.get("interval_name", "hour")
    if not figi:
        raise ExperimentError("figi required")
    if interval_name not in INTERVAL_NAMES:
        raise ExperimentError(f"unknown interval {interval_name}")
    days = int(req.get("days", DEFAULT_DAYS))
    qty = max(1, int(req.get("qty", 1)))
    purpose = req.get("purpose", "DESIGN")

    sp_in = req.get("signal_policy") or {}
    if sp_in.get("id", "ignore_same_side") != "ignore_same_side":
        raise ExperimentError("only ignore_same_side supported in v1")
    min_hold_bars = int((sp_in.get("params") or {}).get("min_hold_bars", 0))

    ep_in = req.get("exit_policy") or {"id": "fixed_sl_tp", "params": {}}
    exit_id = ep_in.get("id", "fixed_sl_tp")
    exit_params = validate_exit_params(exit_id, ep_in.get("params"))

    session_cfg_raw = req.get("session")
    tf_minutes_map = {"1min": 1, "5min": 5, "10min": 10, "15min": 15, "hour": 60, "2h": 120, "4h": 240}
    is_intraday = interval_name in tf_minutes_map

    session_payload = None
    session_config = None
    if is_intraday:
        raw = session_cfg_raw if isinstance(session_cfg_raw, dict) else {}
        session_payload = {
            "id": "moex_intraday_v1",
            "entry_cutoff_bars": int(raw.get("entry_cutoff_bars", 0)),
            "overnight": bool(raw.get("overnight", True)),
        }
        session_config = SessionPolicyConfig(
            entry_cutoff_bars=session_payload["entry_cutoff_bars"],
            overnight=session_payload["overnight"],
        )

    allow_short = bool(req.get("allow_short", True))
    ml_in = req.get("ml_filter") or {}
    ml_model_id = ml_in.get("model_id")
    ml_threshold = float(ml_in["threshold"]) if ml_in.get("threshold") is not None else 0.5
    cost_overrides = req.get("cost_model") or {}
    commission_rate = float(cost_overrides.get("commission_rate", 0.0005))
    slippage_bps = float(cost_overrides.get("slippage_bps", 2))

    run_id, run, sig_result = await prepare_run(
        db, figi, interval_name,
        req.get("strategy_id"), req.get("params"), req.get("strategy_run_id"), days,
    )

    payload = {
        "figi": figi,
        "interval_name": interval_name,
        "days": days,
        "strategy_run_id": str(run_id),
        "signal_policy": {"id": "ignore_same_side", "params": {"min_hold_bars": min_hold_bars}},
        "exit_policy": {"id": exit_id, "params": exit_params},
        "session": session_payload,
        "qty": qty,
        "allow_short": allow_short,
        "engine_version": ENGINE_ID,
        "cost_model": {"id": COST_MODEL_ID, "commission_rate": commission_rate,
                        "slippage_bps": slippage_bps},
        "ml_filter": {"model_id": ml_model_id, "threshold": ml_threshold} if ml_model_id else None,
    }
    c_hash = _config_hash(payload)

    existing = await db.scalar(select(Experiment).where(Experiment.config_hash == c_hash))
    if existing is not None and existing.status == "COMPLETED":
        return await get_experiment(db, existing.id)

    interval = INTERVAL_NAMES[interval_name]
    interval_value = int(getattr(interval, "value", interval))
    _, _, cov_n = await candle_coverage(db, figi, interval_value)

    experiment = Experiment(
        status="RUNNING",
        purpose=purpose if purpose in ("QC", "DESIGN", "VAL") else "DESIGN",
        figi=figi,
        interval_name=interval_name,
        strategy_run_id=run_id,
        strategy_id=run.strategy_id,
        signal_policy=payload["signal_policy"],
        exit_policy=payload["exit_policy"],
        session_policy=session_payload,
        qty=qty,
        engine_version=ENGINE_ID,
        cost_model_version=COST_MODEL_ID,
        config_hash=c_hash,
        data_version=run.data_version,
        bars=cov_n,
    )
    db.add(experiment)
    await db.flush()

    try:
        candles = await _load_candles(db, figi, interval_value)
        if len(candles) < 30:
            raise ExperimentError("not enough candles in database")

        sigs = (
            await db.execute(
                select(StrategySignal)
                .where(StrategySignal.run_id == run_id)
                .order_by(StrategySignal.ts)
            )
        ).scalars().all()

        ml_dropped = None
        if ml_model_id:
            from app.models.ml import MlPrediction
            preds = (
                await db.execute(
                    select(MlPrediction).where(
                        MlPrediction.model_id == uuid.UUID(ml_model_id),
                        MlPrediction.signal_id.in_([x.id for x in sigs]),
                    )
                )
            ).scalars().all()
            keep_ids = {p.signal_id for p in preds if float(p.probability) >= ml_threshold}
            before = len(sigs)
            sigs = [x for x in sigs if x.id in keep_ids]
            ml_dropped = {"before": before, "after": len(sigs), "dropped": before - len(sigs)}

        replay = ReplayStrategy([(s.ts, s.side) for s in sigs])
        cfg = EngineConfig(
            figi=figi,
            qty=qty,
            allow_short=allow_short,
            signal_policy=SignalPolicyConfig(
                id="ignore_same_side", version="1.0.0", min_hold_bars=min_hold_bars
            ),
            session_policy=session_config,
        )
        from app.engine.costs import CostModel

        cfg.cost_model = CostModel(
            id=COST_MODEL_ID,
            commission_rate=commission_rate,
            slippage_bps=slippage_bps,
        )

        runner = EngineRunner(strategy=replay, exit_policy=build_exit_policy(exit_id, exit_params), config=cfg)
        ledger = runner.run(candles)

        report = full_report(ledger.trades)
        verdict = _verdict(report)

        experiment.summary = {
            **report,
            "verdict": verdict,
            "signals_total": len(sigs),
            "ml_filtered": ml_dropped,
            "audit_entries": len(ledger.audit),
            "fingerprint": ledger.fingerprint(),
            "config": payload,
        }
        experiment.research_status = verdict["research_status"]
        experiment.bars = len(candles)
        experiment.from_ts = candles[0].ts
        experiment.to_ts = candles[-1].ts
        experiment.data_version = run.data_version
        experiment.status = "COMPLETED"

        for t in ledger.trades:
            db.add(
                ExperimentTrade(
                    experiment_id=experiment.id,
                    trade_id=t.trade_id,
                    side=t.side,
                    qty=t.qty,
                    entry_index=t.entry_index,
                    entry_time=t.entry_time,
                    entry_price=t.entry_price,
                    exit_index=t.exit_index,
                    exit_time=t.exit_time,
                    exit_price=t.exit_price,
                    bars_held=t.bars_held,
                    gross_pnl=t.gross_pnl,
                    commission=t.commission,
                    slippage=t.slippage,
                    net_pnl=t.net_pnl,
                    exit_reason=t.exit_reason,
                    initial_stop=t.initial_stop,
                    take_profit=t.take_profit,
                )
            )
        await db.commit()
    except Exception as e:
        experiment.status = "FAILED"
        experiment.error = str(e)[:500]
        await db.commit()
        raise ExperimentError(str(e)) from e

    return await get_experiment(db, experiment.id)


async def get_experiment(db: AsyncSession, exp_id: uuid.UUID) -> dict:
    exp = await db.scalar(select(Experiment).where(Experiment.id == exp_id))
    if exp is None:
        raise ExperimentError("experiment not found")
    trades_rows = await db.execute(
        select(ExperimentTrade)
        .where(ExperimentTrade.experiment_id == exp_id)
        .order_by(ExperimentTrade.entry_time)
    )
    trades = [
        {
            "trade_id": t.trade_id,
            "side": t.side,
            "qty": t.qty,
            "entry_time": t.entry_time.isoformat(),
            "entry_price": float(t.entry_price),
            "exit_time": t.exit_time.isoformat(),
            "exit_price": float(t.exit_price),
            "bars_held": t.bars_held,
            "gross_pnl": float(t.gross_pnl),
            "commission": float(t.commission),
            "slippage": float(t.slippage),
            "net_pnl": float(t.net_pnl),
            "exit_reason": t.exit_reason,
            "initial_stop": float(t.initial_stop) if t.initial_stop is not None else None,
            "take_profit": float(t.take_profit) if t.take_profit is not None else None,
        }
        for t in trades_rows.scalars()
    ]
    return {
        "experiment_id": str(exp.id),
        "status": exp.status,
        "purpose": exp.purpose,
        "created_at": exp.created_at.isoformat() if exp.created_at else None,
        "figi": exp.figi,
        "ticker": exp.figi,
        "interval_name": exp.interval_name,
        "strategy_id": exp.strategy_id,
        "strategy_run_id": str(exp.strategy_run_id),
        "signal_policy": exp.signal_policy,
        "exit_policy": exp.exit_policy,
        "session_policy": exp.session_policy,
        "qty": exp.qty,
        "engine_version": exp.engine_version,
        "cost_model_version": exp.cost_model_version,
        "config_hash": exp.config_hash,
        "bars": exp.bars,
        "research_status": exp.research_status,
        "error": exp.error,
        "summary": exp.summary,
        "trades": trades,
    }


async def list_experiments(
    db: AsyncSession, figi: str | None = None, limit: int = 50
) -> list[dict]:
    stmt = select(Experiment).order_by(desc(Experiment.created_at)).limit(min(limit, 200))
    if figi:
        stmt = stmt.where(Experiment.figi == figi)
    rows = await db.execute(stmt)
    return [
        {
            "experiment_id": str(e.id),
            "status": e.status,
            "purpose": e.purpose,
            "figi": e.figi,
            "interval_name": e.interval_name,
            "strategy_id": e.strategy_id,
            "exit_policy": e.exit_policy,
            "qty": e.qty,
            "research_status": e.research_status,
            "summary_net": (e.summary or {}).get("summary", {}).get("net"),
            "summary_pf": (e.summary or {}).get("summary", {}).get("profit_factor"),
            "summary_trades": (e.summary or {}).get("summary", {}).get("trades"),
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in rows.scalars()
    ]


LIQUID_TICKERS = [
    "SBER", "GAZP", "LKOH", "GMKN", "ROSN", "MTSS", "TATN", "MGNT",
    "CHMF", "ALRS", "PLZL", "SNGS", "VTBR", "AFLT", "PHOR",
]


async def _resolve_universe(db: AsyncSession, top_n: int, tickers: list[str] | None) -> list[str]:
    wanted = tickers or LIQUID_TICKERS[:top_n]
    rows = await db.execute(
        select(Instrument.ticker, Instrument.figi).where(Instrument.ticker.in_(wanted))
    )
    mapping = dict(rows.all())
    return [mapping[t] for t in wanted if t in mapping][:top_n]


def _grid_combos(spec: dict, grid: dict, max_trials: int) -> list[dict]:
    keys = [k for k in grid if k in spec]
    if not keys:
        return []
    import itertools

    combos = list(itertools.product(*(grid[k] for k in keys)))
    out = []
    for values in combos[:max_trials]:
        base = {k: spec[k]["default"] for k in spec}
        for k, v in zip(keys, values):
            base[k] = v
        try:
            validate_exit_params_all(spec, base)
            out.append(base)
        except ExperimentError:
            continue
    return out


def validate_exit_params_all(spec: dict, params: dict) -> dict:
    for key, pspec in spec.items():
        v = params[key]
        if v < pspec["min"] or v > pspec["max"]:
            raise ExperimentError(f"{key}={v} out of bounds")
    return params


async def sweep(
    db: AsyncSession, req: dict
) -> dict:
    import time as time_mod

    started = time_mod.monotonic()
    figi = req.get("figi")
    if not figi:
        raise ExperimentError("figi required")
    interval_name = req.get("interval_name", "hour")
    days = int(req.get("days", DEFAULT_DAYS))
    strategy_id = req.get("strategy_id")
    params = req.get("params") or {}
    exit_id = (req.get("exit_policy") or {}).get("id", "fixed_sl_tp")
    spec_full = EXIT_POLICY_SPECS.get(exit_id)
    if spec_full is None:
        raise ExperimentError(f"unknown exit policy {exit_id}")
    spec = spec_full["schema"]
    grid = req.get("grid") or {}
    max_trials = min(int(req.get("max_trials", 12)), 24)

    combos = _grid_combos(spec, grid, max_trials)
    if not combos:
        raise ExperimentError("empty grid")

    results = []
    for combo in combos:
        exp_req = {
            "figi": figi,
            "interval_name": interval_name,
            "days": days,
            "strategy_id": strategy_id,
            "params": params,
            "exit_policy": {"id": exit_id, "params": combo},
            "qty": int(req.get("qty", 1)),
            "purpose": req.get("purpose", "DESIGN"),
            **({"allow_short": req["allow_short"]} if "allow_short" in req else {}),
        }
        try:
            r = await create_and_run_experiment(db, exp_req)
            s = (r.get("summary") or {}).get("summary", {})
            results.append({
                "params": combo,
                "experiment_id": r["experiment_id"],
                "net": s.get("net"),
                "pf": s.get("profit_factor"),
                "trades": s.get("trades"),
                "win_rate": s.get("win_rate"),
                "max_dd_pct": (r.get("summary") or {}).get("max_drawdown_pct"),
                "research_status": r.get("research_status"),
            })
        except ExperimentError:
            continue

    results.sort(key=lambda x: (x["net"] or 0), reverse=True)
    best = results[0] if results else None
    plateau_ok = False
    if len(results) >= 3 and best and (best["net"] or 0) > 0:
        near = [r for r in results if (best["net"] or 0) > 0 and (r["net"] or 0) > 0]
        plateau_ok = len(near) >= max(2, len(results) // 2)

    return {
        "figi": figi,
        "interval_name": interval_name,
        "strategy_id": strategy_id,
        "exit_policy": exit_id,
        "trials_planned": len(combos),
        "trials_done": len(results),
        "elapsed_sec": round(time_mod.monotonic() - started, 1),
        "plateau_detected": plateau_ok,
        "ranked": results,
        "best": best,
    }


async def batch(
    db: AsyncSession, req: dict
) -> dict:
    import time as time_mod

    started = time_mod.monotonic()
    interval_name = req.get("interval_name", "hour")
    days = int(req.get("days", 60))
    top_n = min(int(req.get("top_n", 10)), 20)
    strategy_id = req.get("strategy_id")
    params = req.get("params") or {}
    exit_policy = req.get("exit_policy") or {"id": "fixed_sl_tp", "params": {}}
    purpose = req.get("purpose", "DESIGN")
    tickers = req.get("tickers")

    figis = await _resolve_universe(db, top_n, tickers)
    if not figis:
        raise ExperimentError("universe resolved to empty set; sync instruments first")

    ticker_rows = await db.execute(select(Instrument.ticker, Instrument.figi))
    figi_to_ticker = {f: t for t, f in ticker_rows.all()}

    per_stock = []
    errors = []
    for figi in figis:
        exp_req = {
            "figi": figi,
            "interval_name": interval_name,
            "days": days,
            "strategy_id": strategy_id,
            "params": params,
            "exit_policy": exit_policy,
            "qty": int(req.get("qty", 1)),
            "purpose": purpose,
        }
        try:
            r = await create_and_run_experiment(db, exp_req)
            s = (r.get("summary") or {}).get("summary", {})
            per_stock.append({
                "figi": figi,
                "ticker": figi_to_ticker.get(figi, figi),
                "experiment_id": r["experiment_id"],
                "net": s.get("net"),
                "pf": s.get("profit_factor"),
                "trades": s.get("trades"),
                "win_rate": s.get("win_rate"),
                "max_dd_pct": (r.get("summary") or {}).get("max_drawdown_pct"),
                "research_status": r.get("research_status"),
                "status": r.get("status"),
            })
        except ExperimentError as e:
            errors.append({"figi": figi, "error": str(e)[:200]})

    nets = [p["net"] for p in per_stock if p["net"] is not None]
    summary = {
        "stocks_total": len(figis),
        "stocks_done": len(per_stock),
        "positive_stocks": sum(1 for n in nets if n > 0),
        "total_net": round(sum(nets), 2),
        "avg_net_per_stock": round(sum(nets) / len(nets), 2) if nets else 0,
        "errors": errors,
        "elapsed_sec": round(time_mod.monotonic() - started, 1),
    }
    per_stock.sort(key=lambda x: (x["net"] or 0), reverse=True)
    return {"summary": summary, "per_stock": per_stock}
