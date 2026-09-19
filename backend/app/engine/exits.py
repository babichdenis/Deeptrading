from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Sequence

from app.engine.indicators import atr
from app.engine.models import Candle, ExitPlan, ExitReason, PositionState, Side

import logging

logger = logging.getLogger(__name__)


SAME_BAR_CONFLICT_RULE = "STOP_LOSS_FIRST"


class ExitPolicy(ABC):
    policy_id: str
    version: str

    @abstractmethod
    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan: ...


@dataclass(frozen=True)
class AtrTrailingPolicy(ExitPolicy):
    period: int = 14
    initial_stop_atr: float = 2.0
    activation_atr: float = 1.0
    trail_distance_atr: float = 2.0
    policy_id: str = "atr_trailing"
    version: str = "1.0.0"

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan:
        values = atr(bars, self.period)
        last = values[-1] if values else None
        distance = (last or entry_price * 0.01) * self.initial_stop_atr
        if side is Side.BUY:
            return ExitPlan(stop_loss=entry_price - distance, take_profit=None)
        return ExitPlan(stop_loss=entry_price + distance, take_profit=None)

    def update_stop(
        self,
        side: Side,
        entry_price: float,
        current_stop: float | None,
        bars: Sequence[Candle],
        qty: int | None = None,
        commission: float | None = None,
    ) -> float | None:
        if len(bars) < 3:
            return current_stop
        window = bars[-self.period :]
        values = atr(bars, self.period)
        cur_atr = values[-1] if values else None
        if not cur_atr:
            return current_stop
        if side is Side.BUY:
            highest = max(b.high for b in window)
            move = highest - entry_price
            if move < self.activation_atr * cur_atr:
                return current_stop
            candidate = highest - self.trail_distance_atr * cur_atr
            return max(current_stop or candidate, candidate)
        lowest = min(b.low for b in window)
        move = entry_price - lowest
        if move < self.activation_atr * cur_atr:
            return current_stop
        candidate = lowest + self.trail_distance_atr * cur_atr
        stop = current_stop if current_stop is not None else candidate
        return min(stop, candidate)



@dataclass(frozen=True)
class FixedSlTpPolicy(ExitPolicy):
    stop_pct: float = 0.005
    target_pct: float = 0.01
    policy_id: str = "fixed_sl_tp"
    version: str = "1.0.0"

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan:
        if side is Side.BUY:
            return ExitPlan(
                stop_loss=entry_price * (1 - self.stop_pct),
                take_profit=entry_price * (1 + self.target_pct),
            )
        return ExitPlan(
            stop_loss=entry_price * (1 + self.stop_pct),
            take_profit=entry_price * (1 - self.target_pct),
        )


