"""Multi-Timeframe Resampler: единая агрегация OHLCV 1m -> старшие таймфреймы.

Используется всеми потребителями канонических минутных данных:
  - ReplayFeed (тест-режим: реплей из БД interval=1 на любом TF);
  - CandleHub/live (future): тот же candle semantics в backtest и live.

Контракт:
  - feed(bar) -> закрытый бар ПРЕДЫДУЩЕГО бакета либо None (бар ещё копится).
    Закрытие происходит, когда приходит первый бар нового бакета.
  - flush(require_full=False) -> список оставшихся баков (конец потока/окна).
  - Вход: объект с полями figi, ts, open, high, low, close, volume (утиная типизация).
    Выход: экземпляр ТОГО ЖЕ типа, собранный из 7 канонических полей
    (совместим с ClosedCandle из app.bot.feed).
  - ts закрытого бара = НАЧАЛО бакета (конвенция T-Invest для старших TF).
  - Границы бакетов выравниваются по epoch (UTC): :00/:10/:20 для 10min и т.д.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

logger = logging.getLogger("resampler")

# Поддерживаемые целевые TF (в минутах). Источник всегда 1m.
PERIOD_MIN = {
    "1min": 1, "5min": 5, "10min": 10, "15min": 15,
    "30min": 30, "hour": 60, "2h": 120, "4h": 240,
}

_FIELDS = ("figi", "ts", "open", "high", "low", "close", "volume")


class BarLike(Protocol):
    figi: str
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


class Resampler:
    """Агрегатор минутных баров в целевой таймфрейм. Состояние — per-figi."""

    def __init__(self, target: str, source: str = "1min"):
        if source != "1min":
            raise ValueError(f"Resampler: source поддерживает только 1min (получено {source})")
        if target not in PERIOD_MIN:
            raise ValueError(f"Resampler: неподдерживаемый target timeframe: {target}")
        self.source = source
        self.target = target
        self._period_min = PERIOD_MIN[target]
        # figi -> {"ts": bucket_start, "cls": type, + канонические поля}
        self._state: dict[str, dict] = {}
        self._dropped = 0

    @property
    def period_minutes(self) -> int:
        return self._period_min

    @property
    def dropped_out_of_order(self) -> int:
        """Сколько внеочередных баров отброшено (диагностика качества данных)."""
        return self._dropped

    def _bucket_start(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        epoch_min = int(ts.timestamp()) // 60
        b = (epoch_min // self._period_min) * self._period_min * 60
        return datetime.fromtimestamp(b, tz=timezone.utc)

    def feed(self, bar: BarLike):
        """Подать минутный бар. Вернёт закрытый бар предыдущего бакета или None."""
        if self._period_min == 1:
            # 1min: passthrough — бар уходит как есть, без задержки на бакет.
            return bar
        figi = bar.figi
        bts = self._bucket_start(bar.ts)
        st = self._state.get(figi)

        if st is not None and bts == st["ts"]:
            # Копим в текущий бакет.
            if bar.high > st["high"]:
                st["high"] = bar.high
            if bar.low < st["low"]:
                st["low"] = bar.low
            st["close"] = bar.close
            st["volume"] += float(bar.volume or 0.0)
            st["n"] += 1
            return None

        if st is not None and bts < st["ts"]:
            # Внепорядковый бар из прошлого бакета: не мерджим (поток упорядочен по ts).
            self._dropped += 1
            if self._dropped <= 5 or self._dropped % 500 == 0:
                logger.warning("Resampler: out-of-order bar dropped figi=%s ts=%s (total=%s)",
                               figi, bar.ts, self._dropped)
            return None

        out = self._emit(st) if st is not None else None
        self._state[figi] = {
            "ts": bts, "cls": type(bar), "n": 1,
            "figi": figi,
            "open": bar.open, "high": bar.high,
            "low": bar.low, "close": bar.close,
            "volume": float(bar.volume or 0.0),
        }
        return out

    def flush(self, require_full: bool = False) -> list:
        """Закрыть оставшиеся бакеты (конец потока/окна реплея).

        require_full=True — отдавать только бакеты, набравшие полный период минут
        (осторожно: пропуски 1m в данных сделают бакет «неполным» ложно).
        По умолчанию False — отдаём всё накопленное, семантика закрытого бара.
        """
        states = sorted(self._state.values(), key=lambda s: (s["ts"], s["figi"]))
        self._state.clear()
        out = []
        for st in states:
            if require_full and st["n"] < self._period_min:
                continue
            out.append(self._emit(st))
        return out

    def reset(self) -> None:
        self._state.clear()

    def _emit(self, st: dict):
        cls = st["cls"]
        return cls(**{k: st[k] for k in _FIELDS})
