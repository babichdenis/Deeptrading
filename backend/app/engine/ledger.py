from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.engine.models import AuditEntry, Trade


@dataclass
class TradeLedger:
    trades: list[Trade] = field(default_factory=list)
    audit: list[AuditEntry] = field(default_factory=list)

    def log(self, index: int, time, kind: str, detail: str) -> None:
        self.audit.append(AuditEntry(index=index, time=time, kind=kind, detail=detail))

    def add_trade(self, **kwargs) -> Trade:
        trade = Trade(trade_id=f"T{len(self.trades) + 1:04d}", **kwargs)
        self.trades.append(trade)
        return trade

    def fingerprint(self) -> str:
        payload = "\n".join(repr(t) for t in self.trades)
        payload += "\n" + "\n".join(repr(a) for a in self.audit)
        return hashlib.sha256(payload.encode()).hexdigest()
