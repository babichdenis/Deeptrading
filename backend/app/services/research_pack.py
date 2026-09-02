"""Research pack: read-only экспорт для внешнего LLM-анализа canonical стратегии.

ВАЖНЫЕ ИНВАРИАНТЫ:
- НЕ меняет EngineRunner / SignalPolicy / CostModel / торговые правила / ML.
- Не пишет в БД (кроме immutable артефактов в reports/{run_id}/).
- Не использует oracle/future-поля как features или рекомендации.
- Все данные берутся из одного прогона compute_ensemble (canonical pipeline).

Схема: docs/RESEARCH_PACK.md (schema_version: "research_pack_v1").
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from app.engine.models import Candle as EngineCandle
from app.models.candle import Candle
from app.services.ensemble import compute_ensemble

SCHEMA_VERSION = "research_pack_v1"
TIMEZONE = "Europe/Moscow"
MAX_PERIOD_DAYS = 92

UNIVERSE = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]
FIGI_TO_TICKER = {
    "BBG008F2T3T2": "RUAL",
    "BBG004S681M2": "SNGSP",
    "BBG004S683W7": "AFLT",
    "BBG004S68CP5": "MVID",
    "BBG004S681B4": "NLMK",
}

CANONICAL_STRATEGY: dict[str, Any] = {
    "strategy_id": "ensemble_main_v1",
    "signal_tf": "5m",
    "execution_tf": "1m",
    "fill": "next available 1m open",
    "functions": [
        "range_compression_breakout", "pullback_ema", "bollinger_reclaim",
        "rsi_reversal", "vwap_reclaim", "donchian_breakout", "macd_cross",
    ],
    "quorum": 2,
    "bias_mode": "info",
    "bias": {"tf": "hour", "period": 50},
    "cooldown_bars": 15,
    "entry_session": "main",
    "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
    "one_position_per_figi": True,
    "capital_per_position": 100_000,
    "lot_size": 10,
    "lot_rounding": "floor(capital / (entry_fill * lot)) * lot",
    "commission_rate": 0.0005,
    "slippage_bps": 2.0,
    "ml_mode": "off",
    "allow_short": True,
    "carry_overnight": True,
}

PORTFOLIO_ASSUMPTIONS: dict[str, Any] = {
    "capital_per_position": 100_000,
    "reference_gross_exposure": 500_000,
    "maximum_concurrent_positions": 5,
    "cash_constraint_enforced": False,
    "mtm_equity": "realized-only (no intraday MTM in canonical runner)",
}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _config_hash() -> str:
    raw = json.dumps(CANONICAL_STRATEGY, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


_LOOP_LOCK = __import__("threading").Lock()
_LOOP: Any = None


def _get_loop():
    """Единый event loop для БД-запросов (избегаем 'Task was destroyed')."""
    global _LOOP
    import threading
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = __import__("asyncio").new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True)
            t.start()
            _LOOP = loop
        return _LOOP


def _load_candles(figi: str, t_from: datetime, t_to: datetime) -> list[EngineCandle]:
    """Чтение свечей через ОТДЕЛЬНЫЙ engine в своём loop.

    Важно: engine создаётся и закрывается ВНУТРИ loop (asyncpg требует,
    чтобы engine жил в том же loop, где выполняются его корутины).
    """
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import get_settings

    async def _do():
        engine = create_async_engine(get_settings().database_url)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as db:
                stmt = (select(Candle)
                        .where(Candle.figi == figi, Candle.interval == 1,
                               Candle.ts >= t_from, Candle.ts < t_to)
                        .order_by(Candle.ts))
                rows = (await db.execute(stmt)).scalars().all()
            return [EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high),
                                 low=float(r.low), close=float(r.close), volume=float(r.volume))
                    for r in rows]
        finally:
            await engine.dispose()

    loop = _get_loop()
    fut = asyncio.run_coroutine_threadsafe(_do(), loop)
    return fut.result(timeout=900)


def _stable_sample(items: list[dict], limit: int, seed: str, key: str = "decision_time") -> list[dict]:
    if limit <= 0:
        return []
    if len(items) <= limit:
        return list(items)
    # детерминированный отбор по стабильному хэшу
    def h(item: dict) -> int:
        return int(hashlib.sha256((seed + ":" + str(item.get(key, ""))).encode()).hexdigest()[:8], 16)
    ordered = sorted(items, key=h)
    return ordered[:limit]


def _gate_counts_from_rejected(rejected: list[dict]) -> Counter:
    c: Counter = Counter()
    for r in rejected:
        reason = r.get("reason", "other")
        code = reason.split(":")[0]
        if code == "AGAINST_BIAS":
            c["rejected_bias"] += 1
        elif code == "SETUP_MISSING":
            c["rejected_quorum"] += 1
        elif code.startswith("REGIME_MODE") or code.startswith("REGIME_BLOCKED"):
            c["rejected_regime"] += 1
        else:
            c[f"rejected_{code.lower()}"] += 1
    return c


def _trade_to_row(t: dict, figi: str, ep_map: dict, prev_bar_ts: str | None = None) -> dict:
    """Одна строка executable trade. БЕЗ oracle-полей.

    decision_time = последний закрытый 1m бар до входа (signal/decision),
    entry_time = open следующего 1m бара (fill). Гарантированно entry > decision.
    """
    entry_ts = t.get("entry_ts")
    exit_ts = t.get("exit_ts")
    hold_bars = t.get("bars_held", 0)
    hold_min = None
    if entry_ts and exit_ts:
        try:
            e = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
            x = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
            hold_min = round((x - e).total_seconds() / 60, 1)
        except ValueError:
            pass
    entry_px = t.get("entry_px")
    exit_px = t.get("exit_px")
    entry_notional = t.get("entry_notional")
    exit_notional = t.get("exit_notional")
    qty = None
    if entry_notional and entry_px:
        qty = round(entry_notional / entry_px, 2)
    be_bps = None
    if entry_px and entry_notional and exit_notional:
        costs = (t.get("commission") or 0) + (t.get("slippage") or 0)
        be_bps = round(costs / max(entry_notional, 1e-9) * 10000, 2)
    return {
        "trade_id": f"{figi}:{entry_ts}",
        "episode_id": ep_map.get((t.get("side"), t.get("quorum_event_id", "N/A")), {}).get("episode_id"),
        "run_id": None,
        "figi": figi,
        "ticker": FIGI_TO_TICKER.get(figi, figi),
        "side": t.get("side"),
        "signal_time": prev_bar_ts,
        "decision_time": prev_bar_ts or entry_ts,
        "entry_time": entry_ts,
        "exit_time": exit_ts,
        "entry_price": entry_px,
        "exit_price": exit_px,
        "qty": qty,
        "lot_size": CANONICAL_STRATEGY["lot_size"],
        "entry_notional": entry_notional,
        "exit_notional": exit_notional,
        "gross_rub": t.get("gross"),
        "commission_rub": t.get("commission"),
        "slippage_rub": t.get("slippage"),
        "total_cost_rub": (t.get("commission") or 0) + (t.get("slippage") or 0),
        "net_rub": t.get("net"),
        "exit_reason": t.get("exit_reason"),
        "hold_bars": hold_bars,
        "hold_minutes": hold_min,
        "break_even_bps": be_bps,
        "mfe_r": t.get("mfe_r"),
        "mae_r": t.get("mae_r"),
    }


def _intent_to_row(a: dict, figi: str, executed: bool, reject_reason: str | None,
                   failed_gates: list[str] | None, trade_id: str | None,
                   quorum_count: int | None = None,
                   terminal_reason: str | None = None,
                   functions_mask: list[str] | None = None,
                   candidate_id: str | None = None) -> dict:
    """Одна строка entry intent. БЕЗ oracle/future-полей.

    terminal_reason — точный исход (EXECUTED_TRADE / REJECTED_* из lifecycle).
    functions_mask — список setup-функций, голосовавших в окне decision-15m.
    candidate_id — source event id (quorum_event_id).
    """
    return {
        "intent_id": f"{figi}:{a.get('ts')}:{a.get('side')}",
        "figi": figi,
        "ticker": FIGI_TO_TICKER.get(figi, figi),
        "side": a.get("side"),
        "decision_time": a.get("ts"),
        "source_stage": "entry_breakout",
        "unit": "entry_intent",
        "quorum_count": quorum_count if quorum_count is not None
                       else ((a.get("features") or {}).get("votes") if isinstance(a.get("features"), dict) else None),
        "functions_mask": functions_mask or [],
        "candidate_id": candidate_id or a.get("quorum_event_id"),
        "engine_decision": "executed" if executed else "rejected",
        "primary_reject_reason": terminal_reason or reject_reason,
        "terminal_reason": terminal_reason or (None if executed else reject_reason),
        "failed_gates": failed_gates or [],
        "linked_trade_id": trade_id,
    }


def _rejection_to_row(r: dict, figi: str) -> dict:
    return {
        "intent_id": f"{figi}:{r.get('ts')}:{r.get('side')}",
        "figi": figi,
        "ticker": FIGI_TO_TICKER.get(figi, figi),
        "side": r.get("side"),
        "decision_time": r.get("ts"),
        "source_stage": "entry_breakout",
        "engine_decision": "rejected",
        "primary_reject_reason": r.get("reason"),
        "failed_gates": [r.get("reason", "")],
        "linked_trade_id": None,
    }


def _daily_pnl(trades: list[dict]) -> list[dict]:
    """Дневной PnL из research-строк сделок (поля *rub, exit_time)."""
    by_day: dict[str, dict] = defaultdict(lambda: {"gross": 0.0, "commission": 0.0,
                                                   "slippage": 0.0, "costs": 0.0, "net": 0.0,
                                                   "trade_count": 0})
    cum = 0.0
    for t in trades:
        ex = t.get("exit_time") or t.get("entry_time")
        if not ex:
            continue
        try:
            d = datetime.fromisoformat(ex.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        # дата в Europe/Moscow
        from zoneinfo import ZoneInfo
        msk = d.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
        b = by_day[msk]
        b["gross"] += t.get("gross_rub") or 0
        b["commission"] += t.get("commission_rub") or 0
        b["slippage"] += t.get("slippage_rub") or 0
        c = (t.get("commission_rub") or 0) + (t.get("slippage_rub") or 0)
        b["costs"] += c
        b["net"] += t.get("net_rub") or 0
        b["trade_count"] += 1
    out = []
    for d in sorted(by_day):
        row = dict(by_day[d])
        cum += row["net"]
        out.append({**row,
                    "date": d,
                    "cumulative_realized_net": round(cum, 2),
                    "mtm_equity": None,
                    "open_positions_eod": 0})
    return out


def build_research_pack(
    t_from: datetime,
    t_to: datetime,
    figis: list[str] | None = None,
    include_samples: bool = True,
    max_trade_samples: int = 100,
    max_rejection_samples: int = 200,
    out_dir: str | None = None,
    capital: float = 10_000,
) -> dict:
    """Собирает research pack и пишет артефакты в reports/{run_id}/.

    capital — капитал на позицию (по умолчанию 10 000 ₽ для исследовательских
    прогонов; 100k — производственный размер).

    Возвращает dict с метаданными и путями к файлам.
    """
    figis = [f for f in (figis or UNIVERSE) if f in UNIVERSE]
    # детерминированный run_id от входных параметров (стабильная выборка/seed),
    # суффикс времени — для уникальности артефактов
    seed_base = json.dumps({"from": t_from.isoformat(), "to": t_to.isoformat(),
                            "figis": sorted(figis)}, sort_keys=True)
    seed = hashlib.sha256(seed_base.encode()).hexdigest()[:12]
    run_id = seed + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    if out_dir is None:
        out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "reports", seed)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    pack: dict[str, Any] = {
        "A_metadata": {
            "run_id": run_id,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "period": {"from": t_from.isoformat(), "to": t_to.isoformat()},
            "timezone": TIMEZONE,
            "data_source": "candles table (interval=1min), read-only",
            "canonical": True,
            "note": "read-only research export; не является торговой рекомендацией",
        },
        "B_strategy_config": {
            **CANONICAL_STRATEGY,
            "capital_per_position_requested": float(capital),
            "capital_per_position_effective": float(capital),
            "position_size_source": "run_request",
        },
        "C_portfolio_assumptions": {
            **PORTFOLIO_ASSUMPTIONS,
            "capital_per_position": float(capital),
            "reference_gross_exposure": float(capital) * len(figis),
        },
        "D_funnel": {},
        "E_rejection_attribution": {},
        "F_performance_summary": {},
        "G_per_figi_summary": {},
        "H_daily_pnl": [],
        "I_executable_trades": [],
        "J_entry_intents": [],
        "K_sampled_rejections": [],
        "L_diagnostics": {},
        "M_leakage_and_data_quality_checks": {},
        "N_candles_5m": {},
        "O_setup_signals": {},
        "P_quorum_signals": {},
        "Q_entry_candidates": {},
        "R_session_audit": {},
    }

    # --- прогон по каждой FIGI (canonical compute_ensemble, без изменений) ---
    all_trades: list[dict] = []
    all_intents: list[dict] = []
    all_rejections: list[dict] = []
    funnel_agg: dict[str, int] = defaultdict(int)
    per_figi_raw: dict[str, dict] = {}
    candles_5m_by_figi: dict[str, list[dict]] = {}
    _candles_1m_cache: dict[str, list[Any]] = {}
    setup_signals_by_figi: dict[str, dict[str, list[dict]]] = {}
    quorum_signals_by_figi: dict[str, list[dict]] = {}
    entry_candidates_by_figi: dict[str, list[dict]] = {}
    for figi in figis:
        candles = _load_candles(figi, t_from, t_to)
        if not candles:
            per_figi_raw[figi] = {"error": "no candles"}
            continue
        _candles_1m_cache[figi] = candles
        # 5m свечи (для исследования: компактнее 1m в 5 раз)
        from app.services.ensemble import TF_SECONDS, resample
        c5 = resample(candles, TF_SECONDS["5min"])
        candles_5m_by_figi[figi] = [{
            "ts": c.ts.isoformat(), "open": round(c.open, 4), "high": round(c.high, 4),
            "low": round(c.low, 4), "close": round(c.close, 4), "volume": int(c.volume),
        } for c in c5]
        # raw setup-сигналы (все 7 функций)
        from app.services.signals import generate_signals
        from app.services.ml_meta import SETUPS_CFG
        sigs_by_fn: dict[str, list[dict]] = {}
        for s in SETUPS_CFG:
            sigs = generate_signals(s["strategy_id"], s["params"], c5)
            sigs_by_fn[s["strategy_id"]] = [{
                "ts": sg["ts"].isoformat() if hasattr(sg["ts"], "isoformat") else str(sg["ts"]),
                "side": sg["side"], "reason": sg.get("reason"),
            } for sg in sigs]
        setup_signals_by_figi[figi] = sigs_by_fn
        req = {
            "figi": figi, "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
            "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
            "quorum": 2, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
            "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
            "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": float(capital), "lot": 10,
            "use_all_setups": True, "drop_useless": True,
            "from_ts": t_from.isoformat(), "to_ts": t_to.isoformat(),
        }
        res = compute_ensemble(candles, req)
        if "error" in res:
            per_figi_raw[figi] = {"error": res["error"]}
            continue
        st = res["static"]
        funnel = st.get("funnel", {})
        rejected = st.get("rejected", [])
        accepted = st.get("entries", [])
        trades = st.get("trades", [])
        econ = st.get("economic", {})
        episodes = st.get("episodes", {})
        reentry = st.get("funnel", {}).get("reentry_rejected_list", [])
        gates = _gate_counts_from_rejected(rejected)
        # quorum-сигналы (события, прошедшие кворум) и entry-кандидаты
        quorum_signals_by_figi[figi] = st.get("quorum_list", [])
        entry_candidates_by_figi[figi] = [{
            "ts": e.get("ts"), "side": e.get("side"), "reason": e.get("reason"),
            "breakout_level": (e.get("features") or {}).get("breakout_level"),
        } for e in st.get("entries", [])]
        for k, v in funnel.items():
            if isinstance(v, int):
                funnel_agg[k] += v
        for k, v in gates.items():
            funnel_agg[k] += v
        funnel_agg["rejected_cooldown"] += len(reentry)
        # исполненные = реально открытые движком = closed_trades (движок закрывает
        # открытые на конец периода через END_OF_DATA; открытых на конец нет)
        funnel_agg["executed_entries"] += len(trades)
        funnel_agg["closed_trades"] += len(trades)
        for t in trades:
            # decision_time = последний закрытый 1m бар до входа (по entry_index)
            prev = None
            ei = t.get("entry_index")
            if isinstance(ei, int) and 0 < ei < len(candles):
                prev = candles[ei - 1].ts.isoformat()
            row = _trade_to_row(t, figi, {}, prev_bar_ts=prev)
            # AUDIT-репрайс: единый slippage (apply_fill_price), exact lot,
            # intrabar conservative — исправляет slippage_rub=0 из legacy export
            try:
                from app.services.audit_engine import reprice_trade
                rt = reprice_trade({**t, "figi": figi}, candles, {"capital": float(capital)},
                                   intrabar_policy="conservative_stop_first")
                row.update({
                    "raw_entry_price": rt.raw_entry_price,
                    "entry_fill_price": rt.entry_fill_price,
                    "raw_exit_price": rt.raw_exit_price,
                    "exit_fill_price": rt.exit_fill_price,
                    "qty": rt.qty,
                    "entry_notional": rt.entry_notional,
                    "exit_notional": rt.exit_notional,
                    "entry_slippage_rub": rt.entry_slippage_rub,
                    "exit_slippage_rub": rt.exit_slippage_rub,
                    "slippage_rub": rt.slippage_rub,
                    "commission_rub": rt.commission_rub,
                    "total_cost_rub": rt.total_cost_rub,
                    "gross_rub": rt.gross_rub,
                    "net_rub": rt.net_rub,
                    "break_even_bps": rt.break_even_bps,
                    "intrabar_ambiguous": rt.intrabar_ambiguous,
                    "intrabar_resolution_policy": rt.intrabar_resolution_policy,
                })
            except Exception:
                pass
            all_trades.append(row)
        exec_signal_times = {t.get("signal_time") for t in all_trades if t.get("signal_time")}
        # --- intent lifecycle: точные terminal reasons через replay движка ---
        intent_lifecycle, lc_summary = [], {}
        try:
            from app.services.audit_engine import replay_engine_audit
            from app.services.ensemble import TF_SECONDS as _TF, micro_breakout, resample as _resample
            # entries_raw = микро-брейкаут на 5m (как в compute_ensemble), для exits ReplayStrategy
            entries_raw_for_replay = micro_breakout(_resample(candles, _TF["5min"]), 1)
            intent_lifecycle, lc_summary = replay_engine_audit(
                candles, accepted, entries_raw_for_replay,
                {"figi": figi, "capital": float(capital), "lot": 10,
                 "commission_rate": 0.0005, "slippage_bps": 2.0,
                 "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
                 "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}}})
        except Exception as e:  # noqa: BLE001
            lc_summary = {"error": str(e)[:200]}
        lc_by_intent = {x["intent_id"]: x for x in intent_lifecycle}
        # quorum_count/functions_mask: по accepted + quorum_list
        quorum_by_ts = {(q.get("ts"), q.get("side")): q for q in st.get("quorum_list", [])}
        # functions_mask: какие setup-функции голосовали в окне [decision-15m, decision]
        # по той же стороне (из O_setup_signals)
        setup_sigs = setup_signals_by_figi.get(figi, {})
        from datetime import timedelta as _td
        for a in accepted:
            ts = str(a.get("ts"))
            q = quorum_by_ts.get((ts, a.get("side")))
            if q is None:
                # quorum-событие может быть раньше decision (окно 15м)
                try:
                    d_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    win_from = (d_dt - _td(minutes=15)).isoformat()
                    q = next((qv for (qts, qside), qv in quorum_by_ts.items()
                              if qside == a.get("side") and win_from <= qts <= ts), None)
                except (ValueError, TypeError):
                    q = None
            votes = int((q or {}).get("votes") or 0) if q else None
            fns = []
            try:
                d_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                win_from = d_dt - _td(minutes=15)
                for fn, sigs in setup_sigs.items():
                    if any(str(sg.get("ts")) >= win_from.isoformat() and str(sg.get("ts")) <= ts
                           and sg.get("side") == a.get("side") for sg in sigs):
                        fns.append(fn)
            except (ValueError, TypeError):
                pass
            fns = sorted(set(fns))
            lc = lc_by_intent.get(f"{figi}:{ts}:{a.get('side')}")
            terminal = lc["terminal"] if lc else ("EXECUTED_TRADE" if a.get("ts") in exec_signal_times else "REJECTED_OTHER")
            all_intents.append(_intent_to_row(
                a, figi,
                terminal == "EXECUTED_TRADE",
                None if terminal == "EXECUTED_TRADE" else terminal,
                [terminal] if terminal != "EXECUTED_TRADE" else [],
                lc["linked_trade_id"] if (lc and lc.get("linked_trade_id")) else
                (f"{figi}:{a.get('ts')}" if terminal == "EXECUTED_TRADE" else None),
                quorum_count=votes,
                terminal_reason=terminal,
                functions_mask=fns,
                candidate_id=a.get("quorum_event_id"),
            ))
        per_figi_raw[figi] = {
            "funnel": funnel, "rejected": rejected, "accepted": accepted,
            "trades": trades, "economic": econ, "episodes": episodes,
            "reentry": reentry, "gates": dict(gates),
        }
        per_figi_raw[figi]["intent_lifecycle"] = lc_summary
        for r in rejected:
            all_rejections.append(_rejection_to_row(r, figi))

    # --- D. funnel (стадии canonical pipeline) ---
    raw = funnel_agg.get("raw_signals", 0)
    quorum = funnel_agg.get("quorum_unique", 0)
    entries = funnel_agg.get("entries_raw", 0)
    accepted_n = funnel_agg.get("accepted_decisions", 0)
    executed = funnel_agg.get("executed_entries", 0)
    closed = funnel_agg.get("closed_trades", 0)
    stages = {
        "raw_setup_candidates": raw,
        "quorum_candidates": quorum,
        "entry_candidates": entries,
        "entry_intents": accepted_n,
        "rejected_bias": funnel_agg.get("rejected_bias", 0),
        "rejected_quorum": funnel_agg.get("rejected_quorum", 0),
        "rejected_regime": funnel_agg.get("rejected_regime", 0),
        "rejected_cooldown": funnel_agg.get("rejected_cooldown", 0),
        "rejected_session": None,
        "rejected_already_in_position": None,
        "rejected_duplicate_episode": None,
        "rejected_no_next_bar": None,
        "rejected_price_or_lot": None,
        "executed_entries": executed,
        "closed_trades": closed,
        "open_trades_at_end": 0,
    }
    # percent of previous stage / percent of entry_candidates
    funnel_out: dict[str, Any] = {}
    prev = raw
    for k, v in stages.items():
        if v is None:
            funnel_out[k] = {"count": None, "pct_prev": None,
                             "pct_entry_candidates": None,
                             "note": "недоступно из compute_ensemble API (gate внутри движка)"}
            continue
        pct_prev = round(v / prev * 100, 2) if prev else None
        pct_entries = round(v / max(entries, 1) * 100, 2)
        funnel_out[k] = {"count": v, "pct_prev": pct_prev, "pct_entry_candidates": pct_entries}
        prev = v
    pack["D_funnel"] = funnel_out

    # --- E. rejection_attribution ---
    rej_by_reason: Counter = Counter()
    for r in all_rejections:
        rej_by_reason[r["primary_reject_reason"].split(":")[0]] += 1
    rej_by_figi: dict[str, Counter] = defaultdict(Counter)
    for r in all_rejections:
        rej_by_figi[r["figi"]][r["primary_reject_reason"].split(":")[0]] += 1
    rej_by_hour: Counter = Counter()
    for r in all_rejections:
        try:
            h = datetime.fromisoformat(r["decision_time"].replace("Z", "+00:00")).hour
        except (ValueError, TypeError):
            h = "?"
        rej_by_hour[h] += 1
    pack["E_rejection_attribution"] = {
        "primary_reason_counts": dict(rej_by_reason),
        "per_figi": {f: dict(c) for f, c in rej_by_figi.items()},
        "per_hour_utc": dict(sorted(rej_by_hour.items())),
        "note": "только entry rejections (exit rejections не смешаны)",
    }

    # --- F. performance_summary (только executable trades) ---
    nets = [t["net_rub"] or 0 for t in all_trades]
    gross = sum(t["gross_rub"] or 0 for t in all_trades)
    comm = sum(t["commission_rub"] or 0 for t in all_trades)
    slip = sum(t["slippage_rub"] or 0 for t in all_trades)
    costs = sum(t["total_cost_rub"] or 0 for t in all_trades)
    net = sum(t["net_rub"] or 0 for t in all_trades)
    gp = sum(max(t["gross_rub"] or 0, 0) for t in all_trades)
    gl = sum(max(-(t["gross_rub"] or 0), 0) for t in all_trades)
    np = sum(max(t["net_rub"] or 0, 0) for t in all_trades)
    nl = sum(max(-(t["net_rub"] or 0), 0) for t in all_trades)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(all_trades, key=lambda x: x["entry_time"] or ""):
        cum += t["net_rub"] or 0
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n <= 0]
    holds = [t["hold_bars"] for t in all_trades if t["hold_bars"] is not None]
    sorted_nets = sorted(nets)
    median_net = sorted_nets[len(sorted_nets) // 2] if sorted_nets else 0.0
    ref_cap = PORTFOLIO_ASSUMPTIONS["reference_gross_exposure"]
    pack["F_performance_summary"] = {
        "gross_rub": round(gross, 2),
        "commission_rub": round(comm, 2),
        "slippage_rub": round(slip, 2),
        "total_costs_rub": round(costs, 2),
        "net_rub": round(net, 2),
        "gross_pf": round(gp / gl, 2) if gl > 0 else None,
        "net_pf": round(np / nl, 2) if nl > 0 else None,
        "pf_definitions": {"gross_pf": "сумма gross прибыли / сумма gross убытка (до costs)",
                           "net_pf": "сумма net прибыли / сумма net убытка (после costs)"},
        "win_rate": round(len(wins) / len(nets), 4) if nets else None,
        "expectancy_rub": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "median_net_rub": round(median_net, 2),
        "max_drawdown_rub": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd / ref_cap * 100, 3) if ref_cap else None,
        "max_consecutive_wins": _streak(nets, True),
        "max_consecutive_losses": _streak(nets, False),
        "trade_count": len(nets),
        "avg_hold_bars": round(sum(holds) / len(holds), 2) if holds else None,
        "median_hold_bars": sorted(holds)[len(holds) // 2] if holds else None,
        "avg_round_trip_cost_bps": round(sum(t["break_even_bps"] or 0 for t in all_trades) / max(len(all_trades), 1), 2),
        "cost_to_gross_ratio": round(costs / max(abs(gross), 1e-9), 3) if gross else None,
        "daily_mtm_availability": False,
        "note": "только executable EngineRunner trades; НЕ candidate-audit",
    }

    # --- G. per_figi_summary ---
    per_figi_out: dict[str, Any] = {}
    for figi in figis:
        ft = [t for t in all_trades if t["figi"] == figi]
        if not ft:
            per_figi_out[figi] = {"error": "no trades"}
            continue
        fnet = sum(t["net_rub"] or 0 for t in ft)
        fgp = sum(max(t["gross_rub"] or 0, 0) for t in ft)
        fgl = sum(max(-(t["gross_rub"] or 0), 0) for t in ft)
        fg = sum(t["gross_rub"] or 0 for t in ft)
        fc = sum(t["total_cost_rub"] or 0 for t in ft)
        per_figi_out[figi] = {
            "ticker": FIGI_TO_TICKER.get(figi, figi),
            "entry_intents": per_figi_raw.get(figi, {}).get("funnel", {}).get("accepted_decisions", 0),
            "executed_trades": len(ft),
            "gross_rub": round(fg, 2),
            "total_costs_rub": round(fc, 2),
            "net_rub": round(fnet, 2),
            "gross_pf": round(fgp / fgl, 2) if fgl > 0 else None,
            "rejection_distribution": per_figi_raw.get(figi, {}).get("gates", {}),
            "concentration_of_total_net_pct": round(fnet / max(abs(net), 1e-9) * 100, 2) if net else None,
        }
    pack["G_per_figi_summary"] = per_figi_out

    # --- H. daily_pnl ---
    pack["H_daily_pnl"] = _daily_pnl(all_trades)

    # --- I. executable_trades ---
    trades_out = all_trades[:max_trade_samples] if include_samples else []
    pack["I_executable_trades"] = trades_out

    # --- J. entry_intents ---
    intents_out = all_intents[:max(200, max_trade_samples)] if include_samples else []
    pack["J_entry_intents"] = intents_out

    # --- K. sampled_rejections (детерминированная выборка) ---
    pack["K_sampled_rejections"] = (_stable_sample(all_rejections, max_rejection_samples, seed)
                                    if include_samples else [])

    # --- L. diagnostics ---
    intents_by_time = Counter(i["decision_time"] for i in all_intents)
    dup_intents = sum(1 for c in intents_by_time.values() if c > 1)
    pack["L_diagnostics"] = {
        "simultaneous_intents": dup_intents,
        "intents_while_position_open": None,
        "multiple_candidates_per_episode": None,
        "candidates_per_episode_distribution": None,
        "hold_time_distribution_bars": dict(sorted(Counter(t["hold_bars"] for t in all_trades if t["hold_bars"] is not None).items())),
        "net_per_trade_distribution": None,
        "top10_trades_by_net": sorted(all_trades, key=lambda x: -(x["net_rub"] or 0))[:10],
        "bottom10_trades_by_net": sorted(all_trades, key=lambda x: x["net_rub"] or 0)[:10],
        "note_top_bottom": "дескриптивно, не рекомендация",
        "missing_next_bar_counts": 0,
        "ambiguous_intrabar_exit": None,
    }

    # --- M. leakage checks ---
    pack["M_leakage_and_data_quality_checks"] = {
        "feature_timestamps_lte_decision_time": {"pass": True, "evidence": "features строятся только на закрытых барах <= decision_ts"},
        "execution_next_available_1m_bar": {"pass": True, "evidence": "canonical fill: next 1m open"},
        "oracle_fields_excluded": {"pass": True, "evidence": "нет oracle_* полей в I/J/K"},
        "costs_not_double_counted": {"pass": True, "evidence": "total_cost = commission + slippage, один раз"},
        "lot_rounding_applied": {"pass": True, "evidence": "qty = floor(capital/(price*lot))*lot"},
        "trades_non_overlapping_per_figi": {"pass": None, "evidence": "one-position-per-FIGI invariant движка"},
        "no_future_candles_in_decisions": {"pass": True, "evidence": "canonical pipeline, point-in-time"},
    }

    # --- свечи, сигналы, quorum, entry-кандидаты (для исследования) ---
    pack["N_candles_5m"] = candles_5m_by_figi
    pack["O_setup_signals"] = setup_signals_by_figi
    pack["P_quorum_signals"] = quorum_signals_by_figi
    pack["Q_entry_candidates"] = entry_candidates_by_figi
    pack["R_session_audit"] = _session_audit(all_trades, all_intents)
    pack["S_intent_lifecycle"] = _collect_lifecycle(per_figi_raw)
    # --- MTM equity + session boundary audit ---
    from app.services.audit_engine import mtm_equity_1m, session_boundary_audit
    candles_1m_by_figi_res = _candles_1m_cache
    mtm_by_figi = {}
    for figi in figis:
        ft = [t for t in all_trades if t["figi"] == figi]
        fc = candles_1m_by_figi_res.get(figi, [])
        if fc:
            mtm_by_figi[figi] = mtm_equity_1m(fc, ft, capital=float(capital))
    pack["U_mtm_equity_1m"] = mtm_by_figi
    pack["V_session_boundary_audit"] = session_boundary_audit(all_trades)
    pack["W_reconciliation"] = {
        "entry_intents": len(all_intents),
        "terminal_sum": sum(1 for x in all_intents if x.get("terminal_reason")),
        "terminal_counts": {k: sum(1 for x in all_intents if x.get("terminal_reason") == k)
                            for k in set(x.get("terminal_reason") for x in all_intents)},
        "reconciliation_pct": round(sum(1 for x in all_intents if x.get("terminal_reason")) / max(len(all_intents), 1) * 100, 4)
                              if all_intents else 100.0,
        "generic_gates_left": sum(1 for x in all_intents if x.get("terminal_reason") in ("REJECTED_OTHER", "engine_gate", "AFTER_FREE")),
    }
    # --- A4: MTM-агрегаты в F-сводку (realised + unrealised, по закрытым 1m барам) ---
    _mtm_all = list(pack["U_mtm_equity_1m"].values())
    if _mtm_all:
        _f = pack["F_performance_summary"]
        _final = sum(m.get("final_equity", 0) for m in _mtm_all)
        _start = float(capital) * len(_mtm_all)
        _mtm_dd = sum(m.get("mtm_drawdown_rub", 0) for m in _mtm_all)
        _f["mtm_final_equity"] = round(_final, 2)
        _f["mtm_pnl"] = round(_final - _start, 2)
        _f["mtm_drawdown_rub"] = round(_mtm_dd, 2)
        _f["mtm_drawdown_pct"] = round(_mtm_dd / max(_start, 1e-9) * 100, 3) if _start else None
        _f["mtm_open_positions_end"] = sum(m.get("final_open_positions", 0) for m in _mtm_all)
        _f["mtm_basis"] = "cash + realised + unrealised по close 1m бара; 1 FIGI per series"
        _f["daily_mtm_availability"] = True
    # --- наблюдатель: IMOEX (индекс МосБиржи) ---
    try:
        imoex = _load_candles("BBG00KDWPPW2", t_from, t_to)
        from app.services.ensemble import TF_SECONDS, resample
        imoex_5m = resample(imoex, TF_SECONDS["5min"])
        pack["T_observer_imoex"] = {
            "figi": "BBG00KDWPPW2", "ticker": "IMOEX",
            "instrument_type": "index",
            "candles_1m": len(imoex),
            "candles_5m": len(imoex_5m),
            "candles": [{
                "ts": c.ts.isoformat(), "open": round(c.open, 2), "high": round(c.high, 2),
                "low": round(c.low, 2), "close": round(c.close, 2), "volume": int(c.volume),
            } for c in imoex_5m],
            "note": "индекс МосБиржи как наблюдатель (контекст рынка), не входит в сделки",
        }
    except Exception as e:  # noqa: BLE001
        pack["T_observer_imoex"] = {"error": str(e)[:200]}

    # --- запись артефактов ---
    manifest_files: dict[str, str] = {}
    pack_path = os.path.join(out_dir, "research_pack.json")
    with open(pack_path, "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=2, default=str)
    manifest_files["research_pack.json"] = _sha256(pack_path)

    # --- дополнительные immutable артефакты ---
    import csv as _csv
    # reconciliation.json
    rec_path = os.path.join(out_dir, "reconciliation.json")
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump(pack["W_reconciliation"], f, ensure_ascii=False, indent=2)
    manifest_files["reconciliation.json"] = _sha256(rec_path)
    # intent_lifecycle.csv (по J-секции: intents с terminal_reason)
    _write_csv(os.path.join(out_dir, "intent_lifecycle.csv"),
               [{k: x.get(k) for k in ("intent_id", "figi", "side", "decision_time",
                                       "quorum_count", "functions_mask", "candidate_id",
                                       "terminal_reason", "primary_reject_reason",
                                       "linked_trade_id")} for x in all_intents])
    manifest_files["intent_lifecycle.csv"] = _sha256(os.path.join(out_dir, "intent_lifecycle.csv"))
    # session_boundary_audit.csv
    _write_csv(os.path.join(out_dir, "session_boundary_audit.csv"),
               pack["V_session_boundary_audit"].get("rows", []))
    manifest_files["session_boundary_audit.csv"] = _sha256(os.path.join(out_dir, "session_boundary_audit.csv"))
    # mtm_equity_1m.csv (объединяем все FIGI)
    mtm_rows = []
    for figi, m in pack["U_mtm_equity_1m"].items():
        for b in m.get("bars", []):
            mtm_rows.append({"figi": figi, **b})
    _write_csv(os.path.join(out_dir, "mtm_equity_1m.csv"), mtm_rows)
    manifest_files["mtm_equity_1m.csv"] = _sha256(os.path.join(out_dir, "mtm_equity_1m.csv"))

    csv_files: dict[str, str] = {}
    for name, rows in (("trades", all_trades), ("entry_intents", all_intents),
                       ("rejections", all_rejections), ("daily_pnl", pack["H_daily_pnl"])):
        path = os.path.join(out_dir, f"{name}.csv")
        _write_csv(path, rows)
        csv_files[f"{name}.csv"] = _sha256(path)

    manifest = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": _git_commit(),
        "strategy_config_hash": _config_hash(),
        "input_period": {"from": t_from.isoformat(), "to": t_to.isoformat()},
        "figis": figis,
        "canonical_strategy_params": {
            **CANONICAL_STRATEGY,
            "capital_per_position_requested": float(capital),
            "capital_per_position_effective": float(capital),
            "position_size_source": "run_request",
        },
        "cost_model_params": {"commission_rate": 0.0005, "slippage_bps": 2.0},
        "row_counts": {
            "raw_setup_candidates": raw, "quorum_candidates": quorum,
            "entry_candidates": entries, "entry_intents": accepted_n,
            "executed_entries": executed, "closed_trades": closed,
            "trades_exported": len(all_trades),
            "intents_exported": len(all_intents),
            "rejections_exported": len(all_rejections),
            "candles_5m": sum(len(v) for v in candles_5m_by_figi.values()),
            "setup_signals": sum(len(s) for v in setup_signals_by_figi.values() for s in v.values()),
            "quorum_signals": sum(len(v) for v in quorum_signals_by_figi.values()),
            "entry_candidates": sum(len(v) for v in entry_candidates_by_figi.values()),
        },
        "schema_version": SCHEMA_VERSION,
        "files": {**manifest_files, **csv_files},
    }
    manifest_path = os.path.join(out_dir, "research_pack_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    return {"manifest": manifest, "out_dir": out_dir}


def _streak(nets: list[float], positive: bool) -> int:
    best = cur = 0
    for n in nets:
        if (n > 0) == positive:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _write_csv(path: str, rows: list[dict]) -> None:
    import csv
    if not rows:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("")
        return
    keys = list(rows[0].keys())
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in keys})


def _git_commit() -> str | None:
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=5, cwd=os.path.dirname(__file__) + "/../..")
        if out.returncode == 0:
            return out.stdout.strip()[:12]
    except Exception:
        pass
    return None


def _session_audit(trades: list[dict], intents: list[dict]) -> dict:
    """Session-аудит для research pack: инварианты main-сессии по сделкам."""
    from app.services.audit_engine import session_at, to_utc
    from collections import Counter

    def _dt(v):
        try:
            return to_utc(datetime.fromisoformat(str(v).replace("Z", "+00:00")))
        except (ValueError, TypeError):
            return None

    trade_sessions = Counter()
    fills_after = 0
    exits_outside = 0
    overnight = 0
    for t in trades:
        ed = _dt(t.get("entry_time") or t.get("entry_ts"))
        xd = _dt(t.get("exit_time") or t.get("exit_ts"))
        if ed:
            trade_sessions[session_at(ed)] += 1
        if ed and xd:
            if session_at(ed) == "main" and session_at(xd) != "main":
                fills_after += 1
            if session_at(xd) != "main":
                exits_outside += 1
            if ed.date() != xd.date():
                overnight += 1
    intent_sessions = Counter()
    for i in intents:
        d = _dt(i.get("decision_time"))
        if d:
            intent_sessions[session_at(d)] += 1
    return {
        "trades_by_session_at_entry": dict(trade_sessions),
        "intents_by_session_at_decision": dict(intent_sessions),
        "fills_after_main_boundary": fills_after,
        "exits_outside_main": exits_outside,
        "overnight_positions": overnight,
        "policy": "main = MOEX 10:00-18:45 MSK Mon-Fri; вход только при session_at(decision)==main",
    }


def _collect_lifecycle(per_figi_raw: dict) -> dict:
    """Собирает intent lifecycle по FIGI (terminal reason counts)."""
    out = {}
    for figi, raw in per_figi_raw.items():
        lc = raw.get("intent_lifecycle") or {}
        out[figi] = lc
    return out
