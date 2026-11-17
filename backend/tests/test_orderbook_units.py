"""Стакан: метка времени и состояние достоверности.

Сеть не нужна: подделывается клиент SDK. Проверяется то, что ломалось в жизни —
поле метки называется по-разному в слоях SDK, закрытый рынок отдаёт нулевую метку,
пустой стакан отдаётся нулями вместо честного «пусто».
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services import orderbook as ob


class FakeQuotation:
    def __init__(self, value: float):
        self.units = int(value)
        self.nano = int(round((value - int(value)) * 1e9))


class FakeOrder:
    def __init__(self, price: float, quantity: int):
        self.price = FakeQuotation(price)
        self.quantity = quantity


def fake_book(*, bids=(), asks=(), last=100.0, ts=None, consistent=True, ts_field="orderbook_ts"):
    payload = {
        "bids": [FakeOrder(p, q) for p, q in bids],
        "asks": [FakeOrder(p, q) for p, q in asks],
        "last_price": FakeQuotation(last),
        "is_consistent": consistent,
    }
    payload[ts_field] = ts
    return SimpleNamespace(**payload)


class FakeMarketData:
    def __init__(self, book):
        self._book = book

    def get_order_book(self, *, figi, depth):
        return self._book


class _StubClient:
    """Заглушка клиента: __enter__ отдаёт сервисы, как настоящий SDK."""

    def __init__(self, token):
        self._token = token

    def __enter__(self):
        return self._services

    def __exit__(self, *exc):
        return False


def patch_sdk(monkeypatch, book):
    services = SimpleNamespace(market_data=FakeMarketData(book))
    stub = type("Stub", (_StubClient,), {"_services": services})
    monkeypatch.setattr("t_tech.invest.Client", stub)
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(feed_token="test-token"),
    )
    # клиент кэшируется на уровне модуля: без сброса тест увидит живой
    ob._client = None
    ob._client_key = None
    ob._cache.clear()


@pytest.fixture(autouse=True)
def _clean_cache():
    ob._client = None
    ob._client_key = None
    ob._cache.clear()
    ob._meta_cache.clear()
    yield
    ob._client = None
    ob._client_key = None
    ob._cache.clear()
    ob._meta_cache.clear()


# ── метка времени ───────────────────────────────────────────────────────────


def protobuf_ts(seconds):
    return SimpleNamespace(seconds=seconds, nanos=0)


def test_timestamp_taken_from_orderbook_ts_field():
    fixed = datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc)
    book = fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)], ts=fixed)

    assert ob._book_ts(book) == fixed.isoformat()


def test_timestamp_taken_from_plain_time_field():
    fixed = datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc)
    book = fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)], ts=fixed, ts_field="time")

    assert ob._book_ts(book) == fixed.isoformat()


def test_protobuf_timestamp_is_converted_to_iso():
    seconds = 1_763_129_400  # 2025-11-14T09:30:00Z
    book = fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)], ts=protobuf_ts(seconds))

    assert ob._book_ts(book) == datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


def test_zero_timestamp_means_unknown_not_1970():
    book = fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)], ts=protobuf_ts(0))

    assert ob._book_ts(book) is None


def test_missing_timestamp_is_none():
    book = fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)])

    assert ob._book_ts(book) is None


def test_last_price_ts_is_trade_time_not_book_time():
    fixed = datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc)
    book = fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)], ts=fixed,
                     ts_field="last_price_ts")

    assert ob._book_ts(book) is None


def test_meta_with_share_lot_marks_depth_known():
    bids = [{"p": 100.0, "q": 10}]
    asks = [{"p": 101.0, "q": 5}]
    value, state = ob.book_depth_rub(bids, asks, {"lot": 10, "class_code": "TQBR", "currency": "rub"})

    assert state == "shares"
    assert value == pytest.approx((100.0 * 10 + 101.0 * 5) * 10)


def test_meta_absent_leaves_depth_unknown():
    value, state = ob.book_depth_rub([{"p": 100.0, "q": 10}], [], None)

    assert value is None
    assert state == "instrument_unknown"


def test_missing_currency_leaves_depth_unknown():
    value, state = ob.book_depth_rub([{"p": 100.0, "q": 10}], [],
                                     {"lot": 10, "class_code": "TQBR", "currency": ""})
    assert value is None
    assert state == "currency_unknown"


def test_other_boards_leave_depth_unknown():
    value, state = ob.book_depth_rub([{"p": 100.0, "q": 10}], [],
                                     {"lot": 1, "class_code": "SPBFUT", "currency": "rub"})

    assert value is None
    assert state == "not_share"


# ── достоверность ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_live_book_is_ok_and_carries_timestamp(monkeypatch):
    fixed = datetime(2026, 11, 14, 12, 30, tzinfo=timezone.utc)
    patch_sdk(monkeypatch, fake_book(bids=[(99.0, 10), (98.0, 5)],
                                     asks=[(101.0, 8)], ts=fixed))

    result = await ob.fetch_orderbook("FIGI", 5)

    assert result["state"] == "ok"
    assert result["ts"] == fixed.isoformat()
    assert result["best_bid"] == 99.0
    assert result["best_ask"] == 101.0
    assert result["bid_qty"] == 15
    assert result["ask_qty"] == 8


@pytest.mark.asyncio
async def test_empty_book_is_not_normal_liquidity(monkeypatch):
    patch_sdk(monkeypatch, fake_book(bids=[], asks=[], ts=None))

    result = await ob.fetch_orderbook("FIGI", 5)

    assert result["state"] == "empty"
    assert result["ts"] is None
    assert result["best_bid"] is None and result["best_ask"] is None
    assert result["depth_rub"] is None
    assert result["depth_rub_state"] == "instrument_unknown"


@pytest.mark.asyncio
async def test_one_sided_book_is_named(monkeypatch):
    patch_sdk(monkeypatch, fake_book(bids=[(99.0, 10)], asks=[]))

    result = await ob.fetch_orderbook("FIGI", 5)

    assert result["state"] == "one_sided"
    assert result["spread_bps"] is None


@pytest.mark.asyncio
async def test_crossed_book_is_named(monkeypatch):
    patch_sdk(monkeypatch, fake_book(bids=[(102.0, 10)], asks=[(101.0, 10)]))

    result = await ob.fetch_orderbook("FIGI", 5)

    assert result["state"] == "crossed"


@pytest.mark.asyncio
async def test_inconsistent_book_is_named(monkeypatch):
    patch_sdk(monkeypatch, fake_book(bids=[(99.0, 10)], asks=[(101.0, 10)],
                                     consistent=False))

    result = await ob.fetch_orderbook("FIGI", 5)

    assert result["state"] == "inconsistent"
    assert result["is_consistent"] is False


@pytest.mark.asyncio
async def test_spread_and_imbalance_on_live_book(monkeypatch):
    patch_sdk(monkeypatch, fake_book(bids=[(99.0, 30)], asks=[(101.0, 10)]))

    result = await ob.fetch_orderbook("FIGI", 5)

    assert result["spread_bps"] == pytest.approx(200.0, abs=0.5)
    assert result["imbalance"] == pytest.approx(0.5, abs=0.01)
