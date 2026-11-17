"""PaperBroker: путь уже исполненной цены (`price_is_filled`) и учёт комиссий.

Разница принципиальная: цена из стакана (биржа уже свела) не должна второй раз
проходить через модель проскальзывания, иначе вход систематически дороже реального.
Тесты держат оба пути, оба направления, списание комиссии и леджер сделок.
"""
from __future__ import annotations

import itertools

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session as SyncSession
from sqlalchemy.pool import StaticPool

from app.bot.paper_broker import PaperBroker
from app.engine.costs import CostModel
from app.models.paper import PaperAccount, PaperPosition, PaperTrade

_trades_pk_seq = None


def _install_paper_trade_pk_counter() -> None:
    """В sqlite нет автоприраста для этой таблицы — задаём id вручную (как в проекте)."""
    global _trades_pk_seq
    if _trades_pk_seq is None:
        _trades_pk_seq = itertools.count(1)

        @event.listens_for(SyncSession, "before_flush")
        def _assign_paper_trade_ids(session, flush_context, instances):
            for obj in session.new:
                if isinstance(obj, PaperTrade) and obj.id is None:
                    obj.id = next(_trades_pk_seq)


@pytest_asyncio.fixture
async def make_broker():
    """Фабрика брокеров: проскальзывание на входе/выходе задаётся тестом."""
    engines = []

    async def _make(*, slippage_bps: float = 20.0, commission_rate: float = 0.0005, clock=None):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        from app.database import Base as _Base

        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: _Base.metadata.create_all(
                    sync_conn,
                    tables=[t.__table__ for t in (PaperAccount, PaperPosition, PaperTrade)],
                )
            )
        engines.append(engine)
        _install_paper_trade_pk_counter()
        Session = async_sessionmaker(engine, expire_on_commit=False)
        b = PaperBroker(Session, cost_model=CostModel(commission_rate=commission_rate,
                                                      slippage_bps=slippage_bps), clock=clock)
        await b.reset(initial_cash=100_000.0)
        return b

    yield _make
    for engine in engines:
        await engine.dispose()


@pytest_asyncio.fixture
async def broker(make_broker):
    return await make_broker()


async def _open(broker, *, side: str, price: float, filled: bool, qty: int = 10,
                figi: str = "FIGI_A") -> float | None:
    return await broker.open_position(
        figi=figi, ticker=figi.replace("FIGI_", ""), side=side, qty=qty, price=price,
        stop_loss=None, take_profit=None, strategy_id="s1", price_is_filled=filled,
    )


# ── Уже исполненная цена ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_filled_price_is_used_as_is(broker):
    fill = await _open(broker, side="BUY", price=250.0, filled=True)
    assert fill == pytest.approx(250.0)
    pos = await broker.get_position("FIGI_A")
    assert float(pos.entry_price) == pytest.approx(250.0)


@pytest.mark.asyncio
async def test_filled_price_skips_slippage(broker):
    """Без проскальзывания: два вызова с одинаковой ценой дают одинаковый вход."""
    a = await _open(broker, side="BUY", price=100.0, filled=True, figi="FIGI_A")
    b = await _open(broker, side="BUY", price=100.0, filled=True, figi="FIGI_B")
    assert a == b == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_raw_price_goes_through_slippage(broker):
    """Обычный путь: покупка дороже котировки, продажа дешевле."""
    buy = await _open(broker, side="BUY", price=100.0, filled=False, figi="FIGI_A")
    sell = await _open(broker, side="SELL", price=100.0, filled=False, figi="FIGI_B")
    assert buy > 100.0
    assert sell < 100.0


@pytest.mark.asyncio
async def test_filled_price_differs_from_raw_path(broker):
    raw = await _open(broker, side="BUY", price=300.0, filled=False, figi="FIGI_A")
    filled = await _open(broker, side="BUY", price=300.0, filled=True, figi="FIGI_B")
    assert filled == pytest.approx(300.0)
    assert raw > filled


# ── Направления ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_side_is_kept_on_position(broker, side):
    await _open(broker, side=side, price=200.0, filled=True)
    pos = await broker.get_position("FIGI_A")
    assert pos.side == side
    assert int(pos.qty) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("side,direction", [("LONG", 1), ("SHORT", -1)])
async def test_filled_entry_pnl_direction(make_broker, side, direction):
    """Без проскальзывания: PnL — чистая разница цен в нужную сторону."""
    broker = await make_broker(slippage_bps=0.0)
    await _open(broker, side=side, price=100.0, filled=True)
    trade = await broker.close_position("FIGI_A", 110.0, "tp")
    gross = float(trade.gross_pnl)
    assert gross == pytest.approx(direction * 10 * (110.0 - 100.0), abs=0.02)


