from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Sequence

from app.engine.models import Candle, Signal, Side
from app.engine.ensemble_v2 import EnsembleVoteParams, EnsembleVoteStrategy
from app.engine.regime_ensembles import (
    HighVolatilityEnsembleStrategy,
    HighVolatilityParams,
    LongEnsembleParams,
    LongEnsembleStrategy,
    Momentum1BarParams,
    Momentum1BarStrategy,
    NeutralEnsembleParams,
    NeutralEnsembleStrategy,
    RangeEnsembleParams,
    RangeEnsembleStrategy,
    ShortEnsembleParams,
    ShortEnsembleStrategy,
)
from app.engine.ose.strategy import (
    OseAllParams,
    OseAllStrategy,
    OseBollingerStrategy,
    OseEnvelopTrendStrategy,
    OsePriceChannelStrategy,
    OseRobotParams,
    OseRsiContrtrendStrategy,
    OseRsiTradeStrategy,
    OseSmaStochParams,
    OseSmaStochStrategy,
)
from app.engine.wave1 import (
    BollingerReclaimParams,
    BollingerReclaimStrategy,
    PullbackEmaParams,
    PullbackEmaStrategy,
    RsiReversalParams,
    RsiReversalStrategy,
    SqueezeBreakoutParams,
    SqueezeBreakoutStrategy,
    StochasticParams,
    StochasticStrategy,
    VwapReclaimParams,
    VwapReclaimStrategy,
)


@dataclass(frozen=True)
class MacdCrossParams:
    fast: int = 12
    slow: int = 26
    signal_period: int = 9


class MacdCrossStrategy:
    strategy_id = "macd_cross"
    version = "1.0.0"

    def __init__(self, params: MacdCrossParams | None = None):
        self.params = params or MacdCrossParams()
        p = self.params
        self._kf = 2 / (p.fast + 1)
        self._ks = 2 / (p.slow + 1)
        self._kg = 2 / (p.signal_period + 1)
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._ema_signal: float | None = None
        self._prev_hist: float | None = None
        self._count = 0

    def warmup_bars(self) -> int:
        return self.params.slow + self.params.signal_period

    def reset(self) -> None:
        """ENG-007: возврат в исходное состояние — повторный run() детерминирован."""
        self._ema_fast = None
        self._ema_slow = None
        self._ema_signal = None
        self._prev_hist = None
        self._count = 0

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        close = candles[-1].close
        self._count += 1

        if self._ema_fast is None:
            self._ema_fast = close
            self._ema_slow = close
        else:
            self._ema_fast = close * self._kf + self._ema_fast * (1 - self._kf)
            self._ema_slow = close * self._ks + self._ema_slow * (1 - self._ks)

        macd = self._ema_fast - self._ema_slow
        if self._count >= self.params.slow:
            if self._ema_signal is None:
                self._ema_signal = macd
            else:
                self._ema_signal = macd * self._kg + self._ema_signal * (1 - self._kg)

        hist = None if self._ema_signal is None else macd - self._ema_signal

        signal_out: Signal | None = None
        if self._prev_hist is not None and hist is not None:
            features = {"macd": macd, "signal": self._ema_signal, "hist": hist}
            if self._prev_hist <= 0 < hist:
                signal_out = Signal(
                    strategy_id=self.strategy_id,
                    side=Side.BUY,
                    time=candles[-1].ts,
                    reason="bullish_cross",
                    features=features,
                )
            elif self._prev_hist >= 0 > hist:
                signal_out = Signal(
                    strategy_id=self.strategy_id,
                    side=Side.SELL,
                    time=candles[-1].ts,
                    reason="bearish_cross",
                    features=features,
                )

        if hist is not None:
            self._prev_hist = hist
        return signal_out


@dataclass(frozen=True)
class DonchianParams:
    period: int = 20


class DonchianBreakoutStrategy:
    strategy_id = "donchian_breakout"
    version = "1.0.0"

    def __init__(self, params: DonchianParams | None = None):
        self.params = params or DonchianParams()

    def warmup_bars(self) -> int:
        return self.params.period + 1

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        n = len(candles)
        period = self.params.period
        if n < period + 1:
            return None
        window = candles[n - period - 1 : n - 1]
        highest = max(b.high for b in window)
        lowest = min(b.low for b in window)
        last = candles[-1]
        features = {"donchian_high": highest, "donchian_low": lowest}
        if last.close > highest:
            return Signal(
                strategy_id=self.strategy_id,
                side=Side.BUY,
                time=last.ts,
                reason="breakout_up",
                features=features,
            )
        if last.close < lowest:
            return Signal(
                strategy_id=self.strategy_id,
                side=Side.SELL,
                time=last.ts,
                reason="breakdown",
                features=features,
            )
        return None


