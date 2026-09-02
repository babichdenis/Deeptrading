from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("lab.queue")

import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import asyncio
import logging
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.engine.exits import ExitPolicy
from app.engine.metrics import full_report
from app.engine.models import Candle as EngineCandle
from app.engine.policies import SignalPolicyConfig
from app.engine.runner import EngineConfig, EngineRunner
from app.engine.sessions import SessionPolicyConfig
from app.engine.wave1 import ReplayStrategy
from app.engine.quorum import merge_quorum
from app.models.configurations import Configuration
from app.models.instrument import Instrument
from app.services.eventbus import event_bus
from app.services.experiments import (
    ExperimentError,
    _load_candles,
    build_exit_policy,
    validate_exit_params,
)
from app.services.candle_cache import DEFAULT_DAYS, ensure_candles
from app.services.signals import compute_signals
from app.services.tinvest import INTERVAL_NAMES


class ConfigurationError(ValueError):
    pass


def validate_members(members: list) -> list[dict]:
    if not isinstance(members, list) or len(members) < 1:
        raise ConfigurationError("нужен минимум один член конфигурации")
    cleaned: list[dict] = []
    for m in members:
        sid = m.get("strategy_id")
        params = m.get("params") or {}
        try:
            from app.engine.strategies import validate_params

            validated = validate_params(sid, params)
        except ParamValidationError as e:
            raise ConfigurationError(str(e))
        cleaned.append({"strategy_id": sid, "params": validated})
    return cleaned


async def create_configuration(db: AsyncSession, body: dict) -> dict:
    name = (body.get("name") or "").strip() or "Без названия"
    interval_name = body.get("interval_name", "hour")
    if interval_name not in INTERVAL_NAMES:
        raise ConfigurationError(f"unknown interval {interval_name}")
    members = validate_members(body.get("members"))
    quorum = int(body.get("quorum", len(members)))
    if not (1 <= quorum <= len(members)):
        raise ConfigurationError("quorum вне диапазона")
    exit_id = (body.get("exit_policy") or {}).get("id", "fixed_sl_tp")
    exit_params = validate_exit_params(exit_id, body.get("exit_policy", {}).get("params"))

    cfg = Configuration(
        name=name,
        status="DRAFT",
        interval_name=interval_name,
        members=members,
        quorum=quorum,
        exit_policy={"id": exit_id, "params": exit_params},
        min_hold_bars=int(body.get("min_hold_bars", 0)),
        allow_short=bool(body.get("allow_short", False)),
        session_policy=body.get("session_policy") or {"entry_cutoff_bars": 0, "overnight": True},
        filters=body.get("filters", []),
        preview_figi=body.get("figi"),
    )
    db.add(cfg)
    await db.commit()
    return await get_configuration(db, cfg.id)


async def update_configuration(db: AsyncSession, config_id: uuid.UUID, body: dict) -> dict:
    cfg_row = await _get_row(db, config_id)
    if cfg_row.status == "IN_LAB":
        raise ConfigurationError("идёт тест — дождитесь завершения или удалите конфигурацию")
    editable = ["name", "members", "quorum", "exit_policy", "min_hold_bars",
                "allow_short", "session_policy", "preview_figi", "filters"]
    for key in editable:
        if key not in body:
            continue
        value = body[key]
        if key == "name":
            cfg_row.name = (value or "").strip()
        elif key == "members":
            cfg_row.members = validate_members(value)
        elif key == "exit_policy":
            exit_id = value.get("id", "fixed_sl_tp")
            cfg_row.exit_policy = {"id": exit_id, "params": validate_exit_params(exit_id, value.get("params"))}
        else:
            setattr(cfg_row, key, value)
    if "quorum" in body:
        q = int(body["quorum"])
        if not (1 <= q <= len(cfg_row.members)):
            raise ConfigurationError("quorum вне диапазона")
        cfg_row.quorum = q
    cfg_row.status = "DRAFT"
    await db.commit()
    return await get_configuration(db, config_id)


