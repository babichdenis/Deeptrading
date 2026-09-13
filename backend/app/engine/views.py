"""Лёгкие view-обёртки над списками свечей без копирования (hot-path движка)."""
from __future__ import annotations

from typing import Sequence


class CandleWindow(Sequence):
    """Представление candles[lo:hi] без копирования списка.

    Заменяет срезы в горячем пути (срез 28k баров × десятки тысяч раз = O(n²))."""

    __slots__ = ("_src", "_lo", "_hi")

    def __init__(self, src: Sequence, lo: int, hi: int):
        self._src = src
        self._lo = lo
        self._hi = hi

    def __len__(self) -> int:
        return self._hi - self._lo

    def __iter__(self):
        # Быстрая итерация: без поэлементного __getitem__ (isinstance-проверок).
        src = self._src
        lo, hi = self._lo, self._hi
        try:
            return iter(src[lo:hi])
        except Exception:
            return iter([src[j] for j in range(lo, hi)])

    def __getitem__(self, i):
        lo, hi = self._lo, self._hi
        n = hi - lo
        if isinstance(i, slice):
            start, stop, step = i.indices(n)
            return [self._src[lo + j] for j in range(start, stop, step)]
        if i < 0:
            i += n
        if i < 0 or i >= n:
            raise IndexError(i)
        return self._src[lo + i]