@pytest.mark.asyncio
async def test_exit_goes_through_slippage(broker):
    """Выход по котировке проходит через модель: результат хуже котировки в обе стороны."""
    await _open(broker, side="LONG", price=100.0, filled=True, figi="FIGI_A")
    long_trade = await broker.close_position("FIGI_A", 110.0, "tp")
    await _open(broker, side="SHORT", price=100.0, filled=True, figi="FIGI_B")
    short_trade = await broker.close_position("FIGI_B", 90.0, "tp")

    assert float(long_trade.exit_price) < 110.0, "продажа выхода дешевле котировки"
    assert float(short_trade.exit_price) > 90.0, "покупка выхода дороже котировки"
    assert float(long_trade.gross_pnl) < 100.0
    assert float(short_trade.gross_pnl) < 100.0


@pytest.mark.asyncio
async def test_position_is_single_per_figi(broker):
    first = await _open(broker, side="BUY", price=100.0, filled=True)
    second = await _open(broker, side="BUY", price=100.0, filled=True)
    assert first is not None
    assert second is None, "повторный вход по той же бумаге не открывает вторую позицию"
    assert len(await broker.positions()) == 1


# ── Комиссии и леджер ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_commission_is_charged_on_entry(broker):
    before = await broker.cash()
    fill = await _open(broker, side="BUY", price=100.0, filled=True, qty=10)
    after = await broker.cash()
    expected = CostModel(commission_rate=0.0005).commission(fill * 10)
    assert before - after == pytest.approx(expected, abs=1e-6)


@pytest.mark.asyncio
async def test_trade_ledger_records_commission_and_net(make_broker):
    broker = await make_broker(slippage_bps=0.0, commission_rate=0.0005)
    fill = await _open(broker, side="BUY", price=100.0, filled=True, qty=10)
    trade = await broker.close_position("FIGI_A", 110.0, "signal")

    gross = float(trade.gross_pnl)
    exit_comm = float(trade.commission)
    assert gross == pytest.approx(100.0, abs=0.02), "без проскальзывания gross — чистая разница"
    assert exit_comm == pytest.approx(0.0005 * 110.0 * 10, abs=1e-6)
    assert float(trade.net_pnl) == pytest.approx(gross - exit_comm, abs=1e-6)


@pytest.mark.asyncio
async def test_ledger_trade_matches_position(broker):
    fill = await _open(broker, side="SHORT", price=500.0, filled=True, qty=3)
    trade = await broker.close_position("FIGI_A", 490.0, "tp")
    assert float(trade.entry_price) == pytest.approx(fill)
    assert int(trade.qty) == 3
    assert trade.strategy_id == "s1"


@pytest.mark.asyncio
async def test_close_returns_none_without_position(broker):
    assert await broker.close_position("FIGI_NONE", 100.0, "signal") is None


# ── Вызов из runtime ───────────────────────────────────────────────────────


def test_runtime_passes_price_is_filled():
    """runtime обязан помечать вход как исполненный, иначе цена пройдёт слиппедж дважды."""
    import inspect

    from app.bot import runtime as rt

    src = inspect.getsource(rt)
    idx = src.find("price_is_filled=True")
    assert idx > 0, "вызов open_position с price_is_filled=True не найден"
    call_window = src[max(0, idx - 1200):idx]
    assert "open_position(" in call_window, "флаг должен стоять в вызове open_position"


@pytest.mark.asyncio
@pytest.mark.parametrize("lot", [1, 10, 0, None])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_runtime_paper_quantity_boundary(make_broker, lot, side):
    from app.bot.live_broker import LiveBroker
    from app.engine.exits import FixedSlTpPolicy
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    from sqlalchemy import select

    broker = await make_broker(slippage_bps=0.0)
    source = Path(__file__).resolve().parents[1] / "app/bot/runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try)
              and any(isinstance(statement, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == "broker_qty"
                              for target in statement.targets)
                      for statement in node.body)]
    assert len(blocks) == 1

    class QuantitySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def execute(self, query):
            return SimpleNamespace(scalar_one_or_none=lambda: lot)

    wrapper = ast.parse("async def execute_boundary():\n    pass\n")
    wrapper.body[0].body = blocks[0].body
    ast.fix_missing_locations(wrapper)
    namespace = {
        "self": SimpleNamespace(broker=broker), "PaperBroker": PaperBroker, "LiveBroker": LiveBroker,
        "exit_policy": FixedSlTpPolicy(),
        "SessionLocal": QuantitySession, "select": select,
        "order": SimpleNamespace(qty=2, ticker="AUDIT", side=side, meta={}, id="PAPER_TEST"),
        "figi": "AUDIT", "_fill": 100.0,
        "cfg": SimpleNamespace(strategy_id="audit", interval_name="1min"),
        "plan": SimpleNamespace(stop_loss=None, take_profit=None, atr=None,
                                atr_period=None, atr_as_of=None,
                                atr_multiplier=None, policy_id="audit"),
    }
    exec(compile(wrapper, str(source), "exec"), namespace)
    if not lot:
        with pytest.raises(ValueError, match="known positive instrument lot"):
            await namespace["execute_boundary"]()
        assert await broker.get_position("AUDIT") is None
        assert await broker.cash() == pytest.approx(100000.0)
        return
    await namespace["execute_boundary"]()
    position = await broker.get_position("AUDIT")
    assert int(position.qty) == 2 * lot
    assert float(position.entry_price) == pytest.approx(100.0)
    assert await broker.cash() == pytest.approx(100000.0 - 100.0 * 2 * lot * 0.0005)


