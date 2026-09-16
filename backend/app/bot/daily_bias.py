"""Дневной bias по MACD: направление торговли по дневным свечам.

Чистые функции (без БД/сети) — тестируются юнит-тестами.
Используется runtime'ом: входы против дневного MACD-bias блокируются (veto)
или помечаются (info).
"""
from __future__ import annotations


def ema(values: list[float], period: int) -> list[float]:
    """EMA с классическим сглаживанием k = 2/(period+1); первое значение = SMA."""
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1.0)
    out = [float(values[0])]
    for v in values[1:]:
        out.append(float(v) * k + out[-1] * (1.0 - k))
    return out


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict:
    """MACD по дневным закрытиям: линия, сигнальная, гистограмма (последние значения)."""
    n = len(closes or [])
    if n < slow + signal:
        return {"ok": False, "reason": f"мало свечей ({n} < {slow + signal})"}
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [a - b for a, b in zip(ef, es)]
    sig = ema(line, signal)
    hist = line[-1] - sig[-1]
    return {"ok": True, "macd": round(line[-1], 6), "signal": round(sig[-1], 6),
            "hist": round(hist, 6), "bars": n}


def bias_from_closes(closes: list[float], fast: int = 12, slow: int = 26,
                     signal: int = 9) -> dict:
    """Дневной bias: 'up' если MACD выше сигнальной, 'down' если ниже, иначе 'flat'."""
    m = macd(closes, fast, slow, signal)
    if not m.get("ok"):
        return {"bias": "unknown", "reason": m.get("reason")}
    hist = float(m["hist"])
    bias = "up" if hist > 0 else ("down" if hist < 0 else "flat")
    return {"bias": bias, "hist": hist, "macd": m["macd"], "signal": m["signal"],
            "bars": m["bars"]}


def bias_allows(bias: str, side: str) -> bool:
    """Разрешён ли вход этой стороны при данном bias (veto-логика)."""
    b = str(bias or "").lower()
    s = str(side or "").upper()
    if b == "up":
        return s != "SELL"
    if b == "down":
        return s != "BUY"
    return True  # flat/unknown — не мешаем
