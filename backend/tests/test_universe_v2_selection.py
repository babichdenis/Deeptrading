"""Selection (Universe 2.0): generic Top-N поверх strategy candidates.

Тесты §25 (Top-N block) и §18-§19:
  - Selection работает поверх уже отфильтрованных кандидатов.
  - score приходит снаружи (score_fn), а не из hardcoded ATR-ranking.
  - Top-N — generic-механизм.
  - Selection не создаёт ордеров.
"""
from __future__ import annotations

from app.bot.universe.domain import InstrumentRef
from app.bot.universe.selection import RankedCandidate, rank_candidates, select_top_n

A = InstrumentRef(ticker="AAA", figi="FIGI_A")
B = InstrumentRef(ticker="BBB", figi="FIGI_B")
C = InstrumentRef(ticker="CCC", figi="FIGI_C")


def test_rank_candidates_orders_by_score_desc():
    ranked = rank_candidates([A, B, C], lambda ref: {"AAA": 3.0, "BBB": 1.0, "CCC": 2.0}[ref.ticker])
    assert [r.instrument for r in ranked] == [A, C, B]
    assert [r.rank for r in ranked] == [1, 2, 3]


def test_tie_break_by_ticker_asc():
    ties = [InstrumentRef(ticker="ZZZ"), InstrumentRef(ticker="AAA"), InstrumentRef(ticker="MMM")]
    ranked = rank_candidates(ties, lambda ref: 5.0)
    assert [r.instrument.ticker for r in ranked] == ["AAA", "MMM", "ZZZ"]


def test_select_top_n_generic_and_capped():
    ranked = select_top_n([A, B, C], lambda ref: {"AAA": 3.0, "BBB": 1.0, "CCC": 2.0}[ref.ticker], top_n=2)
    assert [r.instrument for r in ranked] == [A, C]


def test_select_top_n_zero_gives_empty():
    assert select_top_n([A, B, C], lambda ref: 1.0, top_n=0) == ()


def test_score_fn_external_not_atr():
    """score приходит снаружи: здесь это НЕ atr_pct, а произвольный score."""
    ranked = rank_candidates([A, B], lambda ref: {"AAA": -7.0, "BBB": 2.0}[ref.ticker])
    assert [r.instrument for r in ranked] == [B, A]


def test_selection_result_has_no_orders():
    ranked = rank_candidates([A, B], lambda ref: 1.0)
    assert all(isinstance(r, RankedCandidate) for r in ranked)
    assert all(not hasattr(r, "side") for r in ranked)
    assert all(not hasattr(r, "quantity") for r in ranked)
    assert all(not hasattr(r, "order") for r in ranked)


def test_deterministic():
    r1 = rank_candidates([A, B, C], lambda ref: 1.0)
    r2 = rank_candidates([A, B, C], lambda ref: 1.0)
    assert r1 == r2