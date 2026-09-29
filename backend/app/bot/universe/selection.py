"""Отбор: ранжирование и срез Top-N.

Ровно тот же порядок, что давал list.sort(key=..., reverse=True):
сортировка стабильная, при равных значениях порядок кандидатов сохраняется.
"""
from __future__ import annotations

from typing import Callable, Iterable, TypeVar

T = TypeVar("T")


def top_n(items: Iterable[T], key: Callable[[T], float], limit: int) -> list[T]:
    """Сортирует по ключу по убыванию и возвращает первые limit элементов."""
    ordered = sorted(items, key=key, reverse=True)
    return ordered[:limit]
