"""Selection: детерминированное ранжирование и срез Top-N.

Слой отвечает ровно на один вопрос — «кто выше в ранкинге». Здесь НЕТ и не будет:
весов, номиналов, количества лотов, целевых долей, ордеров и доступа к позициям.
Allocation (Phase 6) и Rebalance (Phase 7) — следующие этапы.

Смена ранкинга НЕ является сигналом на выход: бумага может выпасть из Top-N и
ничего не продаётся. Это различие зафиксировано тестом
test_ranking_change_does_not_produce_exit.

Tie-break: score DESC, затем ticker ASC. Инструменты с одинаковым score всегда
дают один и тот же порядок, независимо от порядка во входных данных.

Percentile — кросс-секционный, считается ТОЛЬКО по инструментам текущего
вызова. Будущие снимки не участвуют.

Функция top_n внизу файла — legacy-обёртка для compat-пути, новый код
использует rank_features/select.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, Protocol, TypeVar

from app.bot.universe.domain import (
    FeatureSet,
    InstrumentRef,
    RankingMethod,
    SelectionItem,
    SelectionResult,
)

T = TypeVar("T")


class SelectionStrategy(Protocol):
    """Интерфейс будущего кастомного скоринга.

    Реализациям достаточно вернуть score из FeatureSet. Стратегия не имеет
    доступа к БД, портфелю и брокеру — только к посчитанным признакам.
    Momentum/signal_count сюда НЕ добавлены: таких канонических признаков в
    проекте пока нет, и выдумывать их значения запрещено.
    """

    name: str

    def score(self, features: FeatureSet) -> float: ...


def _atr_score(features: FeatureSet) -> float:
    return float(features.atr_pct or 0.0)


def _percentile(value: float | None, population: tuple[float, ...]) -> float | None:
    """Доля элементов выборки, не превышающих value, в процентах.

    Конвенция (задокументирована и покрыта тестом):
      percentile = 100 * count(x <= value) / n

    Следствия: одинаковые значения получают ОДИНАКОВЫЙ percentile (максимум
    для этой группы), один инструмент в выборке даёт 100.0, пустая выборка или
    value=None дают None. Статистического смысла «среднего ранга» тут нет —
    это доля рынка, не превосходящая инструмент по ATR%.
    """
    if value is None or not population:
        return None
    return round(100.0 * sum(1 for p in population if p <= value) / len(population), 3)


def rank_features(
    features: Iterable[FeatureSet], *, strategy: SelectionStrategy | None = None
) -> tuple[SelectionItem, ...]:
    """Проранжирует признаки в SelectionItem. Top-N здесь НЕ применяется.

    Порядок: score DESC, затем ticker ASC. rank — 1-based позиция после
    tie-break. percentile считается по всей входной выборке, а не по срезу
    Top-N, иначе перцентили зависели бы от top_n.
    """
    items = list(features)
    scored = [
        (strategy.score(fs) if strategy is not None else _atr_score(fs), fs) for fs in items
    ]
    ordered = sorted(scored, key=lambda pair: (-pair[0], pair[1].instrument.ticker))

    population = tuple(fs.atr_pct for fs in items if fs.atr_pct is not None)

    return tuple(
        SelectionItem(
            instrument=fs.instrument,
            score=score,
            rank=position,
            atr=fs.atr,
            atr_pct=fs.atr_pct,
            atr_pct_percentile=_percentile(fs.atr_pct, population),
        )
        for position, (score, fs) in enumerate(ordered, start=1)
    )


def select(
    features: Iterable[FeatureSet],
    *,
    top_n: int,
    as_of: datetime | None = None,
    method: RankingMethod = RankingMethod.TOP_N_ATR,
    strategy: SelectionStrategy | None = None,
) -> SelectionResult:
    """Ранжирует признаки и возвращает SelectionResult.

    RankingMethod.TOP_N_ATR режет по top_n; top_n <= 0 даёт пустой selected.
    RankingMethod.ALL_ELIGIBLE возвращает всех проранжированных независимо от
    top_n — это baseline для сравнения «шортлист против всего».

    Исследовательский API. Live рантайм его НЕ вызывает и продолжает идти
    через compat-путь select_eligible_universe(top_n=9999).
    """
    items = list(features)
    if as_of is None:
        as_of = max((fs.as_of for fs in items), default=None)

    ranked = rank_features(items, strategy=strategy)
    if method is RankingMethod.ALL_ELIGIBLE:
        selected = ranked
    else:
        selected = ranked[: max(top_n, 0)]

    name = strategy.name if strategy is not None else method.value
    return SelectionResult(
        as_of=as_of,
        items=ranked,
        selected=selected,
        ranking_method=name,
        top_n=top_n,
    )


def top_n(items: Iterable[T], key: Callable[[T], float], limit: int) -> list[T]:
    """Сортирует по ключу по убыванию и возвращает первые limit элементов.

    Ровно тот же порядок, что давал list.sort(key=..., reverse=True):
    сортировка стабильная, при равных значениях порядок кандидатов сохраняется.
    Оставлено для legacy-обёрток; новый конвейер использует select().
    """
    ordered = sorted(items, key=key, reverse=True)
    return ordered[:limit]


# ── Параллельный слой Universe 2.0: generic Top-N над кандидатами ───────────
# Selection работает поверх уже отфильтрованных strategy candidates
# (StrategyScreener -> кандидат + score). URL больше НЕ «Universe -> hardcoded
# ATR ranking -> Selection»: score приходит снаружи (score_fn), а Top-N остаётся
# generic-механизмом. legacy RankingMethod.TOP_N_ATR сохранён в compat-пути.


@dataclass(frozen=True)
class RankedCandidate:
    """Кандидат с внешним score и позицией в ранкинге (1-based).

    scoring не зависит от FeatureSet: score_fn решает, что считать score.
    tie-break — ticker ASC (как и в legacy rank_features).
    """

    instrument: InstrumentRef
    score: float
    rank: int


def rank_candidates(
    candidates: Iterable[InstrumentRef],
    score_fn: Callable[[InstrumentRef], float],
) -> tuple[RankedCandidate, ...]:
    """Ранжирует кандидатов по score_fn (DESC, затем ticker ASC)."""
    scored = [(score_fn(ref), ref) for ref in candidates]
    ordered = sorted(scored, key=lambda pair: (-pair[0], pair[1].ticker))
    return tuple(
        RankedCandidate(instrument=ref, score=score, rank=position)
        for position, (score, ref) in enumerate(ordered, start=1)
    )


def select_top_n(
    candidates: Iterable[InstrumentRef],
    score_fn: Callable[[InstrumentRef], float],
    *,
    top_n: int,
) -> tuple[RankedCandidate, ...]:
    """Ранжирует и возвращает первые top_n кандидатов (generic Top-N).

    top_n <= 0 даёт пустой результат. Никакого ATR/ranking снаружи: score
    целиком определяет score_fn.
    """
    return rank_candidates(candidates, score_fn)[: max(top_n, 0)]
