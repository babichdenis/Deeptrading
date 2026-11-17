"""ARCH155: изоляция сделок по брокерскому счёту (broker_account_id).

Сделка live/sandbox должна привязываться к счёту активного брокера, а не только
к mode. Тесты держат: два счёта с одной бумагой не пересекаются при открытии и
закрытии, legacy NULL-строки не приписываются новому счёту, и рестарт того же
счёта восстанавливает ровно его позицию.
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bot import runtime as runtime_module
from app.bot.runtime import (
    PaperBotRuntime,
    _MISSING_ACCOUNT,
    _account_scope_of,
    _account_scope_or_raise,
)
from app.models.sandbox_trade import SandboxTrade

STAMP = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)


@pytest_asyncio.fixture
async def sandbox_db(monkeypatch):
    """In-memory sqlite только для sandbox_trades; подменяем runtime.SessionLocal."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    from app.database import Base as _Base

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: _Base.metadata.create_all(
                sync, tables=[SandboxTrade.__table__]
            )
        )
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(runtime_module, "SessionLocal", Session)

    # В sqlite BIGINT PK не автоприрастает (как в Postgres). Присваиваем id локально,
    # на время этого теста, и снимаем в teardown — иначе слушатель протекает в
    # соседние тесты (они используют свой id-назначитель).
    counter = itertools.count(1)

    def _assign_id(mapper, connection, target):
        if target.id is None:
            target.id = next(counter)

    event.listen(SandboxTrade, "before_insert", _assign_id)
    try:
        yield Session
    finally:
        event.remove(SandboxTrade, "before_insert", _assign_id)
        await engine.dispose()


def make_runtime(mode: str, account: str | None, test_name: str | None = None) -> PaperBotRuntime:
    rt = PaperBotRuntime.__new__(PaperBotRuntime)
    rt.broker_mode = mode
    rt.broker = SimpleNamespace(_account=account)
    rt.config = SimpleNamespace(
        test_name=test_name, commission_rate=0.0005, slippage_bps=0.0
    )
    rt._swing = set()
    rt._regimes = {}
    rt._entry_regime = {}
    rt.strategies = {}
    rt._trail_active = {}
    rt._trail_info = {}
    rt._peak_pnl = {}
    rt._bot_now = lambda: STAMP
    rt._entry_card = lambda figi, meta, er: {"entry": {"reason": "test"}}
    rt._log = lambda *a, **k: None
    rt._update_loss_streak = lambda *a, **k: None
    rt.atr_now = lambda figi: None
    return rt


def test_account_scope_selects_active_broker_account():
    assert _account_scope_of(make_runtime("live", "ACC-1")) == "ACC-1"
    assert _account_scope_of(make_runtime("sandbox", "ACC-2")) == "ACC-2"
    assert _account_scope_of(make_runtime("paper", None)) is None
    # live/sandbox без счёта → sentinel, НЕ None (None матчил бы legacy NULL)
    assert _account_scope_of(make_runtime("live", None)) == _MISSING_ACCOUNT
    assert _account_scope_of(make_runtime("live", "")) == _MISSING_ACCOUNT


def test_account_scope_or_raise_blocks_missing_identity():
    import pytest as _pytest

    assert _account_scope_or_raise(make_runtime("live", "ACC-1")) == "ACC-1"
    assert _account_scope_or_raise(make_runtime("paper", None)) is None
    with _pytest.raises(ValueError):
        _account_scope_or_raise(make_runtime("live", None))
    with _pytest.raises(ValueError):
        _account_scope_or_raise(make_runtime("sandbox", ""))


