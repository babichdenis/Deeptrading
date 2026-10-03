from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Sequence

from app.engine.ensemble_v2 import EnsembleVoteParams, EnsembleVoteStrategy
from app.engine.models import Candle, Side, Signal
from app.engine.ose.strategy import (
    OseAllParams,
    OseAllStrategy,
    OseBbPowerStrategy,
    OseBollingerReversStrategy,
    OseBollingerStrategy,
    OseBollingerTrailingStrategy,
    OseCciTradeStrategy,
    OseEnvelopTrendStrategy,
    OseMacdReversStrategy,
    OseMacdTrailStrategy,
    OsePcVolatilityStrategy,
    OsePriceChannelStrategy,
    OseRobotParams,
    OseRsiContrtrendStrategy,
    OseRsiTradeStrategy,
    OseRviTradeStrategy,
    OseSmaStochParams,
    OseSmaStochStrategy,
    OseSmaTrendStrategy,
)
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
        self.reset()  # латентные state-поля (не были инициализированы — баг Signal Lab 2026-10-03)

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
class RsiMtfHubParams:
    rsi_length: int = 20       # RSI на ТФ входа
    upline: float = 65.0
    downline: float = 35.0
    bias_length: int = 20      # RSI старшего ТФ (детектор режима/направления)
    bias_tf_min: int = 60      # старший ТФ в минутах (1h)
    bias_gap: float = 2.0      # мёртвая зона вокруг 50


class _RsiStateShim:
    """Носитель отдельного RSI-стейта для _hub_rsi_incremental (второй RSI)."""


class RsiMtfHubStrategy:
    """RsiTrade + старший ТФ: RSI(1h) — детектор направления, входы 10m по нему.

    Bias считается по ЗАКРЫТЫМ старшим барам (агрегируются из бара входа,
    границы бакетов по эпохе — как Resampler; look-ahead нет):
      RSI_h > 50+gap → разрешены только LONG-кресты 10m;
      RSI_h < 50−gap → только SHORT-кресты;
      мёртвая зона   → входов нет.
    Выход/реверс разруливает SignalPolicy раннера (как rsi_trade_hub).
    """
    strategy_id = "rsi_mtf_hub"
    version = "1.0.0"

    def __init__(self, params: RsiMtfHubParams | None = None):
        self.p = params or RsiMtfHubParams()
        self.reset()

    def reset(self) -> None:
        self._hub_rsi_state = None
        self._bias_owner = _RsiStateShim()
        self._bias_bars: list[Candle] = []
        self._bkt_ts = None
        self._bkt = None  # {"o","h","l","c","v"} текущего (незакрытого) старшего бакета

    def warmup_bars(self) -> int:
        per = max(1, int(self.p.bias_tf_min) // 10)
        return (int(self.p.bias_length) + 2) * per + int(self.p.rsi_length) + 2

    def _close_bucket(self) -> None:
        if self._bkt is not None and self._bkt_ts is not None:
            self._bias_bars.append(Candle(
                ts=self._bkt_ts, open=self._bkt["o"], high=self._bkt["h"],
                low=self._bkt["l"], close=self._bkt["c"], volume=self._bkt["v"]))
        self._bkt = None

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        last = candles[-1]
        bmin = max(1, int(self.p.bias_tf_min))
        ep = int(last.ts.timestamp()) // 60
        b0 = (ep // bmin) * bmin
        b0_ts = datetime.fromtimestamp(b0 * 60, tz=last.ts.tzinfo or UTC)
        if self._bkt_ts is None or b0_ts != self._bkt_ts:
            self._close_bucket()
            self._bkt_ts = b0_ts
            self._bkt = {"o": last.open, "h": last.high, "l": last.low,
                         "c": last.close, "v": float(last.volume or 0)}
        else:
            self._bkt["h"] = max(self._bkt["h"], last.high)
            self._bkt["l"] = min(self._bkt["l"], last.low)
            self._bkt["c"] = last.close
            self._bkt["v"] += float(last.volume or 0)
        bias = None
        if len(self._bias_bars) >= int(self.p.bias_length) + 1:
            rb = _hub_rsi_incremental(self._bias_owner, self._bias_bars, int(self.p.bias_length))
            bias = rb[-1] if rb else None
        r = _hub_rsi_incremental(self, candles, int(self.p.rsi_length))
        if len(r) < 2 or r[-1] is None or r[-2] is None or bias is None:
            return None
        prev, cur = r[-2], r[-1]
        long_ok = bias >= 50.0 + float(self.p.bias_gap)
        short_ok = bias <= 50.0 - float(self.p.bias_gap)
        if prev < self.p.downline < cur and long_ok:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="rsi_mtf_up", features={"rsi": round(cur, 2), "bias": round(bias, 2)})
        if prev > self.p.upline > cur and short_ok:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="rsi_mtf_dn", features={"rsi": round(cur, 2), "bias": round(bias, 2)})
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
        self.reset()  # латентные state-поля (не были инициализированы — баг Signal Lab 2026-10-03)

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
class WilliamsRangeHubParams:
    wr_length: int = 14        # Period WilliamsRange
    upline: float = -20.0      # Overbought (C# default)
    downline: float = -80.0    # Oversold (C# default)


