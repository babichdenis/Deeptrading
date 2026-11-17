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

def broker_eligibility(instrument: InstrumentRef, evidence: dict | None, *,
                       side: str, observed_at=None, as_of=None, max_age=None,
                       allowed_classes: tuple[str, ...] = ("TQBR",)) -> EligibilityResult:
    from datetime import datetime, timedelta

    reasons: list[str] = []
    if max_age is None:
        max_age = timedelta(hours=1)
    if not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        raise ValueError("max_age must be a positive timedelta")
    if not all(isinstance(value, datetime) and value.utcoffset() is not None
               for value in (observed_at, as_of)):
        reasons.append("broker_evidence_time_unknown")
    elif observed_at > as_of:
        reasons.append("broker_evidence_from_future")
    elif as_of - observed_at > max_age:
        reasons.append("broker_evidence_stale")
    if side not in ("long", "short"):
        raise ValueError("side must be long or short")
    if evidence is None:
        return EligibilityResult(instrument, False, tuple(reasons + ["broker_identity_unknown"]))
    if not instrument.figi or evidence.get("figi") != instrument.figi or not evidence.get("uid"):
        reasons.append("broker_identity_mismatch")
    if evidence.get("class_code") not in allowed_classes:
        reasons.append("trading_class_not_allowed")
    lot = evidence.get("lot")
    if type(lot) is not int or lot <= 0:
        reasons.append("lot_unknown")
    if evidence.get("api_trade_available_flag") is not True:
        reasons.append("api_trade_unavailable")
    required = ("buy_available_flag",) if side == "long" else ("sell_available_flag", "short_enabled_flag")
    for flag in required:
        if evidence.get(flag) is not True:
            reasons.append(flag.removesuffix("_flag") + "_not_confirmed")
    return EligibilityResult(instrument, not reasons, tuple(reasons))