@pytest.mark.asyncio
async def test_two_accounts_same_figi_do_not_cross_close(sandbox_db):
    a = make_runtime("live", "ACC-A")
    b = make_runtime("live", "ACC-B")

    await a._st_open("FIGI", "AAA", "BUY", 1, 100.0, 90.0, 110.0)
    await b._st_open("FIGI", "AAA", "BUY", 1, 100.0, 90.0, 110.0)

    # обе строки открыты, у каждой свой счёт
    async with sandbox_db() as db:
        from sqlalchemy import select
        rows = (await db.execute(
            select(SandboxTrade).where(
                SandboxTrade.figi == "FIGI", SandboxTrade.exit_time.is_(None)
            )
        )).scalars().all()
        assert len(rows) == 2
        assert {r.broker_account_id for r in rows} == {"ACC-A", "ACC-B"}

    # закрытие на счёте A не трогает счёт B
    await a._st_close("FIGI", 105.0, reason="test")

    async with sandbox_db() as db:
        from sqlalchemy import select
        rows = (await db.execute(
            select(SandboxTrade).where(SandboxTrade.figi == "FIGI")
            .order_by(SandboxTrade.broker_account_id)
        )).scalars().all()
        by_acc = {r.broker_account_id: r for r in rows}
        assert by_acc["ACC-A"].exit_time is not None
        assert by_acc["ACC-B"].exit_time is None


@pytest.mark.asyncio
async def test_legacy_null_rows_are_not_reopened_or_closed(sandbox_db):
    # старая история без счёта (NULL) — её нельзя закрывать от имени нового счёта
    async with sandbox_db() as db:
        db.add(SandboxTrade(
            figi="FIGI", ticker="AAA", side="LONG", qty=1,
            entry_time=STAMP - timedelta(days=1), entry_price=100.0,
            mode="live", test_name=None, broker_account_id=None,
        ))
        await db.commit()

    a = make_runtime("live", "ACC-A")
    await a._st_open("FIGI", "AAA", "BUY", 1, 100.0, 90.0, 110.0)

    async with sandbox_db() as db:
        from sqlalchemy import select
        rows = (await db.execute(
            select(SandboxTrade).where(
                SandboxTrade.figi == "FIGI", SandboxTrade.exit_time.is_(None)
            )
        )).scalars().all()
        # legacy NULL остаётся открытым И отдельно появляется строка ACC-A
        assert len(rows) == 2
        assert {r.broker_account_id for r in rows} == {None, "ACC-A"}


@pytest.mark.asyncio
async def test_restart_same_account_restores_its_own_position(sandbox_db):
    a1 = make_runtime("live", "ACC-A")
    await a1._st_open("FIGI", "AAA", "BUY", 1, 100.0, 90.0, 110.0)

    # «перезапуск» — новый объект того же счёта, та же бумага: это его позиция
    a2 = make_runtime("live", "ACC-A")
    await a2._st_close("FIGI", 105.0, reason="manual")

    async with sandbox_db() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(SandboxTrade).where(
                SandboxTrade.figi == "FIGI",
                SandboxTrade.broker_account_id == "ACC-A",
            )
        )).scalar_one()
        assert row.exit_time is not None


@pytest.mark.asyncio
async def test_missing_identity_does_not_close_legacy_null(sandbox_db):
    # legacy-строка без счёта (NULL)
    async with sandbox_db() as db:
        db.add(SandboxTrade(
            figi="FIGI", ticker="AAA", side="LONG", qty=1,
            entry_time=STAMP - timedelta(days=1), entry_price=100.0,
            mode="live", test_name=None, broker_account_id=None,
        ))
        await db.commit()

    # live без счёта: _st_close не должен закрыть legacy NULL (sentinel != NULL)
    rt = make_runtime("live", None)
    await rt._st_close("FIGI", 105.0, reason="test")

    async with sandbox_db() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(SandboxTrade).where(SandboxTrade.figi == "FIGI")
        )).scalar_one()
        assert row.exit_time is None


@pytest.mark.asyncio
async def test_missing_identity_does_not_write(sandbox_db):
    rt = make_runtime("live", None)
    await rt._st_open("FIGI", "AAA", "BUY", 1, 100.0, 90.0, 110.0)

    async with sandbox_db() as db:
        from sqlalchemy import select
        rows = (await db.execute(select(SandboxTrade))).scalars().all()
        assert rows == []