@dataclass(frozen=True)
class AtrStopPolicy(ExitPolicy):
    period: int = 14
    multiplier: float = 2.0
    risk_reward: float | None = None
    trail_activation_r: float | None = None
    trail_distance_r: float | None = None
    trail_activation_comm_mult: float | None = None  # активация трейлинга при PnL >= комиссия_входа * mult
    # --- Динамический трейлинг ---
    trail_compress_r: float = 0.0      # сжатие дистанции по прибыли (в R): 0 = выкл
    trail_min_factor: float = 0.3      # минимальный множитель сжатия
    trail_min_atr: float = 0.0         # минимальная дистанция (в ATR)
    trail_vol_boost: float = 0.0       # влияние объёма: 0 = выкл; >0 — высокий объём шире
    policy_id: str = "atr_stop"
    version: str = "1.2.0"
    _atr_cache: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    def _risk(self, entry_price: float, bars: Sequence[Candle]) -> float:
        n = len(bars)
        c = self._atr_cache
        trs = c.get("trs")
        cn = c.get("n", 0)
        first_ts = bars[0].ts if n else None
        # Валидность кэша: совпадают первый бар И последний закэшированный бар
        # (ts + close). Раньше проверялись только первый ts и длина — другая серия
        # с тем же ts-паттерном молча получала ЧУЖОЙ ATR (audit 2026-09-18).
        cache_ok = (
            trs is not None and 0 < cn <= n
            and c.get("first_ts") == first_ts
            and bars[cn - 1].ts == c.get("last_ts")
            and bars[cn - 1].close == c.get("last_close")
        )
        if not cache_ok:
            # Полный пересчёт true range (только массив TR, без ATR-рекурсии).
            logger.debug("atr_cache rebuild: n=%d prev=%d period=%d", n, cn, self.period)
            c["first_ts"] = first_ts
            trs = [0.0] * n
            for i, bar in enumerate(bars):
                if i == 0:
                    trs[i] = bar.high - bar.low
                else:
                    pc = bars[i - 1].close
                    h, l = bar.high, bar.low
                    tr = h - l
                    a = h - pc
                    if a < 0:
                        a = -a
                    if a > tr:
                        tr = a
                    b = l - pc
                    if b < 0:
                        b = -b
                    if b > tr:
                        tr = b
                    trs[i] = tr
            c["trs"] = trs
            c["n"] = n
            c["last_ts"] = bars[n - 1].ts if n else None
            c["last_close"] = bars[n - 1].close if n else None
            c["value"] = None
            c["value_n"] = 0
        elif n > cn:
            # Инкрементальный досчёт новых баров.
            for i in range(cn, n):
                bar = bars[i]
                if i == 0:
                    trs.append(bar.high - bar.low)
                else:
                    pc = bars[i - 1].close
                    h, l = bar.high, bar.low
                    tr = h - l
                    a = h - pc
                    if a < 0:
                        a = -a
                    if a > tr:
                        tr = a
                    b = l - pc
                    if b < 0:
                        b = -b
                    if b > tr:
                        tr = b
                    trs.append(tr)
            c["n"] = n
            c["last_ts"] = bars[n - 1].ts if n else None
            c["last_close"] = bars[n - 1].close if n else None
        p = self.period
        v = c.get("value")
        vn = c.get("value_n", 0)
        if n >= p:
            if v is None or vn < p:
                v = sum(trs[:p]) / p
                vn = p
            for i in range(vn, n):
                v = (v * (p - 1) + trs[i]) / p
            c["value"] = v
            c["value_n"] = n
        return (v or entry_price * 0.01) * self.multiplier

    def plan_entry(self, side: Side, entry_price: float, bars: Sequence[Candle]) -> ExitPlan:
        distance = self._risk(entry_price, bars)
        if side is Side.BUY:
            stop = entry_price - distance
            target = entry_price + distance * self.risk_reward if self.risk_reward else None
        else:
            stop = entry_price + distance
            target = entry_price - distance * self.risk_reward if self.risk_reward else None
        return ExitPlan(stop_loss=stop, take_profit=target)

    def trailing_activated(
        self,
        side: Side,
        entry_price: float,
        qty: int | None,
        commission: float | None,
        bars: Sequence[Candle],
    ) -> bool:
        """Комиссионная активация трейлинга: PnL (по последнему close) >= комиссия входа × mult.
        Без qty/комиссии (бэктесты-аналитика) никогда не активируется."""
        if self.trail_activation_comm_mult is None or not bars:
            return False
        if qty is None or qty <= 0 or commission is None or commission <= 0:
            return False
        close = bars[-1].close
        pnl = (close - entry_price) * qty if side is Side.BUY else (entry_price - close) * qty
        return pnl >= self.trail_activation_comm_mult * commission

    def update_stop(
        self,
        side: Side,
        entry_price: float,
        current_stop: float | None,
        bars: Sequence[Candle],
        qty: int | None = None,
        commission: float | None = None,
    ) -> float | None:
        """Трейлинг-стоп. Два режима активации:
        - комиссионный (trail_activation_comm_mult): активация при PnL >= комиссия×mult,
          дальше стоп следует за ценой (дистанция trail_distance_r × risk);
        - ATR-режим (trail_activation_r/trail_distance_r): активация по move >= ATR-порога.
        Движение только в сторону прибыли (ratchet), никогда назад."""
        if not bars:
            return current_stop
        if len(bars) < 2:
            return current_stop
        trail = self.trail_distance_r
        if trail is None or trail <= 0:
            return current_stop
        if self.trail_activation_comm_mult is not None:
            if not self.trailing_activated(side, entry_price, qty, commission, bars):
                return current_stop
        elif self.trail_activation_r is None:
            return current_stop
        risk = self._risk(entry_price, bars)
        if risk <= 0:
            return current_stop
        # Дистанция трейла. Комиссионный режим (бот) задаёт trail_distance_r как
        # множитель ЧИСТОГО ATR (trail_distance_atr): стоп идёт за ценой на N×ATR
        # независимо от ширины SL (multiplier = sl_mult). ATR-режим (E5/бэктест)
        # остаётся в R: дистанция trail_distance_r × risk.
        if self.trail_activation_comm_mult is not None:
            _atr_vals = atr(bars, self.period)
            _last_atr = _atr_vals[-1] if _atr_vals else None
            dist_unit = _last_atr if _last_atr else entry_price * 0.01
        else:
            dist_unit = risk
        # --- Динамическая дистанция: сжатие по прибыли + учёт объёма ---
        dist = trail * dist_unit
        if self.trail_compress_r > 0 and risk > 0:
            _close = bars[-1].close
            _pnl = (entry_price - _close) if side is Side.SELL else (_close - entry_price)
            _r = _pnl / risk  # прибыль в единицах риска (R)
            _factor = max(self.trail_min_factor, 1.0 - self.trail_compress_r * max(0.0, _r))
            dist *= _factor
        if self.trail_vol_boost > 0:
            _vols = [float(b.volume or 0) for b in bars[-50:]]
            _mean_v = (sum(_vols) / len(_vols)) if _vols else 0.0
            _vr = (float(bars[-1].volume or 0) / _mean_v) if _mean_v > 0 else 1.0
            # Высокий объём (движение подтверждено) → шире (даём дышать),
            # низкий (затишье/истощение) → теснее.
            _adj = min(1.0 + self.trail_vol_boost, max(1.0 - self.trail_vol_boost, _vr ** 0.5))
            dist *= _adj
        if self.trail_min_atr > 0:
            dist = max(dist, self.trail_min_atr * dist_unit)
        window = bars[-self.period :]
        if side is Side.BUY:
            highest = max(b.high for b in window)
            if self.trail_activation_comm_mult is None:
                move = highest - entry_price
                if move < self.trail_activation_r * risk:
                    return current_stop
            candidate = highest - dist
            return max(current_stop or candidate, candidate)
        lowest = min(b.low for b in window)
        if self.trail_activation_comm_mult is None:
            move = entry_price - lowest
            if move < self.trail_activation_r * risk:
                return current_stop
        candidate = lowest + dist
        stop = current_stop if current_stop is not None else candidate
        return min(stop, candidate)


