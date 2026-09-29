from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from app.engine.models import Candle, Signal, Side

_MSK = ZoneInfo("Europe/Moscow")


class ReplayStrategy:
    """Проигрывает сохранённые сигналы по закрытию бара.

    Поддерживает два потока:
      entries — сигналы на открытие позиции (kind=entry);
      exits   — сигналы на закрытие/подтверждение (kind=exit), отдельно от входа.
    На одном баре приоритет у потока с `priority` (exit > entry).
    """

    strategy_id = "replay"
    version = "1.0.0"

    def __init__(self, signals: Sequence[tuple[datetime | str, str]], warmup: int = 0,
                 exits: Sequence[tuple[datetime | str, str]] | None = None):
        def _norm(items: Sequence[tuple[datetime | str, str]], kind: str) -> list[tuple[datetime, Side, str]]:
            out = []
            for ts, side in items:
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                out.append((ts, Side(side), kind))
            return out
        ordered = sorted(_norm(signals, "entry") + _norm(exits or [], "exit"),
                         key=lambda x: (x[0], 1 if x[2] == "exit" else 0))
        self._pending: deque[tuple[datetime, Side, str]] = deque(ordered)
        self.warmup = warmup
        self.total = len(ordered)

    def warmup_bars(self) -> int:
        return self.warmup

    @staticmethod
    def _rank(kind: str) -> int:
        # тот же порядок, что в __init__: entry (0) раньше exit (1) на одном ts
        return 1 if kind == "exit" else 0

    def extend(self, signals=(), exits=()) -> None:
        """L2.6: добавить сигналы в хвост очереди с сохранением порядка pop.

        Новые сигналы предполагаются не старее уже лежащих (append-only по ts);
        вставка — insort-right по (ts, kind-rank): порядок выдачи совпадает
        с batch-конструкцией на объединённом списке для общего случая
        (одиночные сигналы на ts; множественные одного ts+kind идут порядком
        прибытия — в L2.7 интеграции кормим entries раньше exits за бар).
        """
        from bisect import bisect_right
        new = []
        for ts, side in signals:
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            new.append((ts, Side(side), "entry"))
        for ts, side in (exits or []):
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            new.append((ts, Side(side), "exit"))
        if not new:
            return
        keys = [(p[0], self._rank(p[2])) for p in self._pending]
        for item in new:
            k = (item[0], self._rank(item[2]))
            pos = bisect_right(keys, k)
            keys.insert(pos, k)
            self._pending.insert(pos, item)
        self.total += len(new)

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if not self._pending:
            return None
        current_ts = candles[-1].ts
        emitted: Signal | None = None
        while self._pending and self._pending[0][0] <= current_ts:
            sig_ts, side, kind = self._pending.popleft()
            if sig_ts == current_ts and emitted is None:
                emitted = Signal(
                    strategy_id=self.strategy_id,
                    side=side,
                    time=sig_ts,
                    reason="replay",
                    kind=kind,
                )
        return emitted


def _signal(
    strategy_id: str,
    side: Side,
    ts: datetime,
    reason: str,
    features: dict,
) -> Signal:
    return Signal(strategy_id=strategy_id, side=side, time=ts, reason=reason, features=features)


@dataclass(frozen=True)
class RsiReversalParams:
    period: int = 14
    oversold: float = 35
    overbought: float = 65


