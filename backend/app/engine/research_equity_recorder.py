import json
from dataclasses import asdict
from datetime import timedelta

from app.engine.research_equity import EquityMark, EquityPosition, mark_paper_equity


class ReplayEquityRecorder:
    def __init__(self, *, broker, output, timeframe: timedelta, max_mark_age: timedelta,
                 accrued_costs):
        if timeframe <= timedelta(0):
            raise ValueError('Positive timeframe required')
        self.broker = broker
        self.output = output
        self.timeframe = timeframe
        self.max_mark_age = max_mark_age
        self.accrued_costs = accrued_costs
        self.marks = {}
        self.pending_at = None
        self.points = []

    async def process(self, candle, consumer):
        available_at = candle.ts + self.timeframe
        if self.pending_at is not None:
            if available_at < self.pending_at:
                raise ValueError('Out-of-order portfolio event')
            if available_at > self.pending_at:
                await self.capture(self.pending_at, phase='bar_close')
        await consumer(candle)
        self.marks[candle.figi] = EquityMark(float(candle.close), available_at)
        self.pending_at = available_at

    async def capture(self, as_of, *, phase):
        positions = [EquityPosition(p.figi, p.side, int(p.qty), float(p.entry_price))
                     for p in await self.broker.positions()]
        point = mark_paper_equity(as_of=as_of, cash=await self.broker.cash(), positions=positions,
                                  marks=self.marks, max_mark_age=self.max_mark_age,
                                  accrued_costs=self.accrued_costs(as_of))
        row = asdict(point)
        row['phase'] = phase
        self.output.write(json.dumps(row, default=str, allow_nan=False) + '\n')
        self.output.flush()
        self.points.append(point)
        return point

    async def flush(self):
        if self.pending_at is not None:
            await self.capture(self.pending_at, phase='bar_close')
            self.pending_at = None