@pytest.mark.asyncio
@pytest.mark.parametrize("side,raw,expected", [("LONG", 110.0, 109.78), ("SHORT", 90.0, 90.18)])
async def test_exit_filled_and_raw_have_identical_execution(make_broker, side, raw, expected):
    raw_broker = await make_broker(slippage_bps=20.0)
    filled_broker = await make_broker(slippage_bps=20.0)
    await _open(raw_broker, side=side, price=100.0, filled=True)
    await _open(filled_broker, side=side, price=100.0, filled=True)
    raw_trade = await raw_broker.close_position("FIGI_A", raw, "stop_loss")
    from app.bot.runtime import PaperBotRuntime
    from types import SimpleNamespace
    from unittest.mock import Mock
    from datetime import datetime, timezone
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker = filled_broker
    rt.pending_orders = {}
    rt.events = SimpleNamespace(log=Mock())
    rt._bot_now = lambda: datetime.now(timezone.utc)
    result = await rt._submit_close_reconciled('FIGI_A', 'A', 10, expected, 'stop_loss', paper_filled=True)
    assert result['status'] == 'closed'
    filled_trade = result['trade']
    assert float(raw_trade.exit_price) == pytest.approx(expected)
    assert float(filled_trade.exit_price) == pytest.approx(expected)
    assert float(raw_trade.net_pnl) == pytest.approx(float(filled_trade.net_pnl))
    assert await raw_broker.cash() == pytest.approx(await filled_broker.cash())


@pytest.mark.parametrize("trade,expected", [({"exit_price": 109.78}, 109.78), ({"price": 110.25}, 110.25), ({"exit_price": 109.78, "price": 110.25}, 109.78)])
def test_reduce_positions_uses_executed_price(trade, expected):
    import ast
    from pathlib import Path
    from types import SimpleNamespace

    source = Path(__file__).resolve().parents[1] / "app/bot/runtime.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "reduce_positions")
    value = next(n.value for n in ast.walk(method) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "_px" for t in n.targets))
    expression = ast.Expression(body=value)
    assert eval(compile(expression, str(source), "eval"), {"trade": SimpleNamespace(**trade), "last": 999}) == expected


@pytest.mark.asyncio
async def test_entry_fee_survives_tariff_change_and_position_reload(make_broker):
    from app.bot.paper_cashflow import settle_paper_exit

    broker = await make_broker(slippage_bps=0.0, commission_rate=0.0005)
    await _open(broker, side="LONG", price=100.0, filled=True)
    broker._pos_cache.clear()
    position = await broker.get_position("FIGI_A")
    assert float(position.entry_commission) == pytest.approx(0.5)
    broker.costs = CostModel(commission_rate=0.001, slippage_bps=0.0)
    trade = await broker.close_position("FIGI_A", 110.0, "test")
    assert float(trade.entry_commission) == pytest.approx(0.5)
    result = settle_paper_exit(float(trade.gross_pnl), float(trade.commission), float(trade.entry_commission))
    assert result.total_net == pytest.approx(98.4)
    assert await broker.cash() == pytest.approx(100000.0 + result.total_net)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["known", "legacy", "qty_mismatch"])
