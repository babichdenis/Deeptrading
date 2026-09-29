"""Screener: чистые (без I/O) правила допуска инструмента к торговле.

Профиль кодирует все проверки, которые реально существовали в трёх
legacy-функциях app.bot.universe. Ни ATR, ни ранжирования здесь нет —
только допуск и код причины отсева.
"""
from __future__ import annotations

from dataclasses import dataclass

from .domain import (
    ScreenItem,
    ScreenReason,
    ScreenResult,
    ScreenedInstrument,
)


@dataclass(frozen=True)
class ScreenProfile:
    """Набор требований к кандидату.

    Значения по умолчанию соответствуют бывшей select_eligible_universe,
    поэтому её профиль — это переопределение только отличающихся полей.
    """

    name: str
    min_source_bars: int = 5
    min_resampled_bars: int = 0
    require_valid_features: bool = False
    require_tradeable: bool = False


# Бывшая select_eligible_universe: отсеивала по числу баров, но инструмент
# с непосчитанным ATR ОСТАВЛЯЛА с atr_pct = 0.
ELIGIBLE = ScreenProfile(
    name="eligible",
    min_source_bars=5,
    min_resampled_bars=15,
    require_valid_features=False,
)

# Бывшие select_all_tradeable и select_volatile_universe: отбрасывали
# инструмент целиком, если ATR не посчитан или close = 0.
TRADEABLE = ScreenProfile(
    name="tradeable",
    min_source_bars=30,
    require_valid_features=True,
)

VOLATILE = ScreenProfile(
    name="volatile",
    min_source_bars=20,
    require_valid_features=True,
)

PROFILES = {p.name: p for p in (ELIGIBLE, TRADEABLE, VOLATILE)}


def screen(item: ScreenItem, profile: ScreenProfile) -> ScreenResult:
    """Возвращает вердикт допуска для одного кандидата. Не мутирует вход."""
    if item.blacklisted:
        return ScreenResult(False, ScreenReason.BLACKLISTED, "инструмент в блэклисте")
    if profile.require_tradeable and not item.tradeable:
        return ScreenResult(False, ScreenReason.NOT_TRADEABLE, "нет торговли через API")
    if item.source_bars <= 0:
        return ScreenResult(False, ScreenReason.NO_DATA, "нет свечей в БД")
    if item.source_bars < profile.min_source_bars:
        return ScreenResult(
            False,
            ScreenReason.INSUFFICIENT_BARS,
            f"{item.source_bars} < {profile.min_source_bars} исходных баров",
        )
    if item.resampled_bars < profile.min_resampled_bars:
        return ScreenResult(
            False,
            ScreenReason.INSUFFICIENT_BARS,
            f"{item.resampled_bars} < {profile.min_resampled_bars} баров 5m",
        )
    if profile.require_valid_features and not item.features_valid:
        return ScreenResult(
            False,
            ScreenReason.INVALID_MARKET_DATA,
            "ATR не рассчитан или close = 0",
        )
    return ScreenResult(True)


def screen_all(
    items: list[ScreenItem], profile: ScreenProfile
) -> list[ScreenedInstrument]:
    """Скринит кандидатов, сохраняя входной порядок. Отсеянные отбрасываются."""
    out: list[ScreenedInstrument] = []
    for item in items:
        result = screen(item, profile)
        if not result.eligible:
            continue
        out.append(
            ScreenedInstrument(
                instrument=item.ref,
                result=result,
                source_bars=item.source_bars,
                resampled_bars=item.resampled_bars,
            )
        )
    return out


def screen_reasons(
    items: list[ScreenItem], profile: ScreenProfile
) -> list[tuple[ScreenItem, ScreenReason]]:
    """Диагностический вид: что и почему отсеяно (для логов и /screener)."""
    out: list[tuple[ScreenItem, ScreenReason]] = []
    for item in items:
        result = screen(item, profile)
        if not result.eligible and result.reason is not None:
            out.append((item, result.reason))
    return out
