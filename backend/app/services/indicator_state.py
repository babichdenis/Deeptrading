"""L2.2 — персистентное состояние индикаторов (OsEngine-style).

Принцип: состояние двигается ТОЛЬКО по закрытым барам (события 'closed'
из DataContext). Forming-бар оценивается через snapshot-копию, которая
отбрасывается, — персистентное состояние не загрязняется (решение проблемы
raw 18 vs 21 из L0: повторная подача forming-бакета в живой Wilder-state).

Каноническая математика (паритет с batch по построению, проверяется
tests/test_indicator_state.py):
- RsiState  ↔ entry_gates.rsi_map: seed = средние gain/loss по диффам
  closes[1..period], затем Wilder; l<=0 → 100.0 (g>0) / 50.0.
- AtrState  ↔ конвейер ensemble.py:1089-1097 (НЕ Wilder!): ATR[i] = среднее
  последних 14 TR, TR = max(h-l, |h-pc|, |l-pc|), первое значение на i=14.
- EmaState  ↔ indicators.ema: seed = SMA первых period значений, затем
  k=2/(period+1), первое значение на индексе period-1.

Снапшоты — скалярные dict (копия за наносекунды, никакого deepcopy):
state.clone() = restore(snapshot()). evaluate(forming) = значение на
копии, persistent state не меняется.
"""
from __future__ import annotations

from collections import deque


def _rsi_val(g: float, l: float) -> float:
    if l <= 0:
        return 100.0 if g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + g / l)


class RsiState:
    """Wilder RSI. update() — по закрытым барам; evaluate() — forming."""

    __slots__ = ("period", "avg_gain", "avg_loss", "prev_close",
                 "seed_gain", "seed_loss", "seed_n", "value", "prev_value")

    def __init__(self, period: int = 14):
        self.period = period
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        self.prev_close: float | None = None
        self.seed_gain = 0.0
        self.seed_loss = 0.0
        self.seed_n = 0
        self.value: float | None = None
        self.prev_value: float | None = None

    def update(self, bar) -> float | None:
        """Скормить закрытый бар. Вернуть текущее значение (None до сида)."""
        close = float(bar.close)
        if self.prev_close is not None:
            d = close - self.prev_close
            if self.avg_gain is None:
                if d > 0:
                    self.seed_gain += d
                elif d < 0:
                    self.seed_loss -= d
                self.seed_n += 1
                if self.seed_n >= self.period:
                    self.avg_gain = self.seed_gain / self.period
                    self.avg_loss = self.seed_loss / self.period
                    self.prev_value = None
                    self.value = _rsi_val(self.avg_gain, self.avg_loss)
            else:
                g = d if d > 0 else 0.0
                l = -d if d < 0 else 0.0
                self.avg_gain = (self.avg_gain * (self.period - 1) + g) / self.period
                self.avg_loss = (self.avg_loss * (self.period - 1) + l) / self.period
                self.prev_value = self.value
                self.value = _rsi_val(self.avg_gain, self.avg_loss)
        self.prev_close = close
        return self.value

    def snapshot(self) -> dict:
        return {"avg_gain": self.avg_gain, "avg_loss": self.avg_loss,
                "prev_close": self.prev_close, "seed_gain": self.seed_gain,
                "seed_loss": self.seed_loss, "seed_n": self.seed_n,
                "value": self.value, "prev_value": self.prev_value}

    def restore(self, snap: dict) -> "RsiState":
        self.avg_gain = snap["avg_gain"]
        self.avg_loss = snap["avg_loss"]
        self.prev_close = snap["prev_close"]
        self.seed_gain = snap["seed_gain"]
        self.seed_loss = snap["seed_loss"]
        self.seed_n = snap["seed_n"]
        self.value = snap["value"]
        self.prev_value = snap["prev_value"]
        return self

    def clone(self) -> "RsiState":
        return RsiState(self.period).restore(self.snapshot())

    def evaluate(self, bar) -> float | None:
        """Значение на forming-баре: копия двигается, состояние — нет."""
        return self.clone().update(bar)


class AtrState:
    """Rolling-среднее TR (математика конвейера, не Wilder)."""

    __slots__ = ("period", "prev_close", "trs", "trs_sum", "value")

    def __init__(self, period: int = 14):
        self.period = period
        self.prev_close: float | None = None
        self.trs: deque[float] = deque()
        self.trs_sum = 0.0
        self.value: float | None = None

    def update(self, bar) -> float | None:
        h, l, c = float(bar.high), float(bar.low), float(bar.close)
        if self.prev_close is not None:
            tr = max(h - l, abs(h - self.prev_close), abs(l - self.prev_close))
            self.trs.append(tr)
            self.trs_sum += tr
            if len(self.trs) > self.period:
                self.trs_sum -= self.trs.popleft()
            if len(self.trs) >= self.period:
                self.value = self.trs_sum / self.period
        self.prev_close = c
        return self.value

    def snapshot(self) -> dict:
        return {"prev_close": self.prev_close, "trs": list(self.trs),
                "trs_sum": self.trs_sum, "value": self.value}

    def restore(self, snap: dict) -> "AtrState":
        self.prev_close = snap["prev_close"]
        self.trs = deque(snap["trs"])
        self.trs_sum = snap["trs_sum"]
        self.value = snap["value"]
        return self

    def clone(self) -> "AtrState":
        return AtrState(self.period).restore(self.snapshot())

    def evaluate(self, bar) -> float | None:
        return self.clone().update(bar)


class EmaState:
    """EMA с SMA-сидом (математика indicators.ema). update(value: float)."""

    __slots__ = ("period", "k", "seed_sum", "seed_n", "value")

    def __init__(self, period: int):
        self.period = period
        self.k = 2.0 / (period + 1)
        self.seed_sum = 0.0
        self.seed_n = 0
        self.value: float | None = None

    def update(self, x: float) -> float | None:
        x = float(x)
        if self.value is None:
            self.seed_sum += x
            self.seed_n += 1
            if self.seed_n >= self.period:
                self.value = self.seed_sum / self.period
        else:
            self.value = x * self.k + self.value * (1.0 - self.k)
        return self.value

    def snapshot(self) -> dict:
        return {"seed_sum": self.seed_sum, "seed_n": self.seed_n,
                "value": self.value}

    def restore(self, snap: dict) -> "EmaState":
        self.seed_sum = snap["seed_sum"]
        self.seed_n = snap["seed_n"]
        self.value = snap["value"]
        return self

    def clone(self) -> "EmaState":
        return EmaState(self.period).restore(self.snapshot())

    def evaluate(self, x: float) -> float | None:
        return self.clone().update(x)
