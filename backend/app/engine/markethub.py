"""MarketDataHub — связующий слой CandleHub ↔ IndicatorHub.

Референс: docs/osengine/PORT_NOTES_CANDLEHUB.md.

СЛОЙ НИЧЕГО НЕ РЕШИТ ЗА СВОИХ ХОЗЯЕВ:
- ряд (figi, tf) в CandleHub создаётся ТОЛЬКО когда на этот (figi, tf) кто-то
  подписался на индикатор. Никакого «держим все ТФ напролёт»;
- commit индикаторов происходит по событию закрытия бара серии, а не отдельным
  проходом после ingest — источник правды один (CandleHub);
- прогрев считается по требованиям подписок: required_bars на каждый
  (figi, tf, indicator) и берётся максимум по инструменту;
- forming-бар (partial) уходит только в preview: он не меняет состояние
  индикаторов.

ЖИЗНЕННЫЙ ЦИКЛ figi:
    md.subscribe(figi, "5min", "rsi", {"length": 14})   # ряд 5min создан
    md.seed_1m(figi, history)                            # 1m -> 5min + прогрев
    md.ingest_1m(figi, candle)                           # живой поток
    md.get(figi, "5min", "rsi", {"length": 14})
    md.unsubscribe(figi, "5min", "rsi", {"length": 14})  # ряд отвязан
"""
from __future__ import annotations

from typing import Sequence

from app.engine.candlehub import (
    CandleHub,
    CandleSeries,
    EventListener,
    RebuiltListener,
    tf_to_seconds,
)
from app.engine.indicatorhub import IndicatorHub, IndicatorKey
from app.engine.models import Candle


