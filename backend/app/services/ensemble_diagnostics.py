from __future__ import annotations

from datetime import datetime

from app.engine.exits import ExitPolicy, intrabar_exit
from app.engine.models import Candle as EngineCandle, PositionState, Side
from app.services.ceiling import zigzag_swings


def _oracle_fixed_qty(candles: list[EngineCandle], threshold_pct: float, fee_rate: float,
                      qty_shares: float) -> dict:
    cdicts = [c.__dict__ for c in candles]
    swings = zigzag_swings(cdicts, threshold_pct / 100.0)
    trades: list[dict] = []
    for k in range(len(swings) - 1):
        sw, nxt = swings[k], swings[k + 1]
        # low->high = лонг (купил в минимуме, продал в максимуме)
        # high->low = шорт (продал в максимуме, откупил в минимуме)
        if sw["kind"] == "low":
            side = "LONG"
            entry_px, exit_px = sw["price"], nxt["price"]
        else:
            side = "SHORT"
            entry_px, exit_px = sw["price"], nxt["price"]
        if exit_px == entry_px:
            continue
        gross = (exit_px - entry_px) * qty_shares if side == "LONG" else (entry_px - exit_px) * qty_shares
        commission = (entry_px + exit_px) * qty_shares * fee_rate
        trades.append({
            "side": side,
            "entry_ts": cdicts[sw["idx"]]["ts"].isoformat(),
            "exit_ts": cdicts[nxt["idx"]]["ts"].isoformat(),
            "entry_px": round(entry_px, 6), "exit_px": round(exit_px, 6),
            "gross": round(gross, 2), "commission": round(commission, 2),
            "net": round(gross - commission, 2),
        })
    longs = [t for t in trades if t["side"] == "LONG"]
    shorts = [t for t in trades if t["side"] == "SHORT"]
    return {"trades": trades, "gross": round(sum(t["gross"] for t in trades), 2),
            "net": round(sum(t["net"] for t in trades), 2),
            "longs": len(longs), "shorts": len(shorts),
            "long_gross": round(sum(t["gross"] for t in longs), 2),
            "short_gross": round(sum(t["gross"] for t in shorts), 2)}


def _mfe_mae(candles: list[EngineCandle], entry_idx: int, exit_idx: int,
             entry_px: float, side: str, r: float) -> dict:
    window = candles[entry_idx : exit_idx + 1]
    if not window:
        return {"mfe_r": 0.0, "mae_r": 0.0}
    if side == "LONG":
        mfe = max(b.high for b in window) - entry_px
        mae = entry_px - min(b.low for b in window)
    else:
        mfe = entry_px - min(b.low for b in window)
        mae = max(b.high for b in window) - entry_px
    r = r or entry_px * 0.01
    return {"mfe_r": round(mfe / r, 2), "mae_r": round(mae / r, 2)}


