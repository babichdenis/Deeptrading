from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Iterable

from app.bot.feed import CandleFeed, ClosedCandle, STEP_SEC
from app.database import SessionLocal

logger = logging.getLogger("replay_feed")

# Маппинг interval_name → колонка interval в таблице candles.
INTERVAL_COL = {"1min": 1, "5min": 5, "10min": 10, "15min": 15}


class ReplayFeed(CandleFeed):
    """Исторический фид: свечи из БД (таблица candles) вместо live-стрима.

    Тот же интерфейс, что у CandleFeed — runtime потребляет его одинаково:
        async for candle in feed.stream()  ->  ClosedCandle
    При этом virtual-time бота (runtime._bot_now) движется вместе с подаваемыми
    барами, поэтому все decision-гейты (сессии, свежесть бара, дневной PnL)
    воспроизводят поведение live-бота 1-в-1.

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
            f"pace={self.pace} figis={len(self.figis)} interval={INTERVAL_COL.get(self.interval_name, 1)}m"
        )
        from sqlalchemy import select as _select
        from app.models.candle import Candle

        iv = INTERVAL_COL.get(self.interval_name, 1)
        q = _select(Candle).where(
            Candle.figi.in_(self.figis),
            Candle.interval == iv,
            Candle.ts >= _s,
        )
        if _e is not None:
            q = q.where(Candle.ts <= _e)
        q = q.order_by(Candle.ts, Candle.figi)

        rows = []
        try:
            async with self.session_factory() as db:
                rows = (await db.execute(q)).scalars().all()
        except Exception as ex:
            self._emit(f"replay stream DB ERROR {type(ex).__name__}: {str(ex)[:120]}")
            return

        self._emit(f"replay loaded rows={len(rows)}")
        if not rows:
            return

        # Группируем по минуте закрытия бара (нормализуем ts до секунды=0).
        by_ts: dict[datetime, list[Candle]] = defaultdict(list)
        for r in rows:
            k = r.ts.replace(microsecond=0, second=0)
            by_ts[k].append(r)

        step = STEP_SEC.get(self.interval_name, 60)
        first = min(by_ts)
        last = max(by_ts) if _e is None else _e.replace(microsecond=0, second=0)
        cur = first
        while cur <= last:
            if self._stop_requested:
                self._emit("replay stream stopped by request")
                return
            bucket = by_ts.get(cur)
            if bucket:
                for r in bucket:
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
                    if self.pace == "wall":
                        wait = (r.ts - datetime.now(timezone.utc)).total_seconds()
                        if wait > 0:
                            await asyncio.sleep(wait)
                    yield cc
                    if self._stop_requested:
                        return
            cur += timedelta(seconds=step)
        self._emit("replay stream finished")