@dataclass(frozen=True)
class VolumeDropParams:
    """V4: паника/набор на просадке (close<prev_close, объём ↑). SELL."""
    ma_len: int = 20
    drop_ratio: float = 1.5


@dataclass(frozen=True)
class VolumeClimaxParams:
    """V3: всплеск объёма + длинная тень. climax_short (нижняя тень)→BUY,
    climax_long (верхняя тень)→SELL. Симметрия из VOLUME_EXHAUSTION_2026.md §9 п.4."""
    ma_len: int = 20
    climax_ratio: float = 3.0
    wick_frac: float = 0.5


@dataclass(frozen=True)
class VolumeDivergenceParams:
    """V2/V5: цена новый экстремум, объём ниже среднего. V2(bear)→SELL, V5(bull)→BUY."""
    div_n: int = 20


class _VolumeBase:
    """Общий движок: скользящее окно объёмов и цен (без look-ahead)."""

    def __init__(self) -> None:
        self._volumes: deque[float] = deque()
        self._closes: deque[float] = deque()
        self._highs: deque[float] = deque()
        self._lows: deque[float] = deque()
        self._count = 0

    def reset(self) -> None:
        """ENG-007: сброс накопленного окна состояния."""
        self._volumes.clear()
        self._closes.clear()
        self._highs.clear()
        self._lows.clear()
        self._count = 0

    def _push(self, bar: Candle, maxlen: int) -> None:
        self._volumes.append(float(bar.volume or 0.0))
        self._closes.append(float(bar.close))
        self._highs.append(float(bar.high))
        self._lows.append(float(bar.low))
        if len(self._volumes) > maxlen:
            self._volumes.popleft()
            self._closes.popleft()
            self._highs.popleft()
            self._lows.popleft()
        self._count += 1

    def _ma_vol(self, n: int) -> float:
        w = list(self._volumes)[max(0, len(self._volumes) - n):]
        if not w:
            return 1.0
        return sum(w) / len(w)

    def _highs_n(self, n: int) -> float:
        return max(list(self._highs)[-n:]) if self._highs else 0.0

    def _lows_n(self, n: int) -> float:
        return min(list(self._lows)[-n:]) if self._lows else 0.0


class VolumeDropStrategy(_VolumeBase):
    strategy_id = "volume_drop"
    version = "1.0.0"

    def __init__(self, params: VolumeDropParams | None = None):
        super().__init__()
        self.params = params or VolumeDropParams()
        self._prev_close: float | None = None

    def warmup_bars(self) -> int:
        return self.params.ma_len + 2

    def reset(self) -> None:
        super().reset()
        self._prev_close = None

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        out: Signal | None = None
        if self._count >= self.params.ma_len + 1 and self._prev_close is not None and \
                bar.close < self._prev_close:
            vr = float(bar.volume or 0.0) / self._ma_vol(self.params.ma_len)
            if vr > self.params.drop_ratio:
                out = Signal(strategy_id=self.strategy_id, side=Side.SELL, time=bar.ts,
                             reason="volume_on_drop",
                             features={"vol_ratio": round(vr, 3)})
        self._push(bar, maxlen=self.params.ma_len + 1)
        self._prev_close = bar.close
        return out


class VolumeClimaxStrategy(_VolumeBase):
    strategy_id = "volume_climax"
    version = "1.0.0"

    def __init__(self, params: VolumeClimaxParams | None = None):
        super().__init__()
        self.params = params or VolumeClimaxParams()

    def warmup_bars(self) -> int:
        return self.params.ma_len + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        out: Signal | None = None
        if self._count >= self.params.ma_len + 1:
            vr = float(bar.volume or 0.0) / self._ma_vol(self.params.ma_len)
            rng = bar.high - bar.low
            if vr > self.params.climax_ratio and rng > 1e-9:
                upper = max(0.0, bar.high - max(bar.open, bar.close)) / rng
                lower = max(0.0, min(bar.open, bar.close) - bar.low) / rng
                if upper > self.params.wick_frac and bar.close >= bar.open:
                    out = Signal(strategy_id=self.strategy_id, side=Side.SELL,
                                 time=bar.ts, reason="climax_long",
                                 features={"vol_ratio": round(vr, 3)})
                elif lower > self.params.wick_frac and bar.close <= bar.open:
                    out = Signal(strategy_id=self.strategy_id, side=Side.BUY,
                                 time=bar.ts, reason="climax_short",
                                 features={"vol_ratio": round(vr, 3)})
        self._push(bar, maxlen=self.params.ma_len + 1)
        return out


