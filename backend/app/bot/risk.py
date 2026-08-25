from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskSnapshot:
    daily_pnl: float
    daily_loss_limit: float
    entries_paused: bool

    @property
    def state(self) -> str:
        if self.daily_loss_limit > 0 and self.daily_pnl <= -self.daily_loss_limit:
            return "LOSS_LIMIT"
        if self.entries_paused:
            return "PAUSED"
        return "NORMAL"

    def entries_allowed(self) -> bool:
        return self.state == "NORMAL"