class RsiReversalStrategy:
    strategy_id = "rsi_reversal"
    version = "1.0.0"

    def __init__(self, params: RsiReversalParams | None = None):
        self.params = params or RsiReversalParams()
        self._count = 0
        self._seed_gains: list[float] = []
        self._seed_losses: list[float] = []
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None
        self._prev_close: float | None = None
        self._prev_high: float | None = None
        self._prev_low: float | None = None
        self._prev_rsi: float | None = None

    def warmup_bars(self) -> int:
        return self.params.period + 2

    def reset(self) -> None:
        """ENG-007: сброс streaming-состояния RSI."""
        self._count = 0
        self._seed_gains.clear()
        self._seed_losses.clear()
        self._avg_gain = None
        self._avg_loss = None
        self._prev_close = None
        self._prev_high = None
        self._prev_low = None
        self._prev_rsi = None

    def _update_rsi(self, close: float) -> float | None:
        p = self.params.period
        if self._prev_close is None:
            self._prev_close = close
            return None
        delta = close - self._prev_close
        self._prev_close = close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        if self._avg_gain is None:
            self._seed_gains.append(gain)
            self._seed_losses.append(loss)
            if len(self._seed_gains) == p:
                self._avg_gain = sum(self._seed_gains) / p
                self._avg_loss = sum(self._seed_losses) / p
                self._seed_gains.clear()
                self._seed_losses.clear()
            return None
        self._avg_gain = (self._avg_gain * (p - 1) + gain) / p
        self._avg_loss = (self._avg_loss * (p - 1) + loss) / p
        if self._avg_loss == 0:
            rsi = 100.0
        else:
            rs = self._avg_gain / self._avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)
        self._prev_close = close
        return rsi

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        rsi = self._update_rsi(bar.close)
        prev_high = self._prev_high
        prev_low = self._prev_low
        prev_rsi = self._prev_rsi
        self._prev_high = bar.high
        self._prev_low = bar.low

        result: Signal | None = None
        if rsi is not None and prev_rsi is not None:
            features = {"rsi": round(rsi, 4), "prev_rsi": round(prev_rsi, 4)}
            if (
                rsi < self.params.oversold
                and rsi > prev_rsi
                and prev_high is not None
                and bar.close > prev_high
            ):
                result = _signal(
                    self.strategy_id, Side.BUY, bar.ts, "rsi_turn_up_oversold", features
                )
            elif (
                rsi > self.params.overbought
                and rsi < prev_rsi
                and prev_low is not None
                and bar.close < prev_low
            ):
                result = _signal(
                    self.strategy_id, Side.SELL, bar.ts, "rsi_turn_down_overbought", features
                )

        if rsi is not None:
            self._prev_rsi = rsi
        return result


@dataclass(frozen=True)
class StochasticParams:
    k_period: int = 14
    d_period: int = 3
    oversold: float = 20
    overbought: float = 80


class StochasticStrategy:
    """Stochastic oscillator: %K/%D cross из зон перепроданности/перекупленности."""
    strategy_id = "stochastic"
    version = "1.0.0"

    def __init__(self, params: StochasticParams | None = None):
        self.params = params or StochasticParams()
        self._highs: list[float] = []
        self._lows: list[float] = []
        self._ks: list[float] = []
        self._prev_k: float | None = None
        self._prev_d: float | None = None

    def warmup_bars(self) -> int:
        return self.params.k_period + self.params.d_period + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        self._highs.append(bar.high)
        self._lows.append(bar.low)
        kp = self.params.k_period
        if len(self._highs) < kp:
            return None
        hh = max(self._highs[-kp:])
        ll = min(self._lows[-kp:])
        rng = hh - ll
        k = 100.0 * (bar.close - ll) / rng if rng > 0 else 50.0
        self._ks.append(k)
        dp = self.params.d_period
        d = sum(self._ks[-dp:]) / min(len(self._ks), dp)
        prev_k, prev_d = self._prev_k, self._prev_d
        self._prev_k, self._prev_d = k, d
        result: Signal | None = None
        if prev_k is not None and prev_d is not None:
            feats = {"k": round(k, 3), "d": round(d, 3),
                     "prev_k": round(prev_k, 3), "prev_d": round(prev_d, 3)}
            if k < self.params.oversold and k > d and prev_k <= prev_d:
                result = _signal(self.strategy_id, Side.BUY, bar.ts, "stoch_cross_up_oversold", feats)
            elif k > self.params.overbought and k < d and prev_k >= prev_d:
                result = _signal(self.strategy_id, Side.SELL, bar.ts, "stoch_cross_down_overbought", feats)
        return result


@dataclass(frozen=True)
class BollingerReclaimParams:
    period: int = 20
    k: float = 2.0


