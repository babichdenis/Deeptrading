"""Единая валидация свечей для бэктеста и live-стрима (AUDIT P2-13).

Раньше логика жила в двух местах: app/services/ensemble.py:_validate_candles
(пакетная, с отбрасыванием битых дней) и app/bot/runtime.py:_candle_ok
(инкрементальная, со статистикой мерцания). Здесь — общие проверки,
обе реализации вызывают их.
"""
from __future__ import annotations

from typing import Iterable


def bar_ok(open_, high, low, close, volume=None) -> bool:
    """Базовые проверки OHLC/volume: положительность, high>=low, containment."""
    try:
        o = float(open_)
        h = float(high)
        l = float(low)
        c = float(close)
    except (TypeError, ValueError):
        return False
    if o <= 0 or c <= 0 or h <= 0 or l <= 0:
        return False
    if h < l or h < o or h < c or l > o or l > c:
        return False
    if volume is not None:
        try:
            if float(volume) < 0:
                return False
        except (TypeError, ValueError):
            pass
    return True


def jump_ratio(prev_close, close) -> float | None:
    """Относительный прыжок цены между двумя барами (0..1+), None если нет данных."""
    try:
        p = float(prev_close)
        c = float(close)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return abs(c - p) / p


def bad_days_flicker(candles: Iterable, msk_zone, flicker_jumps: int = 3,
                     flicker_thr: float = 0.5) -> set:
    """Дни (МСК), где цена 'мерцает': >= flicker_jumps прыжков > flicker_thr."""
    day_jumps: dict[str, int] = {}
    day_prev: dict[str, float] = {}
    for c in candles:
        d = c.ts.astimezone(msk_zone).date().isoformat()
        pv = day_prev.get(d)
        if pv is not None and pv > 0:
            j = jump_ratio(pv, c.close)
            if j is not None and j > flicker_thr:
                day_jumps[d] = day_jumps.get(d, 0) + 1
        day_prev[d] = c.close
    return {d for d, n in day_jumps.items() if n >= flicker_jumps}


def validate_candles(candles: list, msk_zone, flicker_jumps: int = 3,
                     flicker_thr: float = 0.5, jump_thr: float = 0.4) -> tuple:
    """Пакетная проверка списка свечей (используется в бэктесте).

    Returns:
        (valid_candles, skipped_count)
    """
    bad = bad_days_flicker(candles, msk_zone, flicker_jumps, flicker_thr)
    valid: list = []
    skipped = 0
    prev_close: float | None = None
    for c in candles:
        if c.ts.astimezone(msk_zone).date().isoformat() in bad:
            skipped += 1
            continue
        if not bar_ok(c.open, c.high, c.low, c.close, c.volume):
            skipped += 1
            continue
        j = jump_ratio(prev_close, c.close)
        if prev_close is not None and j is not None and j > jump_thr:
            skipped += 1
            continue
        prev_close = float(c.close)
        valid.append(c)
    return valid, skipped