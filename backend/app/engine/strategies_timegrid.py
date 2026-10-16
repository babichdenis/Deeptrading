"""Робот с сигналами по времени: график входов/переворотов задан расписанием, а не функцией.

Отличие от обычных стратегий: решение не выводится из индикаторов. Робот смотрит только
на время закрытого бара и отдаёт сигнал, если его метка попала в расписание.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Sequence
from zoneinfo import ZoneInfo

from app.engine.models import Candle, Signal, Side

_MSK = ZoneInfo("Europe/Moscow")

SIDE_ALIASES = {
    "long": Side.BUY,
    "buy": Side.BUY,
    "b": Side.BUY,
    "+1": Side.BUY,
    "short": Side.SELL,
    "sell": Side.SELL,
    "s": Side.SELL,
    "-1": Side.SELL,
}


class TimeGridError(ValueError):
    pass


def parse_schedule(raw: str) -> tuple[tuple[time, Side, str], ...]:
    """Разбирает расписание `HH:MM:side[,HH:MM:side...]` в кортеж (время, сторона, подпись)."""
    items: list[tuple[time, Side, str]] = []
    for chunk in (raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 3:
            raise TimeGridError(f"time_grid: неверный пункт расписания {item!r}, ждётся HH:MM:side")
        hh, mm, side_raw = parts
        try:
            t = time(int(hh), int(mm))
        except ValueError as err:
            raise TimeGridError(f"time_grid: неверное время в {item!r}") from err
        side = SIDE_ALIASES.get(side_raw.strip().lower())
        if side is None:
            raise TimeGridError(f"time_grid: неверная сторона {side_raw!r} в {item!r}")
        items.append((t, side, f"{hh.zfill(2)}:{mm.zfill(2)} {side.value}"))
    if not items:
        raise TimeGridError("time_grid: пустое расписание")
    return tuple(sorted(items))


@dataclass(frozen=True)
class TimeGridParams:
    schedule: str = "06:55:short,18:45:long"
    weekdays_only: int = 1
    gap_skip_pct: float = 0.0


class TimeGridStrategy:
    """Вход и переворот строго по расписанию: сигнал на баре с меткой из графика."""

    strategy_id = "time_grid"
    version = "1.0.0"

    def __init__(self, params: TimeGridParams | None = None):
        self.p = params or TimeGridParams()
        self._legs = parse_schedule(self.p.schedule)
        self._last_ts: datetime | None = None

    @property
    def legs(self) -> tuple[tuple[time, Side, str], ...]:
        return self._legs

    def warmup_bars(self) -> int:
        return 1

    def reset(self) -> None:
        self._last_ts = None

    def _overnight_gap_pct(self, candles: Sequence[Candle]) -> float | None:
        cur_day = candles[-1].ts.astimezone(_MSK).date()
        day_open: float | None = None
        prev_close: float | None = None
        for bar in reversed(candles):
            if bar.ts.astimezone(_MSK).date() == cur_day:
                day_open = bar.open
                continue
            prev_close = bar.close
            break
        if not day_open or not prev_close:
            return None
        return (day_open / prev_close - 1.0) * 100.0

    def _gap_blocked(self, side: Side, candles: Sequence[Candle]) -> bool:
        limit = float(self.p.gap_skip_pct or 0.0)
        if limit <= 0:
            return False
        gap = self._overnight_gap_pct(candles)
        if gap is None:
            return False
        if side is Side.SELL:
            return gap > limit
        return gap < -limit

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if not candles:
            return None
        cur = candles[-1]
        if self._last_ts is not None and cur.ts <= self._last_ts:
            return None
        self._last_ts = cur.ts
        local = cur.ts.astimezone(_MSK)
        if self.p.weekdays_only and local.weekday() >= 5:
            return None
        bar_t = local.time()
        for leg_t, side, label in self._legs:
            if bar_t == leg_t:
                if self._gap_blocked(side, candles):
                    return None
                return Signal(
                    strategy_id=self.strategy_id,
                    side=side,
                    time=cur.ts,
                    reason=f"time_grid {label}",
                    features={"hm": round(bar_t.hour + bar_t.minute / 60, 4)},
                    kind="entry",
                )
        return None
