"""L2.1 DataContext — персистентные таймфрейм-серии (OsEngine-style).

Архитектура: вместо resample всего буфера на каждом баре (O(N) на вызов)
серии поддерживаются инкрементально. Каждая TF-серия разделена на:

- ``closed`` — append-only список полностью закрытых баров. Персистентное
  состояние (индикаторы L2.2, regime L2.3, стратегии L2.4) двигается ТОЛЬКО
  по закрытым барам — это даёт точную воспроизводимость batch-пути.
- ``forming`` — текущий незакрытый бар. При каждой новой M1-свече ЗАМЕНЯЕТСЯ
  новым объектом (не мутируется in-place): потребители, прочитавшие старый
  forming, не видят чужих изменений; snapshot-семантика для L2.5 бесплатна.

``bars(tf) == closed + [forming]`` всегда равен ``ensemble.resample(m1, tf)``
на том же префиксе — та же bucket-математика (импорт из ensemble_ctx),
тот же fold (open первого, high max, low min, close последнего, volume сумма).
Паритет проверяется tests/test_data_context.py на каждом префиксе.

Контракты:
- Вход — ВАЛИДИРОВАННЫЕ M1-свечи со строго растущими ts (валидация остаётся
  в ctx.sync / конвейере; сброс буфера = новый DataContext).
- События: ``append()`` возвращает dict {tf_sec: "closed"|"forming"} —
  следующие стадии (L2.2+) пересчитывают только то, что реально изменилось.
- Этот модуль никого не трогает: ensemble.py не изменён, подключение — на
  следующих стадиях после зелёного parity-гейта.
"""
from __future__ import annotations

from app.engine.models import Candle as EngineCandle
from app.services.ensemble_ctx import _bucket_of, _key_of


def _fold(prev: EngineCandle, c: EngineCandle) -> EngineCandle:
    """Сложить 1м-свечу в forming-бар: та же математика, что batch-resample."""
    return EngineCandle(
        ts=prev.ts,
        open=prev.open,
        high=max(prev.high, c.high),
        low=min(prev.low, c.low),
        close=c.close,
        volume=prev.volume + c.volume,
    )


def _new_bar(c: EngineCandle, tf_sec: int) -> EngineCandle:
    return EngineCandle(
        ts=_key_of(c.ts, tf_sec),
        open=c.open,
        high=c.high,
        low=c.low,
        close=c.close,
        volume=c.volume,
    )


class TimeframeSeries:
    """Одна TF-серия: append-only closed + заменяемый forming."""

    __slots__ = ("tf_sec", "closed", "forming", "closes_total")

    def __init__(self, tf_sec: int):
        self.tf_sec = tf_sec
        self.closed: list[EngineCandle] = []
        self.forming: EngineCandle | None = None
        self.closes_total = 0

    def update(self, c: EngineCandle) -> bool:
        """Сложить M1-свечу. Вернуть True, если при этом закрылся бар серии.

        Закрытие = пришла свеча следующего бакета: текущий forming уходит
        в closed (навсегда, объекты closed никогда не мутируются), forming
        начинается заново. Обновление внутри бакета — замена forming новым
        объектом, closed не тронут.
        """
        b = _bucket_of(c.ts, self.tf_sec)
        f = self.forming
        if f is None or _bucket_of(f.ts, self.tf_sec) != b:
            closed_event = f is not None
            if f is not None:
                self.closed.append(f)
                self.closes_total += 1
            self.forming = _new_bar(c, self.tf_sec)
            return closed_event
        self.forming = _fold(f, c)
        return False

    def bars(self) -> list[EngineCandle]:
        """closed + forming — эквивалент batch-resample на скормленном префиксе."""
        if self.forming is None:
            return list(self.closed)
        return [*self.closed, self.forming]


class DataContext:
    """Персистентные серии всех ТФ для одного инструмента."""

    def __init__(self, tf_secs: tuple[int, ...] = (60, 300, 600, 1800, 3600)):
        self._series = {tf: TimeframeSeries(tf) for tf in tf_secs}
        self.n = 0

    def append(self, c: EngineCandle) -> dict[int, str]:
        """Скормить одну M1-свечу. Вернуть события по ТФ: 'closed' / 'forming'."""
        events: dict[int, str] = {}
        for tf, s in self._series.items():
            events[tf] = "closed" if s.update(c) else "forming"
        self.n += 1
        return events

    def bars(self, tf_sec: int) -> list[EngineCandle]:
        return self._series[tf_sec].bars()

    def closed(self, tf_sec: int) -> list[EngineCandle]:
        return self._series[tf_sec].closed

    def forming(self, tf_sec: int) -> EngineCandle | None:
        return self._series[tf_sec].forming