class WilliamsRangeHubStrategy:
    """Логика OsEngine WilliamsRangeTrade (CounterTrend) на каноническом %R.

    %R = -100*(HH-C)/(HH-LL) за окно wr_length, round(2) — Scripts/WilliamsRange.cs
    (в C# на прогреве 0, здесь None — семантика канона, как у rsi_trade_hub).
    Вход — момент входа в зону (крест уровня): state-условие C#
    (_lastWr < downline) спамило бы сигнал каждый бар, крест = первый бар в зоне:
      %R пересёк downline сверху вниз → BUY; %R пересёк upline снизу вверх → SELL.
    Выход/реверс разруливает SignalPolicy раннера (как rsi_trade_hub).
    """
    strategy_id = "williams_range_hub"
    version = "1.0.0"

    def __init__(self, params: WilliamsRangeHubParams | None = None):
        self.reset()  # латентные state-поля (не были инициализированы — баг Signal Lab 2026-10-03)
        self.p = params or WilliamsRangeHubParams()

    def reset(self) -> None:
        self._prev_wr = None

    def warmup_bars(self) -> int:
        return int(self.p.wr_length) + 2

    @staticmethod
    def _wr_tail(candles: Sequence[Candle], length: int) -> float | None:
        n = len(candles)
        if length <= 0 or n < length:
            return None
        window = candles[n - length:]
        hh = max(c.high for c in window)
        ll = min(c.low for c in window)
        if hh == ll:
            return 0.0
        return round(-100.0 * (hh - candles[-1].close) / (hh - ll), 2)

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        L = int(self.p.wr_length)
        if len(candles) < L + 2:
            return None
        cur = self._wr_tail(candles, L)
        prev = self._wr_tail(candles[:-1], L)
        if cur is None or prev is None:
            return None
        self._prev_wr = cur
        last = candles[-1]
        if prev >= self.p.downline > cur:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="wr_enter_oversold", features={"wr": cur})
        if prev <= self.p.upline < cur:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="wr_enter_overbought", features={"wr": cur})
        return None


@dataclass(frozen=True)
class MomentumMacdHubParams:
    momentum_length: int = 5   # Length Momentum, точка Close, ×100 (C# default)
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9


def _hub_macd_incremental(owner, candles: Sequence[Candle],
                          fast: int, slow: int, signal: int):
    """Инкрементальный MACD по канону IndicatorHub._macd: state на owner,
    добирает только новые бары; возврат (macd_line, signal_line) последнего
    бара или (None, None), пока ряд не созрел (нужно slow+signal-1 баров)."""
    state = getattr(owner, "_hub_macd_state", None)
    n = len(candles)
    if state is None or state["n"] > n or state["k"] != (fast, slow, signal):
        state = {"n": 0, "k": (fast, slow, signal), "ef": None, "es": None,
                 "vals": [], "sig": None, "macd_last": None}
        owner._hub_macd_state = state
    af, asl = 2.0 / (fast + 1), 2.0 / (slow + 1)
    sg = 2.0 / (signal + 1)
    for i in range(state["n"], n):
        c = float(candles[i].close)
        if i == fast - 1:
            state["ef"] = sum(float(x.close) for x in candles[:fast]) / fast
        elif i >= fast:
            state["ef"] = c * af + state["ef"] * (1 - af)
        if i == slow - 1:
            state["es"] = sum(float(x.close) for x in candles[:slow]) / slow
        elif i >= slow:
            state["es"] = c * asl + state["es"] * (1 - asl)
        if state["ef"] is not None and state["es"] is not None:
            m = state["ef"] - state["es"]
            vals = state["vals"]
            vals.append(m)
            if len(vals) > signal:
                del vals[0]
            if len(vals) == signal and state["sig"] is None:
                state["sig"] = sum(vals) / signal
            elif state["sig"] is not None:
                state["sig"] = m * sg + state["sig"] * (1 - sg)
            state["macd_last"] = m
        state["n"] = i + 1
    return state["macd_last"], state["sig"]


class MomentumMacdHubStrategy:
    """Логика OsEngine MomentumMacd (Trend) на канонических MACD и Momentum.

    C#: Buy — MACD-линия > сигнальной И Momentum(Close, len) > 100;
    Sell — зеркально. Условия state-овые → сигнал на переход условия
    из False в True (иначе спам каждый бар).
    Выход/реверс — SignalPolicy раннера (как rsi_trade_hub).
    """
    strategy_id = "momentum_macd_hub"
    version = "1.0.0"

    def __init__(self, params: MomentumMacdHubParams | None = None):
        self.p = params or MomentumMacdHubParams()
        self.reset()

    def reset(self) -> None:
        self._hub_macd_state = None
        self._prev_long = None
        self._prev_short = None

    def warmup_bars(self) -> int:
        return max(int(self.p.macd_slow) + int(self.p.macd_signal) - 1,
                   int(self.p.momentum_length) + 1) + 1

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        p = self.p
        L = int(p.momentum_length)
        if len(candles) < max(int(p.macd_slow), L + 1) + 1:
            return None
        macd, sig = _hub_macd_incremental(self, candles, int(p.macd_fast),
                                          int(p.macd_slow), int(p.macd_signal))
        if macd is None or sig is None:
            return None
        div = candles[-1 - L].close
        mom = float(candles[-1].close) / float(div) * 100.0 if div else 0.0
        long_now = macd > sig and mom > 100.0
        short_now = macd < sig and mom < 100.0
        was_long, was_short = self._prev_long, self._prev_short
        self._prev_long, self._prev_short = long_now, short_now
        last = candles[-1]
        feat = {"mom": round(mom, 4), "macd": round(macd, 8), "signal": round(sig, 8)}
        if long_now and not was_long:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="mom_macd_long", features=feat)
        if short_now and not was_short:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="mom_macd_short", features=feat)
        return None


