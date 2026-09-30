"""EligibilityScreener: «можно ли рассматривать инструмент как кандидата».

Часть параллельного слоя Universe 2.0: legacy-слой НЕ менялся. Eligibility
отвечает на вопрос «есть данные/история/валидные цены/tradeable/blacklisted»,
а НЕ «хорош ли тренд» и НЕ «достаточно ли волатильности» — это ответственность
VolatilityMeasure/TrendMeasure/StrategyScreener.

ПРИНЦИП ПЕРЕИСПОЛЬЗОВАНИЯ: проверки допуска НЕ копируются — здесь тонко
оборачиваются проверенные legacy-примитивы (screen / screen_all из screener.py),
чтобы новое не расходилось со старым. Ради этого же ELIGIBLE-профиль заимствован
из screener.py, а не переопределён.

Источник кандидатов — существующий discovery (discover_eligible_universe, как в
legacy), без ATR-ранжирования. Текущий этап: новый слой строится рядом, runtime
продолжает использовать compat-путь.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.bot.universe.domain import InstrumentRef, ScreenItem
from app.bot.universe.screener import ELIGIBLE, ScreenProfile, screen_reasons


@dataclass(frozen=True)
class EligibilityResult:
    """Вердикт допуска одного инструмента из нового слоя.

    eligible — допущен ли кандидат; reasons — все причины отсева (screen_reasons),
    пустой кортеж при допуске. Семантика совпадает с legacy screen_all.
    """

    instrument: InstrumentRef
    eligible: bool
    reasons: tuple[str, ...] = ()

    @property
    def summary(self) -> str:
        return "eligible" if self.eligible else "; ".join(self.reasons)


def eligibility_screen(
    items: list[ScreenItem], profile: ScreenProfile = ELIGIBLE
) -> list[EligibilityResult]:
    """Скринит кандидатов через legacy-примитив, возвращая результат нового слоя.

    Интерпретация: ScreenItem -> (eligible, причины). Отсеянные сохраняются в
    выводе (в отличие от screen_all, который их выбрасывал) — это даёт новый
    слой прозрачнее для диагностики, не меняя legacy-поведение.
    """
    out: list[EligibilityResult] = []
    for item in items:
        rejected = screen_reasons([item], profile)
        reasons = tuple(r[1].value for r in rejected)
        out.append(
            EligibilityResult(
                instrument=item.ref,
                eligible=not reasons,
                reasons=reasons,
            )
        )
    return out


def eligible_instruments(
    items: list[ScreenItem], profile: ScreenProfile = ELIGIBLE
) -> tuple[InstrumentRef, ...]:
    """Только допущенные кандидаты в исходном порядке (как legacy screen_all)."""
    return tuple(
        r.instrument for r in eligibility_screen(items, profile) if r.eligible
    )