class VolumeDivergenceStrategy(_VolumeBase):
    strategy_id = "volume_divergence"
    version = "1.0.0"

    def __init__(self, params: VolumeDivergenceParams | None = None):
        super().__init__()
        self.params = params or VolumeDivergenceParams()

    def warmup_bars(self) -> int:
        return self.params.div_n + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        bar = candles[-1]
        out: Signal | None = None
        n = self.params.div_n
        if self._count >= n + 1:
            avg = self._ma_vol(n)
            prev_high = self._highs_n(n)
            prev_low = self._lows_n(n)
            if avg > 0:
                if bar.high > prev_high and float(bar.volume or 0.0) < avg:
                    out = Signal(strategy_id=self.strategy_id, side=Side.SELL,
                                 time=bar.ts, reason="divergence_bear",
                                 features={"vol_avg": round(avg, 3)})
                elif bar.low < prev_low and float(bar.volume or 0.0) < avg:
                    out = Signal(strategy_id=self.strategy_id, side=Side.BUY,
                                 time=bar.ts, reason="divergence_bull",
                                 features={"vol_avg": round(avg, 3)})
        self._push(bar, maxlen=n + 1)
        return out


def _ema_last(values: Sequence[float], span: int) -> float | None:
    if len(values) < span:
        return None
    a = 2 / (span + 1)
    e = values[0]
    for v in values[1:]:
        e = v * a + e * (1 - a)
    return e


def _rsi_last(closes: Sequence[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0.0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0.0)) / period
    if al <= 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


@dataclass(frozen=True)
class TrendUpParams:
    """BUY только в восходящем тренде: цена>SMA(long), EMA(fast)>EMA(slow),
    пробой вверх канала Дончиана + подтверждение объёмом."""
    sma_long: int = 200
    ema_fast: int = 20
    ema_slow: int = 50
    donchian: int = 20
    vol_ma: int = 20
    vol_mult: float = 1.5


class TrendUpStrategy:
    strategy_id = "trend_up"
    version = "1.0.0"

    def __init__(self, params: TrendUpParams | None = None):
        self.params = params or TrendUpParams()

    def warmup_bars(self) -> int:
        p = self.params
        return max(p.sma_long, p.ema_slow, p.donchian, p.vol_ma) + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        p = self.params
        n = len(candles)
        if n < p.sma_long + 2:
            return None
        closes = [float(c.close) for c in candles]
        sma = sum(closes[-p.sma_long:]) / p.sma_long
        ema_f = _ema_last(closes, p.ema_fast)
        ema_s = _ema_last(closes, p.ema_slow)
        window = candles[n - p.donchian - 1: n - 1]
        highest = max(float(b.high) for b in window)
        vols = [float(b.volume or 0.0) for b in candles[-p.vol_ma:]]
        vol_avg = sum(vols) / len(vols)
        last = candles[-1]
        if (ema_f is not None and ema_s is not None and last.close > sma and ema_f > ema_s
                and last.close > highest and vol_avg > 0
                and float(last.volume or 0.0) > p.vol_mult * vol_avg):
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="uptrend_breakout",
                          features={"sma": round(sma, 4), "ema_f": round(ema_f, 4),
                                    "ema_s": round(ema_s, 4), "donchian_high": round(highest, 4),
                                    "vol_ratio": round(float(last.volume or 0.0) / vol_avg, 3)})
        return None


@dataclass(frozen=True)
class TrendDownParams:
    """SELL только в нисходящем тренде: цена<SMA(long), EMA(fast)<EMA(slow),
    пробой вниз канала Дончиана + подтверждение объёмом."""
    sma_long: int = 200
    ema_fast: int = 20
    ema_slow: int = 50
    donchian: int = 20
    vol_ma: int = 20
    vol_mult: float = 1.5