@dataclass(frozen=True)
class ParabolicSarHubParams:
    af: float = 0.02       # Parabolic Af (C# default)
    max_af: float = 0.2    # Parabolic Max Af (C# default)


def _psar_step(candles: Sequence[Candle], i: int, up: bool, ep: float, cur: float,
               accel: float, af: float, max_af: float):
    """Один шаг Wilder Parabolic SAR (канон _parabolic_sar в indicatorhub)."""
    cur = cur + accel * (ep - cur)
    if up:
        lo1 = float(candles[i - 1].low)
        cur = min(cur, lo1, float(candles[i - 2].low) if i >= 2 else lo1)
        if cur > float(candles[i].low):
            return False, float(candles[i].low), ep, af
        if float(candles[i].high) > ep:
            ep, accel = float(candles[i].high), min(accel + af, max_af)
    else:
        hi1 = float(candles[i - 1].high)
        cur = max(cur, hi1, float(candles[i - 2].high) if i >= 2 else hi1)
        if cur < float(candles[i].high):
            return True, float(candles[i].high), ep, af
        if float(candles[i].low) < ep:
            ep, accel = float(candles[i].low), min(accel + af, max_af)
    return up, ep, cur, accel


def _hub_psar_trend(owner, candles: Sequence[Candle], af: float, max_af: float):
    """Инкрементальный Parabolic SAR: state на owner, добирает новые бары.
    Возврат (trend, sar) последнего закрытого бара или (None, None).
    Реплей (len меньше обработанного) → полный пересчёт с нуля."""
    state = getattr(owner, "_hub_psar_state", None)
    n = len(candles)
    if state is None or state["n"] > n or state["k"] != (af, max_af):
        state = {"n": 1, "k": (af, max_af), "up": None, "ep": 0.0,
                 "cur": 0.0, "accel": af}
        owner._hub_psar_state = state
    if n < 2:
        return None, None
    if state["up"] is None:
        up = candles[1].close >= candles[0].close
        state.update(up=up,
                     ep=float(candles[0].high if up else candles[0].low),
                     cur=float(candles[0].low if up else candles[0].high),
                     accel=af, n=1)
    for i in range(max(state["n"], 1), n):
        up, ep, cur, accel = _psar_step(candles, i, state["up"], state["ep"],
                                        state["cur"], state["accel"], af, max_af)
        state.update(up=up, ep=ep, cur=cur, accel=accel, n=i + 1)
    return (1.0 if state["up"] else -1.0), state["cur"]


class ParabolicSarHubStrategy:
    """Логика OsEngine ParabolicSarTrade (Trend) на каноническом SAR.

    C#: Buy — цена > SAR, Sell — цена < SAR (state-условие) → сигнал на флип
    тренда SAR (крест), иначе спам каждый бар.
    Выход/реверс — SignalPolicy раннера (как rsi_trade_hub).
    """
    strategy_id = "parabolic_sar_hub"
    version = "1.0.0"

    def __init__(self, params: ParabolicSarHubParams | None = None):
        self.p = params or ParabolicSarHubParams()
        self.reset()

    def reset(self) -> None:
        self._hub_psar_state = None
        self._prev_trend = None

    def warmup_bars(self) -> int:
        return 3

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        trend, sar = _hub_psar_trend(self, candles,
                                     float(self.p.af), float(self.p.max_af))
        if trend is None:
            return None
        prev, self._prev_trend = self._prev_trend, trend
        if prev is None:
            return None
        last = candles[-1]
        if prev < 0 < trend:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=last.ts,
                          reason="psar_flip_up", features={"sar": round(sar, 6)})
        if prev > 0 > trend:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=last.ts,
                          reason="psar_flip_down", features={"sar": round(sar, 6)})
        return None


@dataclass(frozen=True)
class PriceChannelHubParams:
    length_up: int = 21
    length_down: int = 21


class PriceChannelHubStrategy:
    """Логика OsEngine PriceChannelTrade (Trend) на каноническом канале.

    Канал читается со сдвигом (уровень предыдущего бара, как в ose-порте
    [-2]): High текущего > верх канала за length_up предыдущих баров → BUY;
    Low < низ → SELL. Бар, пробивший обе стороны, входа не даёт. Условие
    state-овое → сигнал на крест (предыдущий бар не пробивал), иначе спам
    каждый бар на тренде. Выход/реверс — SignalPolicy раннера.
    """
    strategy_id = "price_channel_hub"
    version = "1.0.0"

    def __init__(self, params: PriceChannelHubParams | None = None):
        self.p = params or PriceChannelHubParams()

    def reset(self) -> None:
        pass

    def warmup_bars(self) -> int:
        return max(int(self.p.length_up), int(self.p.length_down)) + 2

    @staticmethod
    def _channel_tail(candles: Sequence[Candle], length: int,
                      end: int) -> tuple[float | None, float | None]:
        """max High / min Low за length баров до end включительно."""
        if length <= 0 or end + 1 < length:
            return None, None
        window = candles[end + 1 - length:end + 1]
        return max(c.high for c in window), min(c.low for c in window)

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        lu, ld = int(self.p.length_up), int(self.p.length_down)
        n = len(candles)
        if n < max(lu, ld) + 2:
            return None
        i = n - 1
        up_prev, _ = self._channel_tail(candles, lu, i - 1)
        _, down_prev = self._channel_tail(candles, ld, i - 1)
        up_pp, _ = self._channel_tail(candles, lu, i - 2)
        _, down_pp = self._channel_tail(candles, ld, i - 2)
        if None in (up_prev, down_prev, up_pp, down_pp):
            return None
        cur, prev = candles[i], candles[i - 1]
        broke_up = cur.high > up_prev
        broke_down = cur.low < down_prev
        if broke_up and not broke_down and prev.high <= up_pp:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          reason="pc_break_up",
                          features={"ch_up": round(up_prev, 6),
                                    "ch_down": round(down_prev, 6)})
        if broke_down and not broke_up and prev.low >= down_pp:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          reason="pc_break_down",
                          features={"ch_up": round(up_prev, 6),
                                    "ch_down": round(down_prev, 6)})
        return None