async def preview_configuration(
    db: AsyncSession,
    config_id: uuid.UUID,
    figi: str | None = None,
    days: int = DEFAULT_DAYS,
) -> dict:
    row = await _get_row(db, config_id)
    logger.warning("S2L: конфигурация %s интервал=%s члены=%d", config_id, row.interval_name, len(row.members))
    figi = figi or row.preview_figi
    if not figi:
        raise ConfigurationError("не задан FIGI для preview")

    interval_value = int(getattr(INTERVAL_NAMES[row.interval_name], "value", 3600))

    await ensure_candles(db, figi, row.interval_name, days)
    candles = await _load_candles(db, figi, interval_value)
    if len(candles) < 60:
        raise ConfigurationError("мало свечей в базе для preview")

    member_runs: list[tuple[str, list[dict]]] = []
    source_runs: dict[str, str] = {}
    raw_total = 0
    for m in row.members:
        res = await compute_signals(
            db, figi, row.interval_name, m["strategy_id"], m.get("params", {}), days
        )
        source_runs[m["strategy_id"]] = res["run_id"]
        member_runs.append((m["strategy_id"], res["signals"]))
        raw_total += res["count"]

    merged, funnel = merge_quorum(member_runs, row.quorum)

    replay = ReplayStrategy([(s["ts"], s["side"]) for s in merged])
    exit_obj: ExitPolicy = build_exit_policy(row.exit_policy["id"], row.exit_policy.get("params"))
    session_raw = row.session_policy or {}
    session_config = None
    if row.interval_name in ("1min", "5min", "10min", "15min", "hour", "2h", "4h"):
        session_config = SessionPolicyConfig(
            entry_cutoff_bars=int(session_raw.get("entry_cutoff_bars", 0)),
            overnight=bool(session_raw.get("overnight", True)),
        )
    cfg_engine = EngineConfig(
        figi=figi,
        qty=1,
        allow_short=row.allow_short,
        signal_policy=SignalPolicyConfig(min_hold_bars=row.min_hold_bars),
        session_policy=session_config,
    )
    runner = EngineRunner(strategy=replay, exit_policy=exit_obj, config=cfg_engine)
    ledger = await asyncio.to_thread(runner.run, candles)

    report = full_report(ledger.trades)
    trades_out = [
        {
            "trade_id": t.trade_id,
            "side": t.side,
            "entry_index": t.entry_index,
            "entry_time": t.entry_time.isoformat(),
            "entry_price": t.entry_price,
            "exit_time": t.exit_time.isoformat(),
            "exit_price": t.exit_price,
            "bars_held": t.bars_held,
            "gross_pnl": round(t.gross_pnl, 4),
            "commission": round(t.commission, 4),
            "net_pnl": round(t.net_pnl, 4),
            "initial_stop": None,
            "target": None,
            "exit_reason": t.exit_reason,
        }
        for t in ledger.trades
    ]

    verdict = "PREVIEW"
    s = report["summary"]
    if s["trades"] >= 10 and s["net"] > 0 and s["profit_factor"] >= 1.15:
        verdict = "POSITIVE_PREVIEW"

    row.status = "PREVIEW_COMPLETED"
    row.preview_figi = figi
    row.source_runs = source_runs
    row.lab_result = {
        **report,
        "verdict": {"preview_verdict": verdict},
        "funnel": funnel,
        "trades_preview": trades_out,
        "config_snapshot": {
            "members": row.members,
            "quorum": row.quorum,
            "exit_policy": row.exit_policy,
            "min_hold_bars": row.min_hold_bars,
            "allow_short": row.allow_short,
            "session_policy": row.session_policy,
        },
        "figi": figi,
        "days": days,
    }
    await db.commit()

    return {
        "configuration_id": str(row.id),
        "status": row.status,
        "preview": True,
        "notice": "PREVIEW — быстрый расчёт на одной акции, не является полноценным backtest",
        "figi": figi,
        "interval_name": row.interval_name,
        "funnel": funnel,
        "summary": {**report, "trades_preview": trades_out},
        "merged_signals_count": len(merged),
    }


