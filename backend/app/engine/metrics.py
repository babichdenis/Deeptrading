from __future__ import annotations

from typing import Sequence

from app.engine.models import Trade


def summarize(trades: Sequence[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {
            "trades": 0,
            "positions": 0,
            "partial_closes": 0,
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
    # ENG-017 (audit 2026-09-29): частичный выход — отдельный Trade для P&L,
    # но НЕ отдельная позиция. Считаем цепочки (chain_id) отдельно от сделок.
    chains = {t.chain_id for t in trades if t.chain_id}
    return {
        "trades": n,
        "positions": len(chains) if chains else n,
        "partial_closes": sum(1 for t in trades if t.exit_reason == "partial_take"),
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


def drawdown_stats(curve_values: Sequence[float]) -> tuple[float, float]:
    """(max_dd_pct, max_dd_abs) от ПЕРВОЙ точки серии как начального пика.

    ENG-005 (audit 2026-09-29): серия обязана включать стартовый капитал,
    иначе первая убыточная сделка становится «пиком» и DD занижается.
    ENG-016: это realised-only просадка (по закрытым сделкам), без MTM
    открытых позиций.
    """
    peak: float | None = None
    max_pct = 0.0
    max_abs = 0.0
    for v in curve_values:
        if peak is None or v > peak:
            peak = v
        dd_abs = peak - v
        if dd_abs > max_abs:
            max_abs = dd_abs
        if peak > 0:
            dd = dd_abs / peak * 100
            if dd > max_pct:
                max_pct = dd
    return round(max_pct, 3), round(max_abs, 4)


def max_drawdown_pct(curve_values: Sequence[float]) -> float:
    return drawdown_stats(curve_values)[0]


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
    # ENG-005: стартовый капитал — первая точка кривой просадки, иначе
    # первая убыточная сделка не даёт просадки вовсе.
    values = [float(start_capital)] + [p["equity"] for p in curve]
    dd_pct, dd_abs = drawdown_stats(values)
    by_figi_map = by_figi(trades)
    top1 = top1_analysis(by_figi_map)
    s_base = summarize(trades)
    report = {
        "summary": s_base,
        "max_drawdown_pct": dd_pct,
        # ENG-016: явное имя realised-метрики; MTM открытых позиций нет.
        "realised_max_drawdown_pct": dd_pct,
        "max_drawdown_abs": dd_abs,
        "dd_basis": "realised_only",
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
        # Стандартный recovery factor: net / максимальная просадка в деньгах
        # (раньше делилось на проценты — смешивание единиц, ENG-016).
        report["recovery"] = round(s_base["net"] / dd_abs, 3) if dd_abs > 0 else None
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
