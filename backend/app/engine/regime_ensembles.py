"""Ансамбли сигналов по режимам + управление выходом.

Реализация из docs/roadmap/TradeRegime.txt (шаг 2):
  4 ансамбля (TrendDown / Breakdown-Momentum / VolumeDown / VolStructureDown)
  → по 1 голосу от каждого → режим-зависимый кворум.

ПОКА РЕАЛИЗОВАНА ТОЛЬКО SHORT-СТОРОНА.
Long / NEUTRAL / RANGE и улучшенный выход из лонга — следующим шагом.

Всё каузально: признаки считаются по закрытым барам до последнего включительно.

Пометки по индикаторам (по рецензии):
  * RSI — классический Wilder (рекурсивное сглаживание).
  * ATR — SMA по True Range (не строгий Wilder-smoothing); для внутренней системы ок.
  * ADX — «ADX-like» (SMA по DX/+DI/−DI), не строгий Wilder; пороги калибровать.
  * BB — population std (деление на n), как в большинстве библиотек.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.engine.models import Candle, Side, Signal


# ============================================================
# Индикаторы (списки, без внешних зависимостей)
# ============================================================
def _sma(vals: Sequence[float], n: int) -> float | None:
    if n <= 0 or len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def _ema_series(vals: Sequence[float], n: int) -> list[float]:
    if not vals:
        return []
    a = 2 / (n + 1)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(a * float(v) + (1 - a) * out[-1])
    return out


def _rsi_last(closes: Sequence[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += d if d > 0 else 0.0
        losses += -d if d < 0 else 0.0
    ag, al = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + (d if d > 0 else 0.0)) / period
        al = (al * (period - 1) + (-d if d < 0 else 0.0)) / period
    if al <= 0:
        return 100.0
    rs = ag / al
    return 100 - 100 / (1 + rs)


def _tr(c: Candle, prev: Candle) -> float:
    return max(float(c.high) - float(c.low),
               abs(float(c.high) - float(prev.close)),
               abs(float(c.low) - float(prev.close)))


def _atr_series(candles: Sequence[Candle], period: int = 14) -> list[float]:
    if len(candles) < 2:
        return []
    trs = [_tr(candles[i], candles[i - 1]) for i in range(1, len(candles))]
    out = []
    for i in range(len(trs)):
        w = trs[max(0, i - period + 1): i + 1]
        out.append(sum(w) / len(w))
    return out


def _adx_series(candles: Sequence[Candle], period: int = 14) -> tuple[list[float], list[float], list[float]]:
    """Возвращает (adx, +di, -di) — простой Wilder-стиль (SMA по DX/DI)."""
    dx, di_p, di_m = [], [], []
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        up = float(cur.high) - float(prev.high)
        down = float(prev.low) - float(cur.low)
        plus_dm = up if (up > down and up > 0) else 0.0
        minus_dm = down if (down > up and down > 0) else 0.0
        tr = _tr(cur, prev)
        if tr <= 0:
            dx.append(0.0); di_p.append(0.0); di_m.append(0.0)
            continue
        dp = plus_dm / tr * 100
        dm = minus_dm / tr * 100
        s = dp + dm
        dx.append(abs(dp - dm) / s * 100 if s else 0.0)
        di_p.append(dp); di_m.append(dm)
    adx, p_out, m_out = [], [], []
    for i in range(len(dx)):
        w = dx[max(0, i - period + 1): i + 1]
        wp = di_p[max(0, i - period + 1): i + 1]
        wm = di_m[max(0, i - period + 1): i + 1]
        adx.append(sum(w) / len(w))
        p_out.append(sum(wp) / len(wp))
        m_out.append(sum(wm) / len(wm))
    return adx, p_out, m_out


def _bb_last(closes: Sequence[float], n: int = 20, k: float = 2.0):
    if len(closes) < n:
        return None, None, None
    w = list(closes[-n:])
    mid = sum(w) / n
    sd = (sum((x - mid) ** 2 for x in w) / n) ** 0.5
    return mid - k * sd, mid, mid + k * sd


def _bb_width_series(closes: Sequence[float], n: int = 20, k: float = 2.0) -> list[float]:
    out = []
    for i in range(n, len(closes) + 1):
        lo, mid, up = _bb_last(closes[:i], n, k)
        out.append((up - lo) / mid if mid else 0.0)
    return out


def _obv_series(closes: Sequence[float], volumes: Sequence[float]) -> list[float]:
    out = [0.0]
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            out.append(out[-1] + volumes[i])
        elif closes[i] < closes[i - 1]:
            out.append(out[-1] - volumes[i])
        else:
            out.append(out[-1])
    return out


def _cmf_last(candles: Sequence[Candle], n: int = 20) -> float | None:
    if len(candles) < n:
        return None
    mfv_sum = 0.0
    vol_sum = 0.0
    for c in candles[-n:]:
        hi, lo, cl, vol = float(c.high), float(c.low), float(c.close), float(c.volume or 0.0)
        rng = hi - lo
        mfm = ((cl - lo) - (hi - cl)) / rng if rng > 1e-12 else 0.0
        mfv_sum += mfm * vol
        vol_sum += vol
    return mfv_sum / vol_sum if vol_sum > 0 else 0.0


def _macd_last(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    if len(closes) < slow + signal:
        return None, None
    ef = _ema_series(closes, fast)
    es = _ema_series(closes, slow)
    macd = [a - b for a, b in zip(ef, es)]
    sig = _ema_series(macd, signal)
    return macd[-1], sig[-1]


# ============================================================
# Признаки (features) по последнему закрытому бару
# ============================================================
def compute_features(candles: Sequence[Candle]) -> dict:
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    vols = [float(c.volume or 0.0) for c in candles]

    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    ema20 = _ema_series(closes, 20)[-1] if len(closes) >= 20 else None
    ema50 = _ema_series(closes, 50)[-1] if len(closes) >= 50 else None
    adx, dip, dim = _adx_series(candles, 14)
    adx_val = adx[-1] if adx else None
    plus_di = dip[-1] if dip else None
    minus_di = dim[-1] if dim else None

    close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else close

    price_below_sma200 = (sma200 is not None) and close < sma200
    price_below_sma50 = (sma50 is not None) and close < sma50
    sma50_below_sma200 = (sma50 is not None and sma200 is not None and sma50 < sma200)
    ema20_below_ema50 = (ema20 is not None and ema50 is not None and ema20 < ema50)
    adx_downtrend = (adx_val is not None and plus_di is not None and minus_di is not None
                     and adx_val > 20 and minus_di > plus_di)

    # Пробойные / импульсные
    N = 20
    donchian_low = min(lows[-N - 1:-1]) if len(lows) >= N + 1 else None
    donchian_breakdown_down = (donchian_low is not None) and close < donchian_low
    low_N = min(lows[-N:]) if len(lows) >= N else None
    # float-safe «новый минимум»
    new_low_N = (low_N is not None) and (close <= low_N * 1.0000001)
    momentum_neg_N = len(closes) > N and ((close - closes[-N - 1]) / closes[-N - 1] < 0)
    rsi_val = _rsi_last(closes, 14)
    rsi_below_45 = (rsi_val is not None) and rsi_val < 45
    macd_line, signal_line = _macd_last(closes)
    macd_bearish = (macd_line is not None and signal_line is not None and macd_line < signal_line)
    lower_band, mid_band, upper_band = _bb_last(closes, 20, 2.0)
    bb_break_lower = (lower_band is not None) and close < lower_band

    # Объёмные
    vol_sma20 = _sma(vols, 20)
    rvol = (vols[-1] / vol_sma20) if (vol_sma20 and vol_sma20 > 0) else 0.0
    volume_surge_down = (close < prev_close) and (vol_sma20 is not None) and (vols[-1] > 1.5 * vol_sma20)
    rvol_down = (close < prev_close) and rvol > 1.5
    obv = _obv_series(closes, vols)
    obv_sma20 = _sma(obv, 20)
    obv_downtrend = (obv_sma20 is not None) and obv[-1] < obv_sma20
    cmf_val = _cmf_last(candles, 20)
    cmf_negative = (cmf_val is not None) and cmf_val < 0
    volume_rally_low = (close > prev_close) and (vol_sma20 is not None) and (vols[-1] < vol_sma20)

    # Волатильность + структура
    bbw = _bb_width_series(closes, 20, 2.0)
    bbw_sma = _sma(bbw, 60)
    bb_squeeze = (bbw and bbw_sma is not None and bbw[-1] < 0.9 * bbw_sma)
    bb_squeeze_down = bool(bb_squeeze) and bb_break_lower
    atr = _atr_series(candles, 14)
    atr_val = atr[-1] if atr else None
    atr_sma20 = _sma(atr, 20)
    atr_expansion_down = (atr_val is not None and atr_sma20 is not None
                          and atr_val > atr_sma20 and close < prev_close)
    M = 20
    if len(highs) >= M + 1 and len(lows) >= M + 1:
        hh_prev = max(highs[-M - 1:-1])
        ll_prev = min(lows[-M - 1:-1])
        lower_highs_lows = (highs[-1] < hh_prev) and (lows[-1] < ll_prev)
    else:
        lower_highs_lows = False

    return {
        "price_below_sma200": price_below_sma200,
        "price_below_sma50": price_below_sma50,
        "sma50_below_sma200": sma50_below_sma200,
        "ema20_below_ema50": ema20_below_ema50,
        "adx_downtrend": adx_downtrend,
        "donchian_breakdown_down": donchian_breakdown_down,
        "new_low_N": new_low_N,
        "momentum_neg_N": momentum_neg_N,
        "rsi_below_45": rsi_below_45,
        "macd_bearish": macd_bearish,
        "bb_break_lower": bb_break_lower,
        "volume_surge_down": volume_surge_down,
        "rvol_down": rvol_down,
        "obv_downtrend": obv_downtrend,
        "cmf_negative": cmf_negative,
        "volume_rally_low": volume_rally_low,
        "bb_squeeze_down": bb_squeeze_down,
        "atr_expansion_down": atr_expansion_down,
        "lower_highs_lows": lower_highs_lows,
        # значения для отладки
        "_rsi": rsi_val, "_adx": adx_val, "_rvol": rvol, "_cmf": cmf_val,
    }


# ============================================================
# 4 ансамбля (short) — каждый возвращает 0/1
# ============================================================
def ensemble_trend_down(f: dict) -> int:
    score = 0
    score += 1 if f["price_below_sma200"] else 0
    score += 1 if f["price_below_sma50"] else 0
    score += 1 if f["sma50_below_sma200"] else 0
    score += 1 if f["ema20_below_ema50"] else 0
    score += 2 if f["adx_downtrend"] else 0  # ADX критичен
    return 1 if score >= 3 else 0


def ensemble_breakdown_momentum(f: dict) -> int:
    breakout = (1 if f["donchian_breakdown_down"] else 0) + \
               (1 if f["new_low_N"] else 0) + (1 if f["bb_break_lower"] else 0)
    impulse = (1 if f["momentum_neg_N"] else 0) + \
              (1 if f["rsi_below_45"] else 0) + (1 if f["macd_bearish"] else 0)
    total = breakout + impulse
    return 1 if (breakout >= 1 and total >= 2) else 0


def ensemble_volume_down(f: dict) -> int:
    surge = (1 if f["volume_surge_down"] else 0) + (1 if f["rvol_down"] else 0)
    structure = (1 if f["obv_downtrend"] else 0) + (1 if f["cmf_negative"] else 0) + \
                (1 if f["volume_rally_low"] else 0)
    total = surge + structure
    return 1 if (surge >= 1 and total >= 2) else 0


def ensemble_vol_structure_down(f: dict) -> int:
    score = (1 if f["bb_squeeze_down"] else 0) + (1 if f["atr_expansion_down"] else 0) + \
            (2 if f["lower_highs_lows"] else 0)
    return 1 if score >= 3 else 0


# ============================================================
# Режим-зависимый кворум → итоговый short-сигнал
# ============================================================
def short_signal(candles: Sequence[Candle], regime: str, require_breakdown: bool = True):
    """Возвращает (signal: bool, votes: int, details: dict)."""
    f = compute_features(candles)
    t = ensemble_trend_down(f)
    b = ensemble_breakdown_momentum(f)
    v = ensemble_volume_down(f)
    s = ensemble_vol_structure_down(f)
    votes = t + b + v + s
    details = {"trend": t, "breakdown": b, "volume": v, "vol_struct": s}

    if regime == "TREND_UP":
        return False, votes, details  # в аптренде не шортим
    k = 2
    if regime == "HIGH_VOLATILITY":
        k = 3
    elif regime == "RANGE":
        k = 3
    elif regime == "NEUTRAL":
        k = 3
    if require_breakdown and b == 0:
        return False, votes, details
    return (votes >= k), votes, details


# ============================================================
# Выход из шорта (профили + трейлинг + тайм-аут)
# ============================================================
@dataclass
class ShortPosition:
    entry_price: float
    entry_bar: int
    initial_stop: float
    tp1: float
    tp2: float | None = None
    profile: str = "breakdown"     # trend | breakdown | mean_reversion
    current_stop: float | None = None
    highest_high: float = 0.0
    lowest_low: float = float("inf")
    tp1_hit: bool = False


def exit_short_signal(pos: ShortPosition, candles: Sequence[Candle], regime: str,
                      atr_mult_trail: float = 2.5, max_bars: int = 40):
    """Возвращает (exit: bool, reason: str, price: float|None)."""
    close = float(candles[-1].close)
    high = float(candles[-1].high)
    low = float(candles[-1].low)
    atr = _atr_series(candles, 14)
    atr_v = atr[-1] if atr else 0.0
    ema20 = _ema_series([float(c.close) for c in candles], 20)[-1]

    pos.highest_high = max(pos.highest_high, high)
    pos.lowest_low = min(pos.lowest_low, low)
    if pos.current_stop is None:
        pos.current_stop = pos.initial_stop

    # 1) стоп
    if close > pos.current_stop:
        return True, "stop_loss", pos.current_stop

    # 2) тейки
    if not pos.tp1_hit and close <= pos.tp1:
        pos.tp1_hit = True
        pos.current_stop = min(pos.current_stop, pos.entry_price)
        return True, "tp1", close
    if pos.tp2 is not None and close <= pos.tp2:
        return True, "tp2", close

    # 3) трейлинг по профилю
    if pos.profile == "trend":
        trail = pos.highest_high + atr_mult_trail * atr_v
        if trail < pos.current_stop:
            pos.current_stop = trail
        if close > ema20:
            return True, "ema_break", close
    elif pos.profile == "breakdown":
        trail = pos.highest_high + 2.0 * atr_v
        if trail < pos.current_stop:
            pos.current_stop = trail

    # 4) смена режима против шорта
    if regime == "TREND_UP":
        return True, "regime_change", close

    # 5) тайм-аут
    if len(candles) - pos.entry_bar >= max_bars:
        return True, "timeout", close

    return False, "hold", None


# ============================================================
# Стратегия-обёртка для движка (одна из 4-ансамблевых голосов)
# ============================================================
class ShortEnsembleStrategy:
    """Отдаёт SELL-сигнал, когда short_signal(candles, regime='TREND_DOWN')==True.

    Режим здесь фиксируется по умолчанию TREND_DOWN (стратегия включается в нужных
    режимах через regime_setups_filter движка).
    """
    strategy_id = "short_ensemble"
    version = "0.1.0"

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.regime = str(self.params.get("regime", "TREND_DOWN"))
        self.require_breakdown = bool(self.params.get("require_breakdown", True))
        self.atr_mult_trail = float(self.params.get("atr_mult_trail", 2.5))
        self.max_bars = int(self.params.get("max_bars", 40))

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        sig, votes, details = short_signal(candles, self.regime, self.require_breakdown)
        if sig:
            return Signal(strategy_id=self.strategy_id, side=Side.SELL,
                          time=candles[-1].ts, reason="short_ensemble",
                          features={"votes": votes, "regime": self.regime, **details})
        return None


# ============================================================
# LONG-СТОРОНА (зеркало short)
# ============================================================
def compute_features_long(candles: Sequence[Candle]) -> dict:
    closes = [float(c.close) for c in candles]
    highs = [float(c.high) for c in candles]
    lows = [float(c.low) for c in candles]
    vols = [float(c.volume or 0.0) for c in candles]

    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    ema20 = _ema_series(closes, 20)[-1] if len(closes) >= 20 else None
    ema50 = _ema_series(closes, 50)[-1] if len(closes) >= 50 else None
    adx, dip, dim = _adx_series(candles, 14)
    adx_val = adx[-1] if adx else None
    plus_di = dip[-1] if dip else None
    minus_di = dim[-1] if dim else None

    close = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else close

    # Трендовые up
    price_above_sma200 = (sma200 is not None) and close > sma200
    price_above_sma50 = (sma50 is not None) and close > sma50
    sma50_above_sma200 = (sma50 is not None and sma200 is not None and sma50 > sma200)
    ema20_above_ema50 = (ema20 is not None and ema50 is not None and ema20 > ema50)
    adx_uptrend = (adx_val is not None and plus_di is not None and minus_di is not None
                   and adx_val > 20 and plus_di > minus_di)

    # Пробойные / импульсные up
    N = 20
    donchian_high = max(highs[-N - 1:-1]) if len(highs) >= N + 1 else None
    donchian_breakout_up = (donchian_high is not None) and close > donchian_high
    high_N = max(highs[-N:]) if len(highs) >= N else None
    new_high_N = (high_N is not None) and (close >= high_N * 0.9999999)
    momentum_pos_N = len(closes) > N and ((close - closes[-N - 1]) / closes[-N - 1] > 0)
    rsi_val = _rsi_last(closes, 14)
    rsi_above_55 = (rsi_val is not None) and rsi_val > 55
    macd_line, signal_line = _macd_last(closes)
    macd_bullish = (macd_line is not None and signal_line is not None and macd_line > signal_line)
    lower_band, mid_band, upper_band = _bb_last(closes, 20, 2.0)
    bb_break_upper = (upper_band is not None) and close > upper_band

    # Объёмные up
    vol_sma20 = _sma(vols, 20)
    rvol = (vols[-1] / vol_sma20) if (vol_sma20 and vol_sma20 > 0) else 0.0
    volume_surge_up = (close > prev_close) and (vol_sma20 is not None) and (vols[-1] > 1.5 * vol_sma20)
    rvol_up = (close > prev_close) and rvol > 1.5
    obv = _obv_series(closes, vols)
    obv_sma20 = _sma(obv, 20)
    obv_uptrend = (obv_sma20 is not None) and obv[-1] > obv_sma20
    cmf_val = _cmf_last(candles, 20)
    cmf_positive = (cmf_val is not None) and cmf_val > 0
    volume_pullback_low = (close < prev_close) and (vol_sma20 is not None) and (vols[-1] < vol_sma20)

    # Волатильность + структура up
    bbw = _bb_width_series(closes, 20, 2.0)
    bbw_sma = _sma(bbw, 60)
    bb_squeeze = (bbw and bbw_sma is not None and bbw[-1] < 0.9 * bbw_sma)
    bb_squeeze_up = bool(bb_squeeze) and bb_break_upper
    atr = _atr_series(candles, 14)
    atr_val = atr[-1] if atr else None
    atr_sma20 = _sma(atr, 20)
    atr_expansion_up = (atr_val is not None and atr_sma20 is not None
                        and atr_val > atr_sma20 and close > prev_close)
    M = 20
    if len(highs) >= M + 1 and len(lows) >= M + 1:
        hh_prev = max(highs[-M - 1:-1])
        ll_prev = min(lows[-M - 1:-1])
        higher_highs_lows = (highs[-1] > hh_prev) and (lows[-1] > ll_prev)
    else:
        higher_highs_lows = False

    return {
        "price_above_sma200": price_above_sma200,
        "price_above_sma50": price_above_sma50,
        "sma50_above_sma200": sma50_above_sma200,
        "ema20_above_ema50": ema20_above_ema50,
        "adx_uptrend": adx_uptrend,
        "donchian_breakout_up": donchian_breakout_up,
        "new_high_N": new_high_N,
        "momentum_pos_N": momentum_pos_N,
        "rsi_above_55": rsi_above_55,
        "macd_bullish": macd_bullish,
        "bb_break_upper": bb_break_upper,
        "volume_surge_up": volume_surge_up,
        "rvol_up": rvol_up,
        "obv_uptrend": obv_uptrend,
        "cmf_positive": cmf_positive,
        "volume_pullback_low": volume_pullback_low,
        "bb_squeeze_up": bb_squeeze_up,
        "atr_expansion_up": atr_expansion_up,
        "higher_highs_lows": higher_highs_lows,
        "_rsi": rsi_val, "_adx": adx_val, "_rvol": rvol, "_cmf": cmf_val,
    }


def ensemble_trend_up(f: dict) -> int:
    score = 0
    score += 1 if f["price_above_sma200"] else 0
    score += 1 if f["price_above_sma50"] else 0
    score += 1 if f["sma50_above_sma200"] else 0
    score += 1 if f["ema20_above_ema50"] else 0
    score += 2 if f["adx_uptrend"] else 0
    return 1 if score >= 3 else 0


def ensemble_breakout_momentum(f: dict) -> int:
    breakout = (1 if f["donchian_breakout_up"] else 0) + \
               (1 if f["new_high_N"] else 0) + (1 if f["bb_break_upper"] else 0)
    impulse = (1 if f["momentum_pos_N"] else 0) + \
              (1 if f["rsi_above_55"] else 0) + (1 if f["macd_bullish"] else 0)
    total = breakout + impulse
    return 1 if (breakout >= 1 and total >= 2) else 0


def ensemble_volume_up(f: dict) -> int:
    surge = (1 if f["volume_surge_up"] else 0) + (1 if f["rvol_up"] else 0)
    structure = (1 if f["obv_uptrend"] else 0) + (1 if f["cmf_positive"] else 0) + \
                (1 if f["volume_pullback_low"] else 0)
    total = surge + structure
    return 1 if (surge >= 1 and total >= 2) else 0


def ensemble_vol_structure_up(f: dict) -> int:
    score = (1 if f["bb_squeeze_up"] else 0) + (1 if f["atr_expansion_up"] else 0) + \
            (2 if f["higher_highs_lows"] else 0)
    return 1 if score >= 3 else 0


def long_signal(candles: Sequence[Candle], regime: str, require_breakout: bool = True):
    """Возвращает (signal: bool, votes: int, details: dict)."""
    f = compute_features_long(candles)
    t = ensemble_trend_up(f)
    b = ensemble_breakout_momentum(f)
    v = ensemble_volume_up(f)
    s = ensemble_vol_structure_up(f)
    votes = t + b + v + s
    details = {"trend": t, "breakout": b, "volume": v, "vol_struct": s}

    if regime == "TREND_DOWN":
        return False, votes, details  # в даунтренде не лонгуем
    k = 2
    if regime == "HIGH_VOLATILITY":
        k = 3
    elif regime == "RANGE":
        k = 3
    elif regime == "NEUTRAL":
        k = 3
    if require_breakout and b == 0:
        return False, votes, details
    return (votes >= k), votes, details


@dataclass
class LongPosition:
    entry_price: float
    entry_bar: int
    initial_stop: float
    tp1: float
    tp2: float | None = None
    profile: str = "breakout"      # trend | breakout | mean_reversion
    current_stop: float | None = None
    highest_high: float = 0.0
    lowest_low: float = float("inf")
    tp1_hit: bool = False


def exit_long_signal(pos: LongPosition, candles: Sequence[Candle], regime: str,
                     atr_mult_trail: float = 2.5, max_bars: int = 40):
    """Возвращает (exit: bool, reason: str, price: float|None)."""
    close = float(candles[-1].close)
    high = float(candles[-1].high)
    low = float(candles[-1].low)
    atr = _atr_series(candles, 14)
    atr_v = atr[-1] if atr else 0.0
    ema20 = _ema_series([float(c.close) for c in candles], 20)[-1]

    pos.highest_high = max(pos.highest_high, high)
    pos.lowest_low = min(pos.lowest_low, low)
    if pos.current_stop is None:
        pos.current_stop = pos.initial_stop

    # 1) стоп
    if close < pos.current_stop:
        return True, "stop_loss", pos.current_stop

    # 2) тейки
    if not pos.tp1_hit and close >= pos.tp1:
        pos.tp1_hit = True
        pos.current_stop = max(pos.current_stop, pos.entry_price)
        return True, "tp1", close
    if pos.tp2 is not None and close >= pos.tp2:
        return True, "tp2", close

    # 3) трейлинг по профилю
    if pos.profile == "trend":
        trail = pos.lowest_low - atr_mult_trail * atr_v
        if trail > pos.current_stop:
            pos.current_stop = trail
        if close < ema20:
            return True, "ema_break", close
    elif pos.profile == "breakout":
        trail = pos.lowest_low - 2.0 * atr_v
        if trail > pos.current_stop:
            pos.current_stop = trail

    # 4) смена режима против лонга
    if regime == "TREND_DOWN":
        return True, "regime_change", close

    # 5) тайм-аут
    if len(candles) - pos.entry_bar >= max_bars:
        return True, "timeout", close

    return False, "hold", None


class LongEnsembleStrategy:
    """Отдаёт BUY-сигнал, когда long_signal(candles, regime='TREND_UP')==True."""
    strategy_id = "long_ensemble"
    version = "0.1.0"

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.regime = str(self.params.get("regime", "TREND_UP"))
        self.require_breakout = bool(self.params.get("require_breakout", True))
        self.atr_mult_trail = float(self.params.get("atr_mult_trail", 2.5))
        self.max_bars = int(self.params.get("max_bars", 40))

    def warmup_bars(self) -> int:
        return 210

    def on_bar(self, candles: Sequence[Candle]) -> Signal | None:
        if len(candles) < self.warmup_bars():
            return None
        sig, votes, details = long_signal(candles, self.regime, self.require_breakout)
        if sig:
            return Signal(strategy_id=self.strategy_id, side=Side.BUY,
                          time=candles[-1].ts, reason="long_ensemble",
                          features={"votes": votes, "regime": self.regime, **details})
        return None
