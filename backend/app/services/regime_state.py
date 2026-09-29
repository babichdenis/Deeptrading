"""L2.3 — инкрементальное состояние режима (OsEngine-style).

Дословный перенос математики RegimeDetector.compute в по-барное обновление:
те же EMA (alpha-сид первым значением — НЕ indicators.ema!), тот же ATR
(скользящее среднее TR с частичными окнами в начале — НЕ AtrState!), тот же
ADX, процентиль (окно 200), vol_ratio (окно 50 без текущего), дрейф за 6,
консистентность за 7, то же дерево решений и те же округления фич.

Состояние двигается ТОЛЬКО по закрытым барам. Forming-бар оценивается через
clone() (снапшот скаляров/деков, дёшево), оригинал не меняется.

Тонкости паритета (проверены тестами tests/test_regime_state.py):
- batch заполняет atr_pct[0] значением TR_1, и оно участвует в процентилях
  первых 200 баров — здесь hist[0] проставляется задним числом при приходе
  второго бара (на выходные строки не влияет: там warmup, features=None).
- warmup-строки имеют features=None — как в batch.
- Нормализация Decimal→float — на входе update(), как в batch.
"""
from __future__ import annotations

from bisect import bisect_left, insort
from collections import deque

from app.services.regime import RegimeDetector


class _AlphaEma:
    """EMA regime-варианта: seed = первое значение. Совпадает с _ema()."""

    __slots__ = ("alpha", "value")

    def __init__(self, span: int):
        self.alpha = 2.0 / (span + 1)
        self.value: float | None = None

    def update(self, x: float) -> float:
        if self.value is None:
            self.value = float(x)
        else:
            self.value = self.alpha * float(x) + (1.0 - self.alpha) * self.value
        return self.value


