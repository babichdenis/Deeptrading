from __future__ import annotations

from typing import Sequence

from app.engine.models import Trade


def summarize(trades: Sequence[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy": 0.0,
            "net": 0.0,
            "gross": 0.0,
            "commission": 0.0,
            "long_count": 0,
            "short_count": 0,
            "best": 0.0,
            "worst": 0.0,
        }
    nets = [t.net_pnl for t in trades]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 2),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else 999.0,
        "expectancy": round(sum(nets) / n, 4),
        "net": round(sum(nets), 4),
        "gross": round(sum(t.gross_pnl for t in trades), 4),
        "commission": round(sum(t.commission for t in trades), 4),
        "long_count": sum(1 for t in trades if t.side == "LONG"),
        "short_count": sum(1 for t in trades if t.side == "SHORT"),
        "best": round(max(nets), 4),
        "worst": round(min(nets), 4),
    }


def equity_curve(trades: Sequence[Trade], start_capital: float = 100_000.0) -> list[dict]:
    curve: list[dict] = []
    equity = start_capital
    ordered = sorted(trades, key=lambda t: (t.exit_time, t.trade_id))
    for t in ordered:
        equity += t.net_pnl
        curve.append({"time": t.exit_time.isoformat(), "equity": round(equity, 2)})
    return curve


def max_drawdown_pct(curve_values: Sequence[float]) -> float:
    peak = float("-inf")
    max_dd = 0.0
    for v in curve_values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak * 100
            if dd > max_dd:
                max_dd = dd
    return round(max_dd, 3)


def halves(trades: Sequence[Trade]) -> dict:
    if not trades:
        return {"h1": summarize([]), "h2": summarize([])}
    ordered = sorted(trades, key=lambda t: (t.entry_time, t.trade_id))
    mid = ordered[len(ordered) // 2].entry_time
    h1 = [t for t in ordered if t.entry_time < mid]
    h2 = [t for t in ordered if t.entry_time >= mid]
    return {"h1": summarize(h1), "h2": summarize(h2)}


def by_figi(trades: Sequence[Trade]) -> dict:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(t.figi, []).append(t)
    return {figi: summarize(g) for figi, g in sorted(groups.items())}


def max_consecutive_losses_from_sorted(trades: Sequence[Trade]) -> int:
    return max_consecutive_losses(trades)


def full_report(trades: Sequence[Trade], start_capital: float = 100_000.0) -> dict:
    curve = equity_curve(trades, start_capital)
    values = [p["equity"] for p in curve] or [start_capital]
    by_figi_map = by_figi(trades)
    top1 = top1_analysis(by_figi_map)
    s_base = summarize(trades)
    report = {
        "summary": s_base,
        "max_drawdown_pct": max_drawdown_pct(values),
        "halves": halves(trades),
        "by_figi": by_figi_map,
        "by_day": per_day(trades),
        "curve": curve,
        "consecutive_losses": max_consecutive_losses(trades),
        "top1_analysis": top1,
        "final_equity": values[-1],
        "curve_points": len(curve),
    }
    if s_base["trades"] > 0 and s_base["net"] != 0:
        dd = report["max_drawdown_pct"]
        report["recovery"] = round(s_base["net"] / dd, 3) if dd > 0 else None
    else:
        report["recovery"] = None
    return _json_safe(report)


def per_day(trades: Sequence[Trade]) -> list[dict]:
    groups: dict = {}
    for t in trades:
        day = t.exit_time.date().isoformat()
        g = groups.setdefault(day, {"trades": 0, "gross": 0.0, "commission": 0.0, "net": 0.0})
        g["trades"] += 1
        g["gross"] += t.gross_pnl
        g["commission"] += t.commission
        g["net"] += t.net_pnl
    out = [
        {
            "date": day,
            "trades": g["trades"],
            "gross": round(g["gross"], 4),
            "commission": round(g["commission"], 4),
            "net": round(g["net"], 4),
        }
        for day, g in sorted(groups.items())
    ]
    return out


def max_consecutive_losses(trades: Sequence[Trade]) -> int:
    worst = streak = 0
    for t in sorted(trades, key=lambda x: x.exit_time):
        if t.net_pnl <= 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    return worst


def top1_analysis(by_figi_map: dict[str, dict]) -> dict:
    nets = {figi: d.get("net", 0.0) for figi, d in by_figi_map.items()}
    if not nets:
        return {"top1_figi": None, "top1_net": 0.0, "net_without_top1": 0.0,
                "total_net": 0.0, "concentration_pct": 0.0}
    total = sum(nets.values())
    top_figi = max(nets, key=nets.get)
    without = total - nets[top_figi]
    conc = abs(nets[top_figi]) / abs(total) * 100 if total != 0 else 0.0
    return {
        "top1_figi": top_figi,
        "top1_net": round(nets[top_figi], 4),
        "net_without_top1": round(without, 4),
        "total_net": round(total, 4),
        "concentration_pct": round(conc, 1),
    }


def _json_safe(obj):
    import math
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj)):
        return None
    return obj