def _hub_parabolic(owner, candles: Sequence[Candle], key: str,
                   up_rows: list, down_rows: list,
                   averaging: int, mult: float, vol_days: float = 1.0,
                   touch_eq: bool = False):
    """Инкрементальная параболическая линия P — общая для ParabolicBollinger
    и ParabolicPriceChannel (OsEngine *_indicator.cs).

    Волатильность бара = (max High - min Low) за vol_days суток до бара;
    шаг P = среднее последних averaging значений (в оригинале volMult
    применяется в среднем второй раз — квирк сохранён). Пробой верхней
    границы → P перескакивает к нижней и растёт, пробой нижней → к верхней
    и убывает, всегда внутри границ. Возвращает (up, down, p, p_prev)
    последнего бара; p/p_prev = None, пока границ нет."""
    from datetime import timedelta
    state = getattr(owner, key, None)
    n = len(candles)
    if state is None or state["n"] > n or state["k"] != (averaging, mult, vol_days, touch_eq):
        state = {"n": 0, "k": (averaging, mult, vol_days, touch_eq),
                 "p": None, "below": None, "vols": [], "prow": []}
        setattr(owner, key, state)
    for i in range(state["n"], n):
        c = candles[i]
        up_i, down_i = up_rows[i], down_rows[i]
        if up_i is None or down_i is None:
            state.update(p=None, below=None, n=i + 1)
            state["prow"].append(None)
            continue
        t = c.ts - timedelta(days=vol_days)
        hi, lo = c.high, c.low
        j = i - 1
        while j >= 0 and candles[j].ts >= t:
            if candles[j].high > hi:
                hi = candles[j].high
            if candles[j].low < lo:
                lo = candles[j].low
            j -= 1
        vols = state["vols"]
        vols.append((hi - lo) * mult)
        if len(vols) > averaging:
            del vols[0]
        if len(vols) <= averaging:
            avg = vols[-1]
        else:  # квирк C#: среднее значений, УЖЕ умноженных на mult, снова ×mult
            avg = sum(v * mult for v in vols[-averaging:]) / averaging
        below = state["below"]
        if touch_eq:
            if c.high >= up_i:
                below = True
            if c.low <= down_i:
                below = False
        else:
            if c.high > up_i:
                below = True
            if c.low < down_i:
                below = False
        change = below != state["below"]
        if below:
            p = down_i if (change or state["p"] is None) \
                else min(max(state["p"] + avg, down_i), up_i)
        else:
            p = up_i if (change or state["p"] is None) \
                else min(max(state["p"] - avg, down_i), up_i)
        state.update(p=p, below=below, n=i + 1)
        state["prow"].append(p)
    prow = state["prow"]
    p_prev = prow[-2] if len(prow) >= 2 else None
    return up_rows[n - 1], down_rows[n - 1], state["p"], p_prev


@dataclass(frozen=True)
class ParabolicBollingerHubParams:
    bb_length: int = 28
    deviation: float = 2.0
    averaging: int = 15
    vol_mult: float = 0.2