class TrendDownStrategy:
    strategy_id = "trend_down"
    version = "1.0.0"

    def __init__(self, params: TrendDownParams | None = None):
        self.params = params or TrendDownParams()

    def warmup_bars(self) -> int:
        p = self.params
        return max(p.sma_long, p.ema_slow, p.donchian, p.vol_ma) + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        p = self.params
        n = len(candles)
        if n < p.sma_long + 2:
            return None
        closes = [float(c.close) for c in candles]
        sma = sum(closes[-p.sma_long:]) / p.sma_long
        ema_f = _ema_last(closes, p.ema_fast)
        ema_s = _ema_last(closes, p.ema_slow)
        window = candles[n - p.donchian - 1: n - 1]
        lowest = min(float(b.low) for b in window)
        vols = [float(b.volume or 0.0) for b in candles[-p.vol_ma:]]
        vol_avg = sum(vols) / len(vols)
        last = candles[-1]
        if (ema_f is not None and ema_s is not None and last.close < sma and ema_f < ema_s
                and last.close < lowest and vol_avg > 0
                and float(last.volume or 0.0) > p.vol_mult * vol_avg):
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="downtrend_breakdown",
                          features={"sma": round(sma, 4), "ema_f": round(ema_f, 4),
                                    "ema_s": round(ema_s, 4), "donchian_low": round(lowest, 4),
                                    "vol_ratio": round(float(last.volume or 0.0) / vol_avg, 3)})
        return None


@dataclass(frozen=True)
class RangeReversionParams:
    """Mean reversion в боковике: узкий диапазон + RSI-экстремум + касание полос BB."""
    lookback: int = 20
    range_pct: float = 6.0     # (max-min)/close*100 < range_pct → боковик
    rsi_period: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bb_period: int = 20
    bb_k: float = 2.0


class RangeReversionStrategy:
    strategy_id = "range_reversion"
    version = "1.0.0"

    def __init__(self, params: RangeReversionParams | None = None):
        self.params = params or RangeReversionParams()

    def warmup_bars(self) -> int:
        p = self.params
        return max(p.lookback, p.rsi_period, p.bb_period) + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        p = self.params
        n = len(candles)
        if n < self.warmup_bars():
            return None
        window = candles[n - p.lookback: n]
        hi = max(float(b.high) for b in window)
        lo = min(float(b.low) for b in window)
        last = candles[-1]
        width_pct = (hi - lo) / float(last.close) * 100 if last.close else 0.0
        if width_pct > p.range_pct:
            return None  # не боковик
        closes = [float(c.close) for c in candles]
        rsi = _rsi_last(closes, p.rsi_period)
        if rsi is None:
            return None
        bb_win = closes[-p.bb_period:]
        mid = sum(bb_win) / len(bb_win)
        var = sum((x - mid) ** 2 for x in bb_win) / len(bb_win)
        sd = var ** 0.5
        lower, upper = mid - p.bb_k * sd, mid + p.bb_k * sd
        feat = {"rsi": round(rsi, 1), "bb_lower": round(lower, 4), "bb_upper": round(upper, 4),
                "range_pct": round(width_pct, 2)}
        if rsi <= p.rsi_oversold and last.close <= lower:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="range_oversold", features=feat)
        if rsi >= p.rsi_overbought and last.close >= upper:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="range_overbought", features=feat)
        return None


@dataclass(frozen=True)
class RsiTradeHubParams:
    rsi_length: int = 20
    upline: float = 65.0
    downline: float = 35.0


def _hub_rsi_incremental(owner, candles: Sequence[Candle], length: int) -> list:
    """Wilder-RSI (канон hub) с хвостовым досчётом: значения бит-в-бит как у
    indicatorhub._rsi, но на новом баре считаем один шаг, а не всю серию."""
    from app.engine.indicatorhub import _rsi as _hub_rsi
    n = len(candles)
    st = getattr(owner, "_hub_rsi_state", None)
    if (st is not None and st["n"] == n - 1 and st["len"] == length
            and st["ag"] is not None):
        prev = candles[n - 2].close
        diff = candles[n - 1].close - prev
        g = diff if diff > 0 else 0.0
        lo = -diff if diff < 0 else 0.0
        ag = (st["ag"] * (length - 1) + g) / length
        al = (st["al"] * (length - 1) + lo) / length
        val = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
        st["ag"], st["al"], st["n"] = ag, al, n
        st["series"].append(val)
        return st["series"]
    series = _hub_rsi(list(candles), length)
    ag = al = None
    if n >= length + 1:
        gains = [0.0] * n
        losses = [0.0] * n
        for i in range(1, n):
            d = candles[i].close - candles[i - 1].close
            if d > 0:
                gains[i] = d
            else:
                losses[i] = -d
        ag = sum(gains[1:length + 1]) / length
        al = sum(losses[1:length + 1]) / length
        for i in range(length + 1, n):
            ag = (ag * (length - 1) + gains[i]) / length
            al = (al * (length - 1) + losses[i]) / length
    owner._hub_rsi_state = {"n": n, "len": length, "ag": ag, "al": al, "series": series}
    return series


