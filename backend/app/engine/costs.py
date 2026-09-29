from __future__ import annotations

from dataclasses import dataclass

from app.engine.models import Side


@dataclass(frozen=True)
class CostModel:
    """Контракт затрат (ENG-020, audit 2026-09-29).

    round_price — nearest-tick (banker's rounding). Это ЯВНЫЙ КОНТРАКТ:
    слиппедж на 2 bps при tick=0.01 может частично поглотиться округлением
    (BUY округлится вниз, SELL вверх). Модель консервативна не строго —
    для строго adverse-семантики нужен side-aware ceil/floor, что изменит
    базовые числа; до отдельного решения фиксируем текущий контракт.

    Поля Trade: gross_pnl уже посчитан по slipped-ценам входа/выхода —
    slippage хранится отдельно как СПРАВКА, а не как вычитаемое.
    Внешние отчёты НЕ должны вычитать его второй раз (net = gross - commission).
    """

    id: str = "canonical_v1"
    version: str = "1.0.0"
    commission_rate: float = 0.0005
    slippage_bps: float = 2.0
    tick_size: float = 0.01

    def round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        return round(round(price / self.tick_size) * self.tick_size, 10)

    def fill_price(self, base: float, side: Side) -> float:
        slip = base * self.slippage_bps / 10_000
        adjusted = slip if side is Side.BUY else -slip
        return self.round_price(base + adjusted)

    def commission(self, notional: float) -> float:
        return notional * self.commission_rate