class BollingerReclaimStrategy:
    strategy_id = "bollinger_reclaim"
    version = "1.0.0"

    def __init__(self, params: BollingerReclaimParams | None = None):
        self.params = params or BollingerReclaimParams()
        self._window: deque[float] = deque(maxlen=self.params.period)
        self._prev_close: float | None = None
        self._prev_lower: float | None = None
        self._prev_upper: float | None = None

    @staticmethod
    def _bands(window: Sequence[float], k: float) -> tuple[float, float]:
        n = len(window)
        mean = sum(window) / n
        variance = sum((x - mean) ** 2 for x in window) / n
        std = variance**0.5
        return mean - k * std, mean + k * std

    def warmup_bars(self) -> int:
        return self.params.period + 1

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        result: Signal | None = None
        if len(self._window) == self.params.period:
            lower, upper = self._bands(self._window, self.params.k)
            pc = self._prev_close
            pl = self._prev_lower
            pu = self._prev_upper
            if pc is not None and pl is not None and pu is not None:
                if pc < pl and bar.close > lower:
                    result = _signal(
                        self.strategy_id,
                        Side.BUY,
                        bar.ts,
                        "reclaim_lower_band",
                        {"band_lower": round(lower, 4), "band_upper": round(upper, 4)},
                    )
                elif pc > pu and bar.close < upper:
                    result = _signal(
                        self.strategy_id,
                        Side.SELL,
                        bar.ts,
                        "reclaim_upper_band",
                        {"band_lower": round(lower, 4), "band_upper": round(upper, 4)},
                    )
            self._prev_lower = lower
            self._prev_upper = upper
            self._prev_close = bar.close
        self._window.append(bar.close)
        return result


@dataclass(frozen=True)
class PullbackEmaParams:
    trend_ema: int = 50
    pull_ema: int = 20


class PullbackEmaStrategy:
    strategy_id = "pullback_ema"
    version = "1.0.0"

    def __init__(self, params: PullbackEmaParams | None = None):
        self.params = params or PullbackEmaParams()
        self._kf = 2 / (self.params.trend_ema + 1)
        self._kp = 2 / (self.params.pull_ema + 1)
        self._ema_trend: float | None = None
        self._ema_pull: float | None = None
        self._prev_ema_trend: float | None = None
        self._prev_high: float | None = None
        self._prev_low: float | None = None

    def warmup_bars(self) -> int:
        return max(self.params.trend_ema, self.params.pull_ema) + 1

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        if self._ema_trend is None:
            self._ema_trend = bar.close
            self._ema_pull = bar.close
            self._prev_ema_trend = self._ema_trend
            self._prev_high = bar.high
            self._prev_low = bar.low
            return None

        self._prev_ema_trend = self._ema_trend
        self._ema_trend = bar.close * self._kf + self._ema_trend * (1 - self._kf)
        self._ema_pull = bar.close * self._kp + self._ema_pull * (1 - self._kp)

        result: Signal | None = None
        ph = self._prev_high
        plow = self._prev_low
        trend_up = self._ema_trend > self._prev_ema_trend
        trend_down = self._ema_trend < self._prev_ema_trend
        features = {
            "ema_trend": round(self._ema_trend, 4),
            "ema_pull": round(self._ema_pull, 4),
        }
        if ph is not None and plow is not None:
            if (
                trend_up
                and bar.close > self._ema_trend
                and bar.low <= self._ema_pull
                and bar.close > ph
            ):
                result = _signal(
                    self.strategy_id, Side.BUY, bar.ts, "pullback_resume_up", features
                )
            elif (
                trend_down
                and bar.close < self._ema_trend
                and bar.high >= self._ema_pull
                and bar.close < plow
            ):
                result = _signal(
                    self.strategy_id, Side.SELL, bar.ts, "pullback_resume_down", features
                )

        self._prev_high = bar.high
        self._prev_low = bar.low
        return result


@dataclass(frozen=True)
class VwapReclaimParams:
    k: float = 2.0


