from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class InstrumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    figi: str
    ticker: str
    class_code: str
    isin: str | None = None
    name: str
    currency: str
    sector: str | None = None
    lot: int


class CandleOut(BaseModel):
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
