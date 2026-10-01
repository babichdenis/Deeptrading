"""Накопительный контекст compute_ensemble — инкрементальный кэш производных рядов.

Проблема: compute_ensemble вызывается на каждом 1m-баре с буфером ~10 дней, и каждый
раз всё пересчитывается заново (resample, валидация, RSI, bias, сигналы) —
O(N) на вызов, O(N^2) за день. Здесь — персистентное состояние: производные ряды
обновляются инкрементально (append-only), что даёт те же результаты бит-в-бит,
но за O(1) на бар.

Паритет: каждая функция повторяет математику batch-кода в том же порядке операций.
Проверка: golden-тесты + дифф сделок до/после на одних данных.

Использование:
    ctx = EnsembleContext()            # один на стратегию (живёт между барами)
    res = compute_ensemble(candles, req, ctx)
Без ctx — прежний (медленный) путь для бэктестов/исследований.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Sequence

from app.engine.models import Candle as EngineCandle
from app.engine.strategies import build_strategy
from app.engine.views import CandleWindow
from app.services.candle_guard import bar_ok, jump_ratio
from app.services.entry_gates import rsi_map as _rsi_map_batch

MSK = None


def _msk():
    global MSK
    if MSK is None:
        from zoneinfo import ZoneInfo
        MSK = ZoneInfo("Europe/Moscow")
    return MSK


def _secs_of(ts) -> int:
    """UTC unix-секунды (для bucket-математики)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp())


def _bucket_of(ts, tf_sec: int) -> int:
    """UTC-grid floor bucketing: bucket = unix - unix % tf — КАНОН START.

    Совпадает с marketdata.Resampler, CandleHub.build_tf и _resample_batch.
    """
    u = _secs_of(ts)
    return u - u % tf_sec


def _key_of(ts, tf_sec: int):
    b = _bucket_of(ts, tf_sec)
    return datetime.fromtimestamp(b, tz=timezone.utc)


def _msk_day(ts) -> str:
    return ts.astimezone(_msk()).date().isoformat()


def _ema(values: list[float], span: int) -> list[float]:
    if not values:
        return []
    alpha = 2 / (span + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1 - alpha) * out[-1])
    return out


def _val(g: float, l: float) -> float:
    if l <= 0:
        return 100.0 if g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + g / l)