def intrabar_exit(
    bar: Candle,
    state: PositionState,
    stop_loss: float | None,
    take_profit: float | None,
    close_based: bool = False,
) -> tuple[float | None, str]:
    """Выход по стопу/TP на баре.

    close_based=False (по умолчанию, движок/бэктест): классический стоп на
    касание — срабатывает по bar.low (LONG) / bar.high (SHORT), т.е. любой
    хвост, дошедший до уровня, закрывает позицию.

    close_based=True (бот, после активации трейлинга): трейлинг-стоп
    срабатывает только если бар ЗАКРЫЛСЯ за уровнем (или открылся за ним —
    гэп через стоп). Хвост, который коснулся стопа и вернулся, НЕ закрывает
    позицию. Используется для трейлинг-стопа, чтобы не выбивать из сделки
    ложными пробоями.
    """
    if state is PositionState.LONG:
        if stop_loss is not None:
            if close_based:
                if bar.open <= stop_loss:
                    return bar.open, ExitReason.STOP_LOSS.value
                if bar.close <= stop_loss:
                    return bar.close, ExitReason.STOP_LOSS.value
            else:
                if bar.open <= stop_loss:
                    return bar.open, ExitReason.STOP_LOSS.value
                if bar.low <= stop_loss:
                    return stop_loss, ExitReason.STOP_LOSS.value
        if take_profit is not None and bar.high >= take_profit:
            if bar.open >= take_profit:
                return bar.open, ExitReason.TARGET.value
            return take_profit, ExitReason.TARGET.value
        return None, ""
    if state is PositionState.SHORT:
        if stop_loss is not None:
            if close_based:
                if bar.open >= stop_loss:
                    return bar.open, ExitReason.STOP_LOSS.value
                if bar.close >= stop_loss:
                    return bar.close, ExitReason.STOP_LOSS.value
            else:
                if bar.open >= stop_loss:
                    return bar.open, ExitReason.STOP_LOSS.value
                if bar.high >= stop_loss:
                    return stop_loss, ExitReason.STOP_LOSS.value
        if take_profit is not None and bar.low <= take_profit:
            if bar.open <= take_profit:
                return bar.open, ExitReason.TARGET.value
            return take_profit, ExitReason.TARGET.value
        return None, ""
    return None, ""
