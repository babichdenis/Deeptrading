"""Frozen audit replay для ensemble_main_v1 (AUDIT-ONLY).

Читает свечи из БД, прогоняет compute_ensemble (canonical pipeline БЕЗ изменений),
затем пересчитывает fills/P&L существующих сделок audit-слоем:
  slippage=2bps per side, conservative_stop_first, точный lot sizing,
  session enforcement, funnel reconciliation, contention telemetry.

НЕ меняет сигналы/выходы/параметры стратегии. НЕ оптимизация.

Использование:
    python scripts/audit_replay.py --from 2026-07-01 --to 2026-08-31 \
        --out-json /tmp/audit_oos.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.engine.models import Candle as EngineCandle  # noqa: E402
from app.models.candle import Candle  # noqa: E402
from app.services.audit_engine import (  # noqa: E402
    TERMINAL_STATES,
    apply_fill_price,
    contention_groups,
    intent_session_fields,
    qty_for_fill,
    reprice_trade,
    session_at,
    split_funnel,
    trades_stats,
)
from app.services.ensemble import compute_ensemble  # noqa: E402

UNIVERSE = ["BBG008F2T3T2", "BBG004S681M2", "BBG004S683W7", "BBG004S68CP5", "BBG004S681B4"]

REQ: dict = {
    "bias_mode": "info", "bias": {"tf": "hour", "period": 50},
    "entry_tf": "5min", "entry": {"tf": "5min", "lookback": 1}, "entry_session": "main",
    "quorum": 2, "same_side_reentry_cooldown_bars": 15, "carry_overnight": True,
    "exit_policy": {"id": "atr_stop", "params": {"period": 14, "multiplier": 2.0, "risk_reward": 2.0}},
    "commission_rate": 0.0005, "slippage_bps": 2.0, "capital": 10_000, "lot": 10,
    "use_all_setups": True, "drop_useless": True,
}


_LOOP = None
_LOOP_LOCK = __import__("threading").Lock()


def _get_loop():
    global _LOOP
    import threading
    with _LOOP_LOCK:
        if _LOOP is None or _LOOP.is_closed():
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True)
            t.start()
            _LOOP = loop
        return _LOOP


def load_candles(figi: str, t_from: datetime, t_to: datetime) -> list[EngineCandle]:
    """Чтение свечей через ОТДЕЛЬНЫЙ engine в своём loop (глобальный engine
    привязан к loop uvicorn — 'attached to a different loop')."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.config import get_settings

    async def _do(engine):
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            stmt = (select(Candle)
                    .where(Candle.figi == figi, Candle.interval == 1,
                           Candle.ts >= t_from, Candle.ts < t_to)
                    .order_by(Candle.ts))
            rows = (await db.execute(stmt)).scalars().all()
        return [EngineCandle(ts=r.ts, open=float(r.open), high=float(r.high),
                             low=float(r.low), close=float(r.close), volume=float(r.volume))
                for r in rows]

    loop = _get_loop()
    engine = create_async_engine(get_settings().database_url)
    try:
        fut = asyncio.run_coroutine_threadsafe(_do(engine), loop)
        return fut.result(timeout=120)
    finally:
        stop = asyncio.run_coroutine_threadsafe(engine.dispose(), loop)
        stop.result(timeout=30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="t_from", default="2026-07-01")
    ap.add_argument("--to", dest="t_to", default="2026-07-31")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--intrabar-policy", default="conservative_stop_first",
                    choices=["conservative_stop_first", "optimistic_target_first", "current_legacy"])
    ap.add_argument("--out-json", default="/tmp/audit_oos.json")
    args = ap.parse_args()

    t_from = datetime.fromisoformat(args.t_from).replace(tzinfo=timezone.utc)
    t_to = datetime.fromisoformat(args.t_to).replace(tzinfo=timezone.utc)
    t0 = time.time()

    all_legacy: list[dict] = []     # экономика из движка (as-is)
    all_repriced: list[dict] = []   # audit reprice
    funnel_agg: dict = {"raw_signals": 0, "unique_raw_ts": 0, "quorum_unique": 0,
                        "entries_raw": 0, "accepted_decisions": 0, "entries_rejected": 0}
    rejected_agg: list[dict] = []
    accepted_agg: list[dict] = []
    reentry_agg: list[dict] = []
    per_figi: dict = {}

    for figi in UNIVERSE:
        candles = load_candles(figi, t_from, t_to)
        if not candles:
            print(f"{figi}: нет свечей", flush=True)
            continue
        req = {**REQ, "figi": figi, "from_ts": t_from.isoformat(), "to_ts": t_to.isoformat(),
               "capital": args.capital}
        res = compute_ensemble(candles, req)
        if "error" in res:
            print(f"{figi}: {res['error']}", flush=True)
            continue
        st = res["static"]
        funnel = st.get("funnel", {})
        rejected = st.get("rejected", [])
        accepted = st.get("entries", [])
        trades = st.get("trades", [])
        econ = st.get("economic", {})
        reentry = funnel.get("reentry_rejected_list", [])

        for k in funnel_agg:
            funnel_agg[k] += funnel.get(k, 0)
        rejected_agg.extend(rejected)
        accepted_agg.extend(accepted)
        reentry_agg.extend(reentry)

        # legacy (движок as-is)
        for t in trades:
            t2 = dict(t)
            t2["figi"] = figi
            t2["legacy_slippage"] = (t2.get("entry_slippage") or 0) + (t2.get("exit_slippage") or 0)
            all_legacy.append(t2)

        # audit reprice (только fills/P&L)
        for t in trades:
            try:
                rt = reprice_trade({**t, "figi": figi}, candles, REQ,
                                   intrabar_policy=args.intrabar_policy)
                all_repriced.append({
                    "trade_id": rt.trade_id, "figi": rt.figi, "side": rt.side,
                    "raw_entry_price": rt.raw_entry_price,
                    "entry_fill_price": rt.entry_fill_price,
                    "raw_exit_price": rt.raw_exit_price,
                    "exit_fill_price": rt.exit_fill_price,
                    "qty": rt.qty, "lot_size": rt.lot_size,
                    "entry_slippage_rub": rt.entry_slippage_rub,
                    "exit_slippage_rub": rt.exit_slippage_rub,
                    "slippage_rub": rt.slippage_rub,
                    "commission_rub": rt.commission_rub,
                    "total_cost_rub": rt.total_cost_rub,
                    "gross_rub": rt.gross_rub, "net_rub": rt.net_rub,
                    "break_even_bps": rt.break_even_bps,
                    "exit_reason": rt.exit_reason,
                    "intrabar_ambiguous": rt.intrabar_ambiguous,
                    "intrabar_resolution_policy": rt.intrabar_resolution_policy,
                    "entry_time": rt.entry_time.isoformat(),
                    "exit_time": rt.exit_time.isoformat(),
                    "session_at_entry": session_at(rt.entry_time),
                    "session_at_exit": session_at(rt.exit_time),
                    **intent_session_fields(rt.entry_time, rt.exit_time),
                })
            except Exception as e:  # noqa: BLE001
                print(f"  reprice {figi} {t.get('trade_id')}: {e}", flush=True)

        # contention
        cg = contention_groups(accepted)
        per_figi[figi] = {
            "candles": len(candles),
            "legacy": {"trades": len(trades), "net": econ.get("net", 0), "gross": econ.get("gross", 0),
                       "commission": econ.get("commission", 0), "slippage": econ.get("slippage", 0),
                       "costs": econ.get("costs", 0), "pf": econ.get("profit_factor")},
            "repriced_n": len([r for r in all_repriced if r["figi"] == figi]),
            "contention_groups": cg,
        }
        print(f"{figi}: legacy net={econ.get('net', 0):.0f} ({len(trades)} сделок) "
              f"| repriced={len([r for r in all_repriced if r['figi'] == figi])}", flush=True)

    # --- before/after ---
    legacy_stats = {
        "n": len(all_legacy),
        "gross": round(sum(t.get("gross", 0) or 0 for t in all_legacy), 2),
        "commission": round(sum(t.get("commission", 0) or 0 for t in all_legacy), 2),
        "slippage": round(sum(t.get("legacy_slippage", 0) or 0 for t in all_legacy), 2),
        "net": round(sum(t.get("net", 0) or 0 for t in all_legacy), 2),
        "slippage_source": "движок (fill_price), но export брал entry_slippage+exit_slippage с захардкоженным slip_bps=0.0002",
    }
    rt_objs = []
    # считаем stats из all_repriced (структура — dict)
    after = trades_stats_from_dicts(all_repriced)

    funnel_split = split_funnel(funnel_agg, rejected_agg, accepted_agg,
                                all_legacy, reentry_agg)
    # session enforcement: сколько intents вне main
    session_rejects = sum(1 for a in accepted_agg
                          if session_at(datetime.fromisoformat(str(a["ts"]).replace("Z", "+00:00"))) != "main")

    # --- разложение IN_POSITION + session-инварианты ---
    from app.services.audit_engine import decompose_in_position, session_invariants
    in_position_decomp = decompose_in_position(accepted_agg, all_legacy, reentry_agg,
                                               funnel_agg["entries_raw"])
    sess_inv = session_invariants(all_legacy, accepted_agg)

    report = {
        "audit": True,
        "period": [t_from.isoformat(), t_to.isoformat()],
        "intrabar_policy": args.intrabar_policy,
        "slippage_bps": REQ["slippage_bps"],
        "universe": UNIVERSE,
        "before_legacy": legacy_stats,
        "after_audit_reprice": after,
        "funnel_split": funnel_split,
        "session_enforcement": {
            "intents_total": len(accepted_agg),
            "intents_outside_main": session_rejects,
            "policy": "main: MOEX 10:00-18:45 MSK Mon-Fri (schedule provider)",
            "enforced": "new entry только при session_at_decision == main",
            "invariants": sess_inv,
        },
        "in_position_decomposition": in_position_decomp,
        "contention": {"groups": [g for g in [] for _ in []] or _flatten(per_figi)},
        "per_figi": {k: {kk: vv for kk, vv in v.items()} for k, v in per_figi.items()},
        "trades_full": all_repriced,
        "entry_intents_full": accepted_agg,
        "rejections_full": rejected_agg,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "AUDIT-ONLY: не оптимизация, сигналы/выходы не менялись",
    }
    # полный экспорт сделок (для внешнего исследования) — отдельный файл
    trades_path = args.out_json.replace(".json", "_trades.json")
    with open(trades_path, "w") as f:
        json.dump({"trades": all_repriced, "entry_intents": accepted_agg,
                   "rejections": rejected_agg, "funnel": funnel_split,
                   "session": sess_inv, "generated_at": report["generated_at"]},
                  f, ensure_ascii=False, indent=2, default=str)
    with open(args.out_json, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\nОтчёт: {args.out_json} ({time.time()-t0:.0f}с)")
    print(f"Сделки (полные): {trades_path} — {len(all_repriced)} сделок")
    print("before:", json.dumps(legacy_stats, ensure_ascii=False))
    print("after: ", json.dumps(after, ensure_ascii=False))
    print("reconciliation:", json.dumps(funnel_split["reconciliation"], ensure_ascii=False))


def trades_stats_from_dicts(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    nets = [r["net_rub"] for r in rows]
    gp = sum(max(r["gross_rub"], 0) for r in rows)
    gl = sum(max(-r["gross_rub"], 0) for r in rows)
    np = sum(max(r["net_rub"], 0) for r in rows)
    nl = sum(max(-r["net_rub"], 0) for r in rows)
    cum, peak, dd = 0.0, 0.0, 0.0
    for r in sorted(rows, key=lambda x: x["entry_time"]):
        cum += r["net_rub"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    wins = [n for n in nets if n > 0]
    return {
        "n": len(rows),
        "gross": round(sum(r["gross_rub"] for r in rows), 2),
        "commission": round(sum(r["commission_rub"] for r in rows), 2),
        "slippage": round(sum(r["slippage_rub"] for r in rows), 2),
        "total_costs": round(sum(r["total_cost_rub"] for r in rows), 2),
        "net": round(sum(r["net_rub"] for r in rows), 2),
        "gross_pf": round(gp / gl, 2) if gl > 0 else None,
        "net_pf": round(np / nl, 2) if nl > 0 else None,
        "win_rate": round(len(wins) / len(nets), 4) if nets else None,
        "expectancy": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "median_net": round(sorted(nets)[len(nets) // 2], 2) if nets else 0.0,
        "max_drawdown_rub": round(dd, 2),
        "avg_break_even_bps": round(sum(r["break_even_bps"] for r in rows) / len(rows), 2) if rows else None,
        "intrabar_ambiguous_count": sum(1 for r in rows if r["intrabar_ambiguous"]),
        "avg_hold_minutes": round(sum((datetime.fromisoformat(r["exit_time"].replace("Z", "+00:00"))
                                       - datetime.fromisoformat(r["entry_time"].replace("Z", "+00:00"))).total_seconds() / 60
                                      for r in rows) / len(rows), 1) if rows else None,
    }


def _flatten(per_figi: dict) -> list[dict]:
    out = []
    for figi, v in per_figi.items():
        for g in v.get("contention_groups", []):
            g = dict(g)
            g["figi"] = figi
            out.append(g)
    return out


if __name__ == "__main__":
    main()