class RsiTradeHubStrategy:
    """Логика OsEngine RsiTrade на КАНОНИЧЕСКОМ Wilder-RSI (IndicatorHub).

    Отличие от порта ose_rsi_trade: там реплика Scripts/Rsi.cs
    (MovingAverageHard, буфер 20 баров, round(2)) — сигналы расходятся
    (на SBER 10м совпало 24 из 99 кроссоверов). Здесь канонический RSI
    проекта; сигналы — те же кресты (вверх через downline → BUY, вниз
    через upline → SELL), выход/реверс разруливает SignalPolicy раннера.
    """
    strategy_id = "rsi_trade_hub"
    version = "1.0.0"

    def __init__(self, params: RsiTradeHubParams | None = None):
        self.p = params or RsiTradeHubParams()

    def reset(self) -> None:
        self._hub_rsi_state = None

    def warmup_bars(self) -> int:
        return int(self.p.rsi_length) + 2

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        r = _hub_rsi_incremental(self, candles, int(self.p.rsi_length))
        if len(r) < 2 or r[-1] is None or r[-2] is None:
            return None
        prev, cur = r[-2], r[-1]
        last = candles[-1]
        if prev < self.p.downline < cur:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="rsi_up_down", features={"rsi": round(cur, 2)})
        if prev > self.p.upline > cur:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="rsi_dn_up", features={"rsi": round(cur, 2)})
        return None


@dataclass(frozen=True)
class EnvelopTrendHubParams:
    length: int = 10
    deviation: float = 0.3
    trail_stop: float = 0.1


class EnvelopTrendHubStrategy:
    """Логика OsEngine EnvelopTrend на КАНОНИЧЕСКИХ конвертах (IndicatorHub).

    Вход: касание полосы предыдущего закрытого бара (в порту — стоп-заявки по
    полосам, филл по цене активации). Выход: трейлинг-стоп от полосы со сдвигом
    trail_stop% — приходит exit-интентом (kind="exit"): раннер закроет позицию,
    на флэте такой сигнал игнорируется (как exit-голоса OSE-адаптера).
    Приоритет на баре: сначала выход-интент, затем вход (не терять закрытие).
    """
    strategy_id = "envelop_trend_hub"
    version = "1.0.0"

    def __init__(self, params: EnvelopTrendHubParams | None = None):
        self.p = params or EnvelopTrendHubParams()

    def reset(self) -> None:
        self._closes_cache = []

    def warmup_bars(self) -> int:
        return int(self.p.length) + 3

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        # Хвостовая формула канона (совпадает с indicatorhub._envelops для
        # индекса -2 бит-в-бит): полоса предыдущего закрытого бара = SMA(L)
        # от closes[-L-1:-1] ± deviation%. O(L) на бар; closes кэшируются
        # инкрементально (без пересборки списка на каждом баре).
        n = len(candles)
        closes = getattr(self, "_closes_cache", None)
        if closes is None or len(closes) != n - 1:
            closes = [c.close for c in candles[:-1]]
        else:
            closes = closes + [candles[n - 1].close]
        self._closes_cache = closes
        L = int(self.p.length)
        if len(closes) < L + 1:
            return None
        sma_prev = sum(closes[-L - 1:-1]) / L
        dev = float(self.p.deviation)
        up_prev = sma_prev + sma_prev * dev / 100.0
        dn_prev = sma_prev - sma_prev * dev / 100.0
        bar = candles[-1]
        # Вход — приоритетно: касание полосы предыдущего закрытого бара
        # (в порту эквивалент стоп-заявки с филлом по активации). Выход-интент
        # (трейлинг от полосы) — только как else: на флэте он игнорируется
        # раннером, но приоритет «выход вперёд» глушил бы все входы, потому что
        # в боковике trail-условие истинно почти на каждом баре.
        if bar.high >= up_prev:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=bar.ts,
                          reason="break_up", features={"up": round(up_prev, 4)})
        if bar.low <= dn_prev:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=bar.ts,
                          reason="break_down", features={"down": round(dn_prev, 4)})
        trail = float(self.p.trail_stop or 0.0)
        if trail > 0.0:
            if bar.low <= up_prev * (1.0 - trail / 100.0):
                return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=bar.ts,
                              reason="trail_long_exit", kind="exit",
                              features={"level": round(up_prev * (1.0 - trail / 100.0), 4)})
            if bar.high >= dn_prev * (1.0 + trail / 100.0):
                return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=bar.ts,
                              reason="trail_short_exit", kind="exit",
                              features={"level": round(dn_prev * (1.0 + trail / 100.0), 4)})
        return None


