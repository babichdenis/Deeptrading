"""Portfolio risk: экспозиции, сектора, маржа, стресс-тест по бете к IMOEX.

Чистые функции (без БД/сети) — тестируются юнит-тестами.
Данные по секторам/бетам приходят из instruments (sector, imoex_beta).

Используется runtime'ом:
- лимиты перед входом (net exposure, сектор, маржа, стресс);
- сводка для API/UI и для контекста AI-гейта.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PortfolioLimits:
    max_net_exposure_pct: float = 0.5   # |net notional| <= 50% equity (0 = off)
    max_sector_pct: float = 0.35        # notional сектора <= 35% equity (0 = off)
    max_margin_use_pct: float = 0.8     # starting_margin <= 80% equity (0 = off)
    max_stress_loss_pct: float = 0.10   # убыток при ±5% IMOEX <= 10% equity (0 = off)


def meta_for(meta: dict | None, ticker: str, figi: str = "") -> dict:
    """Мета инструмента по тикеру или figi (ключи в meta могут быть обоими)."""
    m = meta or {}
    return (m.get(str(ticker or "").upper()) or m.get(str(figi or ""))
            or m.get(str(ticker or "")) or {})


def position_notional(p: dict) -> float:
    """Номинал позиции (₽): entry_price × qty (qty — в штуках)."""
    try:
        return abs(float(p.get("entry") or p.get("entry_price") or 0.0)
                   * float(p.get("qty") or 0.0))
    except Exception:
        return 0.0


def snapshot(equity: float, positions: list[dict], meta_by_ticker: dict | None = None,
             margin: dict | None = None) -> dict:
    """Сводка портфеля: экспозиции, сектора, маржа, стресс-тест.

    positions: [{ticker, side (LONG/SHORT), qty (штуки), entry, last, pnl}, ...]
    meta_by_ticker: {ticker: {"sector": str, "beta": float}}
    margin: {"liquid": float, "starting_margin": float, "minimal_margin": float}
    """
    meta_by_ticker = meta_by_ticker or {}
    equity = float(equity or 0.0)
    longs = shorts = gross = 0.0
    sectors: dict[str, float] = {}
    stress = {"imoex_-10%": 0.0, "imoex_-5%": 0.0, "imoex_+5%": 0.0, "imoex_+10%": 0.0}
    for p in positions or []:
        n = position_notional(p)
        side = str(p.get("side") or "").upper()
        is_long = side in ("LONG", "BUY")
        dir_ = 1.0 if is_long else -1.0
        if is_long:
            longs += n
        else:
            shorts += n
        gross += n
        tk = str(p.get("ticker") or "").upper()
        m = meta_for(meta_by_ticker, tk, str(p.get("figi") or ""))
        sec = str(m.get("sector") or "other")
        sectors[sec] = sectors.get(sec, 0.0) + n
        beta = float(m.get("beta") or 0.0)
        for shock in (-0.10, -0.05, 0.05, 0.10):
            stress[f"imoex_{shock:+.0%}"] += n * beta * shock * dir_
    net = longs - shorts
    margin = margin or {}
    start = float(margin.get("starting_margin") or 0.0)
    liquid = float(margin.get("liquid") or equity)
    out = {
        "equity": round(equity, 2),
        "long_notional": round(longs, 2),
        "short_notional": round(shorts, 2),
        "net_notional": round(net, 2),
        "gross_notional": round(gross, 2),
        "net_exposure_pct": round(net / equity, 4) if equity else 0.0,
        "short_exposure_pct": round(shorts / equity, 4) if equity else 0.0,
        "long_exposure_pct": round(longs / equity, 4) if equity else 0.0,
        "gross_leverage": round(gross / equity, 2) if equity else 0.0,
        "margin_used": round(start, 2),
        "margin_use_pct": round(start / equity, 4) if equity else 0.0,
        "margin_free": round(max(0.0, liquid - start), 2),
        "sector_pct": {k: round(v / equity, 4) if equity else 0.0 for k, v in sectors.items()},
        "stress_pct": {k: round(v / equity, 4) if equity else 0.0 for k, v in stress.items()},
    }
    out["stress_worst"] = min(out["stress_pct"].items(), key=lambda x: x[1]) if out["stress_pct"] else ("—", 0.0)
    return out


def check_order(snap: dict, side: str, notional: float, ticker: str,
                meta_by_ticker: dict | None = None,
                limits: PortfolioLimits | None = None) -> tuple[bool, str]:
    """Проверка заявки по лимитам портфеля. Возвращает (ok, reason)."""
    lim = limits or PortfolioLimits()
    eq = float(snap.get("equity") or 0.0)
    if eq <= 0 or notional <= 0:
        return True, "no_equity_check"
    is_long = str(side).upper() in ("BUY", "LONG")
    dir_ = 1.0 if is_long else -1.0
    # 1) чистая экспозиция
    if lim.max_net_exposure_pct > 0:
        new_net = float(snap.get("net_notional") or 0.0) + dir_ * notional
        if abs(new_net) > eq * lim.max_net_exposure_pct:
            return False, (f"чистая экспозиция {abs(new_net)/eq:.0%} > "
                           f"{lim.max_net_exposure_pct:.0%} (equity)")
    # 2) сектор
    if lim.max_sector_pct > 0:
        m = meta_for(meta_by_ticker, ticker)
        sec = str(m.get("sector") or "other")
        sec_now = float((snap.get("sector_pct") or {}).get(sec) or 0.0) * eq
        if (sec_now + notional) > eq * lim.max_sector_pct:
            return False, (f"сектор {sec}: {(sec_now+notional)/eq:.0%} > "
                           f"{lim.max_sector_pct:.0%} (equity)")
    # 3) маржа (буфер)
    if lim.max_margin_use_pct > 0:
        used = float(snap.get("margin_used") or 0.0)
        # оценка требуемой маржи новой позиции: номинал / gross_leverage портфеля
        lev = max(1.0, float(snap.get("gross_leverage") or 1.0))
        new_margin = notional / lev
        if (used + new_margin) > eq * lim.max_margin_use_pct:
            return False, (f"маржа {(used+new_margin)/eq:.0%} > "
                           f"{lim.max_margin_use_pct:.0%} (equity)")
    # 4) стресс при ±5% IMOEX
    if lim.max_stress_loss_pct > 0:
        m = meta_for(meta_by_ticker, ticker)
        beta = float(m.get("beta") or 0.0)
        # худший сценарий ±5% с учётом новой позиции
        add_up = notional * beta * 0.05 * dir_          # шок +5%
        add_dn = notional * beta * -0.05 * dir_         # шок -5%
        base_up = float((snap.get("stress_pct") or {}).get("imoex_+5%") or 0.0) * eq
        base_dn = float((snap.get("stress_pct") or {}).get("imoex_-5%") or 0.0) * eq
        worst = min(base_up + add_up, base_dn + add_dn)
        if worst < -eq * lim.max_stress_loss_pct:
            return False, (f"стресс ±5% IMOEX: убыток {worst/eq:.0%} > "
                           f"{lim.max_stress_loss_pct:.0%} (equity)")
    return True, "ok"
