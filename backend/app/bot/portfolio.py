"""Portfolio risk: экспозиции, сектора, маржа, стресс-тест по бете к IMOEX.

Чистые функции (без БД/сети) — тестируются юнит-тестами.
Данные по секторам/бетам приходят из instruments (sector, imoex_beta).

Используется runtime'ом:
- лимиты перед входом (net exposure, сектор, маржа, стресс);
- сводка для API/UI и для контекста AI-гейта.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace


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


def regime_limits(base: PortfolioLimits, regime: dict | None = None) -> PortfolioLimits:
    """Адаптивные лимиты по режиму рынка (bear/bull/reversal/neutral).

    bear/bull — направленный тренд: разрешаем net до 100% equity (концентрация
    в сильнейшем), сектор до 40%, но запас маржи и стресс-лимит жёстче.
    reversal — разворот против книги: всё зажимаем.
    """
    r = str((regime or {}).get("state") or "neutral").lower()
    lim = replace(base)
    if r in ("bear", "bull"):
        lim.max_net_exposure_pct = max(lim.max_net_exposure_pct, 1.0)
        lim.max_sector_pct = max(lim.max_sector_pct, 0.40)
        lim.max_margin_use_pct = min(lim.max_margin_use_pct or 0.8, 0.75)
        lim.max_stress_loss_pct = min(lim.max_stress_loss_pct or 0.10, 0.08)
    elif r == "reversal":
        lim.max_net_exposure_pct = min(lim.max_net_exposure_pct or 0.5, 0.35)
        lim.max_margin_use_pct = min(lim.max_margin_use_pct or 0.8, 0.60)
        lim.max_stress_loss_pct = min(lim.max_stress_loss_pct or 0.10, 0.06)
    return lim


def strength_score(*, ret_ticker: float, ret_index: float, beta: float = 1.0,
                   vol_ratio: float = 1.0, breadth_up_pct: float = 50.0,
                   side: str = "SELL") -> dict:
    """Оценка силы кандидата (0–100) в направлении сделки.

    rs = ret_ticker − beta × ret_index (относительная сила к рынку).
    Для SELL сильнее тот, у кого rs ниже (слабее рынка), для BUY — выше.
    Бонусы: объём (vol_ratio) и breadth (совпадение с направлением рынка).
    """
    is_long = str(side).upper() in ("BUY", "LONG")
    rs = float(ret_ticker or 0.0) - float(beta or 0.0) * float(ret_index or 0.0)
    base = rs if is_long else -rs
    rs_pts = base * 300.0   # 1% относительной силы = 3 балла (доминирует)
    vol_bonus = min(max((float(vol_ratio or 1.0) - 1.0) * 6.0, -6.0), 6.0)
    br = float(breadth_up_pct if breadth_up_pct is not None else 50.0)
    br_bonus = min(max(((br - 50.0) if is_long else (50.0 - br)) * 0.1, -5.0), 5.0)
    score = 50.0 + rs_pts + vol_bonus + br_bonus
    return {"score": round(max(0.0, min(100.0, score)), 1), "rs": round(rs, 4),
            "vol_bonus": round(vol_bonus, 1), "breadth_bonus": round(br_bonus, 1)}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def candidate_score(*, ret_ticker: float, ret_index: float, beta: float = 1.0,
                    side: str = "SELL", hist: dict | None = None, turnover: float = 0.0,
                    votes: int = 0, total_members: int = 0, vol_ratio: float = 1.0,
                    regime: str = "neutral", fit: float = 1.0) -> dict:
    """Комплексная оценка кандидата (0–100) с расшифровкой.

    Веса: относительная сила к IMOEX 30%, история сделок по тикеру 25%,
    уверенность сигнала (кворум + объём) 20%, ликвидность 15%, вписываемость в лимиты 10%.
    Вето: неликвид (<1 млн ₽/день) и стабильно убыточная история (n≥10, net<0, WR<35%).
    """
    is_long = str(side).upper() in ("BUY", "LONG")
    rs = float(ret_ticker or 0.0) - float(beta or 0.0) * float(ret_index or 0.0)
    base = rs if is_long else -rs
    f_rs = _clamp01(0.5 + base * 15.0)
    h = hist or {}
    n = int(h.get("n") or 0)
    if n <= 0:
        f_hist = 0.5
    else:
        wr = float(h.get("wr") or 0.0)
        wr5 = float(h.get("wr5") or wr)
        net = float(h.get("net") or 0.0)
        f_hist = _clamp01(0.5 * _clamp01((wr - 0.30) / 0.40)
                          + 0.3 * _clamp01((wr5 - 0.30) / 0.40)
                          + 0.2 * (1.0 if net > 0 else 0.0))
    f_q = (_clamp01(float(votes or 0) / max(1.0, float(total_members) * 0.6))
           if total_members else 0.5)
    f_vol = _clamp01(float(vol_ratio or 1.0) / 2.0)
    f_conf = 0.6 * f_q + 0.4 * f_vol
    t = float(turnover or 0.0)
    f_liq = (1.0 if t > 50e6 else 0.75 if t > 10e6 else 0.5 if t > 1e6
             else 0.2 if t > 0 else 0.5)
    f_fit = _clamp01(fit if fit is not None else 1.0)
    reg = str(regime or "neutral").lower()
    adj = 0.0
    if reg in ("bear", "bull"):
        aligned = (reg == "bear" and not is_long) or (reg == "bull" and is_long)
        adj = 0.05 if aligned else -0.05
    elif reg == "reversal":
        adj = -0.15
    total = (0.30 * f_rs + 0.25 * f_hist + 0.20 * f_conf
             + 0.15 * f_liq + 0.10 * f_fit + adj)
    veto: list[str] = []
    if 0 < t < 1e6:
        veto.append("illiquid")
    if n >= 10 and float(h.get("net") or 0.0) < 0 and float(h.get("wr") or 0.0) < 0.35:
        veto.append("bad_history")
    return {
        "score": round(_clamp01(total) * 100.0, 1),
        "rs": round(rs, 4),
        "factors": {"rs": round(f_rs, 3), "hist": round(f_hist, 3),
                    "conf": round(f_conf, 3), "liq": round(f_liq, 3), "fit": round(f_fit, 3)},
        "regime_adj": round(adj, 3),
        "veto": veto,
        "hist": ({"n": n, "wr": round(float(h.get("wr") or 0.0), 3),
                  "wr5": round(float(h.get("wr5") or 0.0), 3),
                  "net": round(float(h.get("net") or 0.0), 2)} if n else None),
    }


def drawdown_action(equity: float, peak: float, reduce1: float = 0.05,
                    reduce2: float = 0.10, pct1: float = 0.5, pct2: float = 0.8) -> dict:
    """Трейлинг-стоп портфеля: какую долю позиций закрыть по просадке от пика equity."""
    if peak <= 0 or equity <= 0:
        return {"dd": 0.0, "close_pct": 0.0, "level": 0}
    dd = max(0.0, (float(peak) - float(equity)) / float(peak))
    if dd >= reduce2:
        return {"dd": round(dd, 4), "close_pct": pct2, "level": 2}
    if dd >= reduce1:
        return {"dd": round(dd, 4), "close_pct": pct1, "level": 1}
    return {"dd": round(dd, 4), "close_pct": 0.0, "level": 0}