def _resample_batch(candles: list[EngineCandle], tf_sec: int) -> list[EngineCandle]:
    out: list[EngineCandle] = []
    last_bucket: int | None = None
    for c in candles:
        secs = c.ts.hour * 3600 + c.ts.minute * 60 + c.ts.second
        bucket = (secs // tf_sec) * tf_sec
        if last_bucket == bucket and out:
            prev = out[-1]
            out[-1] = EngineCandle(ts=prev.ts, open=prev.open, high=max(prev.high, c.high),
                                   low=min(prev.low, c.low), close=c.close,
                                   volume=prev.volume + c.volume)
        else:
            key = c.ts.replace(hour=bucket // 3600, minute=(bucket % 3600) // 60,
                              second=0, microsecond=0)
            out.append(EngineCandle(ts=key, open=c.open, high=c.high, low=c.low,
                                    close=c.close, volume=c.volume))
            last_bucket = bucket
    return out


def _bias_batch(bars: list[EngineCandle], period: int, tf_sec: int) -> dict[int, int]:
    closes = [c.close for c in bars]
    ema = _ema(closes, period)
    bias: dict[int, int] = {}
    for i in range(1, len(bars)):
        bucket = int(bars[i].ts.timestamp()) // tf_sec
        bias[bucket] = 1 if closes[i - 1] >= ema[i - 1] else -1
    return bias


def _sig_batch(strat, bars: list[EngineCandle]) -> list[dict]:
    warmup = strat.warmup_bars()
    out: list[dict] = []
    total = len(bars)
    for i in range(1, total + 1):
        lo = max(0, i - 400)
        sig = strat.on_bar(CandleWindow(bars, lo, i))
        if sig is None or i <= warmup:
            continue
        out.append({"ts": sig.time, "side": sig.side.value, "status": "CANDIDATE",
                    "reason": sig.reason, "features": sig.features})
    return out


def _bar_stats_batch(candles: list[EngineCandle], tf_sec: int) -> dict:
    stats: dict = {}
    for c in candles:
        secs = c.ts.hour * 3600 + c.ts.minute * 60 + c.ts.second
        bucket = (secs // tf_sec) * tf_sec
        key = c.ts.replace(hour=bucket // 3600, minute=(bucket % 3600) // 60,
                          second=0, microsecond=0)
        st = stats.get(key)
        to = float(c.close) * float(c.volume or 0.0)
        if st is None:
            stats[key] = [1, to]
        else:
            st[0] += 1
            st[1] += to
    return stats


class EnsembleContext:
    """Персистентное состояние производных рядов для одного тикера."""

    def __init__(self):
        self._valid: list[EngineCandle] = []
        self._skipped: int = 0
        self._day_jumps: dict[str, int] = {}
        self._day_prev: dict[str, float] = {}
        self._bad_days: set[str] = set()
        self._prev_close: float | None = None
        self._n: int = 0
        self._first_ts = None
        self._last_ts = None
        self._dirty: bool = False
        self._flicker_jumps: int = 3
        self._flicker_thr: float = 0.5
        self._jump_thr: float = 0.4
        self._resampled: dict[int, list] = {}
        self._resampled_n: dict[int, int] = {}
        self._bias: dict[tuple, dict] = {}
        self._bias_ema: dict[tuple, list] = {}
        self._bias_closes: dict[tuple, list] = {}
        self._bias_n: dict[tuple, int] = {}
        self._strategies: dict[tuple, object] = {}
        self._sig_lists: dict[str, list] = {}
        self._sig_n: dict[str, int] = {}
        self._bar_stats: dict = {}
        self._bar_stats_n: dict = {}
        self._atr: dict[int, list] = {}
        self._atr_trs: dict[int, list] = {}
        self._atr_val: dict[int, float | None] = {}
        self._atr_n: dict[int, int] = {}

    # ------------------------------------------------------------------ sync
    def sync(self, candles: list[EngineCandle]) -> tuple[list[EngineCandle], int]:
        if (self._n is not None and len(candles) == self._n + 1
                and candles[0].ts == self._first_ts and candles[-1].ts > self._last_ts):
            new = [candles[-1]]
            self._n += 1
            self._last_ts = candles[-1].ts
            self._append_validated(new)
        else:
            self._rebuild(candles)
        return self._valid, self._skipped

    def _rebuild(self, candles):
        self._valid = []
        self._skipped = 0
        self._day_jumps = {}
        self._day_prev = {}
        self._bad_days = set()
        self._prev_close = None
        self._n = len(candles)
        self._first_ts = candles[0].ts if candles else None
        self._last_ts = candles[-1].ts if candles else None
        self._dirty = True
        self._resampled = {}
        self._resampled_n = {}
        self._bias = {}
        self._bias_ema = {}
        self._bias_closes = {}
        self._bias_n = {}
        self._strategies = {}
        self._sig_lists = {}
        self._sig_n = {}
        self._bar_stats = {}
        self._bar_stats_n = {}
        self._atr = {}
        self._atr_trs = {}
        self._atr_val = {}
        self._atr_n = {}
        self._append_validated(candles)

    def _append_validated(self, new):
        for c in new:
            d = _msk_day(c.ts)
            pv = self._day_prev.get(d)
            if pv is not None and pv > 0:
                j = jump_ratio(pv, c.close)
                if j is not None and j > self._flicker_thr:
                    self._day_jumps[d] = self._day_jumps.get(d, 0) + 1
                    if self._day_jumps[d] >= self._flicker_jumps and d not in self._bad_days:
                        self._bad_days.add(d)
                        ndrop = sum(1 for b in self._valid if _msk_day(b.ts) == d)
                        self._valid = [b for b in self._valid if _msk_day(b.ts) != d]
                        self._skipped += ndrop
                        self._dirty = True
            self._day_prev[d] = float(c.close)
            if d in self._bad_days:
                self._skipped += 1
                continue
            if not bar_ok(c.open, c.high, c.low, c.close, c.volume):
                self._skipped += 1
                continue
            j = jump_ratio(self._prev_close, c.close)
            if self._prev_close is not None and j is not None and j > self._jump_thr:
                self._skipped += 1
                continue
            self._prev_close = float(c.close)
            self._valid.append(c)

    # --------------------------------------------------------------- resample
    def resample(self, tf_sec: int) -> list[EngineCandle]:
        if self._dirty or tf_sec not in self._resampled:
            self._resampled[tf_sec] = _resample_batch(self._valid, tf_sec)
            self._resampled_n[tf_sec] = len(self._valid)
            self._dirty = False
            return self._resampled[tf_sec]
        bars = self._resampled[tf_sec]
        folded = self._resampled_n[tf_sec]
        new = self._valid[folded:]
        for c in new:
            b = _bucket_of(c.ts, tf_sec)
            if bars and _bucket_of(bars[-1].ts, tf_sec) == b:
                prev = bars[-1]
                bars[-1] = EngineCandle(ts=prev.ts, open=prev.open,
                                        high=max(prev.high, c.high),
                                        low=min(prev.low, c.low), close=c.close,
                                        volume=prev.volume + c.volume)
            else:
                bars.append(EngineCandle(ts=_key_of(c.ts, tf_sec), open=c.open,
                                        high=c.high, low=c.low, close=c.close,
                                        volume=c.volume))
        self._resampled_n[tf_sec] = len(self._valid)
        return bars

    # ------------------------------------------------------------------- bias
    def bias(self, tf_sec: int, period: int) -> dict[int, int]:
        """Инкрементальный bias == _bias_batch (бит-в-бит).

        Персистентное состояние двигается ТОЛЬКО по закрытым барам: последний
        бар resample всегда forming и может переписаться следующим 1m-баром,
        поэтому его значение считается зондом (snapshot closes/ema) без
        сохранения. Старая версия скармливала forming как финальный и больше
        его не пересчитывала — расхождение с batch (знак bucket!).
        """
        key = (tf_sec, period)
        if self._dirty or key not in self._bias:
            self._bias[key] = {}
            self._bias_closes[key] = []
            self._bias_ema[key] = []
            self._bias_n[key] = 0
        bars = self.resample(tf_sec)
        closed = bars[:-1] if bars else []
        closes = self._bias_closes[key]
        ema = self._bias_ema[key]
        alpha = 2.0 / (period + 1)
        for b in closed[self._bias_n.get(key, 0):]:
            if closes:
                bucket = int(b.ts.timestamp()) // tf_sec
                self._bias[key][bucket] = 1 if closes[-1] >= ema[-1] else -1
            # типы как есть (Decimal/float): та же арифметика, что batch _ema
            closes.append(b.close)
            ema.append(closes[0] if len(ema) == 0
                       else alpha * closes[-1] + (1 - alpha) * ema[-1])
        self._bias_n[key] = len(closed)
        out = dict(self._bias[key])
        if bars and closes:
            b = bars[-1]
            bucket = int(b.ts.timestamp()) // tf_sec
            out[bucket] = 1 if closes[-1] >= ema[-1] else -1
        return out

    # ---------------------------------------------------------------- signals
    def setup_signals(self, sid: str, params: dict, tf_sec: int) -> list[dict]:
        # Ключ обязан включать tf_sec: один sid+params может торговаться на разных ТФ.
        key = (sid, json.dumps(params or {}, sort_keys=True), tf_sec)
        if self._dirty or key not in self._strategies:
            bars = self.resample(tf_sec)
            self._strategies[key] = build_strategy(sid, params)
            self._sig_lists[key] = _sig_batch(self._strategies[key], bars)
            self._sig_n[key] = len(bars)
            return self._sig_lists[key]
        bars = self.resample(tf_sec)
        new = bars[self._sig_n.get(key, 0):]
        strat = self._strategies[key]
        sigs = self._sig_lists[key]
        lo_base = len(bars) - len(new)
        warmup = strat.warmup_bars()
        for k in range(1, len(new) + 1):
            lo = max(0, lo_base + k - 400)
            sig = strat.on_bar(CandleWindow(bars, lo, lo_base + k))
            if sig is not None and lo_base + k > warmup:
                sigs.append({"ts": sig.time, "side": sig.side.value,
                             "status": "CANDIDATE", "reason": sig.reason,
                             "features": sig.features})
        self._sig_n[key] = len(bars)
        return sigs

    # ------------------------------------------------------------- bar stats
    def bar_stats(self, tf_sec: int) -> dict:
        if self._dirty or tf_sec not in self._bar_stats:
            self._bar_stats[tf_sec] = _bar_stats_batch(self._valid, tf_sec)
            self._bar_stats_n[tf_sec] = len(self._valid)
            return self._bar_stats[tf_sec]
        stats = self._bar_stats[tf_sec]
        folded = self._bar_stats_n[tf_sec]
        for c in self._valid[folded:]:
            key = _key_of(c.ts, tf_sec)
            st = stats.get(key)
            to = float(c.close) * float(c.volume or 0.0)
            if st is None:
                stats[key] = [1, to]
            else:
                st[0] += 1
                st[1] += to
        self._bar_stats_n[tf_sec] = len(self._valid)
        return stats

    # -------------------------------------------------------------------- atr
    def atr_series(self, period: int = 14) -> list[float | None]:
        key = period
        if self._dirty or key not in self._atr:
            self._atr[key] = _atr_batch(self._valid, period)
            self._atr_trs[key] = []
            self._atr_val[key] = None
            self._atr_n[key] = 0
        # инкрементально досчитываем TR и ATR Вайлдером
        trs = self._atr_trs[key]
        for i in range(self._atr_n.get(key, 0), len(self._valid)):
            b = self._valid[i]
            if i == 0:
                trs.append(b.high - b.low)
            else:
                pc = self._valid[i - 1].close
                trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
        n = len(self._valid)
        out = self._atr[key]
        p = period
        if n >= p:
            if len(out) < p:
                value = sum(trs[:p]) / p
                out.extend([None] * (p - 1 - len(out)))
                out.append(value)
            else:
                value = self._atr_val[key]
            for i in range(max(p, len(out)), n):
                value = (value * (p - 1) + trs[i]) / p
                out.append(value)
            self._atr_val[key] = value
        self._atr_n[key] = n
        return out


def _atr_batch(bars: list[EngineCandle], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(bars)
    if len(bars) < period:
        return result
    trs: list[float] = []
    for i, bar in enumerate(bars):
        if i == 0:
            trs.append(bar.high - bar.low)
        else:
            prev_close = bars[i - 1].close
            trs.append(max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close)))
    value = sum(trs[:period]) / period
    result[period - 1] = value
    for i in range(period, len(bars)):
        value = (value * (period - 1) + trs[i]) / period
        result[i] = value
    return result