@dataclass(frozen=True)
class CanonEnsembleParams:
    members: str = "rsi_trade_hub"  # CSV strategy_id реестра (параметры — дефолтные)
    quorum: int = 1


class CanonEnsembleStrategy:
    """Ансамбль КАНОНИЧЕСКИХ стратегий реестра: голоса членов → кворум.

    Члены считаются на том же окне (build_strategy, дефолтные параметры).
    Вход: сторона набрала >= quorum голосов И больше противоположных (голосуют
    только kind != "exit"). Если вход не сложился — наружу пропускается первый
    exit-интент члена (kind="exit"): раннер закроет позицию, на флэте проигнорит.
    """
    strategy_id = "canon_ensemble"
    version = "1.0.0"

    def __init__(self, params: CanonEnsembleParams | None = None):
        self.p = params or CanonEnsembleParams()
        ids = [m.strip() for m in (self.p.members or "").split(",") if m.strip()]
        if not ids:
            raise ValueError("canon_ensemble: пустой список members")
        self._members = []
        for sid in ids:
            if sid == self.strategy_id:
                raise ValueError("canon_ensemble: самоссылка в members")
            if sid not in STRATEGY_REGISTRY:
                raise ValueError(f"canon_ensemble: неизвестная стратегия {sid!r}")
            self._members.append((sid, build_strategy(sid, None)))

    def reset(self) -> None:
        for _sid, m in self._members:
            r = getattr(m, "reset", None)
            if callable(r):
                r()

    def warmup_bars(self) -> int:
        return max((m.warmup_bars() for _sid, m in self._members), default=0) + 1

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        buy = sell = 0
        exit_sig = None
        for _sid, m in self._members:
            s = m.on_bar(candles)
            if s is None:
                continue
            if str(getattr(s, "kind", "entry") or "entry") == "exit":
                if exit_sig is None:
                    exit_sig = s
                continue
            if s.side is Side.BUY:
                buy += 1
            else:
                sell += 1
        q = max(1, int(self.p.quorum))
        if buy >= q and buy > sell:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=candles[-1].ts,
                          reason=f"canon_vote {buy}B/{sell}S q={q}",
                          features={"buy": buy, "sell": sell, "quorum": q})
        if sell >= q and sell > buy:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=candles[-1].ts,
                          reason=f"canon_vote {buy}B/{sell}S q={q}",
                          features={"buy": buy, "sell": sell, "quorum": q})
        if exit_sig is not None:
            return exit_sig
        return None


