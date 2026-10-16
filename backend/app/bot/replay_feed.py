from __future__ import annotations

import asyncio
import logging
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable

from app.bot.feed import CandleFeed, ClosedCandle
from app.database import SessionLocal

logger = logging.getLogger("replay_feed")

# Маппинг interval_name → колонка interval в таблице candles (оставлен для
# совместимости: сам запрос всегда идёт по каноническим минуткам interval=1).
INTERVAL_COL = {"1min": 1, "5min": 5, "10min": 10, "15min": 15}


class ReplayFeed(CandleFeed):
    """Исторический фид: канонические 1m-свечи из БД (таблица candles).

    Тот же интерфейс, что у CandleFeed — runtime потребляет его одинаково:
        async for candle in feed.stream()  ->  ClosedCandle
    При этом virtual-time бота (runtime._bot_now) движется вместе с подаваемыми
    барами, поэтому все decision-гейты (сессии, свежесть бара, дневной PnL)
    воспроизводят поведение live-бота 1-в-1.

    Данные ВСЕГДА читаются как interval=1 (канонические минутки). Целевой
    старший таймфрейм строится единым Resampler'ом (app.marketdata.resampler):
    те же границы бакетов и closed-семантика, что и в live/CandleHub. История
    теста на любом TF больше не зависит от того, какие производные таблицы
    (interval=5/10/15) случайно лежат в БД.

    pacing:
      - "fast": подаёт бары максимально быстро (для бэктеста/parity-харнеса);
      - "wall":  спит до номинального времени закрытия бара (реальный темп).
    """

    def __init__(
        self,
        interval_name: str,
        figis: Iterable[str],
        start: datetime,
        end: datetime | None = None,
        pace: str = "fast",
        session_factory=SessionLocal,
    ):
        super().__init__("token-not-used", interval_name, list(figis))
        self.mode = "replay"
        self.pace = pace if pace in ("fast", "wall") else "fast"
        self.start = start
        self.end = end
        self.session_factory = session_factory
        self._now: datetime | None = None
        self.on_day_boundary = None

    @property
    def now(self) -> datetime | None:
        """Текущее виртуальное время (ts последней отданной свечи)."""
        return self._now

    async def stream(self) -> AsyncIterator[ClosedCandle]:
        if not self.figis:
            self._emit("replay stream: no figis, abort")
            return
        if self.start is None:
            self._emit("replay stream: missing start, abort")
            return
        _s = self.start.replace(microsecond=0)
        if self.start.tzinfo is None:
            _s = _s.replace(tzinfo=timezone.utc)
        _e = self.end
        if _e is None:
            _e = None
        elif _e.tzinfo is None:
            _e = _e.replace(tzinfo=timezone.utc)
        self._emit(
            f"replay stream start {_s.isoformat()} end={(_e.isoformat() if _e else '…')} "
            f"pace={self.pace} figis={len(self.figis)} target_tf={self.interval_name} (source=1m)"
        )
        from sqlalchemy import func, select as _select
        from app.models.candle import Candle
        from app.marketdata.resampler import Resampler

        # Канонический источник — всегда минутки; старший TF строит Resampler.
        # Загрузка идёт посуточными кусками по ТЕКУЩЕМУ списку figis, чтобы
        # add_figis/remove_figis вступали в силу со следующего дня.
        rs = None if self.interval_name == "1min" else Resampler(self.interval_name)
        if rs is not None:
            self._emit(f"resampler: 1m -> {self.interval_name} (period={rs.period_minutes}m)")

        data_end = _e
        if data_end is None:
            try:
                async with self.session_factory() as db:
                    _mx = await db.execute(
                        _select(func.max(Candle.ts)).where(
                            Candle.figi.in_(self.figis),
                            Candle.interval == 1,
                            Candle.ts >= _s,
                        )
                    )
                    data_end = _mx.scalar()
            except Exception as ex:
                self._emit(f"replay stream DB ERROR {type(ex).__name__}: {str(ex)[:120]}")
                return
        if data_end is None:
            self._emit("replay loaded rows=0")
            return
        if data_end.tzinfo is None:
            data_end = data_end.replace(tzinfo=timezone.utc)

        _wall_t0: float | None = None
        _wall_first: datetime | None = None
        total_rows = 0
        chunk_start = _s.replace(hour=0, minute=0, second=0, microsecond=0)
        while chunk_start <= data_end:
            if self._stop_requested:
                self._emit("replay stream stopped by request")
                return
            chunk_end = chunk_start + timedelta(days=1)
            if not self.figis:
                self._emit("replay stream: figis empty, stop")
                break
            q = _select(Candle).where(
                Candle.figi.in_(self.figis),
                Candle.interval == 1,
                Candle.ts >= max(chunk_start, _s),
                Candle.ts < chunk_end,
                Candle.ts <= data_end,
            )
            q = q.order_by(Candle.ts, Candle.figi)
            try:
                async with self.session_factory() as db:
                    rows = (await db.execute(q)).scalars().all()
            except Exception as ex:
                self._emit(f"replay stream DB ERROR {type(ex).__name__}: {str(ex)[:120]}")
                return
            total_rows += len(rows)
            by_ts: dict[datetime, list[Candle]] = defaultdict(list)
            for r in rows:
                k = r.ts.replace(microsecond=0, second=0)
                by_ts[k].append(r)
            for k in sorted(by_ts):
                if self._stop_requested:
                    self._emit("replay stream stopped by request")
                    return
                for r in by_ts[k]:
                    cc = ClosedCandle(
                        figi=r.figi,
                        ts=r.ts,
                        open=float(r.open),
                        high=float(r.high),
                        low=float(r.low),
                        close=float(r.close),
                        volume=float(r.volume),
                    )
                    self._now = r.ts
                    out = rs.feed(cc) if rs is not None else cc
                    if self.pace == "wall":
                        if _wall_t0 is None:
                            _wall_t0 = _time.monotonic()
                            _wall_first = r.ts
                        _target = (r.ts - _wall_first).total_seconds()
                        _wait = _target - (_time.monotonic() - _wall_t0)
                        if _wait > 0:
                            await asyncio.sleep(_wait)
                    if out is not None:
                        yield out
                        if self._stop_requested:
                            return
            chunk_start = chunk_end
            if self.on_day_boundary is not None:
                await self.on_day_boundary(chunk_start)

        self._emit(f"replay loaded rows={total_rows}")
        # Конец окна реплея: закрываем последний накопленный бакет целевого TF.
        if rs is not None:
            for out in rs.flush():
                yield out
                if self._stop_requested:
                    return
            self._emit(
                f"replay stream finished (resampler 1m->{self.interval_name}, "
                f"dropped_out_of_order={rs.dropped_out_of_order})"
            )
        else:
            self._emit("replay stream finished")