class ParabolicBollingerHubStrategy:
    """Логика OsEngine ParabolicBollinger (Trend) на канонических полосах.

    BB(bb_length, deviation) + параболическая линия P (_hub_parabolic).
    C#: BuyAtStop на верхней границе (close < up), SellAtStop на нижней —
    при P строго внутри полос → канон: крест касания границы (high >= up
    впервые после бара без касания) при P предыдущего бара внутри полос.
    Std: делитель length-1 при length > 30, иначе length (квирк C#).
    Выход — трейлинг по P в C#; здесь отдаётся раннеру (SignalPolicy).
    """
    strategy_id = "parabolic_bollinger_hub"
    version = "1.0.0"

    def __init__(self, params: ParabolicBollingerHubParams | None = None):
        self.p = params or ParabolicBollingerHubParams()
        self.reset()

    def reset(self) -> None:
        self._pb_state = None
        self._bb_cache = None

    def warmup_bars(self) -> int:
        return int(self.p.bb_length) + 3

    def _bb_rows(self, candles: Sequence[Candle]) -> tuple[list, list]:
        """Ряды границ BB; кэш дополняется новыми барами (BB детерминирован)."""
        L, dev = int(self.p.bb_length), float(self.p.deviation)
        n = len(candles)
        cache = self._bb_cache
        if cache is None or cache[0] > n:
            cache = (0, [], [])
        rows_n, up, down = cache
        for i in range(rows_n, n):
            if i + 1 < L:
                up.append(None)
                down.append(None)
                continue
            window = [float(c.close) for c in candles[i + 1 - L:i + 1]]
            sma = sum(window) / L
            div = (L - 1) if L > 30 else L
            sd = (sum((x - sma) ** 2 for x in window) / div) ** 0.5
            up.append(round(sma + sd * dev, 6))
            down.append(round(sma - sd * dev, 6))
        self._bb_cache = (n, up, down)
        return up, down

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        p = self.p
        if len(candles) < int(p.bb_length) + 2:
            return None
        up_rows, down_rows = self._bb_rows(candles)
        up_i, down_i, _par, par_prev = _hub_parabolic(
            self, candles, "_pb_state", up_rows, down_rows,
            int(p.averaging), float(p.vol_mult), 1.0, touch_eq=False)
        up_prev, down_prev = up_rows[-2], down_rows[-2]
        if None in (up_i, down_i, par_prev, up_prev, down_prev):
            return None
        if not (up_prev > par_prev > down_prev):
            return None
        cur, prev = candles[-1], candles[-2]
        if cur.high >= up_i and prev.high < up_prev and prev.close < up_prev:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          reason="pb_touch_up",
                          features={"up": round(up_i, 6), "down": round(down_i, 6),
                                    "p_prev": round(par_prev, 6)})
        if cur.low <= down_i and prev.low > down_prev and prev.close > down_prev:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          reason="pb_touch_down",
                          features={"up": round(up_i, 6), "down": round(down_i, 6),
                                    "p_prev": round(par_prev, 6)})
        return None


@dataclass(frozen=True)
class ParabolicPriceChannelHubParams:
    length_up: int = 21
    length_down: int = 21
    averaging: int = 15
    vol_mult: float = 0.1


class ParabolicPriceChannelHubStrategy:
    """Логика OsEngine ParabolicPriceChannel (Trend) на каноническом канале.

    Канал max/min за length баров + параболическая линия P (_hub_parabolic,
    touch_eq: пробой по >=/<=, как в C#-индикаторе). C#: BuyAtStop на верхней
    границе (close <= up), SellAtStop на нижней — при P строго внутри канала
    → канон: крест касания границы при P предыдущего бара внутри канала.
    Выход — трейлинг по P в C#; здесь отдаётся раннеру (SignalPolicy).
    """
    strategy_id = "parabolic_price_channel_hub"
    version = "1.0.0"

    def __init__(self, params: ParabolicPriceChannelHubParams | None = None):
        self.p = params or ParabolicPriceChannelHubParams()
        self.reset()

    def reset(self) -> None:
        self._ppc_state = None
        self._pc_cache = None

    def warmup_bars(self) -> int:
        return max(int(self.p.length_up), int(self.p.length_down)) + 3

    def _channel_rows(self, candles: Sequence[Candle]) -> tuple[list, list]:
        lu, ld = int(self.p.length_up), int(self.p.length_down)
        n = len(candles)
        cache = self._pc_cache
        if cache is None or cache[0] > n:
            cache = (0, [], [])
        rows_n, up, down = cache
        for i in range(rows_n, n):
            if i + 1 < max(lu, ld):
                up.append(None)
                down.append(None)
                continue
            up.append(max(c.high for c in candles[i + 1 - lu:i + 1]))
            down.append(min(c.low for c in candles[i + 1 - ld:i + 1]))
        self._pc_cache = (n, up, down)
        return up, down

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        up_rows, down_rows = self._channel_rows(candles)
        up_i, down_i, _par, par_prev = _hub_parabolic(
            self, candles, "_ppc_state", up_rows, down_rows,
            int(self.p.averaging), float(self.p.vol_mult), 1.0, touch_eq=True)
        up_prev, down_prev = up_rows[-2], down_rows[-2]
        if None in (up_i, down_i, par_prev, up_prev, down_prev):
            return None
        if not (up_prev > par_prev > down_prev):
            return None
        cur, prev = candles[-1], candles[-2]
        if cur.high >= up_i and prev.high < up_prev and prev.close <= up_prev:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          reason="ppc_touch_up",
                          features={"ch_up": round(up_i, 6), "ch_down": round(down_i, 6),
                                    "p_prev": round(par_prev, 6)})
        if cur.low <= down_i and prev.low > down_prev and prev.close >= down_prev:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          reason="ppc_touch_down",
                          features={"ch_up": round(up_i, 6), "ch_down": round(down_i, 6),
                                    "p_prev": round(par_prev, 6)})
        return None


@dataclass(frozen=True)
class BreakLrChannelHubParams:
    lr_period: int = 50          # Period Linear Regression
    up_deviation: float = 1.0    # Deviation LR (верхняя граница)
    down_deviation: float = 1.0  # Deviation LR (нижняя граница)
    sma_length: int = 100        # Sma Length Filter
    sma_position_filter: bool = False  # Is SMA Filter On (цена не с той стороны SMA)
    sma_slope_filter: bool = False     # Is Sma Slope Filter On (наклон против входа)


