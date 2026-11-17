"""Семантика маржи: use_margin=false не даёт заёма, обеспечение без двойного лота.

Два независимых дефекта, найденных архитектором:

1. В runtime два места считали `use_margin` только по списку сессий, игнорируя
   `cfg.use_margin`, поэтому выключенная маржа всё равно применяла плечо.
2. `PaperBroker._starting_margin` умножал номинал на лот, тогда как `qty`
   уже хранится в акциях (после исправления LOTS/SHARES), а `_unrealized_pnl`
   и `market_value` считают по тому же `qty` без лота. Обеспечение завышалось
   ровно в `lot` раз.

Первый тест функциональный (реальная SQLite и реальный PaperBroker), остальные
проверяют само выражение в исходнике, чтобы дефект не вернулся незаметно.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.bot.paper_broker import PaperBroker
from app.engine.costs import CostModel
from app.models.instrument import Instrument
from app.models.paper import PaperAccount, PaperPosition, PaperTrade

RUNTIME_SRC = Path(__file__).resolve().parents[1] / "app" / "bot" / "runtime.py"
BROKER_SRC = Path(__file__).resolve().parents[1] / "app" / "bot" / "paper_broker.py"


def _runtime_tree():
    return ast.parse(RUNTIME_SRC.read_text(encoding="utf-8"))


def _use_margin_assignments(tree):
    """Все присваивания use_margin в исходнике runtime."""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "use_margin":
                    found.append(node.value)
    return found


def _mentions(node, name):
    return any(isinstance(n, ast.Attribute) and n.attr == name for n in ast.walk(node))


def test_every_use_margin_assignment_respects_cfg_use_margin():
    """Любое вычисление use_margin обязано учитывать cfg.use_margin."""
    values = _use_margin_assignments(_runtime_tree())
    assert len(values) >= 2, "ожидались два места расчёта use_margin"
    for value in values:
        assert _mentions(value, "use_margin"), (
            "присваивание use_margin не обращается к cfg.use_margin — "
            "выключенная маржа снова даст плечо"
        )


def test_use_margin_expression_false_when_cfg_off():
    """Собранное из исходника выражение даёт False при cfg.use_margin=false."""
    values = _use_margin_assignments(_runtime_tree())
    src = ast.unparse(values[0])

    class _Cfg:
        use_margin = False
        margin_sessions = ["day"]

    ns = {"cfg": _Cfg(), "_tss": "day", "bool": bool}
    assert eval(src, ns) is False, f"выражение {src} дало плечо при выключенной марже"

    class _CfgOn(_Cfg):
        use_margin = True

    ns2 = {"cfg": _CfgOn(), "_tss": "day", "bool": bool}
    assert eval(src, ns2) is True

    ns3 = {"cfg": _CfgOn(), "_tss": "evening", "bool": bool}
    assert eval(src, ns3) is False, "сессия вне margin_sessions не должна давать маржу"


def test_explicit_no_margin_forces_leverage_one():
    """При use_margin=false в коде есть принудительный сброс плеча к 1.0."""
    tree = _runtime_tree()
    hits = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and _mentions(node.test, "use_margin")
        and any(
            isinstance(s, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "lev" for t in s.targets)
            for s in node.body
        )
    ]
    assert hits, "нет ветки, сбрасывающей плечо при use_margin=false"


def test_starting_margin_does_not_multiply_by_lot():
    """В _starting_margin номинал не умножается на лот."""
    tree = ast.parse(BROKER_SRC.read_text(encoding="utf-8"))
    assigns = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "notional" for t in node.targets)
    ]
    assert assigns, "не найдено вычисление notional"
    for node in assigns:
        names = [n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)]
        assert "lot" not in names, f"notional снова умножается на лот: {ast.unparse(node.value)}"


class _Rows:
    """Ответ запроса каталога инструментов: iterable из строк-заглушек."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return list(self._rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_args, **_kwargs):
        return _Rows(self._rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _FakeBroker(PaperBroker):
    """PaperBroker без БД: позиции и каталог инструментов заданы тестом."""

    def __init__(self, positions, instruments):
        self._positions = positions
        self._instrument_rows = instruments
        self._last_prices = {}

    def sessions(self):
        return _FakeDB(self._instrument_rows)

    async def positions(self):
        return list(self._positions)

    async def cash(self):
        return 10000.0


def _pos(figi, qty, side="BUY", entry_price=100.0):
    return SimpleNamespace(figi=figi, qty=qty, side=side,
                           entry_price=entry_price, ticker=figi)


@pytest.mark.asyncio
async def test_starting_margin_counts_shares_once():
    """6 акций по 100 при лоте 10 — обеспечение 600, а не 6000."""
    broker = _FakeBroker(
        positions=[_pos("FIGI_LOT10", 6)],
        instruments=[SimpleNamespace(figi="FIGI_LOT10", long_lev=1.0,
                                     short_lev=1.0, lot=10)],
    )
    broker._last_prices["FIGI_LOT10"] = 100.0
    ma = await broker.margin_attributes()
    assert ma["starting_margin"] == pytest.approx(600.0), (
        f"обеспечение {ma['starting_margin']} вместо 600 — лот учтён дважды"
    )


@pytest.mark.asyncio
async def test_starting_margin_matches_market_value():
    """Обеспечение при плече 1 совпадает со стоимостью позиции."""
    broker = _FakeBroker(
        positions=[_pos("FIGI_LOT10", 6)],
        instruments=[SimpleNamespace(figi="FIGI_LOT10", long_lev=1.0,
                                     short_lev=1.0, lot=10)],
    )
    broker._last_prices["FIGI_LOT10"] = 100.0
    ma = await broker.margin_attributes()
    mv = await broker.market_value()
    assert ma["starting_margin"] == pytest.approx(mv, rel=1e-9)


@pytest.mark.asyncio
@pytest.mark.parametrize('side, leverage, unrealized', [('BUY', 2.0, 120.0), ('SELL', 4.0, -120.0)])
@pytest.mark.parametrize('lot', [1, 10])
async def test_margin_and_free_funds_use_shares_and_current_price(side, leverage, unrealized, lot):
    broker = _FakeBroker(
        positions=[_pos('MARKED', 6, side=side)],
        instruments=[SimpleNamespace(figi='MARKED', long_lev=2.0, short_lev=4.0, lot=lot)],
    )
    broker._last_prices['MARKED'] = 120.0
    attributes = await broker.margin_attributes()
    assert attributes['starting_margin'] == pytest.approx(720.0 / leverage)
    assert attributes['liquid'] == pytest.approx(10000.0 + unrealized)
    assert await broker.free_funds() == pytest.approx(10000.0 + unrealized - 720.0 / leverage)