class MarketDataHub:
    """Свечи + индикаторы как одно целое.

    Использование:
        md = MarketDataHub()
        md.subscribe("SBER", 60, "rsi", {"length": 14})
        for candle in stream:
            md.ingest_1m("SBER", candle)
        rsi = md.get("SBER", 60, "rsi", {"length": 14})

    ВАЖНО про maxlen: 1m-ряд обрезается по maxlen, и производные ТФ строятся
    только из того, что в нём осталось. EMA200 на 1h — это 200 × 60 = 12000
    минут, поэтому maxlen должен быть заведомо больше required_1m_bars для
    всех подписанных старших ТФ, иначе ряд не дотянет до нужного числа
    закрытых баров и индикатор останется not ready (это видно в describe()).
    """

    def __init__(self, maxlen: int = 5000, max_lookback: int = 500):
        self.candles = CandleHub(maxlen=maxlen)
        self.indicators = IndicatorHub(max_lookback=max_lookback)
        self._bound: dict[tuple[str, int], CandleSeries] = {}
        self._hooks: dict[
            tuple[str, int], tuple[EventListener, EventListener, RebuiltListener]
        ] = {}
        self.indicators.on_subscribe.append(self._on_subscribe)

    # --- подписки --------------------------------------------------------
    def subscribe(
        self, figi: str, tf: str | int, name: str, params: dict | None = None
    ) -> IndicatorKey:
        """Подписать индикатор и поднять ряд figi×tf (если его ещё нет)."""
        return self.indicators.subscribe(figi, tf_to_seconds(tf), name, params)

    def unsubscribe(
        self, figi: str, tf: str | int, name: str, params: dict | None = None
    ) -> bool:
        key = self.indicators.key_for(figi, tf_to_seconds(tf), name, params)
        if not self.indicators.unsubscribe(key.figi, key.tf_seconds, key.name, params):
            return False
        if not self.indicators.subscriptions(key.figi, key.tf_seconds):
            self._unbind(key.figi, key.tf_seconds)
        return True

    def required_1m_bars(self, figi: str, tf: str | int) -> int:
        """Сколько 1m-баров надо докачать, чтобы прогреть (figi, tf).

        EMA200 на 1h требует 200 закрытых часовых баров = 200 × 60 минут.
        Оценка по полному кадру (одна минута = tf/60 баров TF) без учёта
        клирингов и неполных сессий — докачка сверх этого не помешает,
        а лишних баров движок сам обрежет по maxlen.
        """
        tf_seconds = tf_to_seconds(tf)
        bars = self.indicators.required_bars(figi, tf_seconds)
        if bars <= 0 or tf_seconds == 60:
            return bars
        return bars * (tf_seconds // 60)

    # --- поток данных ----------------------------------------------------
    def ingest_1m(
        self, figi: str, candle: Candle, *, source: str = "live", fire: bool = True
    ) -> str:
        """Принять закрытую 1m-свечу: CandleHub -> все производные ТФ ->
        commit индикаторов по закрытым барам, preview по forming."""
        return self.candles.ingest_1m(figi, candle, source=source, fire=fire)

    def seed_1m(
        self,
        figi: str,
        candles: Sequence[Candle],
        *,
        source: str = "db",
        fire: bool = False,
    ) -> None:
        """Батч 1m: докачка дыр / прогрев истории / коррекция."""
        self.candles.seed_1m(figi, candles, source=source, fire=fire)
        self.warmup_indicators(figi)

    def finalize(self, figi: str | None = None, tf: str | int | None = None) -> None:
        """Закрыть partial-бары (EOD / конец реплея).

        Коммит индикаторов при этом не нужен отдельным проходом: закрытый
        partial уходит в серию как обычный закрытый бар, и commit происходит
        по событию on_closed — тот же путь, что и в живом потоке.
        """
        self.candles.finalize(figi, tf)

    def warmup_indicators(
        self, figi: str, tf: str | int | None = None
    ) -> dict[str, int]:
        """Пересчитать индикаторы figi (или figi×tf) с нуля по истории.

        Возвращает {tf_seconds: сколько закрытых баров взято в прогрев}.
        """
        sizes: dict[str, int] = {}
        for tf_seconds in self._timeframes(figi, tf):
            series = self._bound.get((figi, tf_seconds))
            if series is None:
                continue
            need = max(
                self.indicators.required_bars(figi, tf_seconds),
                self.indicators.max_lookback,
            )
            snapshot = series.snapshot(need)
            if snapshot:
                self.indicators.warmup(figi, tf_seconds, snapshot)
                sizes[str(tf_seconds)] = len(snapshot)
        return sizes

    def get(
        self,
        figi: str,
        tf: str | int,
        name: str,
        params: dict | None = None,
        row: str | None = None,
    ) -> float | None:
        return self.indicators.get(figi, tf_to_seconds(tf), name, params, row)

    def get_at(
        self,
        figi: str,
        tf: str | int,
        name: str,
        ts,
        params: dict | None = None,
        row: str | None = None,
    ) -> float | None:
        return self.indicators.get_at(
            figi, tf_to_seconds(tf), name, params, row=row, ts=ts
        )

    def preview(
        self, figi: str, tf: str | int
    ) -> dict[str, dict[str, float | None]]:
        """Значения по текущему forming-бару (состояние не меняется)."""
        tf_seconds = tf_to_seconds(tf)
        series = self._bound.get((figi, tf_seconds))
        if series is None:
            return {}
        return self.indicators.preview(
            figi, tf_seconds, series.snapshot(), series.partial
        )

    def describe(self, figi: str | None = None) -> list[dict]:
        return self.indicators.describe(figi)

    def bound_timeframes(self, figi: str) -> list[int]:
        return sorted(t for f, t in self._bound if f == figi)

    # --- внутреннее ------------------------------------------------------
    def _on_subscribe(self, key: IndicatorKey) -> None:
        """Ряд поднят — дособрать его из уже накопленной 1m-истории.

        CandleHub не догоняет ряд, созданный ПОСЛЕ данных (см. его
        докстринг), поэтому поздняя подписка на 1h после получаса 1m-стрима
        иначе осталась бы пустой, и индикатор молча не стал бы готовым.
        """
        series = self._bind(key.figi, key.tf_seconds)
        if not series.snapshot() and key.tf_seconds != 60:
            history = self.candles.series(key.figi, "1min").snapshot()
            if history:
                self.candles.seed_1m(key.figi, history, source="db", fire=False)
        if series.snapshot():
            self.warmup_indicators(key.figi, key.tf_seconds)

    def _bind(self, figi: str, tf_seconds: int) -> CandleSeries:
        key = (figi, tf_seconds)
        series = self._bound.get(key)
        if series is not None:
            return series
        series = self.candles.series(figi, tf_seconds)

        def on_closed(candle: Candle, f=figi, t=tf_seconds, s=series) -> None:
            self._on_closed(f, t, s)

        def on_updated(candle: Candle, f=figi, t=tf_seconds, s=series) -> None:
            self._on_forming(f, t, s)

        def on_rebuilt(f=figi, t=tf_seconds, s=series) -> None:
            self._on_rebuilt(f, t, s)

        series.on_closed.append(on_closed)
        series.on_updated.append(on_updated)
        series.on_rebuilt.append(on_rebuilt)
        self._hooks[key] = (on_closed, on_updated, on_rebuilt)
        self._bound[key] = series
        return series

    def _unbind(self, figi: str, tf_seconds: int) -> None:
        key = (figi, tf_seconds)
        series = self._bound.pop(key, None)
        hooks = self._hooks.pop(key, None)
        if series is None or hooks is None:
            return
        on_closed, on_updated, on_rebuilt = hooks
        for hook, bucket in (
            (on_closed, series.on_closed),
            (on_updated, series.on_updated),
            (on_rebuilt, series.on_rebuilt),
        ):
            if hook in bucket:
                bucket.remove(hook)

    def _on_closed(self, figi: str, tf_seconds: int, series: CandleSeries) -> None:
        if self.indicators.subscriptions(figi, tf_seconds):
            self.indicators.commit(figi, tf_seconds, series.snapshot())

    def _on_forming(self, figi: str, tf_seconds: int, series: CandleSeries) -> None:
        if self.indicators.subscriptions(figi, tf_seconds):
            self.indicators.preview(figi, tf_seconds, series.snapshot(), series.partial)

    def _on_rebuilt(self, figi: str, tf_seconds: int, series: CandleSeries) -> None:
        if self.indicators.subscriptions(figi, tf_seconds):
            self.indicators.warmup(figi, tf_seconds, series.snapshot())

    def _timeframes(self, figi: str, tf: str | int | None) -> list[int]:
        if tf is not None:
            return [tf_to_seconds(tf)]
        return sorted(
            t
            for (f, t) in self._bound
            if f == figi and self.indicators.subscriptions(f, t)
        )