class BreakLrChannelHubStrategy:
    """Логика OsEngine BreakLinearRegressionChannel (Trend).

    Канал линейной регрессии по Close за lr_period: центр — МНК-прямая
    (b = (n·Σxy − Σx·Σy)/(n·Σx² − (Σx)²), a = (Σy − Σx·b)/n), шум = среднее
    |Close − линия| за окно (в оригинале делится на период, не на √n —
    квирк сохранён), границы = центр ± deviation·шум. Вход state-крестом:
    Close > upper → BUY, Close < lower → SELL (в оригинале вход каждый бар,
    пока условие и flat — здесь сигнал на первый бар условия). Фильтры
    SMA(sma_length), каждый со своим флагом: position — Close на «неверной»
    стороне SMA блокирует; slope — SMA наклонена против входа блокирует.
    Выход: лонг — стоп на нижней границе уровня предыдущего бара (аналог
    CloseAtStop, поставленного прошлым обработчиком), шорт — на верхней →
    kind="exit". При одновременном входном кресте и стопе приоритет у
    входа (противоположный вход разворачивает позицию политикой раннера);
    режимы OnlyLong/OnlyShort и объёмы — на стороне раннера.
    """
    strategy_id = "break_lr_channel_hub"
    version = "1.0.0"

    def __init__(self, params: BreakLrChannelHubParams | None = None):
        self.p = params or BreakLrChannelHubParams()
        self.reset()  # латентные state-поля (не были инициализированы — баг Signal Lab 2026-10-03)

    def reset(self) -> None:
        self._prev_buy = False
        self._prev_sell = False

    def warmup_bars(self) -> int:
        return max(int(self.p.lr_period), int(self.p.sma_length)) + 3

    def _channel(self, candles: Sequence[Candle], end: int):
        """(upper, lower) границы LRC на баре end; None — окно не готово."""
        n = int(self.p.lr_period)
        if n <= 1 or end + 1 < n:
            return None, None
        ys = [float(c.close) for c in candles[end + 1 - n:end + 1]]
        sx = n * (n - 1) / 2.0
        sxx = (n - 1) * n * (2 * n - 1) / 6.0
        sy = sum(ys)
        sxy = sum(x * y for x, y in enumerate(ys))
        den = n * sxx - sx * sx
        if den == 0:
            return None, None
        b = (n * sxy - sx * sy) / den
        a = (sy - sx * b) / n
        central = a + b * (n - 1)
        se = sum(abs(y - (a + b * x)) for x, y in enumerate(ys)) / n
        return (central + float(self.p.up_deviation) * se,
                central - float(self.p.down_deviation) * se)

    def _sma(self, candles: Sequence[Candle], end: int) -> float | None:
        n = int(self.p.sma_length)
        if end + 1 < n:
            return None
        return sum(float(c.close) for c in candles[end + 1 - n:end + 1]) / n

    def _filtered(self, candles: Sequence[Candle], i: int, for_buy: bool) -> bool:
        cur = candles[i]
        sma = self._sma(candles, i)
        if sma is None:
            return False
        if self.p.sma_position_filter:
            if for_buy and sma > float(cur.close):
                return True
            if not for_buy and sma < float(cur.close):
                return True
        if self.p.sma_slope_filter:
            prev = self._sma(candles, i - 1)
            if prev is not None:
                if for_buy and sma < prev:
                    return True
                if not for_buy and sma > prev:
                    return True
        return False

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        i = len(candles) - 1
        cur = candles[i]
        up, down = self._channel(candles, i)          # вход — канал текущего бара
        up_ex, down_ex = self._channel(candles, i - 1)  # стоп — уровень пред. бара
        if up is None or up_ex is None:
            return None
        buy_cond = float(cur.close) > up
        sell_cond = float(cur.close) < down
        buy_sig = buy_cond and not self._prev_buy and not self._filtered(candles, i, for_buy=True)
        sell_sig = sell_cond and not self._prev_sell and not self._filtered(candles, i, for_buy=False)
        self._prev_buy, self._prev_sell = buy_cond, sell_cond
        feat = {"upper": round(up, 6), "lower": round(down, 6)}
        if buy_sig:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          reason="blrc_break_up", features=feat)
        if sell_sig:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          reason="blrc_break_down", features=feat)
        # exit-поток: касание противоположной границы (стоп-семантика)
        if cur.low <= down_ex:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          kind="exit", reason="blrc_stop_down", features=feat)
        if cur.high >= up_ex:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          kind="exit", reason="blrc_stop_up", features=feat)
        return None


@dataclass(frozen=True)
class BillWilliamsHubParams:
    jaw_length: int = 40    # AlligatorSlowLineLength
    teeth_length: int = 10  # AlligatorMiddleLineLength
    lips_length: int = 3    # AlligatorFastLineLength
    jaw_shift: int = 8
    teeth_shift: int = 5
    lips_shift: int = 3