class RegimeState:
    """Персистентное состояние детектора режима для одной TF-серии."""

    def __init__(self, **detector_kwargs):
        self._kwargs = dict(detector_kwargs)
        self.det = RegimeDetector(**detector_kwargs)
        d = self.det
        self.warmup = d.ema_slow + d.atr_period
        self._n = 0
        self._prev = None  # предыдущий бар (для TR/DX)
        self._ema_f = _AlphaEma(d.ema_fast)
        self._ema_s = _AlphaEma(d.ema_slow)
        self._ema_sp = _AlphaEma(d.ema_slope)
        self._ema_sp_hist: deque[float] = deque(maxlen=6)
        self._tr_window: deque[float] = deque()
        self._dx_window: deque[float] = deque()
        self._atr_hist: list[float] = []     # atr_pct по барам (для процентиля)
        self._atr_sorted: list[float] = []   # сортированное окно ≤ window
        self._vol_hist: deque[float] = deque(maxlen=50)  # объёмы БЕЗ текущего
        # закрытий держим с запасом под drift_bars/cons_bars из параметров
        self._close_keep = max(d.drift_bars, d.cons_bars) + 1
        self._closes: deque[float] = deque(maxlen=self._close_keep)
        self.rows: list[dict] = []

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        return {
            "n": self._n,
            "prev": None if self._prev is None else dict(
                high=self._prev[0], low=self._prev[1], close=self._prev[2]),
            "ema_f": self._ema_f.value, "ema_s": self._ema_s.value,
            "ema_sp": self._ema_sp.value,
            "ema_sp_hist": list(self._ema_sp_hist),
            "tr_window": list(self._tr_window),
            "dx_window": list(self._dx_window),
            "atr_hist": list(self._atr_hist), "atr_sorted": list(self._atr_sorted),
            "vol_hist": list(self._vol_hist), "closes": list(self._closes),
            "rows": [dict(r) for r in self.rows],
        }

    def restore(self, snap: dict) -> "RegimeState":
        self._n = snap["n"]
        p = snap["prev"]
        self._prev = None if p is None else (p["high"], p["low"], p["close"])
        self._ema_f.value = snap["ema_f"]
        self._ema_s.value = snap["ema_s"]
        self._ema_sp.value = snap["ema_sp"]
        self._ema_sp_hist = deque(snap["ema_sp_hist"], maxlen=6)
        self._tr_window = deque(snap["tr_window"])
        self._dx_window = deque(snap["dx_window"])
        self._atr_hist = list(snap["atr_hist"])
        self._atr_sorted = list(snap["atr_sorted"])
        self._vol_hist = deque(snap["vol_hist"], maxlen=50)
        self._closes = deque(snap["closes"], maxlen=self._close_keep)
        self.rows = [dict(r) for r in snap["rows"]]
        return self

    def clone(self) -> "RegimeState":
        return RegimeState(**self._kwargs).restore(self.snapshot())

    # ---------------------------------------------------------------- update
    def update(self, bar) -> dict:
        """Скормить закрытый бар. Вернуть его regime-строку (как batch)."""
        d = self.det
        ts = bar.ts
        o = float(bar.open)
        h = float(bar.high)
        lo = float(bar.low)
        c = float(bar.close)
        v = float(bar.volume or 0)
        i = self._n

        ef = self._ema_f.update(c)
        self._ema_s.update(c)
        esp = self._ema_sp.update(c)
        self._ema_sp_hist.append(esp)

        if self._prev is not None:
            ph, pl, pc = self._prev
            tr = max(h - lo, abs(h - pc), abs(lo - pc))
            self._tr_window.append(tr)
            if len(self._tr_window) > d.atr_period:
                self._tr_window.popleft()
            # сумма — срезом слева направо, как batch (running-sum даёт другой
            # порядок сложения float и ломает round(x, 3) на границе);
            # batch хранит atr_pct = ATR/close*100 — делим здесь же
            a_pct = sum(self._tr_window) / len(self._tr_window)
            a_pct = a_pct / max(c, 1e-9) * 100
            up = h - ph
            down = pl - lo
            plus_dm = up if up > down and up > 0 else 0.0
            minus_dm = down if down > up and down > 0 else 0.0
            if tr == 0:
                dx = 0.0
            else:
                di_p = plus_dm / tr * 100
                di_m = minus_dm / tr * 100
                s = di_p + di_m
                dx = abs(di_p - di_m) / s * 100 if s else 0.0
            self._dx_window.append(dx)
            if len(self._dx_window) > d.atr_period:
                self._dx_window.popleft()
            # сумма — срезом, как batch (см. комментарий у TR выше)
            a_adx = sum(self._dx_window) / len(self._dx_window)
        else:
            a_pct = None  # type: ignore[assignment]
            a_adx = None  # type: ignore[assignment]

        self._atr_hist.append(a_pct)
        if i == 1:
            # batch: atr_pct[0] заполняется значением TR_1 (= a_pct бара 1) и
            # участвует в процентилях. Вставляем его в окно отдельным значением.
            self._atr_hist[0] = a_pct
            insort(self._atr_sorted, self._atr_hist[0])
        if a_pct is not None:
            insort(self._atr_sorted, a_pct)
            # batch-окно бара i: индексы i-window..i (до window+1 значений);
            # выпадает hist[i-window-1] при i >= window+1
            if i >= d.window + 1:
                self._atr_sorted.pop(bisect_left(
                    self._atr_sorted, self._atr_hist[i - d.window - 1]))
        if len(self._atr_sorted) < 5:
            a_perc = 50.0
        else:
            below = bisect_left(self._atr_sorted, a_pct)
            a_perc = below / len(self._atr_sorted) * 100

        vol_window = list(self._vol_hist)
        mean = sum(vol_window) / len(vol_window) if vol_window else 1.0
        v_ratio = v / max(mean, 1e-9)

        self._closes.append(c)
        self._prev = (h, lo, c)
        self._vol_hist.append(v)
        self._n += 1

        if i < self.warmup or i < 2:
            row = {"ts": ts, "state": "NEUTRAL", "features": None,
                   "reason": "warmup"}
            self.rows.append(row)
            return row

        slope = (esp - self._ema_sp_hist[0]) / max(self._ema_sp_hist[0], 1e-9)
        range_pct = (h - lo) / max(c, 1e-9) * 100
        _db = d.drift_bars
        base = self._closes[max(0, len(self._closes) - 1 - _db)]
        dr = (c - base) / max(base, 1e-9) * 100
        _win = list(self._closes)[-d.cons_bars - 1:] if len(self._closes) > 1 else [c]
        _n = len(_win) - 1 if len(_win) > 1 else 1
        _ups = sum(1 for k in range(1, len(_win)) if _win[k] >= _win[k - 1])
        cons = _ups / _n if _n else 0.5
        _minority = min(_ups, _n - _ups) / _n if _n else 0.5
        _driftok = dr >= d.drift_pct
        _consok = cons >= d.cons_pct
        _consrelax = cons >= d.cons_relax_pct and dr >= d.drift_strong_pct
        if a_perc >= d.atr_percentile_threshold or range_pct > d.range_mult * a_pct:
            state, reason = "HIGH_VOLATILITY", "atr_percentile_high_or_range_wide"
        elif slope >= 0 and _driftok and (_consok or _consrelax) and a_adx >= d.adx_threshold:
            state, reason = "TREND_UP", f"drift_up_cons{cons:.2f}"
        elif slope <= 0 and dr <= -d.drift_pct and \
                (cons <= 1 - d.cons_pct or (cons <= 1 - d.cons_relax_pct and dr <= -d.drift_strong_pct)) \
                and a_adx >= d.adx_threshold:
            state, reason = "TREND_DOWN", f"drift_dn_cons{cons:.2f}"
        elif abs(dr) < d.drift_pct:
            # дрейф за окно слишком мал — боковик
            state, reason = "RANGE", "low_drift"
        elif _minority >= 0.34:
            # движение в обе стороны примерно поровну — пила
            state, reason = "RANGE", "low_consistency"
        elif abs(slope) < d.slope_threshold and a_adx < d.adx_threshold:
            state, reason = "RANGE", "flat_ema_low_adx"
        else:
            state, reason = "NEUTRAL", "mixed"
        row = {
            "ts": ts,
            "state": state,
            "reason": reason,
            "features": {
                "atr_pct": round(a_pct, 3),
                "atr_percentile": round(a_perc, 1),
                "ema_slope": round(slope * 100, 3),
                "adx": round(a_adx, 1),
                "volume_ratio": round(v_ratio, 2),
                "drift_pct": round(dr, 3),
                "consistency": round(cons, 2),
            },
        }
        self.rows.append(row)
        return row

    def evaluate(self, bar) -> dict:
        """Строка forming-бара: клон двигается, состояние — нет."""
        return self.clone().update(bar)
