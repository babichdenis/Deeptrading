"""Rebalance: CurrentPortfolio + TargetPortfolio → DirectTargetRebalance → RebalancePlan.

Покрывает раздел 23 спецификации (17 тестов) + интеграцию Phase 1-7 (24),
validation и архитектуру. План — описание изменений, НЕ ордера.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.bot.universe import (
    ATR_PERIOD,
    FEATURE_WINDOW,
    ELIGIBLE,
    CurrentPortfolio,
    CurrentPosition,
    DirectTargetRebalance,
    EqualWeightAllocation,
    InstrumentRef,
    RankingMethod,
    RebalanceAction,
    RebalanceActionType,
    ScreenItem,
    TargetPortfolio,
    TargetPosition,
    current_portfolio_from_dicts,
    screen_all,
    select,
)

AS_OF = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def ref(ticker: str, figi: str = "") -> InstrumentRef:
    return InstrumentRef(ticker=ticker, figi=figi or f"FIGI_{ticker}")


def current(tickers, qtys=None, price=100.0, cash=0.0):
    qtys = qtys or [100] * len(tickers)
    poses = tuple(
        CurrentPosition(instrument=ref(t), current_qty=q, price=price,
                        current_notional=q * price)
        for t, q in zip(tickers, qtys)
    )
    equity = sum(p.current_notional for p in poses) + cash
    return CurrentPortfolio(as_of=AS_OF, equity=equity, positions=poses, cash=cash)


def target(tickers, qtys=None, price=100.0, reserve=0.0):
    qtys = qtys or [100] * len(tickers)
    poses = tuple(
        TargetPosition(instrument=ref(t), target_weight=1.0, actual_weight=1.0,
                       target_qty=q, actual_qty=q,
                       target_notional=q * price, actual_notional=q * price,
                       price=price, lot_size=1)
        for t, q in zip(tickers, qtys)
    )
    used = sum(p.actual_notional for p in poses)
    equity = used / (1.0 - reserve) if reserve < 1.0 else used + 1.0
    return TargetPortfolio(
        as_of=AS_OF, equity=equity, positions=poses,
        cash_target=equity * reserve, unallocated_cash=max(equity - used, 0.0),
        allocation_method="equal_weight", reserve_cash_pct=reserve,
    )


def plan(cur, tgt):
    return DirectTargetRebalance().plan(current=cur, target=tgt, as_of=AS_OF)


def test_equal_current_and_target_produces_no_changes():
    p = plan(current(("A", "B")), target(("A", "B")))
    assert p.actions == ()
    assert p.validate() == ()


def test_increase_position_creates_buy():
    p = plan(current(("A",), qtys=[100]), target(("A",), qtys=[150]))
    assert len(p.actions) == 1
    a = p.actions[0]
    assert a.action is RebalanceActionType.BUY
    assert a.delta_qty == 50


def test_decrease_position_creates_sell():
    p = plan(current(("A",), qtys=[150]), target(("A",), qtys=[100]))
    assert len(p.actions) == 1
    a = p.actions[0]
    assert a.action is RebalanceActionType.SELL
    assert a.delta_qty == -50


def test_new_target_position_creates_buy():
    p = plan(current(("A",)), target(("A", "B")))
    assert [a.instrument.ticker for a in p.actions] == ["B"]
    assert p.actions[0].action is RebalanceActionType.BUY
    assert p.actions[0].delta_qty == 100


def test_missing_target_position_creates_remove():
    p = plan(current(("A", "B"), qtys=[100, 50]), target(("A",)))
    assert [a.instrument.ticker for a in p.actions] == ["B"]
    assert p.actions[0].action is RebalanceActionType.REMOVE
    assert p.actions[0].delta_qty == -50
    assert p.actions[0].target_qty == 0


def test_full_rotation_creates_expected_actions():
    p = plan(current(("A", "B"), qtys=[100, 50]), target(("C", "D"), qtys=[100, 50]))
    acts = [(a.instrument.ticker, a.action.value) for a in p.actions]
    # порядок: сначала target-порядок (C, D), затем только-current (A, B)
    assert acts == [
        ("C", "BUY"), ("D", "BUY"), ("A", "REMOVE"), ("B", "REMOVE"),
    ]


def test_delta_quantity_is_target_minus_current():
    p = plan(current(("A",), qtys=[100]), target(("A",), qtys=[70]))
    a = p.actions[0]
    assert a.delta_qty == 70 - 100


def test_rebalance_does_not_re_round_lots():
    # target_qty=70 уже кратно лоту 10 — Rebalance не должен трогать quantity
    tgt = target(("A",), qtys=[70])
    cur = current(("A",), qtys=[100])
    a = plan(cur, tgt).actions[0]
    assert a.target_qty == 70
    assert a.delta_qty == -30


def test_delta_notional_uses_reference_price():
    price = 1_250.0
    cur = current(("A",), qtys=[100], price=price)
    tgt = target(("A",), qtys=[140], price=price)
    a = plan(cur, tgt).actions[0]
    assert a.delta_notional == pytest.approx(40 * price)
    assert a.current_notional == pytest.approx(100 * price)
    assert a.target_notional == pytest.approx(140 * price)


def test_invalid_target_is_rejected():
    tgt = TargetPortfolio(
        as_of=AS_OF, equity=100.0, positions=(),
        cash_target=0.0, unallocated_cash=100.0,
        allocation_method="manual",
    )
    tgt = TargetPortfolio(
        as_of=AS_OF, equity=-100.0, positions=(),  # invalid: target equity guard
        cash_target=-50.0, unallocated_cash=0.0, allocation_method="manual",
    )
    with pytest.raises(ValueError):
        DirectTargetRebalance().plan(current=current(()), target=tgt)


def test_buy_requires_positive_delta():
    p = plan(current(("A",), qtys=[100]), target(("A",), qtys=[100]))
    assert all(a.action is not RebalanceActionType.BUY or a.delta_qty > 0
               for a in p.actions)


def test_sell_requires_negative_delta():
    p = plan(current(("A",), qtys=[100]), target(("A",), qtys=[120]))
    assert all(a.action is not RebalanceActionType.SELL or a.delta_qty < 0
               for a in p.actions)


def test_remove_requires_zero_target_qty():
    p = plan(current(("A", "B")), target(("A",)))
    for a in p.actions:
        if a.action is RebalanceActionType.REMOVE:
            assert a.target_qty == 0
            assert a.current_qty > 0


def test_rebalance_plan_is_deterministic():
    cur = current(("A", "B"), qtys=[100, 50])
    tgt = target(("C", "D"))
    r1 = plan(cur, tgt)
    r2 = plan(cur, tgt)
    assert r1 == r2
    assert tuple(a.instrument.ticker for a in r1.actions) == \
           tuple(a.instrument.ticker for a in r2.actions)


def test_rebalance_plan_does_not_create_orders():
    p = plan(current(("A",), qtys=[100]), target(("A",), qtys=[150]))
    for a in p.actions:
        for attr in ("order_id", "side", "fill", "slippage", "commission"):
            assert not hasattr(a, attr)
        assert isinstance(a.action, RebalanceActionType)


def test_rebalance_plan_does_not_call_broker():
    import inspect
    import app.bot.universe.rebalance as reb
    src = inspect.getsource(reb)
    assert "paper_broker" not in src
    assert ".buy(" not in src and ".sell(" not in src
    assert "PostOrder" not in src


def test_rank_change_itself_does_not_create_rebalance_action():
    # GAZP выпал из Top-N: TargetPortfolio его не содержит. Действия — только
    # для инструментов из target; отсутствие инструмента в target = REMOVE при
    # наличии текущей позиции, но никогда не мгновенный SELL-ордер.
    cur = current(("GAZP",), qtys=[100])
    tgt = target(("SBER",))
    p = plan(cur, tgt)
    acts = [(a.instrument.ticker, a.action.value) for a in p.actions]
    assert ("GAZP", "SELL") not in acts
    for a in p.actions:
        assert not hasattr(a, "order_id")
        assert not hasattr(a, "side")


def test_adapter_from_runtime_dicts():
    cp = current_portfolio_from_dicts(
        AS_OF, equity=100_000.0,
        positions=[{"ticker": "A", "figi": "FIGI_A", "qty": 10, "price": 1_250.0}],
        cash=1_000.0,
    )
    assert cp.equity == 100_000.0
    assert cp.positions[0].instrument.figi == "FIGI_A"
    assert cp.positions[0].current_notional == pytest.approx(12_500.0)
    tgt = target(("A",), qtys=[10], price=1_250.0)
    p = plan(cp, tgt)
    # 10 штук и в current, и в target -> изменений нет
    assert p.actions == ()
    assert p.validate() == ()


def test_cash_delta_exposed():
    cur = current(("A",), qtys=[100], cash=50.0)
    tgt = target(("A",), qtys=[100])
    p = plan(cur, tgt)
    assert p.current_cash == 50.0
    assert p.target_cash == pytest.approx(tgt.unallocated_cash)
    assert p.cash_delta == pytest.approx(tgt.unallocated_cash - 50.0)


def _bars(figi: str, n: int, base: float):
    from app.engine.models import Candle as EngineCandle
    ts = AS_OF - timedelta(minutes=5 * (n - 1))
    return [
        EngineCandle(ts=ts + timedelta(minutes=5 * i), open=base + i,
                     high=base + i + 1, low=base + i - 1, close=base + i,
                     volume=100)
        for i in range(n)
    ]


def test_end_to_end_universe_to_rebalance_plan():
    """Phase 1-7 E2E: скрининг → признаки → селекция → аллокация →
    TargetPortfolio → CurrentPortfolio → RebalancePlan. Broker отсутствует.

    Current: SBER+GAZP вне шортлиста, GLTR в шортлисте → план должен выдать
    REMOVE (для оставшихся), BUY (для новых из top-2) и SELL (для уменьшения).
    """
    from app.bot.universe import compute_feature_sets

    tickers = ["AAA", "BBB", "CCC"]
    items = [
        ScreenItem(ref=ref(t), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True)
        for t in tickers
    ]
    screened = screen_all(items, ELIGIBLE)
    bars = {s.instrument.figi: _bars(s.instrument.figi, 60, 100.0) for s in screened}
    features = compute_feature_sets([s.instrument for s in screened], bars, as_of=AS_OF)
    sres = select(features, top_n=2, as_of=AS_OF, method=RankingMethod.TOP_N_ATR)
    tgt = EqualWeightAllocation(reserve_cash_pct=0.05).allocate(
        selection=sres, equity=100_000.0,
        prices={s.instrument.figi: 100.0 for s in screened},
        lot_sizes={s.instrument.figi: 1 for s in screened},
        as_of=sres.as_of,
    )
    assert tgt.validate() == ()

    # текущий портфель: одна позиция вне Top-2 + одна в Top-2
    cur = CurrentPortfolio(
        as_of=AS_OF, equity=100_000.0, cash=0.0,
        positions=(
            CurrentPosition(instrument=ref("AAA"), current_qty=600, price=100.0,
                            current_notional=60_000.0),  # выше target 475
            CurrentPosition(instrument=ref("XXX"), current_qty=500, price=100.0,
                            current_notional=50_000.0),  # вне target совсем
        ),
    )
    p = DirectTargetRebalance().plan(current=cur, target=tgt, as_of=AS_OF)
    assert p.validate() == ()

    action_by_ticker = {a.instrument.ticker: a.action for a in p.actions}
    assert action_by_ticker["AAA"] is RebalanceActionType.SELL  # 600 -> 475
    assert action_by_ticker["XXX"] is RebalanceActionType.REMOVE  # только в current
    assert action_by_ticker["BBB"] is RebalanceActionType.BUY  # 0 -> 475
    assert "CCC" not in action_by_ticker  # не в top-2 и не в current

    # архитектура: actions не ордера
    assert all(not hasattr(a, "order_id") for a in p.actions)


def test_plan_without_current_portfolio_builds_all_buys():
    tgt = target(("A", "B"))
    p = DirectTargetRebalance().plan(target=tgt, as_of=AS_OF)
    assert [a.action for a in p.actions] == [RebalanceActionType.BUY, RebalanceActionType.BUY]
    assert p.current_equity == 0.0
    assert p.validate() == ()