async def _commit_progress(row):
    async with SessionLocal() as db:
        db_row = await db.get(Configuration, row.id)
        if db_row:
            db_row.lab_result = row.lab_result
            await db.commit()


async def send_to_lab(
    db: AsyncSession,
    config_id: uuid.UUID,
    period_days: int = 30,
    top_n: int = 10,
    tickers: list[str] | None = None,
    date_from=None,
    date_to=None,
    channel: str | None = None,
) -> dict:
    from app.services.experiments import LIQUID_TICKERS

    row = await _get_row(db, config_id)
    logger.warning("S2L: конфигурация %s интервал=%s члены=%d", config_id, row.interval_name, len(row.members))

    import time as _time
    _t0 = _time.monotonic()

    def _aware(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt

    date_from = _aware(date_from)
    date_to = _aware(date_to)

    wanted = tickers or LIQUID_TICKERS[:top_n]
    rows = await db.execute(select(Instrument.ticker, Instrument.figi).where(Instrument.ticker.in_(wanted)))
    mapping = dict(rows.all())
    figi_to_ticker = {f: t for t, f in mapping.items()}
    figis = [mapping[t] for t in wanted if t in mapping][:top_n]
    tickers = [figi_to_ticker.get(f, f) for f in figis]
    if not figis:
        raise ConfigurationError("universe пуст — синхронизируйте инструменты")
    logger.warning("S2L: figis=%s", figis[:3])

    interval_value = int(getattr(INTERVAL_NAMES[row.interval_name], "value", 3600))
    per_stock = []
    all_trades = []
    all_ledger_trades = []
    errors = []
    total_funnel: dict = {"quorum_signals": 0}

    test_params = {
        "period_days": period_days,
        "top_n": top_n,
        "tickers": [figi_to_ticker.get(f, f) for f in figis],
        "date_from": date_from.isoformat() if date_from else None,
        "date_to": date_to.isoformat() if date_to else None,
    }
    row.lab_result = {
        "status": "RUNNING",
        "test_params": test_params,
        "progress": {"done": 0, "total": len(figis), "current": "", "tickers": tickers,
                    "stock_progress": {t: {"done": 0, "total": 1} for t in tickers}},
        "per_stock": [], "totals": {}, "errors": [],
        "period_days": period_days,
        "universe": [figi_to_ticker.get(f, f) for f in figis],
    }
    await db.commit()

    prog = row.lab_result.setdefault("progress", {})
    prog["total"] = len(figis)
    prog["done"] = 0
    prog["current"] = ""
    prog["stock_progress"] = {t: {"done": 0, "total": 1} for t in figi_to_ticker.values()}
    prog_commit_flag = {"stop": False}

    async def _progress_committer() -> None:
        while not prog_commit_flag["stop"]:
            await asyncio.sleep(2.0)
            try:
                await db.commit()
            except Exception:
                await db.rollback()

    commit_task = asyncio.create_task(_progress_committer())
    logger.warning("S2L: старт цикла, акций=%d", len(figis))
    stock_idx = 0
    for figi in figis:
        ticker = figi_to_ticker.get(figi, figi)
        stock_idx += 1
        sp = prog["stock_progress"].setdefault(ticker, {"done": 0, "total": 1})
        sp["total"] = 2
        sp["done"] = 0
        logger.warning("S2L: акция %s", ticker)
        if channel:
            await event_bus.publish(channel, "FIGI_STARTED", {"ticker": ticker},
                                    entity_type="figi", entity_id=figi)
        try:
            await ensure_candles(db, figi, row.interval_name, period_days,
                                 range_from=date_from, range_to=date_to)
            if date_from is None:
                date_from = datetime.now(timezone.utc) - timedelta(days=period_days)
            candles = await _load_candles(db, figi, interval_value,
                                      date_from=date_from, date_to=date_to)
            member_runs = []
            source_runs_figi: dict[str, str] = {}
            for m in row.members:
                res = await compute_signals(
                    db, figi, row.interval_name, m["strategy_id"], m.get("params", {}),
                    period_days + 10,
                )
                member_runs.append((m["strategy_id"], res["signals"]))
                source_runs_figi[m["strategy_id"]] = str(res.get("run_id", ""))
            merged, funnel_i = merge_quorum(member_runs, row.quorum)
            for _k, _v in funnel_i.items():
                if _k == "quorum":
                    total_funnel["quorum_signals"] += int(_v.get("signals", 0))
                else:
                    b = total_funnel.setdefault(_k, {"BUY": 0, "SELL": 0})
                    b["BUY"] += int(_v.get("BUY", 0))
                    b["SELL"] += int(_v.get("SELL", 0))
            merged_feats = {}
            for ms in merged:
                ts_val = ms["ts"]
                merged_feats[str(ts_val)] = ms.get("features", {})

            def _feat_for(trade):
                idx = trade.entry_index
                if idx <= 0 or idx >= len(candles):
                    return {}
                sig_ts = candles[idx - 1].ts.isoformat()
                return merged_feats.get(sig_ts, {})

            replay = ReplayStrategy([(s["ts"], s["side"]) for s in merged])
            exit_obj = build_exit_policy(row.exit_policy["id"], row.exit_policy.get("params"))
            session_raw = row.session_policy or {}
            session_config = SessionPolicyConfig(
                entry_cutoff_bars=int(session_raw.get("entry_cutoff_bars", 0)),
                overnight=bool(session_raw.get("overnight", False)),
            ) if row.interval_name != "day" else None
            cfg_engine = EngineConfig(
                figi=figi,
                qty=1,
                mode="both",
                allow_short=row.allow_short,
                signal_policy=SignalPolicyConfig(min_hold_bars=row.min_hold_bars),
                session_policy=session_config,
            )
            runner = EngineRunner(strategy=replay, exit_policy=exit_obj, config=cfg_engine)

            def _on_bars(bar_idx: int, bar_total: int, bar_ts) -> None:
                lr = row.lab_result
                if not lr:
                    return
                frac = bar_idx / bar_total if bar_total else 0
                lr["bar_info"] = f"{ticker}: бар {bar_idx}/{bar_total} ({bar_ts.strftime('%d.%m %H:%M')})"
                lr["bar_pct"] = round(frac * 100, 1)
                pr = lr.get("progress", {})
                pr["current"] = f"{ticker} бар {bar_idx}/{bar_total}"
                pr["bars_done"] = bar_idx
                pr["bars_total"] = bar_total
                pr["bar_pct"] = lr["bar_pct"]
                sp_now = pr.setdefault("stock_progress", {}).setdefault(ticker, {"done": 0, "total": 2})
                sp_now["done"] = round(frac, 3)
                sp_now["total"] = 1


            ledger = await asyncio.to_thread(runner.run, candles, progress_cb=_on_bars)

            stock_trades = []
            for t in ledger.trades:
                f = _feat_for(t)
                stock_trades.append({
                    **_trade_dict(t),
                    "ticker": ticker,
                    "entry_votes": f.get("votes"),
                    "entry_members": f.get("members_for"),
                    "entry_opposition": f.get("opposition"),
                })
            s = full_report(ledger.trades)["summary"]
            wins_long = sum(1 for t in ledger.trades if t.side == "LONG" and t.net_pnl > 0)
            losses_long = sum(1 for t in ledger.trades if t.side == "LONG" and t.net_pnl <= 0)
            wins_short = sum(1 for t in ledger.trades if t.side == "SHORT" and t.net_pnl > 0)
            losses_short = sum(1 for t in ledger.trades if t.side == "SHORT" and t.net_pnl <= 0)
            active_days = len({t.exit_time.date() for t in ledger.trades})
            pnl_pos = round(sum(t.net_pnl for t in ledger.trades if t.net_pnl > 0), 2)
            pnl_neg = round(sum(t.net_pnl for t in ledger.trades if t.net_pnl <= 0), 2)

            per_stock.append({
                "ticker": ticker,
                "active_days": active_days,
                "wl": f"{wins_long + wins_short} (L-{wins_long} / S-{wins_short})",
                "wins_total": wins_long + wins_short,
                "wins_l": wins_long,
                "wins_s": wins_short,
                "losses_total": s["trades"] - (wins_long + wins_short),
                "trades": s["trades"],
                "pnl": {"total": s["net"], "pos": pnl_pos, "neg": pnl_neg},
            })
            all_trades.extend(stock_trades)
            all_ledger_trades.extend(ledger.trades)
            if channel:
                await event_bus.publish(
                    channel, "FIGI_COMPLETED",
                    {"ticker": ticker, "trades": len(ledger.trades),
                     "pnl": round(sum(t.net_pnl for t in ledger.trades), 2)},
                    entity_type="figi", entity_id=figi,
                )
                if ledger.trades:
                    await event_bus.publish(
                        channel, "TRADE_CREATED",
                        {"ticker": ticker, "count": len(ledger.trades)},
                        entity_type="figi", entity_id=figi,
                    )
        except Exception as e:
            errors.append({"ticker": ticker, "error": f"{type(e).__name__}: {str(e)[:180]}"})

        sp = row.lab_result.get("progress", {}).get("stock_progress", {})
        sp[ticker] = {"done": 1, "total": 1}
        row.lab_result["progress"] = {
            "done": len(per_stock),
            "total": len(figis),
            "current": ticker,
            "tickers": tickers,
            "stock_progress": sp,
        }
        row.lab_result["per_stock"] = per_stock
        if source_runs_figi:
            row.lab_result["source_runs"] = source_runs_figi
        await db.commit()
        if channel:
            await event_bus.publish(
                channel, "JOB_PROGRESS",
                {"done": len(per_stock), "total": len(figis), "current": ticker},
                entity_type="test_run", entity_id="",
            )
        continue

    prog_commit_flag["stop"] = True
    commit_task.cancel()

    report = full_report(all_ledger_trades)
    total_net = round(sum(p["pnl"]["total"] for p in per_stock), 2)
    positive = len({(p["ticker"]) for p in per_stock if p["pnl"]["total"] > 0})
    tickers_touched = sorted({p["ticker"] for p in per_stock})

    by_ticker_mode: dict = {}
    for p in per_stock:
        by_ticker_mode.setdefault(p["ticker"], []).append(p)

    lab_result = {
        "period_days": period_days,
        "universe": [figi_to_ticker.get(f, f) for f in figis],
        "per_stock": per_stock,
        "by_ticker": {
            t: {
                "rows": by_ticker_mode[t],
                "total_net": round(sum(x["pnl"]["total"] for x in by_ticker_mode[t]), 2),
            }
            for t in tickers_touched
        },
        "totals": {
            "stocks": len(tickers_touched),
            "trades": len(all_trades),
            "positive_stocks": positive,
            "total_net": total_net,
            "wins": sum(1 for t in all_trades if float(t["net_pnl"]) > 0),
            "losses": sum(1 for t in all_trades if float(t["net_pnl"]) <= 0),
        },
        "summary": {
            "summary": report["summary"],
            "max_drawdown_pct": report["max_drawdown_pct"],
            "halves": report["halves"],
            "verdict": {"stable_halves": report["halves"]["h1"]["net"] > 0 and report["halves"]["h2"]["net"] > 0},
        },
        "curve": report["curve"],
        "by_day": report["by_day"],
        "by_figi": report["by_figi"],
        "consecutive_losses": report["consecutive_losses"],
        "top1_analysis": report["top1_analysis"],
        "funnel": total_funnel,
        "elapsed_sec": round(_time.monotonic() - _t0, 1),
        "trades": sorted(all_trades, key=lambda t: t["entry_time"]),
        "errors": errors,
        "executed_at": datetime.utcnow().isoformat(),
    }

    row.lab_result = lab_result
    await db.commit()

    research = "REJECT"
    totals = lab_result["totals"]
    if totals["stocks"] >= 5 and totals["positive_stocks"] / totals["stocks"] >= 0.6:
        research = "DESIGN_CANDIDATE"

    return {
        "configuration_id": str(row.id),
        "name": row.name,
        "status": row.status,
        "research_status": research,
        **lab_result,
    }


def _trade_dict(t) -> dict:
    return {
        "trade_id": t.trade_id,
        "side": t.side,
        "qty": t.qty,
        "entry_index": t.entry_index,
        "entry_time": t.entry_time.isoformat(),
        "entry_price": float(t.entry_price),
        "exit_index": t.exit_index,
        "exit_time": t.exit_time.isoformat(),
        "exit_price": float(t.exit_price),
        "bars_held": t.bars_held,
        "gross_pnl": float(t.gross_pnl),
        "commission": float(t.commission),
        "slippage": float(t.slippage),
        "net_pnl": float(t.net_pnl),
        "exit_reason": t.exit_reason,
        "initial_stop": t.initial_stop,
        "take_profit": t.take_profit,
    }


async def fork_configuration(db: AsyncSession, config_id: uuid.UUID, new_name: str | None = None) -> dict:
    row = await _get_row(db, config_id)
    logger.warning("S2L: конфигурация %s интервал=%s члены=%d", config_id, row.interval_name, len(row.members))
    clone = Configuration(
        name=new_name or f"{row.name} (fork)",
        status="DRAFT",
        interval_name=row.interval_name,
        members=row.members,
        quorum=row.quorum,
        exit_policy=row.exit_policy,
        min_hold_bars=row.min_hold_bars,
        allow_short=row.allow_short,
        session_policy=row.session_policy,
        filters=row.filters,
        preview_figi=row.preview_figi,
    )
    db.add(clone)
    await db.commit()
    return await get_configuration(db, clone.id)


async def _get_row(db: AsyncSession, config_id: uuid.UUID) -> Configuration:
    row = await db.scalar(select(Configuration).where(Configuration.id == config_id))
    if row is None:
        raise ConfigurationError("configuration not found")
    return row


async def get_configuration(db: AsyncSession, config_id: uuid.UUID) -> dict:
    row = await _get_row(db, config_id)
    logger.warning("S2L: конфигурация %s интервал=%s члены=%d", config_id, row.interval_name, len(row.members))
    return _row_to_dict(row)


def _row_to_dict(row: Configuration) -> dict:
    return {
        "configuration_id": str(row.id),
        "name": row.name,
        "status": row.status,
        "interval_name": row.interval_name,
        "members": row.members,
        "quorum": row.quorum,
        "exit_policy": row.exit_policy,
        "min_hold_bars": row.min_hold_bars,
        "allow_short": row.allow_short,
        "session_policy": row.session_policy,
        "filters": row.filters,
        "preview_figi": row.preview_figi,
        "source_runs": row.source_runs,
        "lab_result": row.lab_result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


async def list_configurations(db: AsyncSession, limit: int = 50) -> list[dict]:
    rows = (
        await db.execute(select(Configuration).order_by(desc(Configuration.updated_at)).limit(limit))
    ).scalars().all()
    out = []
    for r in rows:
        d = _row_to_dict(r)
        lr = r.lab_result or {}
        pv = r.lab_result or {}
        summary = pv.get("summary", {}).get("summary", {})
        d["preview_net"] = summary.get("net")
        d["preview_trades"] = summary.get("trades")
        d["lab_totals"] = (lr.get("totals") or {}) if r.status == "LAB_COMPLETED" else None
        d.pop("lab_result", None)
        out.append(d)
    return out
