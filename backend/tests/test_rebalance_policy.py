"""RebalancePolicy: RebalancePlan → OrderIntent (без execution).

Покрывает раздел 32 (21 unit), architecture (§30/§31) и интеграцию Phase 1-8
(§33). Policy — чистый фильтр: TargetPortfolio/RebalancePlan не меняет,
Broker не вызывает.
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
    DirectRebalancePolicy,
    DirectTargetRebalance,
    EqualWeightAllocation,
    InstrumentRef,
    OrderIntent,
    PolicyResult,
    RankingMethod,
    RebalanceAction,
    RebalanceActionType,
    RebalanceContext,
    RebalancePlan,
    RejectedAction,
    RejectionReason,
    ScreenItem,
    TargetPortfolio,
    TargetPosition,
    order_intent_to_legacy,
    screen_all,
    select,
)
from app.engine.models import Side

AS_OF = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def ref(ticker: str, figi: str = "") -> InstrumentRef:
    return InstrumentRef(ticker=ticker, figi=figi or f"FIGI_{ticker}")


def _action(ticker: str, action, cur, tgt, price=100.0):
    d = tgt - cur
    return RebalanceAction(
        instrument=ref(ticker),
        action=action,
        current_qty=cur,
        target_qty=tgt,
        delta_qty=d,
        current_notional=cur * price,
        target_notional=tgt * price,
        delta_notional=d * price,
        price=price,
    )


def _plan(*actions: RebalanceAction) -> RebalancePlan:
    return RebalancePlan(
        as_of=AS_OF, current_equity=1_000_000.0, target_equity=1_000_000.0,
        current_cash=0.0, target_cash=0.0, cash_delta=0.0,
        actions=actions, planner="direct_target",
    )


def _ctx(equity=1_000_000.0, **kw) -> RebalanceContext:
    return RebalanceContext(equity=equity, as_of=AS_OF, **kw)


def _apply(policy, plan, ctx):
    return policy.apply(plan=plan, context=ctx)


def test_direct_policy_turns_buy_into_order_intent():
    r = _apply(DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.BUY, 100, 150)), _ctx())
    (intent,) = r.accepted
    assert intent.side is Side.BUY
    assert intent.quantity == 50
    assert intent.notional == pytest.approx(50 * 100.0)
    assert intent.action is RebalanceActionType.BUY


def test_direct_policy_turns_sell_into_order_intent():
    r = _apply(DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.SELL, 150, 100)), _ctx())
    (intent,) = r.accepted
    assert intent.side is Side.SELL
    assert intent.quantity == 50
    assert not r.has_rejections


def test_remove_becomes_sell_intent():
    r = _apply(DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.REMOVE, 100, 0)), _ctx())
    (intent,) = r.accepted
    assert intent.side is Side.SELL
    assert intent.quantity == 100
    assert intent.action is RebalanceActionType.REMOVE


def test_empty_plan_produces_no_intents():
    r = _apply(DirectRebalancePolicy(), _plan(), _ctx())
    assert r.accepted == ()
    assert r.rejected == ()


def test_below_min_trade_value_is_rejected():
    policy = DirectRebalancePolicy(min_trade_value=5_000.0)
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 3, price=1_000.0)), _ctx())
    assert r.accepted == ()
    (rej,) = r.rejected
    assert rej.reason is RejectionReason.BELOW_MIN_TRADE_VALUE


def test_equal_min_trade_value_is_allowed():
    policy = DirectRebalancePolicy(min_trade_value=5_000.0)
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 5, price=1_000.0)), _ctx())
    assert len(r.accepted) == 1
    assert not r.has_rejections


def test_turnover_within_limit_is_allowed():
    policy = DirectRebalancePolicy(max_turnover_pct=0.5)
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 100)), _ctx())
    assert len(r.accepted) == 1
    assert not r.has_rejections


def test_turnover_above_limit_rejects_plan():
    policy = DirectRebalancePolicy(max_turnover_pct=0.1)
    # gross 10000 / equity 50000 = 20% > 10%
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 100)), _ctx(equity=50_000.0))
    assert r.accepted == ()
    assert len(r.rejected) == 1
    assert r.rejected[0].reason is RejectionReason.MAX_TURNOVER_EXCEEDED


def test_cost_within_limit_is_allowed():
    policy = DirectRebalancePolicy(estimated_cost_rate=0.001, max_estimated_cost=200.0)
    # 100_000 * 0.001 = 100 <= 200
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 100, price=1_000.0)), _ctx())
    assert len(r.accepted) == 1
    assert not r.has_rejections


def test_cost_above_limit_is_rejected():
    policy = DirectRebalancePolicy(estimated_cost_rate=0.001, max_estimated_cost=50.0)
    # 100_000 * 0.001 = 100 > 50
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 100, price=1_000.0)), _ctx())
    assert r.rejected[0].reason is RejectionReason.MAX_COST_EXCEEDED


def test_cooldown_blocks_rebalance():
    policy = DirectRebalancePolicy(cooldown=timedelta(hours=2))
    ctx = _ctx(last_rebalance_at={"FIGI_A": AS_OF - timedelta(minutes=30)})
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 100)), ctx)
    assert r.rejected[0].reason is RejectionReason.COOLDOWN


def test_rebalance_after_cooldown_is_allowed():
    policy = DirectRebalancePolicy(cooldown=timedelta(hours=2))
    ctx = _ctx(last_rebalance_at={"FIGI_A": AS_OF - timedelta(hours=5)})
    r = _apply(policy, _plan(_action("A", RebalanceActionType.BUY, 0, 100)), ctx)
    assert len(r.accepted) == 1
    assert not r.has_rejections


def test_buy_with_sufficient_cash_is_allowed():
    r = _apply(DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.BUY, 0, 100)), _ctx(available_cash=50_000.0))
    assert len(r.accepted) == 1


def test_buy_without_sufficient_cash_is_rejected():
    r = _apply(DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.BUY, 0, 100)), _ctx(available_cash=1_000.0))
    assert r.accepted == ()
    assert r.rejected[0].reason is RejectionReason.INSUFFICIENT_CASH


def test_zero_quantity_is_rejected():
    # защитный кейс §12: план не должен содержать delta=0 (Phase 7 фильтрует),
    # но policy обязана его корректно отклонить
    plan = _plan(_action("A", RebalanceActionType.NOOP, 100, 100))
    r = _apply(DirectRebalancePolicy(), plan, _ctx())
    assert r.rejected[0].reason is RejectionReason.ZERO_QUANTITY


def test_invalid_price_is_rejected():
    a = RebalanceAction(
        instrument=ref("A"), action=RebalanceActionType.BUY,
        current_qty=0, target_qty=10, delta_qty=10,
        current_notional=0.0, target_notional=0.0, delta_notional=0.0, price=0.0,
    )
    r = _apply(DirectRebalancePolicy(), _plan(a), _ctx())
    assert r.rejected[0].reason is RejectionReason.INVALID_PRICE


def test_non_lot_quantity_is_rejected():
    plan = _plan(_action("A", RebalanceActionType.BUY, 0, 5))
    ctx = _ctx(lot_sizes={"FIGI_A": 10})
    r = _apply(DirectRebalancePolicy(), plan, ctx)
    assert r.rejected[0].reason is RejectionReason.INVALID_QUANTITY


def test_policy_is_deterministic():
    plan = _plan(
        _action("A", RebalanceActionType.BUY, 100, 150),
        _action("B", RebalanceActionType.SELL, 100, 50),
        _action("C", RebalanceActionType.REMOVE, 100, 0),
    )
    policy = DirectRebalancePolicy(min_trade_value=100.0)
    ctx = _ctx(available_cash=1_000_000.0)
    r1 = _apply(policy, plan, ctx)
    r2 = _apply(policy, plan, ctx)
    assert r1 == r2
    assert tuple(i.instrument.ticker for i in r1.accepted) == \
           tuple(i.instrument.ticker for i in r2.accepted)


def test_policy_does_not_modify_plan():
    plan = _plan(_action("A", RebalanceActionType.BUY, 0, 100))
    before = plan
    _apply(DirectRebalancePolicy(min_trade_value=0.0), plan, _ctx())
    # планы и действия frozen: policy принципиально не может их изменить,
    # а PolicyResult не содержит ссылок на target-поля весов
    assert plan == before
    assert plan.validate() == ()
    for intent in _apply(DirectRebalancePolicy(), plan, _ctx()).accepted:
        assert not hasattr(intent, "target_weight")
        assert not hasattr(intent, "actual_weight")


def test_policy_does_not_execute_orders():
    r = _apply(DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.BUY, 0, 100)), _ctx())
    for intent in r.accepted:
        for attr in ("order_id", "broker_order_id", "fill_price", "filled_qty",
                     "commission", "execution_status"):
            assert not hasattr(intent, attr)


def test_legacy_adapter_is_non_executing():
    (intent,) = _apply(
        DirectRebalancePolicy(), _plan(_action("A", RebalanceActionType.BUY, 0, 100)), _ctx()
    ).accepted
    d = order_intent_to_legacy(intent)
    assert d["ticker"] == "A"
    assert d["side"] == "BUY"
    assert d["qty"] == 100
    assert "id" not in d
    assert "filled_at" not in d
    assert "status" not in d


def _bars(figi: str, n: int, base: float):
    from app.engine.models import Candle as EngineCandle
    ts = AS_OF - timedelta(minutes=5 * (n - 1))
    return [
        EngineCandle(ts=ts + timedelta(minutes=5 * i), open=base + i,
                     high=base + i + 1, low=base + i - 1, close=base + i,
                     volume=100)
        for i in range(n)
    ]


def test_end_to_end_universe_to_order_intent():
    """Phase 1-8 E2E: скрининг → признаки → селекция → аллокация → план →
    policy → OrderIntent. Среди результатов: BUY, SELL (после REMOVE) и
    rejected (вне min_trade). Broker/Execution отсутствуют.
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
    cur = CurrentPortfolio(
        as_of=AS_OF, equity=100_000.0, cash=0.0,
        positions=(
            CurrentPosition(instrument=ref("AAA"), current_qty=600, price=100.0,
                            current_notional=60_000.0),
            CurrentPosition(instrument=ref("XXX"), current_qty=1, price=100.0,
                            current_notional=100.0),
        ),
    )
    plan = DirectTargetRebalance().plan(current=cur, target=tgt, as_of=AS_OF)
    assert plan.validate() == ()

    # min_trade 1000: small XXX-REMOVE (100₽) должен быть отклонён, остальное — intents
    policy = DirectRebalancePolicy(min_trade_value=1_000.0)
    ctx = _ctx(equity=100_000.0, available_cash=100_000.0,
               lot_sizes={s.instrument.figi: 1 for s in screened})
    result = policy.apply(plan=plan, context=ctx)

    sides = {i.side for i in result.accepted}
    assert Side.BUY in sides
    assert Side.SELL in sides
    assert any(r.reason is RejectionReason.BELOW_MIN_TRADE_VALUE for r in result.rejected)
    assert all(not hasattr(i, "order_id") for i in result.accepted)


def test_rejected_action_keeps_reason_and_delta():
    r = _apply(DirectRebalancePolicy(min_trade_value=5_000.0),
               _plan(_action("A", RebalanceActionType.BUY, 0, 3, price=1_000.0)), _ctx())
    (rej,) = r.rejected
    assert isinstance(rej, RejectedAction)
    assert rej.delta_qty == 3
    assert rej.delta_notional == pytest.approx(3_000.0)
    assert rej.reason is RejectionReason.BELOW_MIN_TRADE_VALUE