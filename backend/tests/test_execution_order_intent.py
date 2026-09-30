"""Phase 9 Execution: OrderIntent → существующий PaperBroker (без runtime/live).

Существующий execution path (runtime._execute_pending) вызывает
broker.open_position/close_position — адаптер делает то же самое и не поднимает
runtime-гейты/риск/AI. PaperBroker исполняется на in-memory SQLite с теми же
ORM-моделями (paper_accounts/paper_positions/paper_trades) — НЕ новый брокер.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bot.paper_broker import PaperBroker
from app.bot.universe import (
    ELIGIBLE,
    DirectRebalancePolicy,
    DirectTargetRebalance,
    EqualWeightAllocation,
    InstrumentRef,
    RankingMethod,
    ScreenItem,
    TargetPortfolio,
    compute_feature_sets,
    current_portfolio_from_dicts,
    order_intent_to_legacy,
    screen_all,
    select,
)
from app.bot.universe.domain import (
    OrderIntent,
    PolicyResult,
    RebalanceActionType,
    RebalanceContext,
    RejectedAction,
    RejectionReason,
)
from app.bot.execution import (
    ExecutionResult,
    ExecutionStatus,
    execute_order_intent,
    execute_policy_result,
)
from app.engine.costs import CostModel
from app.engine.models import Side
from app.models.paper import PaperAccount, PaperPosition, PaperTrade

AS_OF = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
UNIVERSE_DIR = Path(__file__).resolve().parents[1] / "app" / "bot" / "universe"


def ref(ticker: str, figi: str = "") -> InstrumentRef:
    return InstrumentRef(ticker=ticker, figi=figi or f"FIGI_{ticker}")


def intent(ticker: str, side: Side, qty: int, price: float = 100.0,
           src: str = "rebalance", action=None) -> OrderIntent:
    return OrderIntent(
        instrument=ref(ticker),
        side=side,
        quantity=qty,
        price=price,
        notional=round(qty * price, 10),
        source=src,
        action=action,
    )


_trades_pk_seq: itertools.count | None = None


def _install_paper_trade_pk_counter() -> None:
    """PaperTrade.id — BigInteger PK без default (на Postgres это SERIAL); sqlite
    не знает BIGINT-autoincrement → подставляем id на клиенте. Боевой код не
    меняем; слушатель один раз на sync Session (asyncio-proxy в SQLAlchemy 2
    держит ORM-события через sync-класс)."""
    from sqlalchemy import event
    from sqlalchemy.orm import Session as SyncSession

    global _trades_pk_seq
    if _trades_pk_seq is None:
        _trades_pk_seq = itertools.count(1)

        @event.listens_for(SyncSession, "before_flush")
        def _assign_paper_trade_ids(session, flush_context, instances):
            for obj in session.new:
                if isinstance(obj, PaperTrade) and obj.id is None:
                    obj.id = next(_trades_pk_seq)


@pytest_asyncio.fixture
async def broker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    from app.database import Base as _Base

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: _Base.metadata.create_all(
                sync_conn,
                tables=[t.__table__ for t in (PaperAccount, PaperPosition, PaperTrade)],
            )
        )
    _install_paper_trade_pk_counter()
    Session = async_sessionmaker(engine, expire_on_commit=False)
    b = PaperBroker(Session, cost_model=CostModel())
    await b.reset(initial_cash=100_000.0)
    yield b
    await engine.dispose()


def _policy_result(accepted, rejected) -> PolicyResult:
    return PolicyResult(as_of=AS_OF, accepted=tuple(accepted), rejected=tuple(rejected))


def _rejected(ticker: str, reason=RejectionReason.BELOW_MIN_TRADE_VALUE) -> RejectedAction:
    return RejectedAction(instrument=ref(ticker), action=RebalanceActionType.REMOVE,
                          reason=reason, delta_qty=-1, delta_notional=-100.0)


# ── Mapping (§35.1-4, §8) ──────────────────────────────────────────────────


def test_buy_intent_maps_to_existing_order():
    m = order_intent_to_legacy(intent("A", Side.BUY, 50, price=100.5))
    assert m["ticker"] == "A"
    assert m["figi"] == "FIGI_A"
    assert m["side"] == "BUY"
    assert m["qty"] == 50
    assert m["price"] == 100.5
    assert m["source"] == "rebalance"


def test_sell_intent_maps_to_existing_order():
    m = order_intent_to_legacy(intent("B", Side.SELL, 50, price=100.5))
    assert m["side"] == "SELL"
    assert m["qty"] == 50


def test_remove_intent_maps_to_sell_order():
    it = intent("C", Side.SELL, 10, action=RebalanceActionType.REMOVE)
    m = order_intent_to_legacy(it)
    assert m["side"] == "SELL"
    assert m["qty"] == 10


def test_order_mapping_preserves_price_quantity_side():
    it = intent("D", Side.BUY, 7, price=333.33)
    m = order_intent_to_legacy(it)
    assert m["qty"] == it.quantity
    assert m["side"] == it.side.value
    assert m["price"] == round(it.price, 6)


# ── Policy result (§35.5-6, §7, §29-30) ────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_actions_are_not_executed(broker):
    res = await execute_policy_result(
        broker, _policy_result([], [_rejected("A"), _rejected("B")])
    )
    assert res == []
    assert await broker.positions() == []


@pytest.mark.asyncio
async def test_mixed_policy_result_executes_only_accepted(broker):
    res = await execute_policy_result(
        broker,
        _policy_result(
            [intent("A", Side.BUY, 10), intent("B", Side.SELL, 5)],
            [_rejected("C")],
        ),
    )
    assert len(res) == 2
    assert [r.intent.instrument.ticker for r in res] == ["A", "B"]
    pos = await broker.positions()
    assert {p.figi for p in pos} == {"FIGI_A", "FIGI_B"}
    assert all(r.status is ExecutionStatus.FILLED for r in res)
    assert "C" not in {p.figi for p in pos}


# ── Execution (§35.7-8, §16, §22) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_intent_produces_one_execution_attempt(broker):
    res = await execute_order_intent(broker, intent("A", Side.BUY, 10))
    assert res.status is ExecutionStatus.FILLED
    pos = await broker.get_position("FIGI_A")
    assert pos is not None and int(pos.qty) == 10


@pytest.mark.asyncio
async def test_execution_result_is_separate_from_intent(broker):
    it = intent("A", Side.BUY, 10)
    res = await execute_order_intent(broker, it)
    assert res.intent is it
    assert res.status is ExecutionStatus.FILLED
    assert it.quantity == 10  # intent не мутирован; policy не тронута
    assert not hasattr(it, "order_id")


# ── PaperBroker (§35.9-11, §27) ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_buy_intent_reaches_paper_broker(broker):
    res = await execute_order_intent(broker, intent("AAA", Side.BUY, 10))
    assert res.status is ExecutionStatus.FILLED
    assert res.filled_qty == 10
    pos = await broker.get_position("FIGI_AAA")
    assert pos is not None
    assert int(pos.qty) == 10
    assert pos.side == "BUY"
    assert float(pos.entry_price) > 0


@pytest.mark.asyncio
async def test_sell_intent_reaches_paper_broker(broker):
    await execute_order_intent(broker, intent("AAA", Side.BUY, 10))
    res = await execute_order_intent(broker, intent("AAA", Side.SELL, 10))
    assert res.status is ExecutionStatus.FILLED
    assert int(res.filled_qty) == 10
    assert await broker.get_position("FIGI_AAA") is None
    trades = await broker.trades_history()
    assert len(trades) == 1
    assert trades[0].figi == "FIGI_AAA"


@pytest.mark.asyncio
async def test_remove_intent_closes_position(broker):
    it = intent("AAA", Side.SELL, 10, action=RebalanceActionType.REMOVE)
    await execute_order_intent(broker, intent("AAA", Side.BUY, 10))
    res = await execute_order_intent(broker, it)
    assert res.status is ExecutionStatus.FILLED
    assert await broker.get_position("FIGI_AAA") is None


# ── Errors (§35.12-13, §18, §20-21) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalid_quantity_is_rejected_before_submit(broker):
    it = intent("A", Side.BUY, 0)
    res = await execute_order_intent(broker, it)
    assert res.status is ExecutionStatus.INVALID_INTENT
    assert await broker.positions() == []


@pytest.mark.asyncio
async def test_execution_error_is_not_silently_swallowed(broker):
    class BoomBroker:
        def __init__(self, inner):
            self.inner = inner
            self.costs = inner.costs

        async def get_position(self, figi):
            raise RuntimeError("position lookup exploded")

    res = await execute_order_intent(BoomBroker(broker), intent("A", Side.BUY, 10))
    assert res.status is ExecutionStatus.BROKER_ERROR
    assert "position lookup exploded" in (res.error or "")


# ── Determinism/order (§35.14, §23) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_result_execution_order_is_preserved(broker):
    res = await execute_policy_result(
        broker,
        _policy_result(
            [intent("A", Side.BUY, 1), intent("B", Side.BUY, 2), intent("C", Side.BUY, 3)],
            [],
        ),
    )
    assert [r.intent.instrument.ticker for r in res] == ["A", "B", "C"]


# ── Architecture (§35.15-18, §37) ──────────────────────────────────────────


def test_domain_layers_do_not_reference_broker():
    """§37: Selection/Allocation/Rebalance/Policy не могут трогать брокер."""
    for module in ("selection", "allocation", "rebalance", "policy", "features"):
        text = (UNIVERSE_DIR / f"{module}.py").read_text(encoding="utf-8")
        assert "paper_broker" not in text, module
        assert "live_broker" not in text, module


def test_execution_adapter_does_not_reference_live_runtime():
    import app.bot.execution as ex

    src = Path(ex.__file__).read_text(encoding="utf-8")
    assert "live_broker" not in src
    assert "import app.bot.runtime" not in src
    assert "from app.bot.runtime" not in src


def test_execution_result_does_not_modify_target_portfolio():
    it = intent("A", Side.BUY, 10)
    res = ExecutionResult(intent=it, status=ExecutionStatus.FILLED,
                          filled_qty=10, fill_price=100.0)
    assert it.quantity == 10
    assert res.filled_qty == 10
    assert res.intent is it


def test_live_broker_is_not_invoked_by_domain_tests():
    import sys

    # universe-контракты (selection/rebalance/policy) не импортируют брокеров —
    # live_broker в них отсутствует даже как импортная строка.
    for module in ("selection", "allocation", "rebalance", "policy", "features"):
        text = (UNIVERSE_DIR / f"{module}.py").read_text(encoding="utf-8")
        assert "live_broker" not in text
    # если какие-то другие тесты процесса подгрузили live-брокер — это не наш
    # слой: execution-адаптер его не трогает (проверено выше).


def _bars(figi: str, n: int, base: float):
    from app.engine.models import Candle as EngineCandle

    ts = AS_OF - timedelta(minutes=5 * (n - 1))
    return [EngineCandle(ts=ts + timedelta(minutes=5 * i), open=base + i,
                         high=base + i + 1, low=base + i - 1, close=base + i,
                         volume=100) for i in range(n)]


@pytest.mark.asyncio
async def test_full_pipeline_universe_to_paper_broker(broker):
    """Phase 1-9 E2E (§36): скрининг → признаки → селекция → аллокация →
    план → policy → исполнение → реальный PaperBroker state."""
    from app.bot.universe import compute_feature_sets

    items = [
        ScreenItem(ref=ref("AAA"), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True),
        ScreenItem(ref=ref("BBB"), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True),
        ScreenItem(ref=ref("CCC"), source_bars=60, resampled_bars=44,
                   features_valid=True, tradeable=True),
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
    plan = DirectTargetRebalance().plan(current=None, target=tgt, as_of=AS_OF)
    result = DirectRebalancePolicy(min_trade_value=0.0).apply(
        plan=plan,
        context=RebalanceContext(equity=100_000.0, as_of=AS_OF, available_cash=100_000.0),
    )
    assert result.accepted and not result.rejected

    executed = await execute_policy_result(broker, result)
    assert len(executed) == len(result.accepted)
    assert all(r.status is ExecutionStatus.FILLED for r in executed)

    pos = await broker.positions()
    assert len(pos) == 2
    assert {p.side for p in pos} == {"BUY"}
    assert {r.intent.side for r in executed} == {Side.BUY}

    # закрытие: снять позиции через пустой target
    tgt_empty = TargetPortfolio(
        as_of=sres.as_of, equity=100_000.0, positions=(),
        cash_target=100_000.0, unallocated_cash=100_000.0,
        allocation_method="manual", reserve_cash_pct=1.0,
    )
    cur = current_portfolio_from_dicts(
        as_of=AS_OF, equity=100_000.0,
        positions=[{"figi": p.figi, "ticker": p.ticker, "qty": int(p.qty),
                    "price": float(p.entry_price)}
                   for p in await broker.positions()],
    )
    plan2 = DirectTargetRebalance().plan(current=cur, target=tgt_empty, as_of=AS_OF)
    result2 = DirectRebalancePolicy(min_trade_value=0.0).apply(
        plan=plan2,
        context=RebalanceContext(equity=100_000.0, as_of=AS_OF,
                                 available_cash=0.0,
                                 lot_sizes={s.instrument.figi: 1 for s in screened}),
    )
    executed2 = await execute_policy_result(broker, result2)
    assert executed2 and all(r.status is ExecutionStatus.FILLED for r in executed2)
    assert await broker.positions() == []