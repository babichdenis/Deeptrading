"""QuantStats-адаптер: аналитика кривой капитала для прогонов Lab/ML.

Вход: список сделок (ts, net, figi) + параметры капитала.
Выход:
  - daily_returns / equity_curve (pd.Series)
  - metrics.json (sharpe, sortino, calmar, max_dd, exposure, PF, ...)
  - report.html (QuantStats tearsheet)

База капитала задаётся явно:
  portfolio_capital     — капитал портфеля (например 500_000 = 5 позиций x 100k)
  max_concurrent_pos    — максимум одновременных позиций
Доходность считается от portfolio_capital (mark-to-market).

ВАЖНО: QuantStats — reporting adapter ПОСЛЕ EngineRunner/ML, он не исправляет
look-ahead, fill-модель или costs. Метрики считаются по переданным результатам.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import pandas as pd


def build_equity_curves(
    trades: list[dict],
    portfolio_capital: float = 500_000.0,
    timezone_name: str = "Europe/Moscow",
    intraday_bars: list[dict] | None = None,
) -> dict[str, pd.Series]:
    """Строит daily и intraday кривые из списка закрытых сделок.

    trades: [{ts: datetime|str, net: float, entry_ts?, exit_ts?, ...}]
    intraday_bars (опционально): [{ts, equity}] — mark-to-market equity
    с учётом открытых позиций (unrealized P&L по close каждого бара).
    Если передан, используется как источник intraday equity вместо
    простой кумулятивной суммы закрытых P&L.

    Возвращает {"daily_returns": Series, "equity_curve": Series, "daily_equity": Series}.
    """
    if not trades:
        return {}

    def to_dt(x: Any) -> datetime:
        if isinstance(x, str):
            return datetime.fromisoformat(x.replace("Z", "+00:00"))
        if x.tzinfo is None:
            return x.replace(tzinfo=timezone.utc)
        return x

    if intraday_bars:
        # mark-to-market: точки NAV по каждому бару
        bars = sorted(intraday_bars, key=lambda b: to_dt(b["ts"]))
        eq = pd.Series(
            [float(b["equity"]) for b in bars],
            index=pd.DatetimeIndex([to_dt(b["ts"]) for b in bars]),
        )
    else:
        # fallback: кумулятивная сумма закрытых P&L (без открытых позиций)
        rows = sorted(trades, key=lambda t: to_dt(t["ts"]))
        df = pd.DataFrame(
            [{"ts": to_dt(t["ts"]), "net": float(t.get("net") or 0)} for t in rows]
        )
        if df.empty:
            return {}
        df = df.set_index("ts").sort_index()
        eq = portfolio_capital + df["net"].cumsum()

    daily_equity = eq.resample("1D").last().ffill()
    daily_equity = daily_equity.reindex(
        pd.date_range(daily_equity.index.min(), daily_equity.index.max(), freq="D")
    ).ffill()
    daily_returns = daily_equity.pct_change().dropna()
    if daily_returns.empty:
        daily_returns = pd.Series(dtype=float)

    return {
        "daily_returns": daily_returns,
        "equity_curve": eq,
        "daily_equity": daily_equity,
    }


def compute_metrics(
    trades: list[dict],
    portfolio_capital: float = 500_000.0,
    timezone_name: str = "Europe/Moscow",
) -> dict[str, Any]:
    """Полный набор метрик (QuantStats + собственные торговые)."""
    curves = build_equity_curves(trades, portfolio_capital, timezone_name)
    out: dict[str, Any] = {"n_trades": len(trades)}
    if not curves:
        return out

    import quantstats as qs

    r = curves["daily_returns"]
    gross = sum(float(t.get("gross") or 0) for t in trades)
    commission = sum(float(t.get("commission") or 0) for t in trades)
    slippage = sum(float(t.get("slippage") or 0) for t in trades)
    total_costs = sum(float(t.get("total_costs") or 0) for t in trades)
    net = sum(float(t.get("net") or 0) for t in trades)
    gross_pos = sum(max(float(t.get("gross") or 0), 0) for t in trades)
    gross_neg = sum(max(-(float(t.get("gross") or 0)), 0) for t in trades)
    net_pos = sum(max(float(t.get("net") or 0), 0) for t in trades)
    net_neg = sum(max(-(float(t.get("net") or 0)), 0) for t in trades)
    nets = [float(t.get("net") or 0) for t in trades]
    sorted_nets = sorted(nets)
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]

    try:
        sharpe = float(qs.stats.sharpe(r))
        sortino = float(qs.stats.sortino(r))
        calmar = float(qs.stats.calmar(r))
        max_dd = float(qs.stats.max_drawdown(r))
        cvar = float(qs.stats.conditional_value_at_risk(r))
        volatility = float(qs.stats.volatility(r))
    except Exception:
        sharpe = sortino = calmar = max_dd = cvar = volatility = None

    # max drawdown в рублях по intraday equity
    eq = curves["equity_curve"]
    running_max = eq.cummax()
    dd_rub = (running_max - eq).max() if len(eq) else 0.0

    out.update({
        "portfolio_capital": portfolio_capital,
        "gross": round(gross, 2),
        "commission": round(commission, 2),
        "slippage": round(slippage, 2),
        "total_costs": round(total_costs, 2),
        "net": round(net, 2),
        "net_pct_of_capital": round(net / portfolio_capital * 100, 4) if portfolio_capital else 0.0,
        "gross_pf": round(gross_pos / gross_neg, 2) if gross_neg > 0 else None,
        "net_pf": round(net_pos / net_neg, 2) if net_neg > 0 else None,
        "win_rate": round(len(wins) / len(nets), 4) if nets else None,
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "expectancy": round(sum(nets) / len(nets), 2) if nets else 0.0,
        "median_net_trade": round(sorted_nets[len(sorted_nets) // 2], 2) if sorted_nets else 0.0,
        "max_drawdown_rub": round(float(dd_rub), 2),
        "max_drawdown_pct": round(max_dd * 100, 3) if max_dd is not None else None,
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "sortino": round(sortino, 3) if sortino is not None else None,
        "calmar": round(calmar, 3) if calmar is not None else None,
        "cvar_pct": round(cvar * 100, 3) if cvar is not None else None,
        "volatility_pct": round(volatility * 100, 3) if volatility is not None else None,
        "best_day": round(float(r.max()) * 100, 3) if len(r) else None,
        "worst_day": round(float(r.min()) * 100, 3) if len(r) else None,
        "consecutive_wins": _max_streak(nets, positive=True),
        "consecutive_losses": _max_streak(nets, positive=False),
    })
    return out


def _max_streak(nets: list[float], positive: bool) -> int:
    best = cur = 0
    for x in nets:
        if (x > 0) == positive:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def build_html_report(
    trades: list[dict],
    portfolio_capital: float = 500_000.0,
    benchmark: str = "SPY",
    title: str = "Lab run",
    out_path: str | None = None,
    metadata: dict[str, Any] | None = None,
    intraday_bars: list[dict] | None = None,
) -> str:
    """QuantStats HTML tearsheet. Возвращает путь к файлу.

    metadata: {run_id, config_hash, dataset_version, cost_model, timezone,
               time_range, trade_source, generated_at} — встраивается в HTML.
    out_path: если None — reports/{run_id}/quantstats.html (или /tmp/...).
    """
    curves = build_equity_curves(trades, portfolio_capital, intraday_bars=intraday_bars)
    if not curves:
        raise ValueError("нет сделок для отчёта")

    import quantstats as qs

    r = curves["daily_returns"]
    run_id = (metadata or {}).get("run_id") or datetime.now().strftime("%Y%m%d_%H%M%S")
    if out_path is None:
        reports_dir = os.path.join(os.path.dirname(__file__), "..", "..", "reports", str(run_id))
        os.makedirs(reports_dir, exist_ok=True)
        out_path = os.path.join(reports_dir, "quantstats.html")
    meta_html = "<pre>" + json.dumps(metadata or {}, ensure_ascii=False, indent=2, default=str) + "</pre>"
    title_full = f"{title} (run {run_id})"
    if r.empty:
        # мало сделок для дневной агрегации — HTML с метриками вручную
        m = compute_metrics(trades, portfolio_capital)
        rows = "".join(
            f"<tr><td>{k}</td><td>{json.dumps(v, default=str)}</td></tr>"
            for k, v in m.items()
        )
        html_body = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>{title_full}</title></head>
<body>{meta_html}<h1>{title_full}</h1>
<p>Недостаточно сделок для дневной агрегации QuantStats (n={len(trades)}).</p>
<table border="1" cellpadding="6"><tr><th>Метрика</th><th>Значение</th></tr>{rows}</table>
</body></html>"""
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html_body)
        return out_path
    benchmark_arg = None if benchmark in ("", "SPY") else benchmark
    qs.reports.html(r, benchmark=benchmark_arg, title=title_full, output=out_path,
                    download_filename=out_path)
    # встраиваем metadata в начало HTML
    try:
        with open(out_path, encoding="utf-8") as f:
            html = f.read()
        html = html.replace("<body>", f"<body>{meta_html}", 1)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
    except Exception:
        pass
    return out_path


def trades_to_rows(trades: list[dict]) -> list[dict]:
    """Нормализация сделок из Lab/ML отчёта в строки для адаптера."""
    out = []
    for t in trades:
        out.append({
            "ts": t.get("entry_time") or t.get("ts"),
            "net": t.get("net") or t.get("label_net") or 0,
            "gross": t.get("gross") or t.get("label_gross") or 0,
            "commission": t.get("commission") or t.get("label_commission") or 0,
            "slippage": t.get("slippage") or t.get("label_slippage") or 0,
            "total_costs": t.get("total_costs") or t.get("label_total_costs") or 0,
            "figi": t.get("figi") or t.get("ticker") or "",
        })
    return out