def _counterfactual_reentries(candles: list[EngineCandle], rejected: list[dict],
                              exit_obj: ExitPolicy, qty_shares: float,
                              fee_rate: float = 0.0005, slip_bps: float = 0.0002) -> dict:
    """Counterfactual отклонённых same-side re-entry: вход на следующем баре,
    выход той же exit-политикой. Показывает, отбрасывал ли cooldown плохие входы."""
    idx_by_ts = {c.ts.isoformat(): i for i, c in enumerate(candles)}
    results: list[dict] = []
    for r in rejected:
        i = idx_by_ts.get(r["signal_ts"])
        if i is None or i + 1 >= len(candles):
            continue
        side = Side.BUY if r["side"] == "BUY" else Side.SELL
        entry_bar = candles[i + 1]
        fill = entry_bar.open
        entry_notional = fill * qty_shares
        ec, es = entry_notional * fee_rate, entry_notional * slip_bps
        plan = exit_obj.plan_entry(side, fill, candles[: i + 2])
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        exit_price, exit_reason, exit_idx = None, None, None
        for j in range(i + 2, len(candles)):
            px, reason = intrabar_exit(candles[j], state, plan.stop_loss, plan.take_profit)
            if px is not None:
                exit_price, exit_reason, exit_idx = px, reason, j
                break
        if exit_price is None:
            exit_price, exit_reason, exit_idx = candles[-1].close, "end_of_data", len(candles) - 1
        exit_notional = exit_price * qty_shares
        xc, xs = exit_notional * fee_rate, exit_notional * slip_bps
        gross = (exit_price - fill) * qty_shares if side is Side.BUY else (fill - exit_price) * qty_shares
        results.append({
            "side": r["side"], "signal_ts": r["signal_ts"],
            "bars_since_exit": r.get("bars_since_exit"),
            "entry_ts": candles[i + 1].ts.isoformat(),
            "exit_ts": candles[exit_idx].ts.isoformat(),
            "entry_px": round(fill, 6), "exit_px": round(exit_price, 6),
            "exit_reason": exit_reason,
            "gross": round(gross, 2), "costs": round(ec + es + xc + xs, 2),
            "net": round(gross - ec - xc, 2),
        })
    if not results:
        return {"n": 0, "mean_net": None, "median_net": None, "wins_pct": None, "list": []}
    nets = sorted(r["net"] for r in results)
    return {
        "n": len(results),
        "mean_net": round(sum(nets) / len(nets), 2),
        "median_net": nets[len(nets) // 2],
        "wins_pct": round(sum(1 for r in results if r["net"] > 0) / len(results) * 100, 1),
        "list": results,
    }


def _counterfactual_hold(candles: list[EngineCandle], trades: list,
                         exit_obj: ExitPolicy, qty_shares: float,
                         fee_rate: float = 0.0005, slip_bps: float = 0.0002) -> dict:
    """Что было бы, если не выходить по противоположному сигналу, а держать
    позицию до срабатывания stop/target/trailing/session/конца данных."""
    rows: list[dict] = []
    for t in trades:
        if t["exit_reason"] != "signal_exit":
            continue
        side = Side.BUY if t["side"] == "LONG" else Side.SELL
        entry_i = t["entry_index"]
        fill = t["entry_px"]
        stop = t.get("initial_stop") or t.get("stop")
        target = t.get("target") or t.get("take_profit")
        update_stop = getattr(exit_obj, "update_stop", None)
        state = PositionState.LONG if side is Side.BUY else PositionState.SHORT
        exit_px, exit_reason, exit_i = None, None, None
        for j in range(min(entry_i + 1, len(candles)), len(candles)):
            if update_stop is not None:
                stop = update_stop(state, fill, stop, candles[entry_i : j + 1])
            px, reason = intrabar_exit(candles[j], state, stop, target)
            if px is not None:
                exit_px, exit_reason, exit_i = px, reason, j
                break
        if exit_px is None:
            exit_px, exit_reason, exit_i = candles[-1].close, "end_of_data", len(candles) - 1
        hold_net = (exit_px - fill) * qty_shares if side is Side.BUY else (fill - exit_px) * qty_shares
        entry_notional = fill * qty_shares
        exit_notional = exit_px * qty_shares
        hold_net -= entry_notional * fee_rate + exit_notional * fee_rate
        actual_net = t["net"]
        rows.append({
            "entry_ts": candles[entry_i].ts.isoformat(),
            "side": t["side"],
            "actual_exit_ts": t["exit_ts"],
            "actual_net": actual_net,
            "hold_exit_ts": candles[exit_i].ts.isoformat(),
            "hold_exit_reason": exit_reason,
            "hold_net": round(hold_net, 2),
            "delta": round(hold_net - actual_net, 2),
        })
    if not rows:
        return {"n": 0, "mean_delta": None, "actual_mean": None, "hold_mean": None,
                "hold_better": None, "list": []}
    deltas = sorted(r["delta"] for r in rows)
    return {
        "n": len(rows),
        "mean_delta": round(sum(r["delta"] for r in rows) / len(rows), 2),
        "median_delta": deltas[len(deltas) // 2],
        "actual_mean": round(sum(r["actual_net"] for r in rows) / len(rows), 2),
        "hold_mean": round(sum(r["hold_net"] for r in rows) / len(rows), 2),
        "hold_better": sum(1 for r in rows if r["delta"] > 0),
        "list": rows,
    }


def _oracle_coverage(oracle_swings_detail: list[dict], entries_raw: list[dict],
                     accepted: list[dict], rejected: list[dict],
                     window_min: int = 10,
                     executed_signals: set[tuple[str, str]] | None = None) -> dict:
    """Почему не заходим в сделки оракула, без look-ahead.

    oracle_swings_detail — [{side, point_ts, confirmation_ts, point_idx, conf_idx}].
    Две воронки:
      geometric — сигнал того же направления в окне ДО фактической точки экстремума;
      causal    — сигнал доступен к моменту ПОДТВЕРЖДЕНИЯ экстремума (confirmation_ts).
    Причины отсева считаются по каскаду гейтов (сигнал мог не пройти несколько),
    поэтому для каждого входа храним failed_gates + primary_reason.
    executed_signals — пары (signal_ts_iso, side), реально дошедшие до сделки движка.
    """
    exec_set = executed_signals or set()
    raw_acc = {(a["ts"], a["side"]): a for a in accepted}
    raw_rej = {(r["ts"], r["side"]): r["reason"] for r in rejected}
    window_sec = window_min * 60
    _key = lambda s: (s["ts"].isoformat(), s["side"]) if hasattr(s["ts"], "isoformat") else (s["ts"], s["side"])

    ge = {"tot": 0, "raw_before_point": 0, "accepted": 0, "executed": 0}
    ca = {"tot": 0, "raw_before_conf": 0, "accepted": 0, "executed": 0}
    rejected_by_gate = {"not_seen": 0, "quorum": 0, "bias": 0, "regime": 0,
                        "cooldown": 0, "price": 0, "ml": 0, "other": 0}
    points_detail: list[dict] = []

    def _gate_failure(reason: str) -> str:
        if reason.startswith("BIAS"):
            return "bias"
        if reason.startswith("QUORUM"):
            return "quorum"
        if reason.startswith("REGIME"):
            return "regime"
        if reason.startswith("ML_"):
            return "ml"
        if "REENTRY" in reason:
            return "cooldown"
        if reason == "REJECT_SHORT" or reason.startswith("SKIP_ENTRY") or reason.startswith("REJECT_SESSION"):
            return "price"
        return "other"

    # Предгруппировка entries_raw по направлению (O(log n) поиск окна вместо O(n) на swing).
    import bisect as _bisect
    from datetime import timedelta as _td
    _entries_by_side: dict[str, list[dict]] = {"BUY": [], "SELL": []}
    for _s in entries_raw:
        _entries_by_side.setdefault(_s["side"], []).append(_s)
    _side_ts: dict[str, list] = {}
    for _k, _v in _entries_by_side.items():
        _v.sort(key=lambda s: s["ts"])
        _side_ts[_k] = [s["ts"] for s in _v]

    def _window(side: str, t: datetime) -> list[dict]:
        lst = _entries_by_side.get(side) or []
        ts_list = _side_ts.get(side) or []
        if not lst:
            return []
        lo = _bisect.bisect_left(ts_list, t - _td(seconds=window_sec))
        hi = _bisect.bisect_right(ts_list, t)
        return lst[lo:hi]

    for sw in oracle_swings_detail:
        side = sw["side"]
        pt = datetime.fromisoformat(sw["point_ts"])
        conf_ts = datetime.fromisoformat(sw["confirmation_ts"])

        # все raw того же направления в окне вокруг точки (кроме сигналов позже окна)
        raw_window = _window(side, pt)
        raw_causal = _window(side, conf_ts)

        ge["tot"] += 1
        ca["tot"] += 1
        best_geo = max(raw_window, key=lambda s: s["ts"]) if raw_window else None
        best_cau = max(raw_causal, key=lambda s: s["ts"]) if raw_causal else None

        def _fate(best: dict | None, totals: dict, label: str, count_reject: bool):
            if best is None:
                if count_reject:
                    rejected_by_gate["not_seen"] += 1
                return "not_seen", []
            totals["raw_before_point" if label == "geo" else "raw_before_conf"] += 1
            key = _key(best)
            if key in raw_acc:
                totals["accepted"] += 1
                if key in exec_set:
                    totals["executed"] += 1
                return "accepted", []
            why = raw_rej.get(key, "unknown")
            gates = [_gate_failure(why)] if why != "unknown" else ["other"]
            if count_reject:
                rejected_by_gate[gates[0] if gates else "other"] += 1
            return f"REJECTED:{why}", gates

        geo_fate, geo_gates = _fate(best_geo, ge, "geo", count_reject=False)
        cau_fate, cau_gates = _fate(best_cau, ca, "cau", count_reject=False)

        # rejected_by_gate — только по каузальной ветке, со всеми причинами, не одна
        if best_cau is None:
            rejected_by_gate["not_seen"] += 1
        else:
            key = _key(best_cau)
            if key not in raw_acc:
                why = raw_rej.get(key, "unknown")
                gate = _gate_failure(why) if why != "unknown" else "other"
                rejected_by_gate[gate] += 1

        points_detail.append({
            "side": side,
            "oracle_point_ts": sw["point_ts"],
            "oracle_confirmation_ts": sw["confirmation_ts"],
            "raw_same_side_before_point": best_geo is not None,
            "raw_same_side_before_confirmation": best_cau is not None,
            "nearest_before_point_ts": best_geo["ts"].isoformat() if best_geo else None,
            "nearest_before_conf_ts": best_cau["ts"].isoformat() if best_cau else None,
            "causal": cau_fate == "accepted",
            "failed_gates": sorted(set(cau_gates)),
            "primary_reason": cau_fate.split(":")[-1] if cau_fate.startswith("REJECTED") else cau_fate,
        })

    n_ge = max(ge["tot"], 1)
    n_ca = max(ca["tot"], 1)
    lags = sorted(sw["conf_idx"] - sw["point_idx"] for sw in oracle_swings_detail)
    lag_stats = {"mean": round(sum(lags) / len(lags), 1), "median": lags[len(lags) // 2],
                 "max": lags[-1]} if lags else {"mean": None, "median": None, "max": None}
    return {
        "window_minutes": window_min,
        "point_total": ge["tot"],
        "confirmation_lag_bars": lag_stats,
        "geometric": {
            "raw_seen_before_point": ge["raw_before_point"],
            "accepted": ge["accepted"],
            "executed": ge["executed"],
            "coverage_accepted_pct": round(ge["accepted"] / n_ge * 100, 1),
        },
        "causal": {
            "raw_seen_before_confirmation": ca["raw_before_conf"],
            "accepted": ca["accepted"],
            "executed": ca["executed"],
            "coverage_accepted_pct": round(ca["accepted"] / n_ca * 100, 1),
        },
        "rejected_by_gate": rejected_by_gate,
        "points": points_detail,
    }
