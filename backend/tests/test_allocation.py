"""Allocation: SelectionResult → EqualWeightAllocation → TargetPortfolio.

Покрывает раздел 18 спецификации (16 тестов) + интеграцию (19) + инварианты
TargetPortfolio.validate() и семантику target/actual weight.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.bot.universe import (
    ATR_PERIOD,
    FEATURE_WINDOW,
    ELIGIBLE,
    EqualWeightAllocation,
    InstrumentRef,
    RankingMethod,
    ScreenItem,
    SelectionItem,
    SelectionResult,
    TargetPortfolio,
    screen_all,
    select,
)

AS_OF = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def ref(ticker: str, figi: str = "") -> InstrumentRef:
    return InstrumentRef(ticker=ticker, figi=figi or f"FIGI_{ticker}")


def selection(*tickers: str) -> SelectionResult:
    items = tuple(
        SelectionItem(instrument=ref(t), score=10.0 - i, rank=i + 1)
        for i, t in enumerate(tickers)
    )
    return SelectionResult(as_of=AS_OF, items=items, selected=items,
                           ranking_method="equal_weight", top_n=len(items))


def alloc(tickers=("A", "B", "C", "D"), equity=100_000.0, reserve=0.0):
    sel = selection(*tickers)
    return EqualWeightAllocation(reserve_cash_pct=reserve).allocate(
        selection=sel,
        equity=equity,
        prices={f"FIGI_{t}": 1_250.0 for t in tickers},
        lot_sizes={f"FIGI_{t}": 10 for t in tickers},
        as_of=AS_OF,
    )


@pytest.mark.parametrize("tickers,expected", [
    (("A",), [1.0]),
    (("A", "B"), [0.5, 0.5]),
    (("A", "B", "C"), [1 / 3] * 3),
    (("A", "B", "C", "D"), [0.25] * 4),
])
def test_equal_weight_allocation(tickers, expected):
    tp = alloc(tickers=tickers)
    assert tp.allocation_method == "equal_weight"
    assert [p.target_weight for p in tp.positions] == expected


def test_single_instrument_gets_full_weight():
    tp = alloc(tickers=("A",))
    assert len(tp.positions) == 1
    assert tp.positions[0].target_weight == 1.0


def test_empty_selection_keeps_cash():
    tp = alloc(tickers=())
    assert tp.positions == ()
    assert tp.cash_target == 100_000.0
    assert tp.unallocated_cash == 100_000.0


def test_quantity_is_rounded_down_to_lot():
    tp = alloc(tickers=("A",), equity=100_000.0)
    # 100000 / 1250 = 80 -> кратно 10 -> 80
    pos = tp.positions[0]
    assert pos.target_qty == 80
    assert pos.target_qty % pos.lot_size == 0

    tp = alloc(tickers=("A",), equity=99_999.0)
    # 99999 / 1250 = 79.9992 -> 79 -> floor(79/10)*10 = 70
    assert tp.positions[0].target_qty == 70


def test_target_notional_never_exceeds_allocated_notional():
    tp = alloc(tickers=("A",), equity=100_000.0)
    pos = tp.positions[0]
    assert pos.actual_notional <= pos.target_notional
    assert pos.actual_notional == 80 * 1_250.0


def test_small_target_below_one_lot_produces_zero_quantity():
    # equity 1000 -> notional 1000 -> qty 0.8 -> 0 лотов
    tp = alloc(tickers=("A",), equity=1_000.0)
    assert len(tp.positions) == 1
    assert tp.positions[0].target_qty == 0
    assert tp.positions[0].actual_notional == 0.0


def test_reserve_cash():
    tp = alloc(tickers=("A", "B", "C", "D"), equity=100_000.0, reserve=0.05)
    assert tp.cash_target == pytest.approx(5_000.0)
    # investable = 95000, weight 0.25 -> notional 23750 -> /1250=19 -> 10 лотов
    total = sum(p.actual_notional for p in tp.positions)
    assert total == pytest.approx(4 * 10 * 1_250.0)
    assert tp.unallocated_cash == pytest.approx(100_000.0 - total)
    assert tp.unallocated_cash >= tp.cash_target
    assert tp.validate() == ()


def test_zero_equity_produces_no_positions():
    tp = alloc(equity=0.0)
    assert tp.positions == ()
    assert tp.cash_target == 0.0


def test_negative_equity_produces_no_positions():
    tp = alloc(equity=-5_000.0)
    assert tp.positions == ()
    assert tp.cash_target == -5_000.0


def test_invalid_price_is_not_allocated():
    prices = {f"FIGI_A": 0.0, "FIGI_B": 1_250.0}
    sel = selection("A", "B")
    tp = EqualWeightAllocation().allocate(
        selection=sel, equity=100_000.0,
        prices=prices, lot_sizes={"FIGI_A": 10, "FIGI_B": 10}, as_of=AS_OF,
    )
    assert [p.instrument.ticker for p in tp.positions] == ["B"]


def test_invalid_lot_is_not_allocated():
    lot_sizes = {"FIGI_A": 0, "FIGI_B": 10}
    sel = selection("A", "B")
    tp = EqualWeightAllocation().allocate(
        selection=sel, equity=100_000.0,
        prices={"FIGI_A": 1.0, "FIGI_B": 1_250.0}, lot_sizes=lot_sizes, as_of=AS_OF,
    )
    assert [p.instrument.ticker for p in tp.positions] == ["B"]


def test_allocation_is_deterministic():
    r1 = alloc()
    r2 = alloc()
    assert r1 == r2


def test_allocation_does_not_create_orders():
    tp = alloc()
    for pos in tp.positions:
        for attr in ("order_id", "side", "fill", "slippage", "commission"):
            assert not hasattr(pos, attr)


def test_allocation_result_contains_target_state_not_execution():
    from app.bot.universe import TargetPosition
    assert issubclass(TargetPosition, object)
    tp = alloc(tickers=("A",))
    pos = tp.positions[0]
    assert pos.instrument.ticker == "A"
    assert pos.target_qty == 80
    assert pos.actual_qty == 80
    # желаемое состояние без дельты относительно текущего портфеля
    assert not hasattr(tp, "current_positions")
    assert not hasattr(tp, "orders")
    assert not hasattr(pos, "side")


def test_allocation_uses_selected_instruments_only():
    prices = {"FIGI_A": 1_250.0, "FIGI_B": 1_250.0, "FIGI_C": 1_250.0}
    lot_sizes = {f"FIGI_{t}": 10 for t in ("A", "B", "C")}
    tp = EqualWeightAllocation().allocate(
        selection=selection("A", "B"), equity=100_000.0,
        prices=prices, lot_sizes=lot_sizes, as_of=AS_OF,
    )
    assert [p.instrument.ticker for p in tp.positions] == ["A", "B"]


def test_ranking_change_does_not_create_sell_or_order():
    # GAZP выпадает из Top-2: новый TargetPortfolio без GAZP, но без side/SELL/delta
    items = (SelectionItem(instrument=ref("GAZP"), score=12.0, rank=3),)
    sres = SelectionResult(as_of=AS_OF, items=items, selected=items,
                           ranking_method="equal_weight", top_n=1)
    tp = EqualWeightAllocation().allocate(
        selection=sres, equity=100_000.0,
        prices={"FIGI_GAZP": 100.0}, lot_sizes={"FIGI_GAZP": 1}, as_of=AS_OF,
    )
    for pos in tp.positions:
        assert not hasattr(pos, "side")
        assert not hasattr(pos, "delta")
        assert not hasattr(pos, "sell")
        assert not hasattr(pos, "order")


def test_target_vs_actual_weight_semantics():
    # A: price 1250 lot 10; B: price 1333 lot 10
    sel = selection("A", "B")
    tp = EqualWeightAllocation().allocate(
        selection=sel, equity=100_000.0,
        prices={"FIGI_A": 1_250.0, "FIGI_B": 1_333.0},
        lot_sizes={"FIGI_A": 10, "FIGI_B": 10}, as_of=AS_OF,
    )
    aa, bb = tp.positions
    assert aa.target_weight == bb.target_weight == 0.5
    assert aa.actual_weight == pytest.approx(aa.actual_notional / 100_000.0)
    assert aa.actual_weight == 0.5  # 50000/1250 = 40 → кратно лоту 10
    assert bb.actual_weight < bb.target_weight  # floor к лоту ненаправлен вверх


def test_unallocated_cash_is_kept_as_cash():
    tp = alloc(tickers=("A",), equity=100_000.0, reserve=0.05)
    pos = tp.positions[0]
    assert tp.unallocated_cash == pytest.approx(100_000.0 - pos.actual_notional)
    assert tp.unallocated_cash >= tp.cash_target
    assert tp.cash_target == pytest.approx(5_000.0)


def test_validate_invariants_ok():
    tp = alloc(tickers=("A", "B", "C", "D"), equity=100_000.0, reserve=0.05)
    assert tp.validate() == ()


def test_target_portfolio_validate_catches_overspend():
    # ручная позиция с номиналом, превышающим investable
    from app.bot.universe import TargetPosition
    pos = TargetPosition(
        instrument=ref("A"), target_weight=1.0, actual_weight=1.0,
        target_qty=100, actual_qty=100, target_notional=100_000.0,
        actual_notional=100_000.0, price=1_000.0, lot_size=100,
    )
    tp = TargetPortfolio(
        as_of=AS_OF, equity=100_000.0, positions=(pos,),
        cash_target=5_000.0, unallocated_cash=0.0,
        allocation_method="manual", reserve_cash_pct=0.05,
    )
    assert tp.validate() != ()


def _feature_pipeline(items, bars_by_figi, as_of):
    from app.bot.universe import compute_feature_sets
    return compute_feature_sets(
        [s.instrument for s in items], bars_by_figi, as_of=as_of,
        window=FEATURE_WINDOW, period=ATR_PERIOD,
    )


def _bars(figi: str, n: int, base: float):
    from datetime import timedelta
    from app.engine.models import Candle as EngineCandle
    # бары ЗАКАНЧИВАЮТСЯ ровно на as_of — look-ahead фильтр пускает ts <= as_of
    ts = AS_OF - timedelta(minutes=5 * (n - 1))
    bars = []
    for i in range(n):
        close = base + i  # монотонно растущий ряд, ATR>0
        bars.append(EngineCandle(ts=ts, open=close, high=close + 1,
                                 low=close - 1, close=close, volume=100))
        ts = ts + timedelta(minutes=5)
    return bars


def test_end_to_end_universe_to_target_portfolio():
    """Phase 1-6 интеграция: скрининг → признаки → селекция → аллокация.

    Broker/Execution здесь отсутствует: TargetPortfolio — желаемое состояние.
    """
    items = [
        ScreenItem(ref=ref("AAA"), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True),
        ScreenItem(ref=ref("BBB"), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True),
        ScreenItem(ref=ref("CCC"), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True),
    ]
    screened = screen_all(items, ELIGIBLE)
    assert len(screened) == 3

    bars_by_figi = {s.instrument.figi: _bars(s.instrument.figi, 60, 100.0) for s in screened}
    features = _feature_pipeline(screened, bars_by_figi, as_of=AS_OF)
    assert len(features) == 3
    assert all(f.valid for f in features)

    sres = select(features, top_n=2, as_of=AS_OF, method=RankingMethod.TOP_N_ATR)
    assert len(sres.selected) == 2

    tp = EqualWeightAllocation(reserve_cash_pct=0.05).allocate(
        selection=sres, equity=100_000.0,
        prices={s.instrument.figi: 100.0 for s in screened},
        lot_sizes={s.instrument.figi: 1 for s in screened},
        as_of=sres.as_of,
    )
    assert tp.allocation_method == "equal_weight"
    assert len(tp.positions) == 2
    assert all(p.target_weight == 0.5 for p in tp.positions)
    assert tp.cash_target == pytest.approx(5_000.0)
    assert tp.validate() == ()

    # ранкинг в пределах Top-2 детерминирован (higher ATR% -> выше)
    ranks = [p.instrument.ticker for p in tp.positions]
    assert ranks == sorted(ranks)