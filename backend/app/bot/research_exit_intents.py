from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ExitIntent:
    position: object
    decision_at: datetime


class ResearchExitIntents:
    def __init__(self):
        self.pending = {}

    def request(self, position, decision_at):
        if decision_at.utcoffset() is None:
            raise ValueError('Aware exit decision required')
        current = self.pending.get(position.figi)
        if current is None or current.position is not position:
            self.pending[position.figi] = ExitIntent(position, decision_at)

    def execute(self, portfolio, bars, at):
        for figi, intent in list(self.pending.items()):
            if portfolio.positions.get(figi) is not intent.position:
                del self.pending[figi]
                continue
            bar = bars.get(figi)
            if bar is None:
                continue
            if bar.ts != at or at < intent.decision_at:
                raise ValueError('Invalid exit reference time')
            portfolio.close_position(intent.position, bar.open, at, 'ENSEMBLE_SELL')
            del self.pending[figi]