STRATEGY_REGISTRY: dict[str, type] = {
    "macd_cross": MacdCrossStrategy,
    "donchian_breakout": DonchianBreakoutStrategy,
    "rsi_reversal": RsiReversalStrategy,
    "stochastic": StochasticStrategy,
    "bollinger_reclaim": BollingerReclaimStrategy,
    "pullback_ema": PullbackEmaStrategy,
    "vwap_reclaim": VwapReclaimStrategy,
    "range_compression_breakout": SqueezeBreakoutStrategy,
    "volume_drop": VolumeDropStrategy,
    "volume_climax": VolumeClimaxStrategy,
    "volume_divergence": VolumeDivergenceStrategy,
    "trend_up": TrendUpStrategy,
    "trend_down": TrendDownStrategy,
    "range_reversion": RangeReversionStrategy,
    "rsi_trade_hub": RsiTradeHubStrategy,
    "envelop_trend_hub": EnvelopTrendHubStrategy,
    "canon_ensemble": CanonEnsembleStrategy,
    "long_ensemble": LongEnsembleStrategy,
    "short_ensemble": ShortEnsembleStrategy,
    "range_ensemble": RangeEnsembleStrategy,
    "hv_ensemble": HighVolatilityEnsembleStrategy,
    "neutral_ensemble": NeutralEnsembleStrategy,
    "momentum_1bar": Momentum1BarStrategy,
    "ensemble_vote": EnsembleVoteStrategy,
    "ose_all": OseAllStrategy,
    "ose_price_channel": OsePriceChannelStrategy,
    "ose_sma_stoch": OseSmaStochStrategy,
    "ose_envelop_trend": OseEnvelopTrendStrategy,
    "ose_rsi_contrtrend": OseRsiContrtrendStrategy,
    "ose_rsi_trade": OseRsiTradeStrategy,
    "ose_bollinger": OseBollingerStrategy,
}

_PARAMS_BY_STRATEGY: dict[str, type] = {
    "macd_cross": MacdCrossParams,
    "donchian_breakout": DonchianParams,
    "rsi_reversal": RsiReversalParams,
    "stochastic": StochasticParams,
    "bollinger_reclaim": BollingerReclaimParams,
    "pullback_ema": PullbackEmaParams,
    "vwap_reclaim": VwapReclaimParams,
    "range_compression_breakout": SqueezeBreakoutParams,
    "volume_drop": VolumeDropParams,
    "volume_climax": VolumeClimaxParams,
    "volume_divergence": VolumeDivergenceParams,
    "trend_up": TrendUpParams,
    "trend_down": TrendDownParams,
    "range_reversion": RangeReversionParams,
    "rsi_trade_hub": RsiTradeHubParams,
    "envelop_trend_hub": EnvelopTrendHubParams,
    "canon_ensemble": CanonEnsembleParams,
    "long_ensemble": LongEnsembleParams,
    "short_ensemble": ShortEnsembleParams,
    "range_ensemble": RangeEnsembleParams,
    "hv_ensemble": HighVolatilityParams,
    "neutral_ensemble": NeutralEnsembleParams,
    "momentum_1bar": Momentum1BarParams,
    "ensemble_vote": EnsembleVoteParams,
    "ose_all": OseAllParams,
    "ose_price_channel": OseRobotParams,
    "ose_sma_stoch": OseSmaStochParams,
    "ose_envelop_trend": OseRobotParams,
    "ose_rsi_contrtrend": OseRobotParams,
    "ose_rsi_trade": OseRobotParams,
    "ose_bollinger": OseRobotParams,
}


class ParamValidationError(ValueError):
    pass


def validate_params(strategy_id: str, params: dict | None) -> dict:
    from app.engine.catalog import STRATEGY_CATALOG

    card = STRATEGY_CATALOG.get(strategy_id)
    if card is None:
        raise ParamValidationError(f"unknown strategy: {strategy_id}")
    incoming = dict(params or {})
    validated: dict = {}
    for key, spec in card.params_schema.items():
        value = incoming.pop(key, spec.get("default"))
        if value is None:
            raise ParamValidationError(f"missing param {key}")
        expected = spec["type"]
        try:
            if expected == "int":
                value = int(value)
            elif expected == "float":
                value = float(value)
            else:
                value = str(value)
        except (TypeError, ValueError):
            raise ParamValidationError(f"param {key}: wrong type, expected {expected}")
        if "min" in spec and value < spec["min"]:
            raise ParamValidationError(f"param {key}={value} < min {spec['min']}")
        if "max" in spec and value > spec["max"]:
            raise ParamValidationError(f"param {key}={value} > max {spec['max']}")
        validated[key] = value
    if incoming:
        raise ParamValidationError(f"unknown params: {sorted(incoming)}")
    return validated


def build_strategy(strategy_id: str, params: dict | None):
    cls = STRATEGY_REGISTRY.get(strategy_id)
    if cls is None:
        raise ParamValidationError(f"unknown strategy: {strategy_id}")
    validated = validate_params(strategy_id, params)
    params_cls = _PARAMS_BY_STRATEGY[strategy_id]
    return cls(params_cls(**validated))