async def test_runtime_close_persists_complete_or_unknown_net(make_broker, monkeypatch, case):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.bot import runtime as runtime_module
    from app.models.sandbox_trade import SandboxTrade

    broker = await make_broker(slippage_bps=0.0)
    await _open(broker, side="LONG", price=100.0, filled=True)
    trade = await broker.close_position("FIGI_A", 110.0, "test")
    if case == "legacy":
        trade.entry_commission = None
    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    async with broker.sessions() as db:
        await db.run_sync(lambda sync: SandboxTrade.__table__.create(sync.connection()))
        db.add(SandboxTrade(id=1, figi="FIGI_A", ticker="A", side="LONG",
                            qty=100 if case == "qty_mismatch" else 10,
                            entry_price=100, entry_time=stamp, mode="paper", test_name="audit"))
        from datetime import timedelta
        db.add(SandboxTrade(id=2, figi="FIGI_A", ticker="A", side="LONG", qty=10,
                            entry_price=100, entry_time=stamp + timedelta(minutes=1),
                            mode="paper", test_name="other_run"))
        await db.commit()
    monkeypatch.setattr(runtime_module, "SessionLocal", broker.sessions)
    context = SimpleNamespace(broker=broker, broker_mode="paper",
                              config=SimpleNamespace(test_name="audit", commission_rate=0.0005, slippage_bps=0),
                              _trail_active={}, _trail_info={}, _peak_pnl={},
                              _bot_now=lambda: stamp, _update_loss_streak=lambda *args: None)
    await type(runtime_module.runtime)._st_close(context, "FIGI_A", 999.0, reason="test",
                                                net=float(trade.net_pnl), execution=trade)
    async with broker.sessions() as db:
        row = await db.get(SandboxTrade, 1)
        foreign = await db.get(SandboxTrade, 2)
        assert foreign.exit_time is None
        assert foreign.net_pnl is None
        assert row.exit_time is not None
        if case == "known":
            assert float(row.net_pnl) == pytest.approx(98.95)
            assert float(row.commission) == pytest.approx(1.05)
            assert float(row.exit_price) == pytest.approx(110.0)
            assert await broker.cash() == pytest.approx(100000.0 + float(row.net_pnl))
        else:
            assert row.net_pnl is None


@pytest.mark.asyncio
async def test_runtime_open_does_not_archive_other_runs_or_modes(make_broker, monkeypatch):
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from sqlalchemy import event
    from app.bot import runtime as runtime_module
    from app.models.sandbox_trade import SandboxTrade

    broker = await make_broker()
    stamp = datetime(2026, 1, 2, tzinfo=timezone.utc)
    async with broker.sessions() as db:
        await db.run_sync(lambda sync: SandboxTrade.__table__.create(sync.connection()))
        for ident, mode, name in [(1, "paper", "audit"), (2, "paper", "other"), (3, "live", "audit")]:
            db.add(SandboxTrade(id=ident, figi="FIGI_A", ticker="A", side="LONG", qty=10,
                                entry_price=100, entry_time=stamp, mode=mode, test_name=name))
        await db.commit()
    monkeypatch.setattr(runtime_module, "SessionLocal", broker.sessions)
    context = SimpleNamespace(broker_mode="paper", config=SimpleNamespace(test_name="audit"),
                              _bot_now=lambda: stamp, _regimes={}, _entry_regime={}, strategies={},
                              _entry_card=lambda *args: {})

    from unittest.mock import AsyncMock
    context._entry_lot_size = AsyncMock(return_value=10)
    def assign_id(mapper, connection, target):
        if target.id is None:
            target.id = 4

    event.listen(SandboxTrade, "before_insert", assign_id)
    try:
        await type(runtime_module.runtime)._st_open(context, "FIGI_A", "A", "BUY", 1, 100, None, None)
    finally:
        event.remove(SandboxTrade, "before_insert", assign_id)
    async with broker.sessions() as db:
        previous = await db.get(SandboxTrade, 1)
        assert previous.exit_reason == "reopened"
        assert previous.net_pnl is None
        assert previous.exit_price is None
        for ident in (2, 3):
            foreign = await db.get(SandboxTrade, ident)
            assert foreign.exit_time is None
        current = await db.get(SandboxTrade, 4)
        assert current is not None
        assert current.test_name == "audit"
        assert current.exit_time is None


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["BUY", "SELL"])
async def test_execution_clock_preserves_overnight_history(make_broker, side):
    from datetime import datetime, timedelta, timezone

    entry = datetime(2026, 9, 1, 18, 45, tzinfo=timezone(timedelta(hours=3)))
    current = [entry]
    broker = await make_broker(clock=lambda: current[0])
    await _open(broker, side=side, price=100.0, filled=True)
    broker._pos_cache.clear()
    current[0] = entry + timedelta(hours=15)
    trade = await broker.close_position("FIGI_A", 101.0, "clock_test", price_is_filled=True)
    assert trade.entry_time.replace(tzinfo=timezone.utc) == entry.astimezone(timezone.utc)
    assert trade.exit_time == current[0].astimezone(timezone.utc)
    assert trade.exit_time - trade.entry_time.replace(tzinfo=timezone.utc) == timedelta(hours=15)


@pytest.mark.asyncio
async def test_naive_execution_clock_rolls_back_entry(make_broker):
    from datetime import datetime

    broker = await make_broker(clock=lambda: datetime(2026, 9, 1))
    before = await broker.cash()
    with pytest.raises(ValueError, match="timezone-aware"):
        await _open(broker, side="BUY", price=100.0, filled=True)
    assert await broker.get_position("FIGI_A") is None
    assert await broker.cash() == before