class BillWilliamsHubStrategy:
    """Логика OsEngine StrategyBillWilliams (Trend), без доливов.

    Alligator = SSMA (seed SMA(length), далее (prev·(n−1)+c)/n — Ssma.cs)
    с длинами lips/teeth/jaw 3/10/40 и сдвигами 3/5/8: значение бара i
    читается с i−shift; серия робота: up=Lips, middle=Teeth, down=Jaw.
    Fractal 5-баровый со строгими сравнениями, значение публикуется на
    центре (j), доступно с j+2; берётся последний ненулевой. Вход BUY на
    кресте state-условия: Close строго выше Lips, Teeth И Jaw одновременно
    и выше последнего верхнего фрактала (AO в оригинале участвует только в
    доливах той же стороны — в single-position движок не переносится);
    SELL зеркально. Выход: лонг при Close < Teeth, шорт при Close > Teeth
    → kind="exit" на переходе условия (иначе спам каждый бар). При
    одновременном входе и выходе приоритет у входа.
    """
    strategy_id = "bill_williams_hub"
    version = "1.0.0"

    def __init__(self, params: BillWilliamsHubParams | None = None):
        self.p = params or BillWilliamsHubParams()
        self.reset()

    def reset(self) -> None:
        self._prev_buy = False
        self._prev_sell = False
        self._prev_exit_sell = False
        self._prev_exit_buy = False
        self._ssma_state: dict[int, dict] = {}

    def warmup_bars(self) -> int:
        return int(self.p.jaw_length) + int(self.p.jaw_shift) + 6

    def _ssma_point(self, closes: list[float], length: int, upto: int) -> float | None:
        """SSMA OsEngine (Ssma.cs) на баре upto; None до готовности."""
        if length <= 0:
            return None
        st = self._ssma_state.setdefault(length, {"n": 0, "val": None})
        while st["n"] <= upto and st["n"] < len(closes):
            j = st["n"]
            if j == length:
                # OsEngine-квирк Ssma.cs: сид на index == length по барам 1..length (бар 0 пропущен)
                st["val"] = sum(closes[j - length + 1:j + 1]) / length
            elif j > length:
                st["val"] = (st["val"] * (length - 1) + closes[j]) / length
            st["n"] += 1
        return st["val"] if st["n"] > upto and st["val"] is not None else None

    @staticmethod
    def _last_fractal(candles: Sequence[Candle], i: int, up: bool) -> float | None:
        """Последний ненулевой 5-баровый фрактал (центр j ≤ i−2, строго)."""
        for j in range(i - 2, 1, -1):
            if up:
                v = float(candles[j].high)
                if (v > float(candles[j - 1].high) and v > float(candles[j - 2].high)
                        and v > float(candles[j + 1].high) and v > float(candles[j + 2].high)):
                    return v
            else:
                v = float(candles[j].low)
                if (v < float(candles[j - 1].low) and v < float(candles[j - 2].low)
                        and v < float(candles[j + 1].low) and v < float(candles[j + 2].low)):
                    return v
        return None

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        i = len(candles) - 1
        cur = candles[i]
        closes = [float(c.close) for c in candles]
        lips = self._ssma_point(closes, int(self.p.lips_length), i - int(self.p.lips_shift))
        teeth = self._ssma_point(closes, int(self.p.teeth_length), i - int(self.p.teeth_shift))
        jaw = self._ssma_point(closes, int(self.p.jaw_length), i - int(self.p.jaw_shift))
        f_up = self._last_fractal(candles, i, up=True)
        f_dn = self._last_fractal(candles, i, up=False)
        if None in (lips, teeth, jaw) or f_up is None or f_dn is None:
            return None
        px = float(cur.close)
        lines = {"lips": round(lips, 6), "teeth": round(teeth, 6), "jaw": round(jaw, 6)}
        buy_ok = px > lips and px > teeth and px > jaw and px > f_up
        sell_ok = px < lips and px < teeth and px < jaw and px < f_dn
        buy_sig = buy_ok and not self._prev_buy
        sell_sig = sell_ok and not self._prev_sell
        self._prev_buy, self._prev_sell = buy_ok, sell_ok
        if buy_sig:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          reason="bw_alligator_fractal_up",
                          features={**lines, "fractal_up": round(f_up, 6)})
        if sell_sig:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          reason="bw_alligator_fractal_down",
                          features={**lines, "fractal_down": round(f_dn, 6)})
        # exit-поток: лонг при price < Teeth, шорт при price > Teeth (на переходе)
        exit_sell = px < teeth
        exit_buy = px > teeth
        es = exit_sell and not self._prev_exit_sell
        eb = exit_buy and not self._prev_exit_buy
        self._prev_exit_sell, self._prev_exit_buy = exit_sell, exit_buy
        if es:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          kind="exit", reason="bw_close_below_teeth",
                          features={"teeth": round(teeth, 6)})
        if eb:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          kind="exit", reason="bw_close_above_teeth",
                          features={"teeth": round(teeth, 6)})
        return None


@dataclass(frozen=True)
class TwoTimeFramesHubParams:
    pc_length: int = 20    # PC length
    sma_length: int = 30   # Sma length
    big_tf_min: int = 60   # старший ТФ в минутах (в оригинале — вторая вкладка)