class VwapReclaimStrategy:
    strategy_id = "vwap_reclaim"
    version = "1.0.0"

    def __init__(self, params: VwapReclaimParams | None = None):
        self.params = params or VwapReclaimParams()
        self._session_date = None
        self._cum_pv = 0.0
        self._cum_v = 0.0
        self._vwap: float | None = None
        self._prev_dev: float | None = None
        self._sigma: float | None = None
        self._dev_window: deque[float] = deque(maxlen=60)

    def warmup_bars(self) -> int:
        return 10

    def _reset_if_new_session(self, ts: datetime) -> None:
        local_date = ts.astimezone(_MSK).date()
        if local_date != self._session_date:
            self._session_date = local_date
            self._cum_pv = 0.0
            self._cum_v = 0.0

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        self._reset_if_new_session(bar.ts)

        typical = (bar.high + bar.low + bar.close) / 3
        volume = bar.volume if bar.volume > 0 else 1.0
        self._cum_pv += typical * volume
        self._cum_v += volume
        vwap = self._cum_pv / self._cum_v

        dev = bar.close - vwap
        self._dev_window.append(dev)

        result: Signal | None = None
        pd = self._prev_dev
        ps = self._sigma
        if pd is not None and ps is not None and len(self._dev_window) >= 10:
            threshold = self.params.k * ps
            if pd <= -threshold and bar.close > vwap:
                result = _signal(
                    self.strategy_id,
                    Side.BUY,
                    bar.ts,
                    "vwap_reclaim_up",
                    {
                        "vwap": round(vwap, 4),
                        "dev": round(dev, 4),
                        "sigma": round(ps, 4),
                    },
                )
            elif pd >= threshold and bar.close < vwap:
                result = _signal(
                    self.strategy_id,
                    Side.SELL,
                    bar.ts,
                    "vwap_reclaim_down",
                    {
                        "vwap": round(vwap, 4),
                        "dev": round(dev, 4),
                        "sigma": round(ps, 4),
                    },
                )

        self._vwap = vwap
        self._prev_dev = dev
        n = len(self._dev_window)
        mean = sum(self._dev_window) / n
        var = sum((x - mean) ** 2 for x in self._dev_window) / n
        self._sigma = var**0.5
        return result


@dataclass(frozen=True)
class SqueezeBreakoutParams:
    lookback: int = 20
    atr_period: int = 14
    pct: float = 25.0


class SqueezeBreakoutStrategy:
    strategy_id = "range_compression_breakout"
    version = "1.0.0"

    def __init__(self, params: SqueezeBreakoutParams | None = None):
        self.params = params or SqueezeBreakoutParams()
        self._atr: float | None = None
        self._tr_count = 0
        self._sum_tr = 0.0
        self._prev_close: float | None = None
        self._atr_history: deque[float] = deque(maxlen=self.params.lookback)
        self._highs: deque[float] = deque(maxlen=self.params.lookback)
        self._lows: deque[float] = deque(maxlen=self.params.lookback)

    def warmup_bars(self) -> int:
        return max(self.params.atr_period, self.params.lookback) + 2

    def _update_atr(self, bar: Candle) -> None:
        if self._prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - self._prev_close),
                abs(bar.low - self._prev_close),
            )
        self._prev_close = bar.close
        self._tr_count += 1
        if self._tr_count <= self.params.atr_period:
            self._sum_tr += tr
            if self._tr_count == self.params.atr_period:
                self._atr = self._sum_tr / self.params.atr_period
        else:
            self._atr = (self._atr * (self.params.atr_period - 1) + tr) / self.params.atr_period

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        self._update_atr(bar)

        result: Signal | None = None
        ready = (
            self._atr is not None
            and len(self._atr_history) == self._atr_history.maxlen
            and len(self._highs) == self._highs.maxlen
            and len(self._lows) == self._lows.maxlen
        )
        if ready:
            recent = list(self._atr_history)[-3:]
            older = list(self._atr_history)[:-3]
            candidate = max(recent)
            below = sum(1 for v in older if v < candidate)
            rank_pct = below / len(older) * 100
            range_high = max(self._highs)
            range_low = min(self._lows)
            if rank_pct <= self.params.pct:
                if bar.close > range_high:
                    result = _signal(
                        self.strategy_id,
                        Side.BUY,
                        bar.ts,
                        "squeeze_breakout_up",
                        {
                            "atr": round(self._atr, 4),
                            "atr_rank_pct": round(rank_pct, 2),
                            "range_high": round(range_high, 4),
                        },
                    )
                elif bar.close < range_low:
                    result = _signal(
                        self.strategy_id,
                        Side.SELL,
                        bar.ts,
                        "squeeze_breakdown",
                        {
                            "atr": round(self._atr, 4),
                            "atr_rank_pct": round(rank_pct, 2),
                            "range_low": round(range_low, 4),
                        },
                    )

        self._highs.append(bar.high)
        self._lows.append(bar.low)
        if self._atr is not None:
            self._atr_history.append(self._atr)
        return result