class TwoTimeFramesHubStrategy:
    """Логика OsEngine TwoTimeFramesBot (Trend, long-only).

    Вход BUY на кресте state-условия: Close > верх PriceChannel уровня
    предыдущего бара (в оригинале PC DataSeries Values[Count−2]) И close
    последнего ЗАКРЫТОГО бара старшего ТФ > SMA(sma_length) по закрытым
    барам старшего ТФ (агрегация бакетами по эпохе, look-ahead нет — как
    rsi_mtf_hub). Выход: Close < низ PC → kind="exit" SELL (в оригинале
    CloseAtMarket). Шортов нет — long-only (Regime On/Off в оригинале);
    big_tf_min — параметризованный аналог второй вкладки. Уровни PC читают
    окно до предыдущего бара включительно, текущий бар в канал не входит.
    """
    strategy_id = "two_timeframes_hub"
    version = "1.0.0"

    def __init__(self, params: TwoTimeFramesHubParams | None = None):
        self.p = params or TwoTimeFramesHubParams()
        self.reset()

    def reset(self) -> None:
        self._prev_buy = False
        self._bkt_ts = None
        self._bkt_close: float | None = None
        self._big_closes: list[float] = []

    def warmup_bars(self) -> int:
        per = max(1, int(self.p.big_tf_min) // 5)
        return (int(self.p.sma_length) + 1) * per + int(self.p.pc_length) + 2

    @staticmethod
    def _pc_level(candles: Sequence[Candle], length: int, end: int,
                  up: bool) -> float | None:
        if length <= 0 or end + 1 < length:
            return None
        window = candles[end + 1 - length:end + 1]
        if up:
            return float(max(c.high for c in window))
        return float(min(c.low for c in window))

    def _big_tail(self, candles: Sequence[Candle]) -> None:
        last = candles[-1]
        bmin = max(1, int(self.p.big_tf_min))
        ep = int(last.ts.timestamp()) // 60
        b0 = (ep // bmin) * bmin
        b0_ts = datetime.fromtimestamp(b0 * 60, tz=last.ts.tzinfo or UTC)
        if self._bkt_ts is None or b0_ts != self._bkt_ts:
            if self._bkt_close is not None:
                self._big_closes.append(self._bkt_close)
            self._bkt_ts = b0_ts
        self._bkt_close = float(last.close)

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        n = len(candles)
        if n < int(self.p.pc_length) + 2:
            return None
        i = n - 1
        cur = candles[i]
        up = self._pc_level(candles, int(self.p.pc_length), i - 1, up=True)
        down = self._pc_level(candles, int(self.p.pc_length), i - 1, up=False)
        if up is None or down is None:
            return None
        self._big_tail(candles)
        sl = int(self.p.sma_length)
        if sl <= 0 or len(self._big_closes) < sl:
            return None
        big_close = self._big_closes[-1]
        big_sma = sum(self._big_closes[-sl:]) / sl
        if big_close <= 0 or big_sma <= 0:
            return None
        feat = {"pc_up": round(up, 6), "pc_down": round(down, 6),
                "big_close": round(big_close, 6), "big_sma": round(big_sma, 6)}
        buy_ok = float(cur.close) > up and big_close > big_sma
        buy_sig = buy_ok and not self._prev_buy
        self._prev_buy = buy_ok
        if buy_sig:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY, time=cur.ts,
                          reason="ttf_pc_break_bigtf_up", features=feat)
        if float(cur.close) < down:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL, time=cur.ts,
                          kind="exit", reason="ttf_pc_down", features=feat)
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
    "rsi_mtf_hub": RsiMtfHubStrategy,
    "envelop_trend_hub": EnvelopTrendHubStrategy,
    "williams_range_hub": WilliamsRangeHubStrategy,
    "momentum_macd_hub": MomentumMacdHubStrategy,
    "parabolic_sar_hub": ParabolicSarHubStrategy,
    "price_channel_hub": PriceChannelHubStrategy,
    "parabolic_bollinger_hub": ParabolicBollingerHubStrategy,
    "parabolic_price_channel_hub": ParabolicPriceChannelHubStrategy,
    "break_lr_channel_hub": BreakLrChannelHubStrategy,
    "bill_williams_hub": BillWilliamsHubStrategy,
    "two_timeframes_hub": TwoTimeFramesHubStrategy,
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
    "ose_cci_trade": OseCciTradeStrategy,
    "ose_bb_power": OseBbPowerStrategy,
    "ose_rvi_trade": OseRviTradeStrategy,
    "ose_macd_revers": OseMacdReversStrategy,
    "ose_macd_trail": OseMacdTrailStrategy,
    "ose_bollinger_revers": OseBollingerReversStrategy,
    "ose_bollinger_trailing": OseBollingerTrailingStrategy,
    "ose_sma_trend": OseSmaTrendStrategy,
    "ose_pc_volatility": OsePcVolatilityStrategy,
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
    "rsi_mtf_hub": RsiMtfHubParams,
    "envelop_trend_hub": EnvelopTrendHubParams,
    "williams_range_hub": WilliamsRangeHubParams,
    "momentum_macd_hub": MomentumMacdHubParams,
    "parabolic_sar_hub": ParabolicSarHubParams,
    "price_channel_hub": PriceChannelHubParams,
    "parabolic_bollinger_hub": ParabolicBollingerHubParams,
    "parabolic_price_channel_hub": ParabolicPriceChannelHubParams,
    "break_lr_channel_hub": BreakLrChannelHubParams,
    "bill_williams_hub": BillWilliamsHubParams,
    "two_timeframes_hub": TwoTimeFramesHubParams,
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
    "ose_cci_trade": OseRobotParams,
    "ose_bb_power": OseRobotParams,
    "ose_rvi_trade": OseRobotParams,
    "ose_macd_revers": OseRobotParams,
    "ose_macd_trail": OseRobotParams,
    "ose_bollinger_revers": OseRobotParams,
    "ose_bollinger_trailing": OseRobotParams,
    "ose_sma_trend": OseRobotParams,
    "ose_pc_volatility": OseRobotParams,
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
        except (TypeError, ValueError) as err:
            raise ParamValidationError(f"param {key}: wrong type, expected {expected}") from err
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
